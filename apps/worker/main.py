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

Phase 6 of the delivery plan adds the llm-review step (renamed
`review.review_generation.ReviewGenerationStep` by
docs/plans/plan-deliver-verification-pipeline.md's Phase 2 section — see
that module's own module docstring): the third macro step registered into
the same registry, binding the already-built `LLMClient` this composition
root already constructs for the startup connectivity check
(`_build_llm_client`, Phase 3) — no second client is built — and reusing
the exact same `TextRedactor` instance `StaticAnalysisStep` already
receives (Phase 5's `secret_redaction_enabled` flag gates both step's
redaction uniformly).

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

`docs/plans/plan-deliver-memory.md`'s Phase 4 section (Unit D, US-M3) adds
this composition root's third and final job-dispatch dependency bundle:
`adapter.github.reactions.GitHubReactionFetcher` (sharing the same
`token_provider`/`github_http_client` every other GitHub adapter here
already shares) and `memory.reaction_poll.MemoryReactionPollJobDependencies`
(reusing the same `memory_repository` instance Phase 2 already built as
both its `MemorySignalRecorder` and, this phase, its `ReactionDedupStore`
role — Assumption 8), threaded into `apps/worker/dispatch.py`'s
`run_dispatch_loop` alongside `indexing_deps`.

Settings are parsed and logging configured inside `main()`, not at module
import time, so importing this module (mypy, pytest collection) never
requires environment variables or performs process-level side effects.

docs/specs/spec-observability.md's Unit A adds this entrypoint's OTel
wiring: `adapter.telemetry.sdk.init_telemetry` is called in `main()`,
alongside `configure_logging` (both are process-wide observability
bootstrapping, not a fail-fast check with a real external dependency to
verify) — and, once `_amain` has built the dispatch loop's own
dependencies, US14's heartbeat loop runs concurrently with
`run_dispatch_loop` (both run forever), with the heartbeat task started
only when `settings.otel_exporter_otlp_endpoint` is set (US10's no-op
default). Run as a plain `asyncio.create_task`, cancelled in a `finally`
block around awaiting the dispatch loop — not `asyncio.TaskGroup` — so the
dispatch loop's own exception propagates out of `_amain` completely
unwrapped, exactly as it did before the heartbeat task existed, with no
`ExceptionGroup`/`BaseExceptionGroup` unwrapping step needed. This is safe
specifically because `run_heartbeat_loop` never lets anything but
`CancelledError` escape (see its own docstring) — the heartbeat task can
never itself be the one that fails, only be cancelled once the dispatch
loop already has, so there is no second failure mode a `TaskGroup` would
need to help aggregate.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

import dotenv
import httpx

from adapter.analyzers.eslint import ESLINT_CONFIG_FILENAMES, EslintAnalyzer
from adapter.analyzers.gitleaks import GitleaksAnalyzer, GitleaksTextRedactor
from adapter.analyzers.golangci_lint import GOLANGCI_CONFIG_FILENAMES, GolangciLintAnalyzer
from adapter.analyzers.phpstan import PHPSTAN_CONFIG_FILENAMES, PhpstanAnalyzer
from adapter.analyzers.ruff import RUFF_CONFIG_FILENAMES, RuffAnalyzer
from adapter.analyzers.semgrep import SemgrepAnalyzer
from adapter.analyzers.trivy import TRIVY_CONFIG_FILENAMES, TrivyAnalyzer
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.embedding.startup_check import check_embedding_model_loaded
from adapter.github.auth import GitHubInstallationTokenProvider, validate_private_key
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.exceptions import GitHubPrivateKeyInvalidError
from adapter.github.posting import GitHubFindingsPublisher
from adapter.github.reactions import GitHubReactionFetcher
from adapter.github.repo_config import GitHubRepoConfigFetcher
from adapter.github.tree import GitHubTreeFetcher
from adapter.github.walkthrough import GitHubWalkthroughPublisher
from adapter.llm.litellm_client import LiteLLMClient
from adapter.product_knowledge.client import ClientTimeouts
from adapter.product_knowledge.factory import ProviderClientFactory
from adapter.product_knowledge.provider_resolver import FailOpenProviderResolver
from adapter.product_knowledge.transport import EgressPolicy
from adapter.storage.clone_query import PostgresCloneIndexQuery
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.llm_routing import PostgresModelRoutingRepository
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.queue import JobQueueRepository
from adapter.storage.tenancy import PostgresSettingRepository, PostgresUsageCounterRepository
from adapter.telemetry.heartbeat import heartbeat_task_scope
from adapter.telemetry.sdk import (
    init_telemetry,
    resolve_otlp_headers,
    shutdown_telemetry,
)
from apps.worker.dispatch import run_dispatch_loop
from apps.worker.logging_config import configure_logging
from apps.worker.settings import WorkerSettings
from findings.redact import NoOpTextRedactor, TextRedactor
from indexing.embedding import EmbeddingError
from indexing.pipeline import IndexingJobDependencies
from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMError
from llmgateway.routing import FallbackModelRoutingResolver
from llmgateway.startup_check import check_llm_reachable
from memory.reaction_poll import MemoryReactionPollJobDependencies
from review.analyzer_rerun import AnalyzerRerunner
from review.clone_detection import CloneDetectionStep
from review.config import (
    NoViableStepConfigurationError,
    StepEnablement,
    StepTimeouts,
    check_step_configuration_viable,
    context_builder_can_produce_value,
    intent_summary_can_produce_value,
    resolve_review_pipeline_depends_on,
    walkthrough_can_produce_value,
)
from review.context_builder import ContextBuilderStep
from review.file_matchers import match_go, match_iac, match_javascript, match_php, match_python
from review.fix_generation import FixGenerationStep
from review.intent_summary import IntentSummaryStep
from review.memory_suppression import MemorySuppressionConfig
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, StepRegistry
from review.prefilter import PrefilterStep
from review.review_generation import ReviewGenerationStep
from review.static_analysis import (
    AnalyzerRegistration,
    StaticAnalysisStep,
    gitleaks_semgrep_registrations,
)
from review.verification import VerificationStep
from review.walkthrough import WalkthroughStep
from review.walkthrough_render import WalkthroughPublisher


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
    provenance_recorder: PostgresMemoryRepository,
) -> GitHubFindingsPublisher:
    """Wire the domain-defined `FindingsPublisher` Protocol to its
    GitHub-backed implementation (Phase 7), sharing `token_provider` with
    `_build_diff_fetcher` rather than constructing a second one — see
    `adapter/github/posting.py`'s own "no new auth code, no new config, of
    its own" docstring note. See `_build_token_provider`'s own docstring for
    why `http_client` is shared, not built per-publisher.

    `provenance_recorder` — `docs/plans/plan-deliver-memory.md`'s Phase 2
    section (US-M11) — is the same `PostgresMemoryRepository` instance
    `_amain` builds alongside `graph_query`/`similarity_query` (Assumption
    8: one concrete class, one `session_factory`, no second connection
    pool).
    """
    return GitHubFindingsPublisher(
        token_provider=token_provider,
        provenance_recorder=provenance_recorder,
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
        client=http_client,
    )


