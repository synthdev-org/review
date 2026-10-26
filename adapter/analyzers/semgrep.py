"""adapter.analyzers.semgrep — the concrete ``StaticAnalyzer`` implementation
backed by the ``semgrep`` CLI. Semgrep is **not** in the main dependency
group (it hard-pins ``mcp`` 1.x, which conflicts with the product-knowledge
adapter's ``mcp>=2.3``, ADR-0013); it is installed from its own locked
environment (``tools/semgrep``) and found as ``semgrep`` on ``PATH`` — the
Dockerfile's ``semgrep-tool`` stage and ci.yml set that up. It is run against the vendored,
pinned ruleset (``adapter/analyzers/semgrep_ruleset.yaml`` — see that file's
own header comment for the full "why not a live registry pack" reasoning).

Semgrep's own ``--json`` flag writes its full JSON result to stdout (unlike
Gitleaks — see ``adapter/analyzers/gitleaks.py``'s own docstring for that
gap), so this module's invocation goes straight through
``adapter/analyzers/subprocess_runner.py``'s chunked-stdout-read-with-cap
mechanism exactly as the plan's own Phase 5 section describes.

**Matched source line, read from already-fetched content, not Semgrep's own
CLI:** Semgrep's OSS CLI mode does not include the actual matched source
text in its JSON output (that field is a Semgrep-registry/login-gated
feature — confirmed against the installed package's real output, not
assumed) — only the rule's own fixed ``message`` plus file/line/column
position. Since this platform already holds every scanned file's own bytes
in memory (``content: dict[str, bytes]``, passed into ``run()`` per the
``StaticAnalyzer`` Protocol), this module reads the matched line(s) directly
from that same in-memory content and appends them to the finding's own
``message`` — reproducing the plan's "Semgrep's match messages routinely
quote the matched source line verbatim" property (the reason
``findings.redact.TextRedactor`` exists at all for this analyzer's own
findings) without depending on Semgrep's gated feature.

**Argument-injection guard:** a Click-based CLI taking a list of
file/directory positional ``TARGETS`` — this module inserts a ``--``
end-of-options separator before the filename arguments (verified against
the installed package: a filename beginning with ``-`` is otherwise parsed
as a CLI flag, not a positional path), per the plan's Phase 5 section.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from adapter.analyzers.environment import ChildEnvironmentMixin
from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.subprocess_runner import (
    cleanup_temp_directory,
    materialize_into_temp_directory,
    original_path_mapping,
    run_analyzer_subprocess,
)
from findings.models import Finding, Severity
from ingestion.diff import Diff
from review.static_analysis import StaticAnalyzer

logger = logging.getLogger(__name__)

_RULESET_PATH = Path(__file__).parent / "semgrep_ruleset.yaml"

_SEVERITY_MAP: dict[str, Severity] = {
    "ERROR": "high",
    "WARNING": "medium",
    "INFO": "low",
}
_DEFAULT_SEVERITY: Severity = "medium"


def _argv(ruleset_filename: str, paths: list[str], *, disable_nosem: bool = False) -> list[str]:
    argv = _base_argv(ruleset_filename, paths)
    if disable_nosem:
        argv.insert(argv.index("--no-git-ignore") + 1, "--disable-nosem")
    return argv


def _base_argv(ruleset_filename: str, paths: list[str]) -> list[str]:
    return [
        "semgrep",
        "--config",
        # A bare filename, not `_RULESET_PATH`'s own absolute location —
        # deliberate, not a stylistic choice: Semgrep derives each finding's
        # own `check_id` namespace from the *resolved* absolute path of a
        # filesystem `--config` value (confirmed against the installed
        # package's real output, not assumed) — passing the real vendored
        # ruleset's own absolute (or any path that still resolves through
        # it) path would embed this machine's own local filesystem layout
        # into every finding's `rule_id`, differing between dev/CI/prod
        # environments for the exact same ruleset content. This module
        # copies the ruleset into the per-invocation materialization
        # directory first (`SemgrepAnalyzer.run`, below) and references it
        # by this bare, directory-relative filename instead, so `check_id`
        # stays exactly the ruleset's own declared `id` (e.g.
        # `review-eval-usage`), identical across every environment.
        ruleset_filename,
        "--json",
        # Suppresses Semgrep's own human-readable progress/summary banner
        # on stderr — necessary, not cosmetic: `adapter/analyzers/subprocess_runner.py`
        # merges stderr into the same stream as stdout (per that module's own
        # docstring), and without `--quiet`, that banner text would be
        # interleaved into the captured bytes this module parses as JSON,
        # corrupting it (confirmed against the installed package's real
        # output, not assumed).
        "--quiet",
        "--no-git-ignore",
        "--metrics",
        "off",
        # Deterministic/offline-safe, matching the same reasoning behind
        # vendoring the ruleset itself (see semgrep_ruleset.yaml's own
        # header comment) — no network call to check for a newer Semgrep
        # release on every invocation.
        "--disable-version-check",
        # End-of-options separator — a PR-controlled filename beginning
        # with `-` is always treated as a positional path, never parsed as
        # a CLI flag. See the module docstring.
        "--",
        *paths,
    ]


def _parse_output(raw: bytes) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="semgrep", detail=str(exc)) from exc
    try:
        results = parsed["results"]
    except (KeyError, TypeError) as exc:
        raise AnalyzerOutputParseError(tool="semgrep", detail=str(exc)) from exc
    if not isinstance(results, list):
        raise AnalyzerOutputParseError(tool="semgrep", detail="'results' was not a JSON list")
    return results


def _matched_source_text(
    content: dict[str, bytes], path: str, start_line: int, end_line: int
) -> str:
    """Reads lines ``start_line``..``end_line`` (1-indexed, inclusive) from
    ``content[path]`` — the same already-fetched bytes this analyzer scans —
    per the module docstring's "Matched source line" note. Returns an empty
    string if the path/line range can't be resolved (e.g. a line-ending
    mismatch against Semgrep's own count), rather than raising: this is
    supplementary context for the finding's own message, not load-bearing
    for the finding's existence.
    """
    raw = content.get(path)
    if raw is None:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    lines = text.split("\n")
    start_index = start_line - 1
    end_index = end_line
    if start_index < 0 or start_index >= len(lines):
        return ""
    return "\n".join(lines[start_index:end_index])


def _to_finding(
    entry: dict[str, Any], content: dict[str, bytes], original_by_normalized: dict[str, str]
) -> Finding:
    try:
        normalized_path = entry["path"]
        check_id = entry["check_id"]
        start_line = entry["start"]["line"]
        extra = entry["extra"]
        rule_message = extra["message"]
    except (KeyError, TypeError) as exc:
        raise AnalyzerOutputParseError(tool="semgrep", detail=str(exc)) from exc

    # Semgrep reports the (already-normalized) path it actually scanned —
    # map it back to the original GitHub-provided path (the `content` dict's
    # own key) so both the finding's own `file` field and the matched-line
    # lookup below agree with every other analyzer's/step's path handling.
    path = original_by_normalized.get(normalized_path, normalized_path)

    end_line = entry.get("end", {}).get("line", start_line)
    matched_text = _matched_source_text(content, path, start_line, end_line)
    message = f"{rule_message}\n\nMatched line: {matched_text}" if matched_text else rule_message

    raw_severity = extra.get("severity")
    severity = (
        _SEVERITY_MAP.get(raw_severity, _DEFAULT_SEVERITY)
        if isinstance(raw_severity, str)
        else _DEFAULT_SEVERITY
    )

    return Finding(
        file=path,
        line=start_line if isinstance(start_line, int) else None,
        severity=severity,
        message=message,
        source="semgrep",
        rule_id=check_id if isinstance(check_id, str) else None,
    )


class SemgrepAnalyzer(ChildEnvironmentMixin):
    """``review.static_analysis.StaticAnalyzer`` implementation backed by
    the ``semgrep`` package, run against the vendored, pinned ruleset. See
    this module's own docstring for the full design.
    """

    name = "semgrep"

    def __init__(
        self, *, ignore_pr_config: bool = False, environ: Mapping[str, str] | None = None
    ) -> None:
        # Local mode sets it: a `# nosemgrep` in the reviewed (attacker-controlled) source must not
        # silence a rule. Server mode keeps honouring a repository's own suppressions.
        self._ignore_pr_config = ignore_pr_config
        super().__init__(environ=environ)

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        if not content:
            # No positional paths would follow `_argv`'s own trailing `--`
            # end-of-options separator, and Semgrep's CLI falls back to its
            # own default of scanning the current directory when no TARGETS
            # are given — silently scanning the (otherwise-empty except for
            # the copied ruleset) materialization directory instead of the
            # genuine no-op this analyzer is supposed to perform when the
            # pre-filtered diff contributed nothing to scan.
            return []
        directory = await materialize_into_temp_directory(content)
        try:
            # A per-invocation-unique name, not the fixed
            # _RULESET_COPY_FILENAME constant literally — a PR is
            # attacker-controlled input, so a file already materialized at
            # that exact predictable path (however unlikely) must never be
            # silently overwritten by this copy.
            ruleset_copy_name = f".review-semgrep-ruleset-{uuid.uuid4().hex}.yaml"
            # Off the event loop, same as materialize_into_temp_directory/
            # cleanup_temp_directory above and below — a blocking filesystem
            # copy here would stall every other in-flight coroutine on this
            # Worker process for the copy's duration.
            await asyncio.to_thread(shutil.copyfile, _RULESET_PATH, directory / ruleset_copy_name)

            # `materialize_into_temp_directory` writes each file at its
            # `validate_relative_path`-normalized path (backslashes
            # converted to forward slashes) — passing the raw,
            # unnormalized `content` keys here would ask Semgrep to scan a
            # path that was never actually written whenever a filename
            # contains a backslash. Re-running the same (already-succeeded,
            # since materialization above didn't raise) normalization keeps
            # both in agreement, and the mapping lets results be reported
            # back under the original GitHub-provided path.
            original_by_normalized = original_path_mapping(content)
            paths = sorted(original_by_normalized.keys())
            output = await run_analyzer_subprocess(
                _argv(ruleset_copy_name, paths, disable_nosem=self._ignore_pr_config),
                cwd=directory,
                timeout=timeout,
                env=self._environ,
            )
            results = _parse_output(output)
            return [_to_finding(entry, content, original_by_normalized) for entry in results]
        finally:
            await cleanup_temp_directory(directory)


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the Protocol it's built to implement.
_: type[StaticAnalyzer] = SemgrepAnalyzer
