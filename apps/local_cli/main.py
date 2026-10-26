"""The local CLI composition root (ADR-0016, plan step 1b): review a branch on this machine.

The fourth composition root, beside ``apps/webhook_api``, ``apps/worker`` and ``apps/admin_api``.
It parses configuration once (``LocalSettings``), configures logging, fabricates the job seed the
pipeline normally gets from a queue, wires ``adapter/local`` and the shared registry builder
(``review.registry``), and runs the registry in-process: no queue, database, server or container
runtime. It never imports ``apps/worker``.

Exit status (documented in docs/reference/operations.md, one reason string per runtime failure):
0 success (including "nothing to review"), 1 a runtime failure (``RuntimeFailure``), 2 a usage or
configuration error (including a missing model, base ref or unusable ``--output`` path), 3 the CI
severity gate tripped (US-C2, off unless configured). A failed run never exits 0.
Findings are printed on stdout; progress, the run summary and errors go to stderr, so output can be
piped. Output is plain text: no colour and no terminal control sequences, piped or not.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.metadata
import logging
import os
import shutil
import sys
import uuid
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from adapter.analyzers.eslint import ESLINT_CONFIG_FILENAMES, EslintAnalyzer
from adapter.analyzers.gitleaks import (
    GitleaksAnalyzer,
    GitleaksSecretLineFinder,
    GitleaksTextRedactor,
)
from adapter.analyzers.golangci_lint import GOLANGCI_CONFIG_FILENAMES, GolangciLintAnalyzer
from adapter.analyzers.phpstan import PHPSTAN_CONFIG_FILENAMES, PhpstanAnalyzer
from adapter.analyzers.ruff import RUFF_CONFIG_FILENAMES, RuffAnalyzer
from adapter.analyzers.semgrep import SemgrepAnalyzer
from adapter.analyzers.subprocess_runner import analyzer_env
from adapter.analyzers.trivy import TRIVY_CONFIG_FILENAMES, TrivyAnalyzer
from adapter.llm.claude_cli import BACKEND_NAME, DEFAULT_MODEL_LABEL, ClaudeCliClient
from adapter.llm.litellm_client import LiteLLMClient
from adapter.local.diff import LocalGitDiffFetcher
from adapter.local.egress import (
    EgressMcpQuery,
    RecordingLLMClient,
    build_egress_report,
    mask_url,
    render_egress_text,
)
from adapter.local.environment import scrubbed_environment
from adapter.local.exceptions import (
    GitRefNotFoundError,
    LocalGitError,
    MemoryFileError,
    NoMergeBaseError,
    NotAGitRepositoryError,
    RedactionFailedError,
)
from adapter.local.git import (
    ReviewRange,
    resolve_repo_root,
    resolve_review_range,
    resolve_staged_range,
)
from adapter.local.mcp_config import McpConfigError
from adapter.local.mcp_knowledge import (
    QUERY_MARKER_TITLE,
    LocalMcpOpener,
    LocalMcpResolver,
    McpKnowledgeProvider,
)
from adapter.local.memory_file import (
    FileMemoryMatcher,
    LoadedMemoryFile,
    describe_suppressed,
    load_memory_file,
    propose_rules,
    render_proposal,
)
from adapter.local.observer import RunObserver, observing
from adapter.local.output import (
    OutputFormat,
    OutputPathError,
    check_output_path,
    discard_stale_output,
    write_output,
)
from adapter.local.redaction import RedactingDiffFetcher
from adapter.local.report import Report, render_json, render_markdown
from adapter.local.summary import (
    AnalyzerOutcome,
    BackendSummary,
    CiCommentsSummary,
    MemoryFileSummary,
    RedactionSummary,
    RunSummary,
    TruncationSummary,
    render_summary_text,
)
from adapter.local.terminal import TerminalFindingsPublisher
from adapter.local.unavailable import EmptyIndexServices, UnavailableIndexServices
from adapter.local.usage import UsageRecordingClient
from apps.local_cli.ci import (
    CiConfigurationError,
    CiContext,
    CiFindingsPublisher,
    build_ci_publisher,
    gate_count,
    load_ci_context,
)
from apps.local_cli.mcp import (
    annotation,
    build_handles,
    build_provider,
    mcp_outcomes,
    resolve_nominated,
)
from apps.local_cli.settings import LocalSettings, ModelNotConfiguredError
from findings.markdown import strip_display_text
from ingestion.diff import Diff, DiffFetcher
from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMError
from review.config import NoViableStepConfigurationError, check_step_configuration_viable
from review.file_matchers import match_go, match_iac, match_javascript, match_php, match_python
from review.output import FindingsPublisher
from review.pipeline import PipelineFailedError, StepRegistry
from review.registry import build_review_registry
from review.static_analysis import AnalyzerRegistration, gitleaks_semgrep_registrations
from review.step import StepContext

EXIT_OK = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_USAGE = 2
EXIT_GATE_TRIPPED = 3  # US-C2: a finding at or above REVIEW_LOCAL_FAIL_ON_SEVERITY


class RuntimeFailure(StrEnum):
    """The one reason string printed (``error: <reason>: <detail>``) for each runtime failure
    (exit status 1). The values are documented in docs/reference/operations.md; a test keeps the
    two in step."""

    GIT = "git could not read the repository"
    PIPELINE = "a review stage failed"
    REDACTION = "redaction failed, nothing sent"
    OUTPUT = "the output file could not be written"
    CI_PERMISSION = "no permission to comment"
    CI_COMMENT = "the pull-request review could not be posted or refreshed"
    BACKEND = "the model backend is unavailable"


# Secret redaction of LLM output shells out to gitleaks and is fail-closed, so a run without it
# would send the diff to the model and then fail; refuse before sending anything.
_REQUIRED_BINARY = "gitleaks"

# Analyzer name (the key ``StepEnablement`` and the registration use) -> the executable it runs.
# One that is not on PATH is skipped, not run to fail: the summary tells "skipped" from "errored".
_ANALYZER_BINARIES = {
    "gitleaks": "gitleaks",
    "semgrep": "semgrep",
    "ruff": "ruff",
    "eslint": "review-eslint",
    "phpstan": "review-phpstan",
    "golangci_lint": "golangci-lint",
    "trivy": "trivy",
}


_RUN_EPILOG = """\
exit codes:
  0  success, including "Nothing to review"
  1  runtime failure (printed as "error: <reason>: <detail>")
  2  usage or configuration error; nothing was sent to the model
  3  severity gate tripped (only if REVIEW_LOCAL_FAIL_ON_SEVERITY is set)

