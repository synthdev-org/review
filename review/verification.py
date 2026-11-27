"""review.verification — the ``verification`` pipeline step, per
docs/specs/spec-verification-pipeline.md's US-V3 through US-V6.

Judges every ``source == "llm"`` finding ``review_generation`` produced
against a fixed, four-criterion rubric (Sourcery's own decided
valid/actionable/specific/valuable gate — see the spec's Scope section) in
exactly one, structurally separate completion call — never a follow-up in
the same conversation that produced the findings, per design goal 6's
"never self-critique" requirement. Static-analysis findings
(``source in {"gitleaks", "semgrep"}``) bypass this entirely: they are the
platform's own deterministic, free trust anchor (design goal 4) and are
forwarded through this step's output completely unchanged (US-V6).

**Import boundary, per US-F1:** mirrors ``review/review_generation.py``'s own
established pattern — ``_VerificationInput`` below structurally mirrors
``review.review_generation.ReviewGenerationOutput`` (this step's normal
immediate predecessor) via ``from_attributes=True`` rather than importing
it directly.

**Fail-closed verdict correlation.** The
LLM's own batched response correlates each verdict to a candidate finding
by an explicit, LLM-echoed ``index`` field (that finding's 0-based position
in the array supplied in the prompt) — never by the response array's own
position, which nothing enforces stays aligned with the request array's
order. A finding whose index is missing from the response, duplicated, or
out of range is treated as **rejected**, never silently kept — the same
fail-closed posture ``findings/redact.py``'s "a redaction call's own
failure is fatal" and ``review_generation``'s own malformed-finding-drop
already establish for a structurally identical kind of gap.

**No new redaction surface.** ``_VerificationVerdictItem`` carries only an
``index`` and four booleans — no free-text field an LLM could use to echo
diff/secret content back into a log or a posted comment. This step never
redacts anything itself; it operates on findings ``review_generation``
already redacted.

Zero infra imports beyond ``llmgateway`` (per architecture.md's
domain-package rule) — this module binds ``llmgateway.client.LLMClient``,
not ``adapter.llm`` directly. The same holds for the Memory-suppression
pre-check below: this module binds ``indexing.embedding.EmbeddingClient`` and
``memory.store.DismissedFindingMatcher`` — both domain-defined Protocols —
never ``adapter.embedding``/``adapter.storage`` directly.

**Similarity-based suppression (US-M6/US-M7/US-M9/US-M12), a deterministic
pre-check before the rubric LLM call.** When ``memory_suppression`` is
configured and enabled, every candidate (``source == "llm"``) finding's
fingerprint (``memory.fingerprint.build_finding_fingerprint`` — the same
``file``/``rule_id``/``message`` composition the admin API's own approve
route embeds a ``dismissed_finding`` entry from, per that module's own
docstring on why this has to be the *same* function) is embedded and
compared, via ``memory.store.DismissedFindingMatcher.best_effective_similarity``,
against approved, repo-scoped ``dismissed_finding`` Memory entries, weighted
by each entry's own ranking-time decay (``memory.decay.effective_similarity``,
applied inside the matcher implementation, not here). A finding whose best
match exceeds ``memory_suppression.similarity_threshold`` is excluded from
this step's output *before* the rubric batch is even built — no LLM Gateway
call is ever spent on it (US-M6). A finding with no match, or whose best
match falls below threshold, proceeds through the rubric exactly as it does
without suppression (US-M7). ``memory_suppression=None`` (the default) or
``memory_suppression.enabled=False`` skips this pre-check entirely — no
embedding call, no matcher call, byte-for-byte the prior behavior, the
operational rollback lever US-M12 requires. A failure inside the pre-check
(the embedding call or the matcher's own Postgres query) is never caught
here — it propagates as this step's own fatal failure, the same
``FailurePolicy.FATAL`` posture every other failure in this step already
gets, per ``spec-memory.md``'s own "never a silent skip-and-continue that
would let an unsuppressed finding through on a broken check" Error-state
design.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from opentelemetry import metrics
from pydantic import ConfigDict, Field

from findings.models import Finding
from indexing.embedding import EmbeddingClient
from ingestion.diff import (
    Diff,
    DiffFetcher,
)
from llmgateway.client import LLMClient
from llmgateway.routing import FallbackModelRoutingResolver
from memory.fingerprint import build_finding_fingerprint
from memory.store import DismissedFindingMatcher
from review._telemetry import category_label, suppress_telemetry_errors
from review.context_bundle import ContextBundle
from review.diff_prompt import diff_subset
from review.finding_judge import (
    _SYSTEM_PROMPT,
    _SYSTEM_PROMPT_V2,
    ClassifierVerification,
    FindingJudge,
    JudgeInput,
    Judgement,
    LLMFindingJudge,
    VerificationResponseParseError,
    _correlate_verdicts,
    _parse_verdicts,
    _VerificationVerdictItem,
    criteria_for,
    failure_counts,
    select_judge,
)
from review.spec_resolution import ResolvedSpec
from review.step import JobIdentity, Step, StepContext, job_identity_fields
from review.verification_context import (
    FileContextConfig,
    build_file_contexts,
    finding_context_paths,
    render_file_context_section,
)

# Re-exported: these moved to review.finding_judge with the LLM judge, and
# existing tests and callers still import them from here.
__all__ = [
    "_SYSTEM_PROMPT",
    "_SYSTEM_PROMPT_V2",
    "MemorySuppressionConfig",
    "VerificationFileContext",
    "VerificationOutput",
    "VerificationResponseParseError",
    "VerificationStep",
    "_VerificationVerdictItem",
    "_correlate_verdicts",
    "_parse_verdicts",
]

logger = logging.getLogger(__name__)

# Imports only the OTel API (`opentelemetry.metrics`), the same
# `meter = metrics.get_meter(__name__)` style established across this
# codebase (adapter/telemetry/heartbeat.py) — no SDK/exporter construction
# here, that stays exclusively at the composition root
# (apps/webhook_api/main.py, apps/worker/main.py). With no
# MeterProvider registered (no OTLP endpoint configured, US10), every
# `.add()` call below is OTel's own no-op.
meter = metrics.get_meter(__name__)

# Catalog names, verbatim (docs/specs/spec-observability.md's METRIC & SPAN
# CATALOG, US5) — recorded at the same point as the existing
# "verification: batch judgment completed" log line below. No attributes:
# the catalog defines none for this trio, and job-level counts alone carry
# no free text/source content (Privacy/Compliance NFR).
_judged_counter = meter.create_counter(
    "review.verification.judged_total",
    description="Count of source=='llm' findings judged by the verification step.",
)
_rejected_counter = meter.create_counter(
    "review.verification.rejected_total",
    description="Count of findings rejected by the verification step's rubric or correlation.",
)
_unmatched_counter = meter.create_counter(
    "review.verification.unmatched_total",
    description="Count of findings with no verdict correlated to their index (fail-closed).",
)
_file_context_total = meter.create_counter(
    "review.verification.file_context_total",
    description="Verification runs by head-file context outcome (used, fallback, skipped).",
)


class _VerificationInput(JobIdentity):
    """Mirrors ``review.review_generation.ReviewGenerationOutput``'s shape
    (this step's normal immediate predecessor) without importing it — see
    the module docstring's "Import boundary" note. ``from_attributes=True``
    lets ``model_validate`` read this shape off the real upstream instance
    the outer registry hands this step as its own ``input``.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    findings: list[Finding] = Field(default_factory=list)
    context_bundle: ContextBundle | None = None
    # Present only when the optional ``spec_review`` step ran upstream; read
    # off its output like ``context_bundle``. Not forwarded onward.
    spec: ResolvedSpec | None = None
    # Set by a batched review_generation (spec-diff-batching.md); empty otherwise.
    review_batches: tuple[tuple[str, ...], ...] = ()
    unreviewed_files: tuple[str, ...] = ()


class VerificationOutput(JobIdentity):
    """This step's own output: the same shape as ``_VerificationInput``,
    with ``findings`` replaced by this step's own filtered/annotated list —
    rejected findings excluded entirely, surviving ``source == "llm"``
    findings marked ``verification_status="verified"``, every
    ``source != "llm"`` finding forwarded completely unchanged (US-V6).
    ``head_sha``/``context_bundle`` forwarded unchanged, mirroring
    ``ReviewGenerationOutput``'s own forwarding.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    findings: list[Finding]
    head_sha: str
    context_bundle: ContextBundle | None = None
    # Forwarded so the output step can demote off-diff inline findings.
    diff: Diff | None = None
    # Forwarded so the output step can name the files no review call covered.
    unreviewed_files: tuple[str, ...] = ()


def _contexts_for(findings: Sequence[Finding], contexts: Mapping[str, str]) -> dict[str, str]:
    """The contexts of the files ``findings`` cite, in first-citation order."""
    return {f.file: contexts[f.file] for f in findings if f.file in contexts}


def _pass_through_job_fields(parsed: _VerificationInput) -> dict[str, Any]:
    """The ``JobIdentity`` fields (via ``review.step.job_identity_fields``)
    plus ``context_bundle`` that ``VerificationOutput`` forwards unchanged
    from ``_VerificationInput`` on every return path
    (``VerificationStep.run``'s no-LLM-findings early return and its normal
    end-of-method return alike) — extracted so a future field addition
    doesn't have to be kept in sync by hand across two call sites.
    """
    return {
        **job_identity_fields(parsed),
        "context_bundle": parsed.context_bundle,
        "diff": parsed.diff,
        "unreviewed_files": parsed.unreviewed_files,
    }


@dataclass(frozen=True)
class MemorySuppressionConfig:
    """The four mutually-dependent fields US-M6/US-M7/US-M9/US-M12's
    similarity-suppression pre-check needs, grouped into one parameter
    rather than four independent ones on ``VerificationStep.__init__`` —
    ``threshold``/``decay_half_life_days`` are meaningless without ``memory_matcher``/
    ``embedding_client`` present together, so bundling them removes the
    invalid states four independent optional constructor parameters would
    otherwise allow (e.g. a threshold set with no matcher). There is no
    construction-time state where one field is present while another is
    missing — the invalid combination is structurally
    unrepresentable here, not merely validated after the fact.
    """

    memory_matcher: DismissedFindingMatcher
    embedding_client: EmbeddingClient
    similarity_threshold: float
    decay_half_life_days: float
    enabled: bool


@dataclass(frozen=True)
class VerificationFileContext:
    """Head-file context for the judge (docs/specs/spec-review-context.md US-X1):
    the fetcher reads the cited files, ``config`` bounds the text, and a fetch
    slower than ``timeout_seconds`` falls back to diff-only verification.
    """

    diff_fetcher: DiffFetcher
    config: FileContextConfig
    timeout_seconds: float


@dataclass
class _Tally:
    """Per-category (closed set smell/spec/none) judged/rejected/unmatched
    counts, so a noisy check is visible instead of averaged away."""

    judged: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    unmatched: Counter[str] = field(default_factory=Counter)


def _tally_judgements(
    llm_findings: list[Finding], judgements: list[Judgement]
) -> tuple[list[Finding], _Tally]:
    """Keeps verified findings; counts rejections per category."""
    kept: list[Finding] = []
    tally = _Tally()
    for finding, judgement in zip(llm_findings, judgements, strict=True):
        category = category_label(finding.category)
        tally.judged[category] += 1
        if judgement.unmatched:
            # Fail-closed: no verdict correlated to this finding's index at
            # all — rejected, not silently kept.
            tally.rejected[category] += 1
            tally.unmatched[category] += 1
        elif judgement.passed is not True:  # a rejection, or (never expected) undecided
            tally.rejected[category] += 1
        else:
            kept.append(finding.model_copy(update={"verification_status": "verified"}))
    return kept, tally


def _record_verification_telemetry(tally: _Tally) -> None:
    with suppress_telemetry_errors(
        logger, "verification telemetry recording failed (verification unaffected)"
    ):
        for category, count in tally.judged.items():
            _judged_counter.add(count, {"category": category})
            # Always recorded, even at zero, so a category's rejection rate
            # is computable from a series that exists.
            _rejected_counter.add(tally.rejected[category], {"category": category})
            _unmatched_counter.add(tally.unmatched[category], {"category": category})


def _log_judgment(
    context: StepContext,
    repo_id: Any,
    criteria: Any,
    *,
    judged: int,
    rejected: int,
    unmatched: int,
    suppressed: int,
    failures: dict[str, int],
) -> None:
    log_fn = logger.warning if rejected > 0 else logger.info
    log_fn(
        "verification: batch judgment completed",
        extra={
            "job_id": context.job_id,
            "repo_id": repo_id,
            "judged_count": judged,
            "rejected_count": rejected,
            "unmatched_count": unmatched,
            "memory_suppressed_count": suppressed,
            **{f"{criterion}_failures": failures.get(criterion, 0) for criterion in criteria},
        },
    )


class VerificationStep:
    """``review.step.Step`` implementation for US-V3 through US-V6 — see
    this module's own docstring for the full design.
    """

    name = "verification"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model: str,
        timeout: float,
        max_diff_bytes: int,
        depends_on: str = "review_generation",
        memory_suppression: MemorySuppressionConfig | None = None,
        model_resolver: FallbackModelRoutingResolver | None = None,
        classifier: ClassifierVerification | None = None,
        batch_concurrency: int = 1,
        file_context: VerificationFileContext | None = None,
    ) -> None:
        """``depends_on`` names this step's single upstream dependency —
        ``"review_generation"`` by default, its normal immediate
        predecessor in the six-step chain (``context_builder ->
        intent_summary -> review_generation -> verification -> output``).

        A constructor parameter, not a fixed class attribute, mirrors
        ``review.review_generation.ReviewGenerationStep``'s/
        ``review.intent_summary.IntentSummaryStep``'s own identical
        reasoning: ``review.pipeline.StepRegistry`` resolves this step's
        ``input`` from exactly its declared dependency's output, and the
        composition root (``apps/worker/main.py``) determines the
        actual value once it knows which upstream steps are enabled for a
        given deployment.

        ``memory_suppression`` is ``None`` by default — suppression off entirely,
        byte-for-byte this step's behavior without it. Every
        existing unit/integration test constructing a bare
        ``VerificationStep`` keeps working unchanged, matching every other
        "absent upstream data degrades gracefully" precedent this codebase
        already establishes for ``context_bundle``/``intent_summary``.

        ``model_resolver`` is ``None`` by default —
        ``model`` is then used unchanged, byte-for-byte this step's prior
        behavior. When set, ``run()`` calls
        ``model_resolver.resolve(self.name, parsed.org_id)`` itself, at
        dispatch time, instead of using ``model`` — the composition root
        never reaches into this already-built step to overwrite its own
        ``model`` field, per ``docs/rules/architecture.md``'s "no stage
        reaching into another stage's internals" rule.

        ``classifier`` is ``None`` by default: no classifier
        code runs and the LLM judge decides everything, as before. When set,
        its resolver is asked once per job whether this org opted in, and in
        which mode (``review.finding_judge``).

        ``batch_concurrency`` bounds the judge calls in flight when the
        review was batched (spec-diff-batching.md); unused otherwise.

        ``file_context`` is ``None`` by default: the judge sees only the diff,
        and no file is fetched.
        """
        self._llm_judge = LLMFindingJudge(
            llm_client,
            model=model,
            timeout=timeout,
            max_diff_bytes=max_diff_bytes,
            model_resolver=model_resolver,
        )
        self._classifier = classifier
        self._memory_suppression = memory_suppression
        self._batch_concurrency = batch_concurrency
        self._file_context = file_context
        self.depends_on: tuple[str, ...] = (depends_on,)

    async def _load_file_contexts(
        self,
        parsed: _VerificationInput,
        llm_findings: Sequence[Finding],
        context: StepContext,
        config: VerificationFileContext,
    ) -> dict[str, str]:
        """Bounded head-file text per cited file; any fetch problem logs a
        warning and yields no context (the judge then sees the diff only).
        """
        live = {f.path for f in parsed.diff.files if f.status != "removed"}
        paths = [p for p in finding_context_paths(llm_findings) if p in live]
        outcome = "skipped"
        contexts: dict[str, str] = {}
        if paths:
            try:
                async with asyncio.timeout(config.timeout_seconds):
                    contents = await config.diff_fetcher.fetch_content(
                        installation_id=parsed.installation_id,
                        repo_full_name=parsed.repo_full_name,
                        diff=parsed.diff,
                        paths=paths,
                    )
                contexts = await asyncio.to_thread(
                    build_file_contexts, llm_findings, contents, config.config
                )
                outcome = "used" if contexts else "skipped"
            except Exception as error:  # best-effort: token exchange and adapter errors degrade too
                outcome = "fallback"
                logger.warning(
                    "verification: head file context unavailable; judging against the diff only",
                    extra={"job_id": context.job_id, "error_type": type(error).__name__},
                )
        with suppress_telemetry_errors(
            logger, "verification telemetry recording failed (verification unaffected)"
        ):
            _file_context_total.add(1, {"outcome": outcome})
        if contexts:
            logger.info(
                "verification: head file context built",
                extra={
                    "job_id": context.job_id,
                    "file_count": len(contexts),
                    "context_bytes": sum(len(t.encode("utf-8")) for t in contexts.values()),
                },
            )
        return contexts

    @staticmethod
    async def _suppress_via_memory(
        llm_findings: list[Finding], *, config: MemorySuppressionConfig, repo_id: str
    ) -> tuple[list[Finding], int]:
        """US-M6/US-M7/US-M9's own pre-check — see the module docstring's
        "Similarity-based suppression" section for the full design. Returns
        ``(kept_findings, suppressed_count)``. Takes ``config`` explicitly
        (rather than reading ``self._memory_suppression`` and narrowing away
        the ``None`` case internally) — the caller (``run()``) only calls
        this once it has already checked ``self._memory_suppression is not
        None and .enabled`` (US-M12's own kill switch), so there is nothing
        left for this method itself to guard against.
        """
        fingerprints = [
            build_finding_fingerprint(
                file=finding.file, rule_id=finding.rule_id, message=finding.message
            )
            for finding in llm_findings
        ]
        # `embed` is a plain, synchronous, CPU-bound call (per
        # `indexing.embedding.EmbeddingClient`'s own docstring) —
        # `asyncio.to_thread`-wrapped so it never blocks this step's event
        # loop, the same pattern `review.context_builder.ContextBuilderStep`
        # already establishes for its own `embed` call.
        vectors = await asyncio.to_thread(config.embedding_client.embed, fingerprints)

        now = datetime.now(UTC)
        kept: list[Finding] = []
        suppressed_count = 0
        for finding, vector in zip(llm_findings, vectors, strict=True):
            similarity = await config.memory_matcher.best_effective_similarity(
                repo_id=repo_id,
                embedding=vector.values,
                half_life_days=config.decay_half_life_days,
                now=now,
            )
            if similarity is not None and similarity > config.similarity_threshold:
                suppressed_count += 1
                continue
            kept.append(finding)

        return kept, suppressed_count

    async def _judge_by_batch(
        self,
        judge: FindingJudge,
        llm_findings: list[Finding],
        job: JudgeInput,
        batches: Sequence[Sequence[str]],
        contexts: Mapping[str, str],
    ) -> list[Judgement]:
        """Judges each batch's findings against only the diff of the files
        that have findings there; findings in no batch form one extra group
        judged against the whole (cut) diff. Returns judgements in
        ``llm_findings`` order. A judge exception propagates (fatal).
        """
        batch_of = {path: n for n, batch in enumerate(batches) for path in batch}
        groups: dict[int, list[int]] = {}
        for index, finding in enumerate(llm_findings):
            groups.setdefault(batch_of.get(finding.file, -1), []).append(index)
        semaphore = asyncio.Semaphore(self._batch_concurrency)

        async def judge_group(key: int, indexes: list[int]) -> list[Judgement]:
            group = [llm_findings[i] for i in indexes]
            diff = job.diff if key == -1 else diff_subset(job.diff, {f.file for f in group})
            section = render_file_context_section(_contexts_for(group, contexts))
            async with semaphore:
                return await judge.judge(group, replace(job, diff=diff, file_context=section))

        results = await asyncio.gather(*(judge_group(k, ix) for k, ix in groups.items()))
        ordered: list[Judgement | None] = [None] * len(llm_findings)
        for indexes, judgements in zip(groups.values(), results, strict=True):
            for index, judgement in zip(indexes, judgements, strict=True):
                ordered[index] = judgement
        return [j for j in ordered if j is not None]

    async def _judge_all(
        self, parsed: _VerificationInput, llm_findings: list[Finding], context: StepContext
    ) -> list[Judgement]:
        job = JudgeInput(
            diff=parsed.diff,
            context_bundle=parsed.context_bundle,
            spec=parsed.spec,
            org_id=parsed.org_id,
            repo_id=parsed.repo_id,
            job_id=context.job_id,
            review_quality_v2=context.review_quality_v2,
        )
        judge = await select_judge(
            self._classifier, self._llm_judge, job=job, finding_count=len(llm_findings)
        )
        contexts = (
            await self._load_file_contexts(parsed, llm_findings, context, self._file_context)
            if self._file_context is not None
            else {}
        )
        if parsed.review_batches:
            judgements = await self._judge_by_batch(
                judge, llm_findings, job, parsed.review_batches, contexts
            )
        else:
            section = render_file_context_section(_contexts_for(llm_findings, contexts))
            judgements = await judge.judge(llm_findings, replace(job, file_context=section))
        return judgements

    async def _maybe_suppress(
        self, llm_findings: list[Finding], repo_id: Any
    ) -> tuple[list[Finding], int]:
        config = self._memory_suppression
        if config is None or not config.enabled:
            return llm_findings, 0
        return await self._suppress_via_memory(llm_findings, config=config, repo_id=repo_id)

    async def _judge_and_record(
        self,
        parsed: _VerificationInput,
        llm_findings: list[Finding],
        context: StepContext,
        criteria: Any,
        memory_suppressed_count: int,
    ) -> list[Finding]:
        judgements = await self._judge_all(parsed, llm_findings, context)
        criterion_failures = failure_counts(judgements, criteria)

        kept_llm_findings, tally = _tally_judgements(llm_findings, judgements)
        rejected_count = sum(tally.rejected.values())
        unmatched_count = sum(tally.unmatched.values())
        _record_verification_telemetry(tally)

        _log_judgment(
            context,
            parsed.repo_id,
            criteria,
            judged=len(llm_findings),
            rejected=rejected_count,
            unmatched=unmatched_count,
            suppressed=memory_suppressed_count,
            failures=criterion_failures,
        )
        return kept_llm_findings

    async def run(self, step_input: Any, context: StepContext) -> VerificationOutput:
        # Any (not _VerificationInput) because the registry passes the raw
        # upstream step output here for validation — see the module
        # docstring's "Import boundary" note.
        parsed = _VerificationInput.model_validate(step_input, from_attributes=True)

        criteria = criteria_for(context.review_quality_v2)
        llm_findings = [finding for finding in parsed.findings if finding.source == "llm"]
        other_findings = [finding for finding in parsed.findings if finding.source != "llm"]

        # US-V3: nothing to judge means no LLM Gateway call; other findings pass
        # through unchanged (US-V6) and memory suppression is never attempted.
        if not llm_findings:
            return VerificationOutput(findings=other_findings, **_pass_through_job_fields(parsed))

        llm_findings, memory_suppressed_count = await self._maybe_suppress(
            llm_findings, parsed.repo_id
        )

        if not llm_findings:
            # Every candidate finding was memory-suppressed before the
            # rubric call — still logged (memory_suppressed_count, not
            # silently dropped), even though no LLM Gateway call happens;
            # static/other findings still pass through unchanged (US-V6).
            _log_judgment(
                context,
                parsed.repo_id,
                criteria,
                judged=0,
                rejected=0,
                unmatched=0,
                suppressed=memory_suppressed_count,
                failures={},
            )
            return VerificationOutput(
                findings=other_findings,
                **_pass_through_job_fields(parsed),
            )

        kept_llm_findings = await self._judge_and_record(
            parsed, llm_findings, context, criteria, memory_suppressed_count
        )

        return VerificationOutput(
            findings=[*other_findings, *kept_llm_findings],
            **_pass_through_job_fields(parsed),
        )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[Any, VerificationOutput]] = VerificationStep
