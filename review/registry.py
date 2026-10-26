"""review.registry — the one shared builder of the macro-step registry REVIEW jobs run.

Extracted from ``apps/worker/main.py``'s ``_build_review_registry`` (A2 of
docs/plans/plan-local-mode.md, step 1a) so a second composition root can build the same
pipeline without importing the Worker. Behaviour-neutral: the steps, their order, their
``depends_on`` chain, enablement and timeouts are exactly what the Worker built before.

Domain package, zero infra imports (docs/rules/architecture.md): everything concrete (analyzer
registrations, the repo-config fetcher, fetchers, queries, clients, publishers) is injected by the
composition root. Values formerly read off ``WorkerSettings`` arrive as ``ReviewPipelineConfig``.

Every step is constructed unconditionally and gated purely through the ``enabled`` mapping the
``StepRegistry`` reads (``ContextBuilderStep``'s module docstring explains why); only the
walkthrough step is registered conditionally (Phase 12, US-W9). `resolve_review_pipeline_depends_on`
is the single computation of every dynamically-wired step's ``depends_on``, shared with
``check_step_configuration_viable`` so the two cannot drift. ``model_resolver`` is ``None`` when
model routing is off, restoring each LLM step's direct use of ``config.llm_model``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TypedDict

from findings.redact import TextRedactor
from indexing.embedding import EmbeddingClient
from indexing.tree import RepoTreeFetcher
from ingestion.diff import DiffFetcher
from llmgateway.client import LLMClient
from llmgateway.routing import FallbackModelRoutingResolver
from memory.store import DismissedFindingMatcher
from review.analyzer_rerun import AnalyzerRerunner
from review.clone_detection import CloneDetectionStep, CloneIndexQuery
from review.config import (
    ReviewPipelineConfig,
    StepEnablement,
    StepTimeouts,
    context_builder_can_produce_value,
    intent_summary_can_produce_value,
    resolve_review_pipeline_depends_on,
    walkthrough_can_produce_value,
)
from review.context_builder import ContextBuilderStep, GraphQuery, SimilarityQuery
from review.fix_generation import FixGenerationStep
from review.intent_summary import IntentSummaryStep
from review.memory_suppression import (
    FingerprintMatcher,
    FingerprintSuppressionConfig,
    MemorySuppressionConfig,
    SuppressionConfig,
)
from review.output import FindingsOutputStep, FindingsPublisher
from review.pipeline import FailurePolicy, StepRegistry
from review.prefilter import PrefilterStep
from review.product_knowledge import EffectiveProviderResolver, ProviderOpener
from review.review_generation import ReviewGenerationStep
from review.static_analysis import AnalyzerRegistration, RepoConfigFetcher, StaticAnalysisStep
from review.verification import VerificationStep
from review.walkthrough import WalkthroughStep
from review.walkthrough_render import WalkthroughPublisher

__all__ = ["build_review_registry"]


class _LLMStepKwargs(TypedDict):
    """The keyword arguments ``IntentSummaryStep``, ``ReviewGenerationStep`` and
    ``VerificationStep`` share for their LLM Gateway call, typed so mypy checks the
    ``**llm_step_kwargs`` splat against each constructor.
    """

    model: str
    timeout: float
    max_diff_bytes: int


def build_review_registry(
    *,
    enablement: StepEnablement,
    timeouts: StepTimeouts,
    config: ReviewPipelineConfig,
    diff_fetcher: DiffFetcher,
    tree_fetcher: RepoTreeFetcher,
    graph_query: GraphQuery,
    similarity_query: SimilarityQuery,
    clone_query: CloneIndexQuery,
    embedding_client: EmbeddingClient,
    redactor: TextRedactor,
    llm_client: LLMClient,
    publisher: FindingsPublisher,
    model_resolver: FallbackModelRoutingResolver | None,
    analyzer_registrations: Sequence[AnalyzerRegistration],
    config_fetcher: RepoConfigFetcher | None,
    memory_matcher: DismissedFindingMatcher | None = None,
    provider_resolver: EffectiveProviderResolver | None = None,
    provider_opener: ProviderOpener | None = None,
    walkthrough_publisher: WalkthroughPublisher | None = None,
    fingerprint_matcher: FingerprintMatcher | None = None,
) -> StepRegistry:
    """Construct the macro-step registry `REVIEW` jobs dispatch to."""
    enabled_mapping = enablement.as_mapping()
    timeouts_mapping = timeouts.as_mapping()

    # Override the raw flags with their `*_can_produce_value` predicates: each of these steps'
    # only consumer is `review_generation` (or the walkthrough's own preconditions), so running
    # one whose output nothing reads only pays its cost.
    enabled_mapping = {
        **enabled_mapping,
        "context_builder": context_builder_can_produce_value(enablement),
        "intent_summary": intent_summary_can_produce_value(enablement),
        "walkthrough": walkthrough_can_produce_value(enablement),
    }

    resolved = resolve_review_pipeline_depends_on(enablement)

    llm_step_kwargs: _LLMStepKwargs = {
        "model": config.llm_model,
        "timeout": config.llm_timeout_seconds,
        "max_diff_bytes": config.llm_max_diff_bytes,
    }

    # `None` when `memory_suppression_enabled` is False, restoring the pre-Memory construction
    # (US-M12).
    memory_suppression: SuppressionConfig | None = None
    if config.memory_suppression_enabled:
        # Local mode passes an exact-fingerprint matcher (its repository memory file); the
        # Worker never does, so it keeps the embedding-similarity construction.
        if fingerprint_matcher is not None:
            memory_suppression = FingerprintSuppressionConfig(
                fingerprint_matcher=fingerprint_matcher, enabled=True
            )
        elif memory_matcher is not None:
            memory_suppression = MemorySuppressionConfig(
                memory_matcher=memory_matcher,
                embedding_client=embedding_client,
                similarity_threshold=config.memory_similarity_threshold,
                decay_half_life_days=config.memory_decay_half_life_days,
                enabled=True,
            )
        else:
            raise ValueError("memory suppression needs a memory_matcher or a fingerprint_matcher")

    analyzer_slots = asyncio.Semaphore(config.static_analysis_max_concurrent_analyzers)
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
                model=config.llm_model,
                timeout=config.llm_timeout_seconds,
                # Fail open on timeout (US-W8): the step wraps its own work in a shorter
                # wait_for (settings validate the step timeout >= llm_timeout + 15).
                internal_timeout_seconds=float(config.walkthrough_step_timeout_seconds - 5),
                min_files=config.walkthrough_min_files,
                max_files=config.walkthrough_max_files,
                max_chars=config.walkthrough_max_chars,
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
                # One semaphore per process (US-A8), shared by every job because the
                # registry is built once at startup.
                analyzer_slots=analyzer_slots,
                config_fetcher=config_fetcher,
            ),
            CloneDetectionStep(
                diff_fetcher,
                clone_query,
                # A macro step's registry timeout is fatal but US-C14 requires a timeout to
                # fail open, so the step wraps its own work in a shorter wait_for (plan
                # Assumption 9; settings validate the step timeout exceeds 5 s).
                internal_timeout_seconds=float(config.clone_detection_step_timeout_seconds - 5),
                min_tokens=config.clone_detection_min_tokens,
                min_lines=config.clone_detection_min_lines,
                max_findings=config.clone_detection_max_findings,
                memory_suppression=memory_suppression,
                depends_on=resolved["clone_detection"],
            ),
            ContextBuilderStep(
                diff_fetcher,
                tree_fetcher,
                graph_query,
                similarity_query,
                embedding_client,
                internal_timeout_seconds=config.context_builder_internal_timeout_seconds,
                max_total_bytes=config.llm_max_diff_bytes,
                # When context_builder is disabled, `enabled["context_builder"]` skips it at
                # run time regardless; this only needs to name a registered step so
                # StepRegistry.__init__'s dependency validation accepts it.
                depends_on=resolved["context_builder"],
                # `None` wiring => no provider code path at all (byte-for-byte the prior step).
                provider_resolver=provider_resolver,
                provider_opener=provider_opener,
                provider_redactor=redactor,
                provider_deadline_seconds=float(config.product_knowledge_deadline_seconds),
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
                model=config.llm_model,
                timeout=config.llm_timeout_seconds,
                # Fail open on timeout (US-AF12): the step wraps its own work in a
                # shorter wait_for, as CloneDetectionStep does (settings validate > 5 s).
                internal_timeout_seconds=float(config.fix_generation_step_timeout_seconds - 5),
                max_fixes_per_job=config.fix_generation_max_fixes_per_job,
                max_range_lines=config.fix_generation_max_range_lines,
                max_replacement_lines=config.fix_generation_max_replacement_lines,
                window_lines=config.fix_generation_window_lines,
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