Findings go to stdout; progress, the run summary and errors go to stderr.

environment variables (all optional except the model; no .env file is read):
  model (OpenAI-compatible endpoint)
    REVIEW_LOCAL_LLM_ENDPOINT     base URL, e.g. http://localhost:1234/v1 (required)
    REVIEW_LOCAL_LLM_MODEL        model name (required)
    REVIEW_LOCAL_LLM_API_KEY      key, if the endpoint needs one
    REVIEW_LOCAL_LLM_TIMEOUT_SECONDS    per model call (default 30)
    REVIEW_LOCAL_LLM_MAX_DIFF_BYTES     diff size ceiling per request (default 200000)
  model (opt-in Claude CLI backend; Anthropic terms compliance is UNVERIFIED)
    REVIEW_LOCAL_BACKEND          openai_compatible (default) or claude_cli
    REVIEW_LOCAL_CLAUDE_CLI_EXECUTABLE  the claude executable (default claude)
  behaviour
    REVIEW_LOCAL_STATIC_ANALYSIS_ENABLED  true/false (default true)
    REVIEW_LOCAL_FAIL_ON_SEVERITY exit 3 at or above this severity (off by default)
    REVIEW_LOCAL_MEMORY_FILE_ENABLED, REVIEW_LOCAL_MEMORY_FILE,
    REVIEW_LOCAL_MEMORY_FROM_WORKING_TREE   the repository memory file
  redaction limits
    REVIEW_LOCAL_REDACTION_MAX_FILES, REVIEW_LOCAL_REDACTION_MAX_BYTES,
    REVIEW_LOCAL_REDACTION_TIMEOUT_SECONDS
  MCP knowledge (only for servers named with --mcp or in .review/mcp.json)
    REVIEW_LOCAL_MCP_CONFIG, REVIEW_LOCAL_MCP_NOMINATIONS_FILE,
    REVIEW_LOCAL_MCP_DEADLINE_SECONDS, REVIEW_LOCAL_MCP_MAX_CALLS,
    REVIEW_LOCAL_MCP_MAX_PROCESSES, REVIEW_LOCAL_MCP_MAX_STDIO_BYTES,
    REVIEW_LOCAL_MCP_MAX_LIST_TOOLS_BYTES, REVIEW_LOCAL_MCP_MAX_TOOLS,
    REVIEW_LOCAL_MCP_SCAN_TIMEOUT_SECONDS
  GitHub Actions / CI (set by the runner; set CI=true yourself on other CI systems)
    CI, GITHUB_ACTIONS, GITHUB_API_URL, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH,
    GITHUB_REPOSITORY,
    GITHUB_TOKEN, REVIEW_LOCAL_GITHUB_TIMEOUT_SECONDS,
    REVIEW_LOCAL_GITHUB_MAX_RETRY_ATTEMPTS, REVIEW_LOCAL_CI_BOT_LOGIN