def _build_walkthrough_publisher(
    settings: WorkerSettings,
    token_provider: GitHubInstallationTokenProvider,
    http_client: httpx.AsyncClient,
) -> GitHubWalkthroughPublisher:
    """The Phase 12 walkthrough comment publisher; the App id is what identifies the
    platform's own comment (fail closed), so it comes straight from settings."""
    return GitHubWalkthroughPublisher(
        token_provider=token_provider,
        app_id=settings.github_app_id,
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


def _build_reaction_fetcher(
    settings: WorkerSettings,
    token_provider: GitHubInstallationTokenProvider,
    http_client: httpx.AsyncClient,
) -> GitHubReactionFetcher:
    """Wire the Memory domain's `ReactionFetcher` Protocol to its
    GitHub-backed implementation (`docs/plans/plan-deliver-memory.md`'s
    Phase 4 section, Unit D), sharing `token_provider`/`http_client` with
    every other GitHub adapter here — see `_build_token_provider`'s own
    docstring for why.
    """
    return GitHubReactionFetcher(
        token_provider=token_provider,
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
        client=http_client,
    )


def _build_memory_reaction_poll_deps(
    settings: WorkerSettings,
    reaction_fetcher: GitHubReactionFetcher,
    memory_repository: PostgresMemoryRepository,
) -> MemoryReactionPollJobDependencies:
    """Bundle `MEMORY_REACTION_POLL`'s three Protocol-typed dependencies
    (`docs/plans/plan-deliver-memory.md`'s Phase 4 section) plus its one
    plain-config field — `memory_repository` (Assumption 8) implements both
    `MemorySignalRecorder` and `ReactionDedupStore`, the same instance
    `_build_findings_publisher`'s own `provenance_recorder` already is, so
    the same instance is passed for both roles here, mirroring
    `_build_indexing_deps`'s own precedent.
    """
    return MemoryReactionPollJobDependencies(
        reaction_fetcher=reaction_fetcher,
        signal_recorder=memory_repository,
        dedup_store=memory_repository,
        max_comments_per_poll=settings.memory_reaction_poll_max_comments_per_poll,
    )


def _build_indexing_deps(
    tree_fetcher: GitHubTreeFetcher,
    embedding_client: LocalONNXEmbeddingClient,
    index_repository: IndexingRepository,
) -> IndexingJobDependencies:
    """Bundle the indexing domain's five Protocol-typed dependencies
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
        clone_backfill=index_repository,
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


def _log_verification_disabled_warning(settings: WorkerSettings, logger: logging.Logger) -> None:
    """A one-time `warning`-level startup log when `verification_enabled` is
    `False` (US-V9's second scenario,
    docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section) —
    mirrors `_build_text_redactor`'s own `secret_redaction_enabled=False`
    warning above exactly: the deployment can still boot and run correctly
    (disabling `verification` restores exactly Phase 1/2's original,
    already-shipped behavior), but the deviation from the shipped default —
    unverified LLM findings posted directly — must be visible, not silent.
    """
    if not settings.verification_enabled:
        logger.warning(
            "verification is disabled: unverified LLM findings will be posted directly",
            extra={"verification_enabled": False},
        )


def _log_memory_suppression_disabled_warning(
    settings: WorkerSettings, logger: logging.Logger
) -> None:
    """A one-time `warning`-level startup log when `memory_suppression_enabled`
    is `False` (US-M12, `docs/plans/plan-deliver-memory.md`'s Phase 6
    section) — mirrors `_log_verification_disabled_warning`'s own shape
    exactly: the deployment can still boot and run correctly (disabling
    suppression restores exactly this phase's own prior `VerificationStep`
    behavior, with zero interaction with capture (Phases 2-4) or the admin
    API (Phase 5), which keep running regardless), but the deviation from
    the shipped default — no similarity-based suppression, every candidate
    finding reaches the rubric call — must be visible, not silent.
    """
    if not settings.memory_suppression_enabled:
        logger.warning(
            "memory suppression is disabled: no similarity-based pre-check will run "
            "before the rubric call",
            extra={"memory_suppression_enabled": False},
        )


def _log_model_routing_disabled_warning(settings: WorkerSettings, logger: logging.Logger) -> None:
    """A one-time `warning`-level startup log when `model_routing_config_enabled`
    is `False` (docs/plans/plan-deliver-admin-gui.md's Phase 5 section, Unit
    F) — mirrors `_log_memory_suppression_disabled_warning`'s own shape
    exactly: the deployment can still boot and run correctly (disabling
    model routing restores exactly this phase's own prior LLM-calling step
    construction — every step uses `settings.llm_model` directly, zero
    `ModelRoutingConfig` reads), but the deviation from the shipped default
    — every org/stage gets the one static default model, regardless of any
    `ModelRoutingConfig` row an Admin may have written — must be visible,
    not silent.
    """
    if not settings.model_routing_config_enabled:
        logger.warning(
            "model routing is disabled: every stage uses the static default model, "
            "regardless of any ModelRoutingConfig row",
            extra={"model_routing_config_enabled": False},
        )


def _log_product_knowledge_disabled_warning(
    settings: WorkerSettings, logger: logging.Logger
) -> None:
    """
    A one-time `warning`-level startup log when `product_knowledge_enabled` is `False`.

    (plan-deliver-product-knowledge.md Phase 6, US-PK13) — mirrors
    `_log_memory_suppression_disabled_warning`: the kill switch is a deliberate deviation from the
    shipped default and must be visible. No provider client is constructed and no outbound call is
    made, even if provider settings exist.
    """
    if not settings.product_knowledge_enabled:
        logger.warning(
            "product knowledge is disabled: no external provider will be queried "
            "even if provider settings exist",
            extra={"product_knowledge_enabled": False},
        )


def _log_product_knowledge_context_builder_disabled_warning(
    settings: WorkerSettings, enablement: StepEnablement, logger: logging.Logger
) -> None:
    """
    A one-time `warning`-level startup log when configured credentials cannot be used.

    This fires when provider credentials are configured but the Context Builder cannot run (the
    provider is queried only from inside that step), so the operator is not left wondering why a
    configured provider is never called (plan Phase 7).
    """
    if (
        settings.product_knowledge_enabled
        and settings.credential_map().refs
        and not context_builder_can_produce_value(enablement)
    ):
        logger.warning(
            "product knowledge credentials are configured but the context_builder step "
            "is disabled (or cannot produce a value): no provider will be queried",
            extra={"context_builder_enabled": enablement.context_builder_enabled},
        )


@dataclass(frozen=True)
class ProductKnowledgeWiring:
    """
    What the Context Builder's provider branch is handed.

    The fail-open effective-setting resolver and the per-endpoint client factory (a
    `review.product_knowledge.ProviderOpener`). `opener` is `None` when the kill switch is off: the
    resolver is still wired so a *configured* provider is reported as `outcome=disabled` (a visible
    rollback signal, not silence), but no client can ever be built.
    """

    opener: ProviderClientFactory | None
    resolver: FailOpenProviderResolver


def _build_product_knowledge_wiring(
    settings: WorkerSettings,
    setting_repository: PostgresSettingRepository,
    logger: logging.Logger,
) -> ProductKnowledgeWiring:
    """
    With `product_knowledge_enabled=False` no client factory is built.

    So no outbound call is possible even when settings exist; the resolver is still wired for the
    `disabled` outcome. The credential map was already validated by `WorkerSettings` (fail-fast), so
    parsing here cannot raise.
    """
    resolver = FailOpenProviderResolver(setting_repository)
    if not settings.product_knowledge_enabled:
        return ProductKnowledgeWiring(opener=None, resolver=resolver)
    credentials = settings.credential_map()
    policy = EgressPolicy(
        allow_private_networks=settings.product_knowledge_allow_private_networks,
        endpoint_allowlist=settings.endpoint_allowlist(),
    )
    logger.info(
        "product knowledge wired",
        extra={
            "credential_count": len(credentials.refs),
            "allow_private_networks": policy.allow_private_networks,
            "endpoint_allowlist_size": len(policy.endpoint_allowlist),
        },
    )
    return ProductKnowledgeWiring(
        opener=ProviderClientFactory(
            credentials=credentials,
            policy=policy,
            timeouts=ClientTimeouts(
                call_seconds=float(settings.product_knowledge_call_timeout_seconds)
            ),
        ),
        resolver=resolver,
    )


_DEPRECATED_SETTINGS_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("REVIEW_WORKER_LLM_REVIEW_ENABLED", "REVIEW_WORKER_REVIEW_GENERATION_ENABLED"),
    (
        "REVIEW_WORKER_LLM_REVIEW_STEP_TIMEOUT_SECONDS",
        "REVIEW_WORKER_REVIEW_GENERATION_STEP_TIMEOUT_SECONDS",
    ),
)
"""The (old name, new name) pairs from the deliberate, documented breaking
bootstrap-config rename (Assumption 7 of
docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section) —
`StepEnablement.llm_review_enabled`/`StepTimeouts.llm_review_step_timeout_seconds`
became `review_generation_enabled`/`review_generation_step_timeout_seconds`.
"""


def _check_deprecated_settings_env_vars(
    logger: logging.Logger,
    *,
    env_file: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = (
        WorkerSettings.model_config["env_file"]
    ),
) -> None:
    """A one-time startup check for the deprecated `llm_review_*` env var
    names (Assumption 7/8 of docs/plans/plan-deliver-verification-pipeline.md's
    Phase 5 section, revised after a second review round found the first
    version incomplete) — `WorkerSettings`' `extra="ignore"` config
    (`apps/worker/settings.py`) means an operator's own, externally-owned
    deployment config setting one of these old names would silently stop
    taking effect after the rename, falling back to the new field's default
    with zero error or log line.

    `env_file`'s default is **read from `WorkerSettings.model_config`
    itself**, not independently hard-coded to `".env"` a second time — the
    same "two definitions could drift" risk Assumption 6 already names
    elsewhere for `resolve_review_pipeline_depends_on`: if `WorkerSettings`'
    own `env_file` location ever changes, this check must never silently
    stop reading the file the real settings loader actually uses.

    Builds one case-insensitive set of key **names** (membership only, never
    value truthiness — a deprecated var present but set to an empty string
    must still trigger the warning) from both `os.environ` and, if the file
    exists, `dotenv.dotenv_values(env_file)` (the same loader
    pydantic-settings itself uses internally via its dependency on
    `python-dotenv` — no new dependency) — `os.environ`-only would miss a
    deprecated key set only in a `.env` file, since pydantic-settings' own
    dotenv source never populates `os.environ` with the file's contents.
    Case-insensitive because pydantic-settings' own env source is
    case-insensitive by default, so a differently-cased deprecated key would
    otherwise still silently take effect in settings parsing while bypassing
    an exact-case-only check here.

    Each deprecated name present logs its **own** `warning` naming that
    specific old/new pair — two independent checks, not one combined
    message regardless of which is actually set — a `warning`, not a
    fail-fast `SystemExit(1)`, because the deployment can still boot and run
    correctly on the new field's default; what's lost is only the
    operator's prior, now-silently-overridden intent.
    """
    present_names: set[str] = {name.upper() for name in os.environ}

    env_files: tuple[str | os.PathLike[str], ...]
    if env_file is None:
        env_files = ()
    elif isinstance(env_file, (str, os.PathLike)):
        env_files = (env_file,)
    else:
        env_files = tuple(env_file)

    for candidate in env_files:
        if os.path.exists(candidate):
            present_names |= {name.upper() for name in dotenv.dotenv_values(candidate)}

    for old_name, new_name in _DEPRECATED_SETTINGS_ENV_VARS:
        if old_name.upper() in present_names:
            logger.warning(
                "deprecated setting name is still set and no longer has any effect",
                extra={"deprecated_name": old_name, "current_name": new_name},
            )


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
        clone_detection_enabled=settings.clone_detection_enabled,
        fix_generation_enabled=settings.fix_generation_enabled,
        walkthrough_enabled=settings.walkthrough_enabled,
        secret_redaction_enabled=settings.secret_redaction_enabled,
        context_builder_enabled=settings.context_builder_enabled,
        intent_summary_enabled=settings.intent_summary_enabled,
        review_generation_enabled=settings.review_generation_enabled,
        verification_enabled=settings.verification_enabled,
        output_enabled=settings.output_enabled,
        static_analysis_gitleaks_enabled=settings.static_analysis_gitleaks_enabled,
        static_analysis_semgrep_enabled=settings.static_analysis_semgrep_enabled,
        static_analysis_ruff_enabled=settings.static_analysis_ruff_enabled,
        static_analysis_eslint_enabled=settings.static_analysis_eslint_enabled,
        static_analysis_trivy_enabled=settings.static_analysis_trivy_enabled,
        static_analysis_phpstan_enabled=settings.static_analysis_phpstan_enabled,
        static_analysis_golangci_lint_enabled=settings.static_analysis_golangci_lint_enabled,
    )


def _build_step_timeouts(settings: WorkerSettings) -> StepTimeouts:
    """The `StepTimeouts` counterpart to `_build_step_enablement` above."""
    return StepTimeouts(
        prefilter_step_timeout_seconds=settings.prefilter_step_timeout_seconds,
        static_analysis_step_timeout_seconds=settings.static_analysis_step_timeout_seconds,
        static_analysis_analyzer_timeout_seconds=settings.static_analysis_analyzer_timeout_seconds,
        clone_detection_step_timeout_seconds=settings.clone_detection_step_timeout_seconds,
        fix_generation_step_timeout_seconds=settings.fix_generation_step_timeout_seconds,
        walkthrough_step_timeout_seconds=settings.walkthrough_step_timeout_seconds,
        static_analysis_ruff_timeout_seconds=settings.static_analysis_ruff_timeout_seconds,
        static_analysis_eslint_timeout_seconds=settings.static_analysis_eslint_timeout_seconds,
        static_analysis_trivy_timeout_seconds=settings.static_analysis_trivy_timeout_seconds,
        static_analysis_phpstan_timeout_seconds=settings.static_analysis_phpstan_timeout_seconds,
        static_analysis_golangci_lint_timeout_seconds=(
            settings.static_analysis_golangci_lint_timeout_seconds
        ),
        context_builder_step_timeout_seconds=settings.context_builder_step_timeout_seconds,
        intent_summary_step_timeout_seconds=settings.intent_summary_step_timeout_seconds,
        review_generation_step_timeout_seconds=settings.review_generation_step_timeout_seconds,
        verification_step_timeout_seconds=settings.verification_step_timeout_seconds,
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

    if settings.walkthrough_enabled and not walkthrough_can_produce_value(enablement):
        logger.warning(
            "walkthrough_enabled is true but the walkthrough step is ineffective and will not "
            "run: it requires review_generation_enabled and secret_redaction_enabled"
        )

    logger.info(
        "step configuration valid",
        extra={
            "enabled_steps": {
                **enablement.as_mapping(),
                "walkthrough": walkthrough_can_produce_value(enablement),
            },
            "step_timeouts": timeouts.as_mapping(),
            # Phase 5 of docs/plans/plan-deliver-verification-pipeline.md —
            # so an operator can confirm from Worker startup logs alone
            # which of the five dynamically-wired steps ended up pointing
            # at which upstream, without needing to inspect a job's own
            # step-level logs.
            "review_pipeline_depends_on": resolve_review_pipeline_depends_on(enablement),
        },
    )
    return enablement, timeouts


class _LLMStepKwargs(TypedDict):
    """The shared keyword-argument shape ``IntentSummaryStep``,
    ``ReviewGenerationStep``, and ``VerificationStep`` all accept for their
    LLM Gateway call (``model``/``timeout``/``max_diff_bytes``) — typed
    explicitly so mypy can check the ``**llm_step_kwargs`` splat below
    against each constructor's actual parameter types, instead of widening
    to ``dict[str, object]``.
    """

    model: str
    timeout: float
    max_diff_bytes: int


def _build_review_registry(
    enablement: StepEnablement,
    timeouts: StepTimeouts,
    diff_fetcher: GitHubDiffFetcher,
    tree_fetcher: GitHubTreeFetcher,
    graph_query: PostgresGraphQuery,
    similarity_query: PostgresSimilarityQuery,
    clone_query: PostgresCloneIndexQuery,
    embedding_client: LocalONNXEmbeddingClient,
    redactor: TextRedactor,
    llm_client: LLMClient,
    publisher: GitHubFindingsPublisher,
    settings: WorkerSettings,
    memory_matcher: PostgresMemoryRepository,
    model_resolver: FallbackModelRoutingResolver | None,
    product_knowledge: ProductKnowledgeWiring | None = None,
    walkthrough_publisher: WalkthroughPublisher | None = None,
) -> StepRegistry:
    """Construct the macro-step registry `REVIEW` jobs dispatch to.

    Phase 4 registered the first real step, `PrefilterStep` — proving
    Phase 2's own Acceptance criterion (registering a real macro step
    requires zero changes to `review/pipeline.py`/`review/step.py`
    themselves, per US-F3). Phase 5 adds the second, `StaticAnalysisStep` —
    the same zero-framework-change property, now exercised by a second real
    step. Phase 6 adds the third (renamed `ReviewGenerationStep` by
    docs/plans/plan-deliver-verification-pipeline.md's Phase 2 section),
    reusing the same already-built `llm_client` this composition root
    already constructed for the startup connectivity check
    (`_build_llm_client`, Phase 3) and the same `redactor` instance
    `StaticAnalysisStep` already receives (Phase 5's
    `secret_redaction_enabled` flag gates both steps' redaction uniformly).
    Phase 7 adds the fourth and final step, `FindingsOutputStep`, registered
    as this registry's own `terminal_step` (`review/pipeline.py`'s
    "Terminal step" docstring note) — a genuinely new capability the
    framework's own extension point provides, not something
    `review/pipeline.py`/`review/step.py` needed changing for the
    *registration* of, matching every prior macro step's own
    zero-framework-change property.

    The Context Builder delivery plan's Phase 5 section adds a fifth step,
    `ContextBuilderStep`, inserted between `static_analysis`/`prefilter` and
    `review_generation` — reusing the same `diff_fetcher` every other step
    here already shares, plus `tree_fetcher`/`graph_query`/
    `similarity_query`/`embedding_client` (the same `EmbeddingClient`
    instance `_build_indexing_deps` already constructs — no second one).
    **`ContextBuilderStep` is unconditionally constructed and placed in
    `steps=[...]` below, exactly like every other step, regardless of
    `context_builder_enabled`** — enablement is controlled purely via the
    `enabled` mapping `StepRegistry` itself reads, never via a step's
    presence or absence in this list (see that class's own module
    docstring's "ContextBuilderStep is unconditionally constructed" note
    for the full reasoning that phase's plan section gives).

    The verification pipeline delivery plan's Phase 5 section (this phase)
    adds the sixth and seventh steps, `IntentSummaryStep` (between
    `context_builder` and `review_generation`) and `VerificationStep`
    (between `review_generation` and `output`) — the same
    unconditionally-constructed, `enabled`-mapping-gated pattern
    `ContextBuilderStep` already established, extended from two
    dynamically-wired steps to five. `review.config.resolve_review_pipeline_depends_on`
    (Assumption 6 of that plan) is the one shared computation for every one
    of the five steps' own `depends_on` value — this function no longer
    hand-derives that chain itself, so it can't drift from
    `review.config.check_step_configuration_viable`'s own use of the same
    function to validate the configuration at startup.

    `enablement.as_mapping()`/`timeouts.as_mapping()` are each built once
    here and passed unchanged into `StaticAnalysisStep`, which reads the
    same two mappings' own `"gitleaks"`/`"semgrep"` entries for its own
    inner registry — see `review.config.StepEnablement`'s own docstring for
    why this is one shared mapping, not two built separately.

    `docs/plans/plan-deliver-memory.md`'s Phase 6 section (Unit F,
    Assumption 6) adds `memory_matcher` — `VerificationStep` gets a new
    `memory_suppression=MemorySuppressionConfig(...)` keyword argument built
    from it plus the already-built `embedding_client` and three plain
    `WorkerSettings` fields (`memory_suppression_enabled`/
    `memory_similarity_threshold`/`memory_decay_half_life_days`) — never
    `None` when `memory_suppression_enabled` is `True` (else `None`
    entirely, restoring byte-for-byte the prior `VerificationStep`
    construction, per US-M12). Unlike the five dynamically-wired steps
    above, this isn't gated through `enabled_mapping`/`resolved` — it's an
    internal behavior toggle inside the already-registered `verification`
    step, not a separate pipeline stage of its own.

    `docs/plans/plan-deliver-admin-gui.md`'s Phase 5 section (Unit F,
    Assumption 3) adds `model_resolver` — passed as a new
    `model_resolver=` keyword argument to all three LLM-calling steps
    (`IntentSummaryStep`/`ReviewGenerationStep`/`VerificationStep`),
    alongside their existing `llm_step_kwargs` (which still includes
    `model`, now each step's own fallback/default when `model_resolver` is
    `None`). `None` when `model_routing_config_enabled` is `False`,
    restoring byte-for-byte each step's pre-Phase-5 construction (every
    call uses `settings.llm_model` directly). When set, each step resolves
    its own model at dispatch time inside its own `run()` — this function
    never overwrites an already-built step's `model` field from the
    outside, per architecture.md's "no stage reaching into another stage's
    internals" rule.
    """
    enabled_mapping = enablement.as_mapping()
    timeouts_mapping = timeouts.as_mapping()

    # Override the raw `context_builder_enabled`/`intent_summary_enabled`
    # flags with their own shared `*_can_produce_value` predicates: each
    # step's only consumer is `review_generation`, so if that's disabled,
    # running either step at all only pays its own real cost (diff/tree/
    # graph/embedding fetch for context_builder; a full LLM call for
    # intent_summary) for a field nothing will read.
    enabled_mapping = {
        **enabled_mapping,
        "context_builder": context_builder_can_produce_value(enablement),
        "intent_summary": intent_summary_can_produce_value(enablement),
        "walkthrough": walkthrough_can_produce_value(enablement),
    }

    # The one shared chain-resolution function (Assumption 6 of
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section) —
    # also used by `review.config.check_step_configuration_viable`'s own
    # additive check, so the two can't drift.
    resolved = resolve_review_pipeline_depends_on(enablement)

    # The three LLM-calling review-generation-stage steps (intent_summary,
    # review_generation, verification) all read the same three fields off
    # `settings` for their LLM Gateway call — per the "no per-stage model
    # routing yet" scope decision (docs/specs/spec-verification-pipeline.md).
    # Built once here rather than repeated verbatim at each constructor call
    # below, so a future per-stage override only needs one call site changed
    # per step, not the shared-args tuple re-typed from scratch each time.
    llm_step_kwargs: _LLMStepKwargs = {
        "model": settings.llm_model,
        "timeout": settings.llm_timeout_seconds,
        "max_diff_bytes": settings.llm_max_diff_bytes,
    }

    # docs/plans/plan-deliver-memory.md's Phase 6 section (Unit F,
    # Assumption 6) — `None` when `memory_suppression_enabled` is `False`,
    # restoring exactly this step's pre-Phase-6 construction (US-M12).
    memory_suppression = (
        MemorySuppressionConfig(
            memory_matcher=memory_matcher,
            embedding_client=embedding_client,
            similarity_threshold=settings.memory_similarity_threshold,
            decay_half_life_days=settings.memory_decay_half_life_days,
            enabled=True,
        )
        if settings.memory_suppression_enabled
        else None
    )

    analyzer_registrations = [
        *gitleaks_semgrep_registrations(GitleaksAnalyzer(), SemgrepAnalyzer()),
        AnalyzerRegistration(
            "ruff",
            RuffAnalyzer(),
            matches=match_python,
            changed_lines_only=True,
            config_filenames=RUFF_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "eslint",
            EslintAnalyzer(),
            matches=match_javascript,
            changed_lines_only=True,
            config_filenames=ESLINT_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "phpstan",
            PhpstanAnalyzer(),
            matches=match_php,
            changed_lines_only=True,
            config_filenames=PHPSTAN_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "golangci_lint",
            GolangciLintAnalyzer(),
            matches=match_go,
            changed_lines_only=True,
            config_filenames=GOLANGCI_CONFIG_FILENAMES,
        ),
        # Not changed_lines_only: Trivy maps a resource's line range
        # to added lines itself (adapter/analyzers/trivy.py, US-A3).
        AnalyzerRegistration(
            "trivy",
            TrivyAnalyzer(),
            matches=match_iac,
            security_class=True,
            config_filenames=TRIVY_CONFIG_FILENAMES,
        ),
    ]
    analyzer_slots = asyncio.Semaphore(settings.static_analysis_max_concurrent_analyzers)
    config_fetcher = GitHubRepoConfigFetcher(tree_fetcher)
    analyzer_rerunner = AnalyzerRerunner(
        analyzer_registrations, analyzer_slots, timeouts_mapping, config_fetcher
    )

    # Phase 12: registered only when effective, so with the flag off the registry is
    # exactly the pre-Phase-12 one (US-W9). `walkthrough_publisher` is required then.
    walkthrough_steps: list[WalkthroughStep] = []
    if enabled_mapping["walkthrough"]:
        if walkthrough_publisher is None:
            raise ValueError("walkthrough is effective but no walkthrough_publisher was given")
        walkthrough_steps.append(
            WalkthroughStep(
                llm_client,
                redactor,
                walkthrough_publisher,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                # Fail open on timeout (US-W8): the step wraps its own work in a shorter
                # wait_for (settings validate the step timeout >= llm_timeout + 15).
                internal_timeout_seconds=float(settings.walkthrough_step_timeout_seconds - 5),
                min_files=settings.walkthrough_min_files,
                max_files=settings.walkthrough_max_files,
                max_chars=settings.walkthrough_max_chars,
                model_resolver=model_resolver,
                depends_on=resolved["walkthrough"],
            )
        )

    return StepRegistry(
        steps=[
            PrefilterStep(diff_fetcher),
            StaticAnalysisStep(
                diff_fetcher,
                registrations=analyzer_registrations,
                redactor=redactor,
                enabled=enabled_mapping,
                timeouts=timeouts_mapping,
                # One semaphore per Worker process (US-A8), shared by every job
                # because the registry is built once at startup.
                analyzer_slots=analyzer_slots,
                config_fetcher=config_fetcher,
            ),
            CloneDetectionStep(
                diff_fetcher,
                clone_query,
                # A macro step's registry timeout is fatal but US-C14 requires a timeout to
                # fail open, so the step wraps its own work in a shorter wait_for (plan
                # Assumption 9; settings validate the step timeout exceeds 5 s).
                internal_timeout_seconds=float(settings.clone_detection_step_timeout_seconds - 5),
                min_tokens=settings.clone_detection_min_tokens,
                min_lines=settings.clone_detection_min_lines,
                max_findings=settings.clone_detection_max_findings,
                memory_suppression=memory_suppression,
                depends_on=resolved["clone_detection"],
            ),
            ContextBuilderStep(
                diff_fetcher,
                tree_fetcher,
                graph_query,
                similarity_query,
                embedding_client,
                internal_timeout_seconds=settings.context_builder_internal_timeout_seconds,
                max_total_bytes=settings.llm_max_diff_bytes,
                # When context_builder_enabled is False, `enabled["context_builder"]`
                # skips it at run time regardless of this value — this only
                # needs to name *some* valid, registered step so
                # StepRegistry.__init__'s dependency validation doesn't
                # reject it as pointing at an absent step.
                depends_on=resolved["context_builder"],
                # Plan Phase 7: `None` wiring => no provider code path at all
                # (byte-for-byte the prior step). The redactor is the same
                # instance the other steps use; a NoOp one disables the
                # branch (A5).
                provider_resolver=product_knowledge.resolver if product_knowledge else None,
                provider_opener=product_knowledge.opener if product_knowledge else None,
                provider_redactor=redactor,
                provider_deadline_seconds=float(settings.product_knowledge_deadline_seconds),
            ),
            IntentSummaryStep(
                llm_client,
                **llm_step_kwargs,
                model_resolver=model_resolver,
                depends_on=resolved["intent_summary"],
            ),
            ReviewGenerationStep(
                llm_client,
                redactor=redactor,
                **llm_step_kwargs,
                model_resolver=model_resolver,
                depends_on=resolved["review_generation"],
            ),
            VerificationStep(
                llm_client,
                **llm_step_kwargs,
                model_resolver=model_resolver,
                depends_on=resolved["verification"],
                memory_suppression=memory_suppression,
            ),
            FixGenerationStep(
                diff_fetcher,
                llm_client,
                analyzer_rerunner,
                redactor,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                # Fail open on timeout (US-AF12): the step wraps its own work in a
                # shorter wait_for, as CloneDetectionStep does (settings validate > 5 s).
                internal_timeout_seconds=float(settings.fix_generation_step_timeout_seconds - 5),
                max_fixes_per_job=settings.fix_generation_max_fixes_per_job,
                max_range_lines=settings.fix_generation_max_range_lines,
                max_replacement_lines=settings.fix_generation_max_replacement_lines,
                window_lines=settings.fix_generation_window_lines,
                verification_enabled=enablement.verification_enabled,
                model_resolver=model_resolver,
                depends_on=resolved["fix_generation"],
            ),
            *walkthrough_steps,
            FindingsOutputStep(
                publisher,
                depends_on=resolved["output"],
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
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    settings: WorkerSettings,
    worker_id: str,
    http_client: httpx.AsyncClient,
    usage_counter_recorder: PostgresUsageCounterRepository,
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

    `usage_counter_recorder` (docs/plans/plan-deliver-admin-gui.md's Phase
    7 section, Unit H, US-A19) — always a real
    `PostgresUsageCounterRepository` in production; no kill switch, per
    that phase's own Observability & rollback section ("this side effect
    fails soft... a metering bug can never turn into a review-pipeline
    outage" on its own, so no separate off switch is needed the way
    `model_routing_config_enabled` needs one).
    """
    try:
        await run_dispatch_loop(
            job_queue=job_queue,
            review_registry=registry,
            indexing_deps=indexing_deps,
            memory_reaction_poll_deps=memory_reaction_poll_deps,
            worker_id=worker_id,
            poll_interval_seconds=settings.poll_interval_seconds,
            stuck_job_warning_threshold_seconds=settings.stuck_job_warning_threshold_seconds,
            dry_run=settings.dry_run,
            usage_counter_recorder=usage_counter_recorder,
        )
    finally:
        await http_client.aclose()


