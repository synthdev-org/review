"""Worker entrypoint — composition root.

Parses config once (pydantic-settings) and configures logging here; later
phases wire further adapters (analyzers) in via constructor injection at
this same composition root, per docs/rules/architecture.md's Decoupling
section. Phase 3 of the delivery plan adds the LLM Gateway's startup
connectivity check here — the same fail-fast pattern as Phase 2's pgvector
check (`adapter/storage/pgvector_check.py`): make one low-cost call through
the configured LLM endpoint, and if it's unreachable or returns a malformed
response, log a clear error and exit the process non-zero rather than
start up in a silently-broken state.

Phase 2 of the delivery plan (the pipeline step framework) adds a second
fail-fast startup check here, same shape: refuse to start if the
enabled-step configuration can never produce a review (US-F7).

Phase 3 of the delivery plan replaces this entrypoint's previous terminal
behavior (log "worker idle: no job dispatch wired yet" and return) with the
real thing: wire the storage adapter (`JobQueueRepository`), construct the
`review.pipeline.StepRegistry` REVIEW jobs dispatch to (no concrete steps
registered yet — Phases 4-7 each add one real step to it, with zero change
to `review/pipeline.py`/`review/step.py` themselves, per US-F3), and hand
both to `apps/worker/dispatch.py`'s `run_dispatch_loop`, which runs forever.

Phase 4 of the delivery plan (this phase) adds this entrypoint's first real
GitHub-calling adapters and its first real pipeline step: a third fail-fast
startup check (the configured `github_app_private_key` is validated by
attempting one real RS256 signing call with it, same shape as the existing
LLM-reachability and step-configuration checks), a
`GitHubInstallationTokenProvider`/`GitHubDiffFetcher` pair constructed here
and wired into `review.prefilter.PrefilterStep`, and a one-time startup log
when an operator has set `github_retry_max_attempts=0`.

Phase 5 of the delivery plan adds the static-analysis step: the concrete
`GitleaksAnalyzer`/`SemgrepAnalyzer` (`adapter/analyzers`) and the
`TextRedactor` dependency (real, Gitleaks-backed, when
`secret_redaction_enabled` is `True` — the shipped default; a no-op
passthrough plus a one-time `warning`-level startup log otherwise, per
`findings/redact.py`'s own module docstring) are all wired here and
registered as `review.static_analysis.StaticAnalysisStep` in the same
macro-step registry `PrefilterStep` already sits in.

Phase 6 of the delivery plan adds the llm-review step:
`review.llm_review.LLMReviewStep`, the third macro step registered into the
same registry, binding the already-built `LLMClient` this composition root
already constructs for the startup connectivity check (`_build_llm_client`,
Phase 3) — no second client is built — and reusing the exact same
`TextRedactor` instance `StaticAnalysisStep` already receives (Phase 5's
`secret_redaction_enabled` flag gates both step's redaction uniformly, per
`review/llm_review.py`'s own module docstring).

Phase 7 of the delivery plan adds the fourth and final macro step,
`review.output.FindingsOutputStep`, registered as the registry's own
`terminal_step` (`review/pipeline.py`'s own "Terminal step" docstring note)
so it also runs on a fatal upstream failure, not only the happy path. Its
`adapter.github.posting.GitHubFindingsPublisher` dependency shares the same
`GitHubInstallationTokenProvider` instance `_build_diff_fetcher` already
constructs (`_build_token_provider`, extracted out from that function this
phase) — no second token provider, no second cache, per
`adapter/github/posting.py`'s own "no new auth code, no new config, of its
own" docstring note. This phase also reads `WorkerSettings.dry_run` once
here and threads it into `apps/worker/dispatch.py`'s `run_dispatch_loop`,
which sets it on every claimed job's shared `StepContext`.

Phase 4 of the indexing delivery plan (docs/plans/plan-indexing-delivery.md
— a separate plan from the "Phase N" numbering used by every docstring
paragraph above this one, disambiguated explicitly here) adds a fourth
fail-fast startup check: the bundled local embedding model
(`adapter.embedding.local_onnx.LocalONNXEmbeddingClient`) is verified to
load and produce a real inference result once at startup
(`adapter.embedding.startup_check.check_embedding_model_loaded`), the same
fail-fast pattern as the LLM-reachability, step-configuration, and GitHub
private-key checks above.

Phase 7 of the indexing delivery plan wires the `LocalONNXEmbeddingClient`
built for that startup check (no second client) into the indexing job
dispatch path: `adapter.github.tree.GitHubTreeFetcher` (sharing the same
`GitHubInstallationTokenProvider`/`httpx.AsyncClient` every other GitHub
adapter here already shares — no second token cache) and
`adapter.storage.indexing_repository.IndexingRepository` (sharing the same
`session_factory` `JobQueueRepository` already uses) are constructed here
and bundled into `indexing.pipeline.IndexingJobDependencies`, threaded into
`apps/worker/dispatch.py`'s `run_dispatch_loop` — replacing that module's
prior `INDEX`-job stub with a real call into
`indexing.pipeline.run_indexing_job`.

Settings are parsed and logging configured inside `main()`, not at module
import time, so importing this module (mypy, pytest collection) never
requires environment variables or performs process-level side effects.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from adapter.analyzers.gitleaks import GitleaksAnalyzer, GitleaksTextRedactor
from adapter.analyzers.semgrep import SemgrepAnalyzer
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.embedding.startup_check import check_embedding_model_loaded
from adapter.github.auth import GitHubInstallationTokenProvider, validate_private_key
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.exceptions import GitHubPrivateKeyInvalidError
from adapter.github.posting import GitHubFindingsPublisher
from adapter.github.tree import GitHubTreeFetcher
from adapter.llm.litellm_client import LiteLLMClient
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.queue import JobQueueRepository
from apps.worker.dispatch import run_dispatch_loop
from apps.worker.logging_config import configure_logging
from apps.worker.settings import WorkerSettings
from findings.redact import NoOpTextRedactor, TextRedactor
from indexing.embedding import EmbeddingError
from indexing.pipeline import IndexingJobDependencies
from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMError
from llmgateway.startup_check import check_llm_reachable
from review.config import (
    NoViableStepConfigurationError,
    StepEnablement,
    StepTimeouts,
    check_step_configuration_viable,
    static_analysis_can_produce_findings,
)
from review.llm_review import LLMReviewStep
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, StepRegistry
from review.prefilter import PrefilterStep
from review.static_analysis import StaticAnalysisStep


def _build_llm_client(settings: WorkerSettings) -> LLMClient:
    """Wire the domain-defined `LLMClient` Protocol to its LiteLLM-backed
    implementation. The one place in this composition root that does so,
    per architecture.md's "wire concrete implementations only at the
    composition root" rule — and a seam a unit test below substitutes a
    fake at, so `main()`'s config-parsing/logging behavior stays testable
    without a real LLM endpoint.
    """
    return LiteLLMClient(
        endpoint=str(settings.llm_endpoint),
        api_key=settings.llm_api_key.get_secret_value(),
    )


async def _check_llm_startup(
    client: LLMClient, settings: WorkerSettings, logger: logging.Logger
) -> None:
    """Run the LLM Gateway startup connectivity check and raise
    `SystemExit(1)` if it fails — the same fail-fast pattern as Phase 2's
    pgvector check."""
    try:
        await check_llm_reachable(
            client, model=settings.llm_model, timeout=settings.llm_timeout_seconds
        )
    except LLMError as exc:
        logger.error(
            "LLM endpoint unreachable at startup; exiting",
            extra={"llm_endpoint": str(settings.llm_endpoint), "error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info("llm endpoint reachable", extra={"llm_endpoint": str(settings.llm_endpoint)})


def _check_github_private_key_startup(settings: WorkerSettings, logger: logging.Logger) -> None:
    """Run the GitHub App private-key startup validation check and raise
    `SystemExit(1)` if it fails — the same fail-fast pattern as
    `_check_llm_startup` above. A malformed/truncated
    `github_app_private_key` (a Helm value copy-paste or newline-escaping
    error) fails loudly here, not silently on the first real job's
    `adapter.github.diff.GitHubDiffFetcher` call.
    """
    try:
        validate_private_key(
            app_id=settings.github_app_id,
            private_key_pem=settings.github_app_private_key.get_secret_value(),
        )
    except GitHubPrivateKeyInvalidError as exc:
        logger.error(
            "GitHub App private key is not usable at startup; exiting",
            extra={"github_app_id": settings.github_app_id, "error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info("github app private key valid", extra={"github_app_id": settings.github_app_id})


def _log_github_retry_configuration(settings: WorkerSettings, logger: logging.Logger) -> None:
    """A one-time startup log when an operator has set
    `github_retry_max_attempts=0`, stating that GitHub API retries are
    disabled — `info`, not the per-repo rate limiter's own `warning` (see
    the plan's "Assumptions this plan makes" section for the full
    shipped-default-vs-deliberate-deviation distinction between the two):
    this phase's own shipped default (`3`) already ships the retry/backoff
    protection active, so this log only ever fires when an operator has
    actively deviated away from it, never for the shipped default itself.
    """
    if settings.github_retry_max_attempts == 0:
        logger.info(
            "GitHub API retries are disabled (github_retry_max_attempts=0)",
            extra={"github_retry_max_attempts": 0},
        )


def _build_embedding_client(settings: WorkerSettings) -> LocalONNXEmbeddingClient:
    """Wire the indexing domain's `EmbeddingClient` Protocol to its local
    ONNX-backed implementation, per architecture.md's "wire concrete
    implementations only at the composition root" rule — the indexing
    delivery plan's Phase 4 counterpart to `_build_llm_client` above.
    `settings.embedding_model_path` points at the Dockerfile's
    checksum-verified, baked-in model directory (see that file's own
    `embedding-model` stage) — never a network location.
    """
    return LocalONNXEmbeddingClient(model_path=settings.embedding_model_path)


def _check_embedding_model_startup(
    client: LocalONNXEmbeddingClient, settings: WorkerSettings, logger: logging.Logger
) -> None:
    """Run the local embedding model's startup load/inference check and
    raise `SystemExit(1)` if it fails — the same fail-fast pattern as
    `_check_llm_startup` and `adapter/storage/pgvector_check.py` before it
    (indexing delivery plan's Phase 4 "Non-Functional Requirements"
    section). A missing/corrupted baked model artifact fails loudly here,
    at Worker boot, rather than silently on the first real `INDEX` job's
    `embed` stage.

    Deliberately called synchronously, not via `asyncio.to_thread` (unlike
    Phase 7's per-file `embed` calls once the real dispatch loop exists) —
    this runs exactly once, before the dispatch loop starts, the same way
    `_check_github_private_key_startup`'s own synchronous
    `validate_private_key` call already blocks startup briefly by design.
    """
    try:
        check_embedding_model_loaded(client)
    except EmbeddingError as exc:
        logger.error(
            "local embedding model is not usable at startup; exiting",
            extra={"embedding_model_path": settings.embedding_model_path, "error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info(
        "local embedding model loaded",
        extra={"embedding_model_path": settings.embedding_model_path},
    )


def _build_token_provider(
    settings: WorkerSettings, http_client: httpx.AsyncClient
) -> GitHubInstallationTokenProvider:
    """The one `GitHubInstallationTokenProvider` instance this composition
    root constructs — shared by `_build_diff_fetcher` (Phase 4) and
    `_build_findings_publisher` (Phase 7) alike, so both draw from the same
    `(installation_id, repo_full_name)`-keyed token cache rather than each
    holding an independent one. Extracted out of `_build_diff_fetcher`
    itself this phase, which previously constructed it inline as its own
    private concern before a second consumer existed.

    `http_client` is the one long-lived `httpx.AsyncClient` this
    composition root constructs and closes (see `main()`), shared across
    every GitHub adapter instead of each opening/closing its own per-call
    client — keep-alive/TLS session resumption then actually applies across
    calls within a job and across the Worker's whole lifetime.
    """
    return GitHubInstallationTokenProvider(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_app_private_key.get_secret_value(),
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
        client=http_client,
    )


def _build_diff_fetcher(
    settings: WorkerSettings,
    token_provider: GitHubInstallationTokenProvider,
    http_client: httpx.AsyncClient,
) -> GitHubDiffFetcher:
    """Wire the domain-defined `DiffFetcher` Protocol to its GitHub-backed
    implementation, per architecture.md's "wire concrete implementations
    only at the composition root" rule. See `_build_token_provider`'s own
    docstring for why `http_client` is shared, not built per-fetcher.
    """
    return GitHubDiffFetcher(
        token_provider=token_provider,
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
        client=http_client,
    )


def _build_findings_publisher(
    settings: WorkerSettings,
    token_provider: GitHubInstallationTokenProvider,
    http_client: httpx.AsyncClient,
) -> GitHubFindingsPublisher:
    """Wire the domain-defined `FindingsPublisher` Protocol to its
    GitHub-backed implementation (Phase 7), sharing `token_provider` with
    `_build_diff_fetcher` rather than constructing a second one — see
    `adapter/github/posting.py`'s own "no new auth code, no new config, of
    its own" docstring note. See `_build_token_provider`'s own docstring for
    why `http_client` is shared, not built per-publisher.
    """
    return GitHubFindingsPublisher(
        token_provider=token_provider,
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
        client=http_client,
    )


def _build_tree_fetcher(
    settings: WorkerSettings,
    token_provider: GitHubInstallationTokenProvider,
    http_client: httpx.AsyncClient,
) -> GitHubTreeFetcher:
    """Wire the indexing domain's `RepoTreeFetcher` Protocol to its
    GitHub-backed implementation (indexing delivery plan's Phase 7),
    sharing `token_provider`/`http_client` with `_build_diff_fetcher`/
    `_build_findings_publisher` rather than constructing a second token
    cache or a second long-lived client — same reasoning as
    `_build_token_provider`'s own docstring.
    """
    return GitHubTreeFetcher(
        token_provider=token_provider,
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
        client=http_client,
    )


def _build_indexing_deps(
    tree_fetcher: GitHubTreeFetcher,
    embedding_client: LocalONNXEmbeddingClient,
    index_repository: IndexingRepository,
) -> IndexingJobDependencies:
    """Bundle the indexing domain's four Protocol-typed dependencies
    (indexing delivery plan's Phase 7) — `IndexingRepository` implements
    both `IndexRepository` and `IndexCommitTracker` (Assumption 13's
    split), so the same instance is passed for both roles here, the one
    place this composition root's own concrete class is named.
    """
    return IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=embedding_client,
        index_repository=index_repository,
        commit_tracker=index_repository,
    )


def _build_text_redactor(settings: WorkerSettings, logger: logging.Logger) -> TextRedactor:
    """Wires `findings.redact.TextRedactor` conditionally on
    `secret_redaction_enabled` alone — independent of
    `static_analysis_gitleaks_enabled`, per `findings/redact.py`'s own
    module docstring. `True` (the shipped default) wires the real,
    Gitleaks-backed implementation; `False` wires a no-op passthrough and
    logs a one-time `warning` at startup, not per-job — the reduced
    protection is visible, not silent.
    """
    if settings.secret_redaction_enabled:
        return GitleaksTextRedactor()

    logger.warning(
        "secret redaction is disabled",
        extra={"secret_redaction_enabled": False},
    )
    return NoOpTextRedactor()


def _build_step_enablement(settings: WorkerSettings) -> StepEnablement:
    """Translate `WorkerSettings`' own per-step fields into a
    `review.config.StepEnablement` instance — the one place `apps/worker`
    knows both `WorkerSettings`' and `StepEnablement`'s field names, per
    architecture.md's "wire concrete implementations only at the
    composition root" rule.
    """
    return StepEnablement(
        prefilter_enabled=settings.prefilter_enabled,
        static_analysis_enabled=settings.static_analysis_enabled,
        llm_review_enabled=settings.llm_review_enabled,
        output_enabled=settings.output_enabled,
        static_analysis_gitleaks_enabled=settings.static_analysis_gitleaks_enabled,
        static_analysis_semgrep_enabled=settings.static_analysis_semgrep_enabled,
    )


def _build_step_timeouts(settings: WorkerSettings) -> StepTimeouts:
    """The `StepTimeouts` counterpart to `_build_step_enablement` above."""
    return StepTimeouts(
        prefilter_step_timeout_seconds=settings.prefilter_step_timeout_seconds,
        static_analysis_step_timeout_seconds=settings.static_analysis_step_timeout_seconds,
        static_analysis_analyzer_timeout_seconds=settings.static_analysis_analyzer_timeout_seconds,
        llm_review_step_timeout_seconds=settings.llm_review_step_timeout_seconds,
        output_step_timeout_seconds=settings.output_step_timeout_seconds,
    )


def _check_step_configuration(
    settings: WorkerSettings, logger: logging.Logger
) -> tuple[StepEnablement, StepTimeouts]:
    """Run the pipeline step framework's startup validation check and
    raise `SystemExit(1)` if the enabled-step configuration can never
    produce a review — the same fail-fast pattern as `_check_llm_startup`
    above. Returns the `StepEnablement`/`StepTimeouts` it built and
    validated, so a later phase's dispatch loop (which constructs the real
    `review.pipeline.StepRegistry`) doesn't have to rebuild them.
    """
    enablement = _build_step_enablement(settings)
    timeouts = _build_step_timeouts(settings)
    try:
        check_step_configuration_viable(enablement)
    except NoViableStepConfigurationError as exc:
        logger.error(
            "step configuration cannot produce a review; exiting",
            extra={"error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info(
        "step configuration valid",
        extra={"enabled_steps": enablement.as_mapping(), "step_timeouts": timeouts.as_mapping()},
    )
    return enablement, timeouts


def _build_review_registry(
    enablement: StepEnablement,
    timeouts: StepTimeouts,
    diff_fetcher: GitHubDiffFetcher,
    redactor: TextRedactor,
    llm_client: LLMClient,
    publisher: GitHubFindingsPublisher,
    settings: WorkerSettings,
) -> StepRegistry:
    """Construct the macro-step registry `REVIEW` jobs dispatch to.

    Phase 4 registered the first real step, `PrefilterStep` — proving
    Phase 2's own Acceptance criterion (registering a real macro step
    requires zero changes to `review/pipeline.py`/`review/step.py`
    themselves, per US-F3). Phase 5 adds the second, `StaticAnalysisStep` —
    the same zero-framework-change property, now exercised by a second real
    step. Phase 6 adds the third, `LLMReviewStep`, reusing the same
    already-built `llm_client` this composition root already constructed
    for the startup connectivity check (`_build_llm_client`, Phase 3) and
    the same `redactor` instance `StaticAnalysisStep` already receives
    (Phase 5's `secret_redaction_enabled` flag gates both steps' redaction
    uniformly). Phase 7 (this phase) adds the fourth and final step,
    `FindingsOutputStep`, registered as this registry's own `terminal_step`
    (`review/pipeline.py`'s "Terminal step" docstring note) — a genuinely
    new capability the framework's own extension point provides, not
    something `review/pipeline.py`/`review/step.py` needed changing for the
    *registration* of, matching every prior macro step's own
    zero-framework-change property.

    `enablement.as_mapping()`/`timeouts.as_mapping()` are each built once
    here and passed unchanged into `StaticAnalysisStep`, which reads the
    same two mappings' own `"gitleaks"`/`"semgrep"` entries for its own
    inner registry — see `review.config.StepEnablement`'s own docstring for
    why this is one shared mapping, not two built separately.
    """
    enabled_mapping = enablement.as_mapping()
    timeouts_mapping = timeouts.as_mapping()
    return StepRegistry(
        steps=[
            PrefilterStep(diff_fetcher),
            StaticAnalysisStep(
                diff_fetcher,
                gitleaks_analyzer=GitleaksAnalyzer(),
                semgrep_analyzer=SemgrepAnalyzer(),
                redactor=redactor,
                enabled=enabled_mapping,
                timeouts=timeouts_mapping,
            ),
            LLMReviewStep(
                llm_client,
                redactor=redactor,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                max_diff_bytes=settings.llm_max_diff_bytes,
                # Mirrors FindingsOutputStep's own depends_on wiring below:
                # static_analysis produces no output at all when disabled
                # (or when both its analyzer sub-flags are), so llm_review
                # must fall back to reading prefilter's output directly in
                # that case instead of declaring an unsatisfiable dependency.
                # Uses the same shared predicate review.config's fail-fast
                # startup check validates against, so the two can't drift.
                depends_on=(
                    "static_analysis"
                    if static_analysis_can_produce_findings(enablement)
                    else "prefilter"
                ),
            ),
            FindingsOutputStep(
                publisher,
                # Deliberately checks the raw flag, not
                # static_analysis_can_produce_findings(enablement) (unlike
                # LLMReviewStep's depends_on above): when llm_review is
                # disabled, review.config.check_step_configuration_viable's
                # own fail-fast startup check already guarantees
                # static_analysis_can_produce_findings(enablement) is True
                # (it raises NoViableStepConfigurationError otherwise), so
                # falling back to "static_analysis" here is always safe
                # without re-deriving that guarantee — re-checking the
                # predicate here would be redundant, not more correct.
                depends_on="llm_review" if enablement.llm_review_enabled else "static_analysis",
            ),
        ],
        enabled=enabled_mapping,
        timeouts=timeouts_mapping,
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )


def _build_worker_id() -> str:
    """A per-process identifier passed to `JobQueueRepository.claim_next()`
    (persisted as `claimed_by`) — unique enough to distinguish concurrent
    Worker replicas/processes in logs and the `job_queue` table, with no
    coordination needed between them.
    """
    return f"worker-{uuid.uuid4()}"


async def _run_dispatch_loop_forever(
    job_queue: JobQueueRepository,
    registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    settings: WorkerSettings,
    worker_id: str,
    http_client: httpx.AsyncClient,
) -> None:
    """The one seam `main()` exposes for the dispatch loop itself — real in
    production, substituted with a no-op fake in
    `apps/worker/test_main.py`'s composition-root tests (which only prove
    `main()` reaches this point with the right arguments, not the loop's
    own behavior — that's `apps/worker/test_dispatch.py`'s job, against a
    real registry and a real Postgres), the same seam pattern
    `_build_llm_client` already establishes for the LLM client.

    `http_client` (the shared GitHub adapter client — see
    `_build_token_provider`'s docstring) is closed here, in the same
    coroutine/event-loop it's used from for the loop's entire lifetime,
    regardless of whether the loop returns normally or the process is
    interrupted.
    """
    try:
        await run_dispatch_loop(
            job_queue=job_queue,
            review_registry=registry,
            indexing_deps=indexing_deps,
            worker_id=worker_id,
            poll_interval_seconds=settings.poll_interval_seconds,
            stuck_job_warning_threshold_seconds=settings.stuck_job_warning_threshold_seconds,
            dry_run=settings.dry_run,
        )
    finally:
        await http_client.aclose()


async def _amain(settings: WorkerSettings, logger: logging.Logger, client: LLMClient) -> None:
    """The async body of `main()` below, run inside a single `asyncio.run()`
    call for the whole process lifetime.

    Everything here — the LLM startup check and the dispatch loop alike —
    must share one event loop: `client` (and, once built, `github_http_client`)
    carry async resources (e.g. LiteLLM's own cached async HTTP client/
    connection pool, keyed by call params rather than by event loop) that
    are bound to whichever loop first uses them. Splitting the startup check
    and the dispatch loop across two separate `asyncio.run()` calls, as an
    earlier version of this function did, closes the first loop out from
    under those resources before the dispatch loop (running in a second,
    new loop) ever gets to reuse them — surfacing as a
    "bound to a different event loop" error on the first real job, not at
    startup.
    """
    await _check_llm_startup(client, settings, logger)

    enablement, timeouts = _check_step_configuration(settings, logger)

    _check_github_private_key_startup(settings, logger)
    _log_github_retry_configuration(settings, logger)

    embedding_client = _build_embedding_client(settings)
    _check_embedding_model_startup(embedding_client, settings, logger)

    engine = create_storage_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)
    job_queue = JobQueueRepository(session_factory)
    # One shared, long-lived client for every GitHub adapter this
    # composition root wires — see `_build_token_provider`'s own docstring
    # for why (keep-alive/TLS session reuse across calls and jobs instead of
    # each adapter opening/closing its own client per request). Closed in
    # `_run_dispatch_loop_forever`, in the same event loop it's used from.
    github_http_client = httpx.AsyncClient(timeout=settings.github_api_timeout_seconds)
    token_provider = _build_token_provider(settings, github_http_client)
    diff_fetcher = _build_diff_fetcher(settings, token_provider, github_http_client)
    redactor = _build_text_redactor(settings, logger)
    publisher = _build_findings_publisher(settings, token_provider, github_http_client)
    registry = _build_review_registry(
        enablement, timeouts, diff_fetcher, redactor, client, publisher, settings
    )
    tree_fetcher = _build_tree_fetcher(settings, token_provider, github_http_client)
    index_repository = IndexingRepository(session_factory)
    indexing_deps = _build_indexing_deps(tree_fetcher, embedding_client, index_repository)
    worker_id = _build_worker_id()

    logger.info("worker started", extra={"worker_id": worker_id})
    await _run_dispatch_loop_forever(
        job_queue, registry, indexing_deps, settings, worker_id, github_http_client
    )


def main() -> None:
    """Process entrypoint: parse config, configure logging, verify the LLM
    endpoint is reachable and the enabled-step configuration can produce a
    review (exiting non-zero if either check fails), then run the
    claim/dispatch loop (`apps/worker/dispatch.py`) forever.

    Everything async runs inside one `asyncio.run()` call (`_amain` above) —
    see its own docstring for why splitting the startup check and the
    dispatch loop across two separate `asyncio.run()` calls is unsafe.
    """
    settings = WorkerSettings()
    configure_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info("worker starting", extra={"environment": settings.environment})

    client = _build_llm_client(settings)
    asyncio.run(_amain(settings, logger, client))


if __name__ == "__main__":
    main()