Requires git and the gitleaks binary on PATH (run 'review run --dry-run' to preview what
would be sent). Full reference: docs/reference/operations.md ("Local mode").
"""


def _version() -> str:
    try:
        return importlib.metadata.version("review")
    except importlib.metadata.PackageNotFoundError:
        return "unknown (not installed as a package)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review",
        description="Self-hosted AI code review, on your machine.",
        epilog="Try: review run --base main   (see 'review run --help' for every option "
        "and environment variable)",
    )
    parser.add_argument("--version", action="version", version=f"review {_version()}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log more to stderr (-v: info, -vv: debug); logs never contain code",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run",
        help="review the changes on the current branch",
        epilog=_RUN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scope = run.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--base", help="review the branch against this branch or commit (for example main)"
    )
    scope.add_argument(
        "--staged", action="store_true", help="review only the changes staged for commit"
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent and where, and send nothing",
    )
    run.add_argument(
        "--output",
        type=Path,
        help="also write the findings to this file: .md for markdown, .json for JSON",
    )
    run.add_argument(
        "--mcp",
        action="append",
        default=[],
        metavar="NAME[:TOOL]",
        help="consult this MCP server from your own MCP configuration (repeatable; none is "
        "started or contacted unless named here or in .review/mcp.json)",
    )
    run.add_argument(
        "--allow-claude-cli-in-ci",
        action="store_true",
        help="allow REVIEW_LOCAL_BACKEND=claude_cli when CI is detected (refused otherwise)",
    )
    return parser


# The only structured extras a log line shows: names and counts, never content. Everything else a
# step attaches (job ids, error text, file lists) stays out of local logs.
_LOGGED_EXTRAS = ("step_name", "analyzer_name", "status", "duration_ms")


# Third-party loggers (openai, httpx, litellm) log prompts and headers at DEBUG, so only these
# first-party roots follow -v/-vv; the root logger stays at WARNING.
_FIRST_PARTY = (
    "review",
    "adapter",
    "apps",
    "findings",
    "iam",
    "indexing",
    "ingestion",
    "llmgateway",
    "memory",
    "observability",
    "tenancy",
)


class _LogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Drop exc_info: tracebacks and exception text can embed model or endpoint response bodies.
        safe = logging.makeLogRecord(
            {**record.__dict__, "exc_info": None, "exc_text": None, "stack_info": None}
        )
        text = super().format(safe)
        extras = [f"{k}={record.__dict__[k]}" for k in _LOGGED_EXTRAS if k in record.__dict__]
        return f"{text} [{' '.join(extras)}]" if extras else text


def configure_logging(verbosity: int) -> None:
    """Plain-text logging to stderr; only the message and the whitelisted extras above are shown,
    so logs hold no code content."""
    root = logging.getLogger()
    root.handlers.clear()
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(verbosity, 2)]
    handler = logging.StreamHandler(stream=sys.stderr)
    # The handler filters too, because the run observer lowers two pipeline loggers to INFO to hear
    # step events, and those must not reach stderr unless -v asked for them.
    handler.setLevel(level)
    handler.setFormatter(_LogFormatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    for name in _FIRST_PARTY:
        logging.getLogger(name).setLevel(level)


def build_seed(
    repo_dir: Path, review_range: ReviewRange, ci: CiContext | None = None, *, mcp: bool = False
) -> dict[str, object]:
    """The job payload the queue would have carried (``review.step.JobIdentity``), fabricated:
    there is no organisation or installation locally, and no pull request unless GitHub Actions
    told us which one (``ci``). ``head_sha`` is what the diff is read from: the checked-out commit,
    or in Actions the pull request head (the merge commit if the head is not fetched). ``mcp`` (a
    server is nominated) adds a fixed marker as the pull-request title, so the shared context
    builder runs its provider branch; the query itself is built from the diff by the MCP provider,
    not from this."""
    seed: dict[str, object] = {
        "org_id": "local",
        "repo_id": "local",
        "installation_id": 0,
        "repo_full_name": f"local/{repo_dir.name}" if ci is None else ci.repo_full_name,
        "pr_number": 0 if ci is None else ci.pr_number,
        "head_sha": review_range.head_sha,
        "base_sha": review_range.base_sha,
    }
    if mcp:
        seed["pr_title"] = QUERY_MARKER_TITLE
    return seed


def _analyzer_registrations(environ: Mapping[str, str]) -> list[AnalyzerRegistration]:
    return [
        *gitleaks_semgrep_registrations(
            GitleaksAnalyzer(ignore_pr_config=True, environ=environ),
            SemgrepAnalyzer(ignore_pr_config=True, environ=environ),
        ),
        AnalyzerRegistration(
            "ruff",
            RuffAnalyzer(environ=environ),
            matches=match_python,
            changed_lines_only=True,
            config_filenames=RUFF_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "eslint",
            EslintAnalyzer(environ=environ),
            matches=match_javascript,
            changed_lines_only=True,
            config_filenames=ESLINT_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "phpstan",
            PhpstanAnalyzer(environ=environ),
            matches=match_php,
            changed_lines_only=True,
            config_filenames=PHPSTAN_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "golangci_lint",
            GolangciLintAnalyzer(environ=environ),
            matches=match_go,
            changed_lines_only=True,
            config_filenames=GOLANGCI_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "trivy",
            TrivyAnalyzer(environ=environ),
            matches=match_iac,
            security_class=True,
            config_filenames=TRIVY_CONFIG_FILENAMES,
        ),
    ]


def build_diff_fetcher(
    settings: LocalSettings,
    repo_dir: Path,
    review_range: ReviewRange,
    *,
    environ: Mapping[str, str],
) -> RedactingDiffFetcher:
    """The only diff source the pipeline gets: every byte of it passes input-side redaction. There
    is no unredacted path and no setting that skips it (US-L15)."""
    return RedactingDiffFetcher(
        LocalGitDiffFetcher(repo_dir, review_range, environ=environ),
        GitleaksSecretLineFinder(
            timeout_seconds=settings.redaction_timeout_seconds,
            ignore_pr_config=True,
            environ=environ,
        ),
        max_files=settings.redaction_max_files,
        max_bytes=settings.redaction_max_bytes,
    )


def build_registry(
    settings: LocalSettings,
    *,
    model: str,
    diff_fetcher: DiffFetcher,
    llm_client: LLMClient,
    publisher: FindingsPublisher,
    skipped_analyzers: Collection[str] = (),
    memory_matcher: FileMemoryMatcher | None = None,
    mcp: tuple[LocalMcpResolver, LocalMcpOpener] | None = None,
    environ: Mapping[str, str],
) -> StepRegistry:
    """Wire the concrete local adapters into the shared builder. Raises
    ``NoViableStepConfigurationError`` if the step flags could never produce a review.
    ``memory_matcher`` (the repository memory file) turns memory suppression on; ``mcp`` (the
    resolver and opener over the nominated servers) turns the context builder's provider branch
    on, with a code branch that finds nothing because there is no index."""
    enablement = settings.step_enablement(skipped_analyzers=skipped_analyzers, mcp=mcp is not None)
    check_step_configuration_viable(enablement)
    unavailable = UnavailableIndexServices()
    no_index = EmptyIndexServices() if mcp is not None else unavailable
    return build_review_registry(
        enablement=enablement,
        timeouts=settings.step_timeouts(),
        config=settings.pipeline_config(
            model, memory_suppression=memory_matcher is not None, mcp=mcp is not None
        ),
        diff_fetcher=diff_fetcher,
        tree_fetcher=no_index,
        graph_query=no_index,
        similarity_query=no_index,
        clone_query=unavailable,
        embedding_client=no_index,
        redactor=GitleaksTextRedactor(ignore_pr_config=True, environ=environ),
        llm_client=llm_client,
        publisher=publisher,
        fingerprint_matcher=memory_matcher,
        model_resolver=None,
        analyzer_registrations=_analyzer_registrations(environ),
        # No repo-config fetcher: analyzers use their default profiles (a repo's own config is
        # attacker-influenced in CI and is not read in the first version).
        config_fetcher=None,
        provider_resolver=None if mcp is None else mcp[0],
        provider_opener=None if mcp is None else mcp[1],
    )


def preset_analyzers(settings: LocalSettings) -> dict[str, AnalyzerOutcome]:
    """The analyzers review itself decides not to run: all of them when static analysis is off,
    otherwise those whose executable is not on PATH. (Present-but-failing is "errored", which only
    the run can tell.)"""
    if not settings.static_analysis_enabled:
        return {
            name: AnalyzerOutcome(name=name, status="skipped", reason="static analysis disabled")
            for name in _ANALYZER_BINARIES
        }
    return {
        name: AnalyzerOutcome(name=name, status="skipped", reason="not installed")
        for name, binary in _ANALYZER_BINARIES.items()
        if shutil.which(binary) is None
    }


def _fail(err: TextIO, reason: RuntimeFailure, detail: object) -> int:
    # Detail can carry file names, git stderr or provider response bodies: strip and cap it.
    print(f"error: {reason}: {strip_display_text(str(detail))[:200]}", file=err)
    return EXIT_RUNTIME_FAILURE


def _ci_outcome(
    err: TextIO, summary: RunSummary, ci_publisher: CiFindingsPublisher
) -> tuple[RunSummary, int | None]:
    """Fold the CI comment counts into the summary; the exit status when posting failed."""
    counts = ci_publisher.github.counts
    summary = summary.model_copy(
        update={"ci_comments": CiCommentsSummary(**dataclasses.asdict(counts))}
    )
    if ci_publisher.github.failure is None:
        return summary, None
    # The findings are already in the log (the CI publisher prints before it posts). The caller
    # still runs ``_finish`` (summary and ``--output`` file) before it returns this status.
    kind, detail = ci_publisher.github.failure
    if kind == "permission":
        return summary, _fail(
            err,
            RuntimeFailure.CI_PERMISSION,
            "grant pull-requests: write (a fork pull request's token is read-only), "
            f"or retry if GitHub is rate limiting: {detail}",
        )
    return summary, _fail(err, RuntimeFailure.CI_COMMENT, detail)


async def _finish(
    *,
    repo_dir: Path,
    summary: RunSummary,
    findings: Report,
    output: Path | None,
    output_format: OutputFormat | None,
    err: TextIO,
) -> int:
    """Print the run summary (every run ends with one) and write the ``--output`` file."""
    print(render_summary_text(summary), file=err)
    if output is None or output_format is None:
        return EXIT_OK
    text = (
        render_json(findings) if output_format is OutputFormat.JSON else render_markdown(findings)
    )
    try:
        await asyncio.to_thread(write_output, repo_dir, output, text)
    except OutputPathError as exc:  # the path became a symlink after the pre-run check
        return _fail(err, RuntimeFailure.OUTPUT, exc)
    except OSError as exc:
        return _fail(err, RuntimeFailure.OUTPUT, f"{output}: {exc.strerror or type(exc).__name__}")
    return EXIT_OK


def _redaction_error(failure: PipelineFailedError | None) -> RedactionFailedError | None:
    cause = None if failure is None else failure.__cause__
    return cause if isinstance(cause, RedactionFailedError) else None


def _redaction_summary(
    fetcher: RedactingDiffFetcher, failure: PipelineFailedError | None
) -> RedactionSummary:
    if _redaction_error(failure) is not None:
        return RedactionSummary(status="failed")
    if fetcher.report is None:
        return RedactionSummary()
    return RedactionSummary(
        status="applied",
        count=fetcher.report.redacted_lines,
        denied_paths=list(fetcher.report.denied_paths),
    )


def _truncation_summary(
    observer: RunObserver, failure: PipelineFailedError | None
) -> TruncationSummary:
    if observer.truncation.status != "not_reported" or failure is not None:
        return observer.truncation
    return TruncationSummary(status="none")


def _print_memory_report(out: TextIO, memory: LoadedMemoryFile, matcher: FileMemoryMatcher) -> None:
    """What the memory file dismissed (a dismissal is never silent), and, when the same dismissal
    now covers enough files, a suggested change to the file. The suggestion is only printed:
    nothing here writes to the repository (US-L13)."""
    if matcher.suppressed:
        print(f"Suppressed by {memory.path} ({len(matcher.suppressed)}):", file=out)
        for line in describe_suppressed(matcher.suppressed):
            # The line starts with a PR-controlled file path; a leading "::" would be a GitHub
            # Actions workflow command once the runner trims the indent (see terminal.py).
            print(f"  {'./' + line if line.startswith(':') else line}", file=out)
    proposal = render_proposal(memory, propose_rules(memory.entries))
    if proposal:
        print(
            f"Suggested change to {memory.path} (not applied; review it and commit it yourself):",
            file=out,
        )
        print(proposal, end="", file=out)


async def run_review(
    settings: LocalSettings,
    *,
    repo_dir: Path,
    out: TextIO,
    err: TextIO,
    base: str | None = None,
    staged: bool = False,
    output: Path | None = None,
    dry_run: bool = False,
    allow_claude_cli_in_ci: bool = False,
    mcp: Sequence[str] = (),
    home: Path | None = None,
) -> int:
    """Review ``repo_dir``'s branch against ``base`` (or, with ``staged``, the staged changes);
    returns the process exit status. No credential-named variable reaches a child process.
    ``mcp`` are the nominated MCP server names (``--mcp``); ``home`` is where the developer's own
    MCP configuration is looked for (the home directory by default)."""
    # The one place the process environment is read: every child below gets this scrubbed copy.
    environ = analyzer_env(scrubbed_environment(os.environ))
    return await _run_review(
        settings,
        repo_dir=repo_dir,
        out=out,
        err=err,
        base=base,
        staged=staged,
        output=output,
        dry_run=dry_run,
        allow_claude_cli_in_ci=allow_claude_cli_in_ci,
        mcp=mcp,
        home=Path.home() if home is None else home,
        environ=environ,
        # Only for `${VAR}` in the developer's own MCP entries; never handed to a child as-is.
        mcp_variables=dict(os.environ),
    )


async def _run_review(
    settings: LocalSettings,
    *,
    repo_dir: Path,
    out: TextIO,
    err: TextIO,
    base: str | None,
    staged: bool,
    output: Path | None,
    dry_run: bool,
    allow_claude_cli_in_ci: bool,
    mcp: Sequence[str],
    home: Path,
    environ: Mapping[str, str],
    mcp_variables: Mapping[str, str] | None = None,
) -> int:
    if (base is None) == (not staged):
        print("error: pass exactly one of --base and --staged", file=err)
        return EXIT_USAGE
    use_cli = settings.backend == "claude_cli"
    in_ci = settings.ci or settings.github_actions
    if use_cli:
        # US-L8: best-effort guard, not a security control (a self-hosted runner can unset CI).
        if in_ci and not allow_claude_cli_in_ci:
            print(
                "error: the Claude CLI backend is refused because CI was detected: it would use "
                "a personal Claude login on a shared runner. Pass --allow-claude-cli-in-ci to "
                "override (this is a best-effort guard, not a security control).",
                file=err,
            )
            return EXIT_USAGE
        model, endpoint = settings.llm_model or DEFAULT_MODEL_LABEL, BACKEND_NAME
    else:
        try:
            model, endpoint = settings.require_model()
        except ModelNotConfiguredError as exc:
            print(f"error: {exc}", file=err)
            return EXIT_USAGE
    if dry_run and output is not None:
        print("error: --output cannot be combined with --dry-run (nothing is reviewed)", file=err)
        return EXIT_USAGE
    try:
        # A dry run posts nothing, so it needs no GitHub configuration.
        ci = None if dry_run else await asyncio.to_thread(load_ci_context, settings)
    except CiConfigurationError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_USAGE
    if ci is not None and staged:
        # The index is a developer-machine notion; in CI the "base commit" it would give the memory
        # file is HEAD, the pull-request head itself.
        print("error: --staged is not available in GitHub Actions; use --base", file=err)
        return EXIT_USAGE
    if (
        settings.memory_file_enabled
        and (settings.ci or settings.github_actions)
        and ci is None
        and not dry_run
        and not settings.memory_from_working_tree
    ):
        # A CI run that is not a recognised pull-request context: the working tree is the
        # pull-request head, which can dismiss its own findings through the memory file.
        print(
            "error: CI was detected (CI=true) without a recognised pull-request context, so the "
            "memory file would be read from the pull-request head. Set "
            "REVIEW_LOCAL_MEMORY_FILE_ENABLED=false to run without it, or "
            "REVIEW_LOCAL_MEMORY_FROM_WORKING_TREE=true if this checkout is trusted.",
            file=err,
        )
        return EXIT_USAGE

    try:
        repo_dir = await resolve_repo_root(repo_dir, environ=environ)
    except LocalGitError as exc:
        return _fail(err, RuntimeFailure.GIT, exc)

    # Before any model call: a run must not spend one on findings it cannot keep.
    output_format = None
    if output is not None:
        try:
            output_format = await check_output_path(repo_dir, output, environ=environ)
        except OutputPathError as exc:
            print(f"error: --output: {exc}", file=err)
            return EXIT_USAGE
        except LocalGitError as exc:
            return _fail(err, RuntimeFailure.GIT, exc)
        # A run that fails before the pipeline (git, backend preflight, missing gitleaks) must not
        # leave an earlier run's findings in the file; a successful run rewrites it.
        await asyncio.to_thread(discard_stale_output, output)

    try:
        if base is None:
            review_range = await resolve_staged_range(repo_dir, environ=environ)
        elif ci is None:
            review_range = await resolve_review_range(repo_dir, base, environ=environ)
        else:
            try:
                review_range = await resolve_review_range(
                    repo_dir, base, environ=environ, head=ci.head_sha
                )
            except GitRefNotFoundError as missing:
                if missing.ref != ci.head_sha:
                    raise  # the base is what is missing, not the head
                # A shallow checkout of the merge ref does not contain the pull request's head.
                print(
                    "warning: the pull request head is not in this checkout (use fetch-depth: 0); "
                    "comment lines come from the merge commit and may be off if the base moved",
                    file=err,
                )
                review_range = await resolve_review_range(repo_dir, base, environ=environ)
    except (NotAGitRepositoryError, GitRefNotFoundError, NoMergeBaseError) as exc:
        print(f"error: {exc}", file=err)
        return EXIT_USAGE
    except LocalGitError as exc:
        return _fail(err, RuntimeFailure.GIT, exc)

    # Before any model call, so a malformed file stops the run. In CI the file comes from the base
    # commit: the pull request being reviewed cannot dismiss its own findings.
    memory: LoadedMemoryFile | None = None
    memory_status = MemoryFileSummary()  # "disabled"
    if settings.memory_file_enabled:
        try:
            memory = await load_memory_file(
                repo_dir,
                settings.memory_file,
                base_commit=None if ci is None else review_range.base_sha,
                environ=environ,
            )
        except MemoryFileError as exc:
            print(f"error: {exc}", file=err)
            return EXIT_USAGE
        except LocalGitError as exc:
            return _fail(err, RuntimeFailure.GIT, exc)
        memory_status = (
            MemoryFileSummary(status="not_found")
            if memory is None
            else MemoryFileSummary(status="loaded", entries=len(memory.entries))
        )
    matcher = None if memory is None else FileMemoryMatcher(memory.entries)

    # Before any model call: an unknown or unusable nominated server stops the run. Nothing is
    # read, started or contacted when no server is nominated. In CI the nomination file comes from
    # the base commit, so the pull request cannot nominate a server.
    diff_fetcher = build_diff_fetcher(settings, repo_dir, review_range, environ=environ)
    mcp_provider: McpKnowledgeProvider | None = None
    mcp_seam: tuple[LocalMcpResolver, LocalMcpOpener] | None = None
    try:
        resolved = await resolve_nominated(
            settings,
            repo_dir=repo_dir,
            home=home,
            options=mcp,
            in_ci=in_ci,
            base_commit=review_range.base_sha,
            staged=staged,
            environ=environ,
            variables=mcp_variables,
        )
        handles = build_handles(settings, resolved, environ=environ) if resolved else []
    except McpConfigError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_USAGE
    except LocalGitError as exc:
        return _fail(err, RuntimeFailure.GIT, exc)
    if resolved:
        mcp_provider, mcp_resolver, mcp_opener = build_provider(
            settings,
            handles,
            diff=lambda: diff_fetcher.diff,
            finder=GitleaksSecretLineFinder(
                timeout_seconds=settings.mcp_scan_timeout_seconds,
                ignore_pr_config=True,
                environ=environ,
            ),
            consult=not dry_run,
        )
        mcp_seam = (mcp_resolver, mcp_opener)

    summary = RunSummary(
        backend=BackendSummary(
            kind=settings.backend, model=model, ci_guard_overridden=use_cli and in_ci
        ),
        memory_file=memory_status,
        suppressed_count=None if matcher is None else 0,
        mcp_servers=mcp_outcomes(mcp_provider, ()),
    )
    terminal = TerminalFindingsPublisher(
        out, annotate=None if mcp_provider is None else annotation(mcp_provider)
    )
    ci_publisher = (
        None
        if ci is None
        else build_ci_publisher(
            settings,
            ci,
            terminal,
            suppressed_lines=None
            if matcher is None
            else lambda: describe_suppressed(matcher.suppressed),
        )
    )
    publisher: FindingsPublisher = terminal if ci_publisher is None else ci_publisher

    if not review_range.has_changes:
        print(
            "Nothing to review: no staged changes."
            if staged
            else f"Nothing to review: no changes against {base}.",
            file=out,
        )
        if ci_publisher is not None:
            # Comments from an earlier push must still be reconciled (marked outdated).
            await ci_publisher.github.post_check_run(
                installation_id=0,
                repo_full_name=ci.repo_full_name if ci else "",
                head_sha=review_range.head_sha,
                finding_count=0,
            )
            summary, failed = _ci_outcome(err, summary, ci_publisher)
        else:
            failed = None
        status = await _finish(
            repo_dir=repo_dir,
            summary=summary,
            findings=Report(findings=[], summary=summary),
            output=output,
            output_format=output_format,
            err=err,
        )
        return status if failed is None else failed

    if shutil.which(_REQUIRED_BINARY, path=environ.get("PATH")) is None:
        print(
            f"error: {_REQUIRED_BINARY} was not found on PATH. It redacts secrets from the "
            "diff before it is sent, so nothing is sent to the model without it. Install it "
            "(https://github.com/gitleaks/gitleaks) and retry.",
            file=err,
        )
        return EXIT_USAGE

    preset = preset_analyzers(settings)
    # A dry run builds the same requests against a client that records them and sends nothing.
    recorder = RecordingLLMClient()
    cli_client = ClaudeCliClient(executable=settings.claude_cli_executable, environ=environ)
    sending = UsageRecordingClient(
        cli_client
        if use_cli
        else LiteLLMClient(
            endpoint=endpoint, api_key=settings.llm_api_key.get_secret_value(), max_retries=0
        )
    )
    llm_client: LLMClient = recorder if dry_run else sending
    if use_cli and not dry_run:
        # Before any content is sent: a missing or logged-out CLI stops the run here.
        try:
            await cli_client.preflight()
        except LLMError as exc:
            return _fail(err, RuntimeFailure.BACKEND, exc)
    try:
        registry = build_registry(
            settings,
            model=model,
            diff_fetcher=diff_fetcher,
            llm_client=llm_client,
            publisher=publisher,
            skipped_analyzers=preset,
            memory_matcher=matcher,
            mcp=mcp_seam,
            environ=environ,
        )
    except NoViableStepConfigurationError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_USAGE

    observer = RunObserver(analyzer_names=tuple(_ANALYZER_BINARIES), preset=preset, progress=err)
    failure: PipelineFailedError | None = None
    with observing(observer):
        try:
            await registry.run(
                build_seed(repo_dir, review_range, ci, mcp=mcp_seam is not None),
                # Context.dry_run only suppresses posting, which is the terminal output; a real
                # run needs it. A dry run sends nothing (the recording client) and prints no
                # findings (this flag).
                StepContext(job_id=str(uuid.uuid4()), dry_run=dry_run),
            )
        except PipelineFailedError as exc:
            failure = exc

    summary = summary.model_copy(
        update={
            "stages": list(observer.stages.values()),
            "analyzers": [
                observer.analyzers.get(name)
                or AnalyzerOutcome(name=name, status="skipped", reason="not run")
                for name in _ANALYZER_BINARIES
            ],
            "token_usage": None if dry_run else sending.usage(),
            "redaction": _redaction_summary(diff_fetcher, failure),
            "truncation": _truncation_summary(observer, failure),
            "suppressed_count": None if matcher is None else len(matcher.suppressed),
            "mcp_servers": mcp_outcomes(mcp_provider, terminal.findings),
        }
    )
    if failure is not None:
        if output is not None and output_format is not None:
            await asyncio.to_thread(discard_stale_output, output)
        redaction_error = _redaction_error(failure)
        if redaction_error is not None:
            # Raised in the first step, before any request could be built: nothing was sent.
            print(render_summary_text(summary), file=err)
            return _fail(err, RuntimeFailure.REDACTION, redaction_error.reason)
        # No partial findings are written as if complete; the summary still says where it stopped.
        print(render_summary_text(summary), file=err)
        # Step, cause class and the configured endpoint (US-L6 names it; userinfo dropped) only:
        # the cause text can echo prompt content or response bodies.
        cause = type(failure.__cause__).__name__
        where = mask_url(endpoint)
        return _fail(
            err, RuntimeFailure.PIPELINE, f"step {failure.step_name!r} ({cause}) via {where}"
        )
    if dry_run:
        print(render_summary_text(summary), file=err)
        report = build_egress_report(
            recorder.requests,
            endpoint=endpoint,
            diff=diff_fetcher.diff or Diff(files=()),
            ceiling_bytes=settings.llm_max_diff_bytes,
            truncation=summary.truncation,
            withheld_paths=summary.redaction.denied_paths,
            mcp=[
                EgressMcpQuery(
                    server=record.name,
                    transport=record.transport,
                    host=record.host,
                    tool=record.tool,
                    query=record.query,
                    note=record.reason if record.query is None else None,
                )
                for record in ([] if mcp_provider is None else mcp_provider.records.values())
            ],
        )
        print(render_egress_text(report), file=out)
        return EXIT_OK
    if memory is not None and matcher is not None:
        _print_memory_report(out, memory, matcher)
    ci_failed: int | None = None
    if ci_publisher is not None:
        summary, ci_failed = _ci_outcome(err, summary, ci_publisher)
    found = terminal.findings
    status = await _finish(
        repo_dir=repo_dir,
        summary=summary,
        findings=Report(findings=found, summary=summary),
        output=output,
        output_format=output_format,
        err=err,
    )
    if ci_failed is not None:
        return ci_failed
    gated = gate_count(settings.fail_on_severity, found)
    if status == EXIT_OK and gated:
        print(
            f"gate: {gated} finding(s) at or above severity {settings.fail_on_severity}",
            file=err,
        )
        return EXIT_GATE_TRIPPED
    return status


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        settings = LocalSettings()
    except ValidationError as exc:
        # Field names and reasons only: the offending values may be credentials.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        print(f"error: invalid configuration: {problems}", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(
        run_review(
            settings,
            base=args.base,
            staged=args.staged,
            output=args.output,
            dry_run=args.dry_run,
            allow_claude_cli_in_ci=args.allow_claude_cli_in_ci,
            mcp=args.mcp,
            repo_dir=Path.cwd(),
            out=sys.stdout,
            err=sys.stderr,
        )
    )


def console_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    console_main()