async def _amain(
    settings: WorkerSettings, logger: logging.Logger, client: LLMClient, *, telemetry_enabled: bool
) -> None:
    """The async body of `main()` below, run inside a single `asyncio.run()`
    call for the whole process lifetime.

    A thin try/finally around `_run` below: flushes/shuts down the OTel
    providers before the process exits — see `shutdown_telemetry`'s own
    docstring for why this is needed on top of `init_telemetry`'s default
    batching/export intervals — the same "wrap the real body in a thin
    finally, don't re-indent it" shape `apps/webhook_api/main.py`'s
    `_build_lifespan` uses for the equivalent shutdown hook. Wraps the
    startup checks too (not just the dispatch loop/heartbeat task) so a
    fail-fast `SystemExit` from any of them (e.g. a misconfigured GitHub
    App key) still flushes whatever telemetry was already buffered, instead
    of dropping it silently.
    """
    try:
        await _run(settings, logger, client, telemetry_enabled=telemetry_enabled)
    finally:
        if telemetry_enabled:
            await shutdown_telemetry()


async def _run(
    settings: WorkerSettings, logger: logging.Logger, client: LLMClient, *, telemetry_enabled: bool
) -> None:
    """`_amain`'s real body: startup checks, adapter/registry construction,
    then the dispatch loop — everything here must share one event loop.
    `client` (and, once built, `github_http_client`) carry async resources
    (e.g. LiteLLM's own cached async HTTP client/connection pool, keyed by
    call params rather than by event loop) that are bound to whichever loop
    first uses them. Splitting the startup check and the dispatch loop
    across two separate `asyncio.run()` calls, as an earlier version of
    this function did, closes the first loop out from under those resources
    before the dispatch loop (running in a second, new loop) ever gets to
    reuse them — surfacing as a "bound to a different event loop" error on
    the first real job, not at startup. `_amain` is the only caller.
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
    tree_fetcher = _build_tree_fetcher(settings, token_provider, github_http_client)
    redactor = _build_text_redactor(settings, logger)
    _log_verification_disabled_warning(settings, logger)
    _log_memory_suppression_disabled_warning(settings, logger)
    _log_model_routing_disabled_warning(settings, logger)
    _log_product_knowledge_disabled_warning(settings, logger)
    _log_product_knowledge_context_builder_disabled_warning(settings, enablement, logger)
    # plan-deliver-product-knowledge.md Phases 6-7: consumed by the Context
    # Builder's provider branch (passed to `_build_review_registry` below).
    product_knowledge = _build_product_knowledge_wiring(
        settings, PostgresSettingRepository(session_factory), logger
    )
    # docs/plans/plan-deliver-admin-gui.md's Phase 5 section (Unit F,
    # Assumption 3) — `None` when `model_routing_config_enabled` is
    # `False`, restoring exactly the pre-Phase-5 construction (every
    # LLM-calling step uses `settings.llm_model` directly, zero
    # `ModelRoutingConfig` reads). The same `FallbackModelRoutingResolver`
    # *class* `apps/admin_api/main.py`'s own composition root also wires
    # (a separate instance here, sharing the class, never a second
    # independently-derived fallback chain) — see that module's own
    # docstring for why this one implementation is shared by name across
    # both entrypoints.
    model_routing_repository = PostgresModelRoutingRepository(session_factory)
    model_routing_resolver = (
        FallbackModelRoutingResolver(
            model_routing_repository, static_default_model=settings.llm_model
        )
        if settings.model_routing_config_enabled
        else None
    )
    # The same database session factory IndexRepository already uses (see
    # `_build_indexing_deps` below) — no second connection pool. Built here,
    # ahead of `publisher`, so `_build_findings_publisher` (Phase 2, US-M11)
    # can pass it through as `provenance_recorder`.
    memory_repository = PostgresMemoryRepository(session_factory)
    publisher = _build_findings_publisher(
        settings, token_provider, github_http_client, memory_repository
    )
    graph_query = PostgresGraphQuery(session_factory)
    similarity_query = PostgresSimilarityQuery(session_factory)
    clone_query = PostgresCloneIndexQuery(session_factory)
    registry = _build_review_registry(
        enablement,
        timeouts,
        diff_fetcher,
        tree_fetcher,
        graph_query,
        similarity_query,
        clone_query,
        embedding_client,
        redactor,
        client,
        publisher,
        settings,
        memory_repository,
        model_routing_resolver,
        product_knowledge,
        _build_walkthrough_publisher(settings, token_provider, github_http_client),
    )
    index_repository = IndexingRepository(session_factory)
    indexing_deps = _build_indexing_deps(tree_fetcher, embedding_client, index_repository)
    # docs/plans/plan-deliver-memory.md's Phase 4 section (Unit D) — reuses
    # `token_provider`/`github_http_client`/`memory_repository`, no second
    # token cache, no second connection pool.
    reaction_fetcher = _build_reaction_fetcher(settings, token_provider, github_http_client)
    memory_reaction_poll_deps = _build_memory_reaction_poll_deps(
        settings, reaction_fetcher, memory_repository
    )
    worker_id = _build_worker_id()
    # docs/plans/plan-deliver-admin-gui.md's Phase 7 section (Unit H,
    # US-A19) — the same `session_factory` every other storage adapter
    # here already shares, no second connection pool. Always wired in, no
    # kill switch (see `_run_dispatch_loop_forever`'s own docstring).
    usage_counter_recorder = PostgresUsageCounterRepository(session_factory)

    logger.info("worker started", extra={"worker_id": worker_id})
    dispatch_loop_forever = _run_dispatch_loop_forever(
        job_queue,
        registry,
        indexing_deps,
        memory_reaction_poll_deps,
        settings,
        worker_id,
        github_http_client,
        usage_counter_recorder,
    )
    # The heartbeat task is started only when telemetry is configured,
    # per US10 — see `heartbeat_task_scope`'s own docstring for the
    # create/cancel sequencing shared with `apps/webhook_api/main.py`.
    async with heartbeat_task_scope(telemetry_enabled):
        await dispatch_loop_forever


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
    # docs/specs/spec-observability.md's Unit A: process-wide observability
    # bootstrapping, alongside `configure_logging` above — a true no-op
    # (US10) when `settings.otel_exporter_otlp_endpoint` is unset.
    telemetry_enabled = init_telemetry(
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        service_name="review-worker",
        otlp_headers=resolve_otlp_headers(settings.otel_exporter_otlp_headers),
    )

    logger = logging.getLogger(__name__)
    _check_deprecated_settings_env_vars(logger)

    logger.info("worker starting", extra={"environment": settings.environment})

    client = _build_llm_client(settings)
    asyncio.run(_amain(settings, logger, client, telemetry_enabled=telemetry_enabled))


if __name__ == "__main__":
    main()
