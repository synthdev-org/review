"""review.verification — the ``verification`` pipeline step (Unit C), per
docs/plans/plan-deliver-verification-pipeline.md's Phase 3 section and
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

**Fail-closed verdict correlation (Assumption 5 of the plan above).** The
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
pre-check below (``docs/plans/plan-deliver-memory.md``'s Phase 6 section,
Unit F): this module binds ``indexing.embedding.EmbeddingClient`` and
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
match falls below threshold, proceeds through the rubric exactly as it did
before this phase (US-M7). ``memory_suppression=None`` (the default) or
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

import json
import logging
from typing import Any

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from findings.models import Finding
from ingestion.diff import Diff
from llmgateway.client import LLMClient
from llmgateway.routing import FallbackModelRoutingResolver
from review._telemetry import suppress_telemetry_errors
from review.context_bundle import (
    ContextBundle,
    render_context_bundle_section,
    render_product_knowledge_section,
)
from review.diff_prompt import render_and_truncate_diff
from review.llm_response import build_completion_request, parse_llm_json_envelope
from review.llm_telemetry import record_llm_token_usage
from review.memory_suppression import (
    MemorySuppressionConfig as MemorySuppressionConfig,  # re-export
)
from review.memory_suppression import SuppressionConfig, suppress_via_memory
from review.step import JobIdentity, Step, StepContext, job_identity_fields

logger = logging.getLogger(__name__)

# Imports only the OTel API (`opentelemetry.metrics`), the same
# `meter = metrics.get_meter(__name__)` style established across this
# codebase (adapter/telemetry/heartbeat.py) — no SDK/exporter construction
# here, that stays exclusively at the composition root
# (apps/webhook_api/main.py, apps/worker/main.py per Unit A). With no
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

# This step's own fixed pipeline-stage name, matching review_generation.py's
# analogous constant — a closed-set label value, never free text. Passed to
# review.llm_telemetry.record_llm_token_usage, which owns the shared
# "review.llm" meter/counter every LLM-calling stage records against, so
# every stage's LLM spend rolls up into one metric distinguished only by the
# {stage} attribute.
_PIPELINE_STAGE_NAME = "verification"


_SYSTEM_PROMPT = (
    "You are an automated code review verification assistant — a structurally "
    "separate check, never the same call that produced the findings you are "
    "given. You will be given a list of candidate findings an earlier review "
    "pass produced for a single pull request's unified diff, each tagged with "
    'its own integer "index" (its position in the list you were given), plus '
    'the diff itself and, optionally, a "Related code in this repository" '
    "section of context. For EACH candidate finding, independently judge it "
    "against all four of these fixed criteria:\n"
    "- valid: is the finding actually correct about the code shown in the diff?\n"
    "- actionable: could the PR author actually act on this finding?\n"
    "- specific: does it point at a specific, identifiable issue, not a vague "
    "generality?\n"
    "- valuable: is this worth the PR author's time to read?\n\n"
    "Respond with ONLY a single JSON object, no other text, matching exactly "
    "this shape:\n"
    '{"verdicts": [{"index": <int>, "valid": <bool>, "actionable": <bool>, '
    '"specific": <bool>, "valuable": <bool>}, ...]}\n\n'
    "Include exactly one verdict per candidate finding, echoing back its own "
    'given "index" value unchanged.'
)


class VerificationResponseParseError(Exception):
    """Raised when the LLM's completion response content does not parse as
    valid JSON with a top-level ``verdicts`` list — mirrors
    ``review.review_generation.LLMResponseParseError``'s own shape/role.
    Never caught within ``VerificationStep.run`` — propagates as this
    step's own fatal failure, identical mechanism to
    ``review_generation``'s own parse-error path (US-V7, made fatal by
    Phase 5's registration into the existing ``FailurePolicy.FATAL``
    registry).
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(
            f"could not parse verification response against the verdicts schema: {detail}"
        )
        self.detail = detail


class _VerificationVerdictItem(BaseModel):
    """One item of the LLM's own batched verdicts response — the fixed
    schema stated in ``_SYSTEM_PROMPT`` above: which candidate finding
    (``index``) this verdict is for, plus the four fixed rubric criteria.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    valid: bool
    actionable: bool
    specific: bool
    valuable: bool


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
    # Phase 12: carried forward unchanged for the walkthrough step.
    intent_summary: str | None = None


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
    # Phase 12: carried forward for the walkthrough step; `reviewed_diff` is the diff
    # this step judged, named apart from `diff` so fix_generation's fetch is untouched.
    intent_summary: str | None = None
    reviewed_diff: Diff | None = None


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
        "intent_summary": parsed.intent_summary,
        "reviewed_diff": parsed.diff,
    }


def _render_candidate_findings(findings: list[Finding]) -> str:
    """Renders every candidate (``source == "llm"``) finding as the JSON
    array supplied in the verification prompt — each item carries its own
    explicit ``"index"`` field (its 0-based position in ``findings``, per
    Assumption 5), the value the LLM is expected to echo back on its own
    verdict for that finding.
    """
    return json.dumps(
        [
            {
                "index": index,
                "file": finding.file,
                "line": finding.line,
                "message": finding.message,
                "context_ref_ids": (
                    list(finding.context_ref_ids) if finding.context_ref_ids is not None else None
                ),
            }
            for index, finding in enumerate(findings)
        ]
    )


def _build_user_message(
    diff_text: str, candidate_findings: list[Finding], context_bundle: ContextBundle | None
) -> str:
    findings_json = _render_candidate_findings(candidate_findings)
    context_bundle_section = render_context_bundle_section(context_bundle)
    return (
        'Candidate findings to verify (each tagged with its own "index"):\n'
        f"{findings_json}\n\n"
        f"Diff:\n{diff_text}"
        f"{context_bundle_section}"
        f"{render_product_knowledge_section(context_bundle)}"
    )


def _parse_verdicts(content: str, *, job_id: str) -> list[_VerificationVerdictItem]:
    """Parses ``response.content`` against the fixed verdicts schema,
    raising ``VerificationResponseParseError`` (never an unhandled
    exception) on a malformed/unparseable *response* — not JSON at all, or
    missing the top-level ``verdicts`` list entirely.

    **One malformed verdict item never fails the whole response** — each
    item in ``verdicts`` is validated independently against
    ``_VerificationVerdictItem``; a single item that doesn't match the
    fixed shape is dropped and logged at ``warning``, mirroring
    ``review.review_generation._parse_llm_findings``'s own per-item-tolerant
    pattern.
    """
    return parse_llm_json_envelope(
        content,
        envelope_key="verdicts",
        item_model=_VerificationVerdictItem,
        make_parse_error=lambda detail: VerificationResponseParseError(detail=detail),
        logger=logger,
        job_id=job_id,
        step_name="verification",
        item_label="verdict",
        item_index_field="verdict_position",
    )


def _correlate_verdicts(
    verdicts: list[_VerificationVerdictItem], *, candidate_count: int, job_id: str
) -> dict[int, _VerificationVerdictItem]:
    """Builds ``index -> verdict`` from the parsed, per-item-validated
    ``verdicts`` list, per Assumption 5's fail-closed correlation design.

    A duplicate ``index`` keeps the first occurrence — the second (and any
    later) occurrence is dropped and logged at ``warning`` with the
    offending ``index``/``job_id`` as structured ``extra`` fields, never a
    bare message. An ``index`` outside ``range(candidate_count)`` is
    dropped and logged the same structured way, under its own distinct
    message — both cases get the same visible-not-silent treatment (the
    plan's own "asymmetric treatment" fix).
    """
    verdict_by_index: dict[int, _VerificationVerdictItem] = {}
    for verdict in verdicts:
        if verdict.index < 0 or verdict.index >= candidate_count:
            message = "verification: dropping out-of-range verdict index"
        elif verdict.index in verdict_by_index:
            message = "verification: dropping duplicate verdict index; keeping first occurrence"
        else:
            verdict_by_index[verdict.index] = verdict
            continue
        logger.warning(message, extra={"job_id": job_id, "index": verdict.index})
    return verdict_by_index


#: The four load-bearing rubric criteria `_VerificationVerdictItem` carries
#: (Sourcery's four criteria, per docs/research/research-current-art.md) —
#: shared by `_passes_rubric` and `VerificationStep.run`'s per-criterion
#: failure-count aggregation so the two can't drift apart.
_RUBRIC_CRITERIA: tuple[str, ...] = ("valid", "actionable", "specific", "valuable")


def _passes_rubric(verdict: _VerificationVerdictItem) -> bool:
    return all(getattr(verdict, criterion) for criterion in _RUBRIC_CRITERIA)


class VerificationStep:
    """``review.step.Step`` implementation for US-V3 through US-V6 — see
    this module's own docstring for the full design."""

    name = "verification"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model: str,
        timeout: float,
        max_diff_bytes: int,
        depends_on: str = "review_generation",
        memory_suppression: SuppressionConfig | None = None,
        model_resolver: FallbackModelRoutingResolver | None = None,
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
        composition root (``apps/worker/main.py``, Phase 5) determines the
        actual value once it knows which upstream steps are enabled for a
        given deployment.

        ``memory_suppression`` (``docs/plans/plan-deliver-memory.md``'s
        Phase 6 section) is ``None`` by default — suppression off entirely,
        byte-for-byte this step's behavior before that phase. Every
        existing unit/integration test constructing a bare
        ``VerificationStep`` keeps working unchanged, matching every other
        "absent upstream data degrades gracefully" precedent this codebase
        already establishes for ``context_bundle``/``intent_summary``.

        ``model_resolver`` (``docs/plans/plan-deliver-admin-gui.md``'s
        Phase 5 section, Unit F, Assumption 3) is ``None`` by default —
        ``model`` is then used unchanged, byte-for-byte this step's prior
        behavior. When set, ``run()`` calls
        ``model_resolver.resolve(self.name, parsed.org_id)`` itself, at
        dispatch time, instead of using ``model`` — the composition root
        never reaches into this already-built step to overwrite its own
        ``model`` field, per ``docs/rules/architecture.md``'s "no stage
        reaching into another stage's internals" rule.
        """
        self._llm_client = llm_client
        self._model = model
        self._timeout = timeout
        self._max_diff_bytes = max_diff_bytes
        self._memory_suppression = memory_suppression
        self._model_resolver = model_resolver
        self.depends_on: tuple[str, ...] = (depends_on,)

    async def run(self, input: Any, context: StepContext) -> VerificationOutput:
        # Any (not _VerificationInput) because the registry passes the raw
        # upstream step output here for validation — see the module
        # docstring's "Import boundary" note.
        parsed = _VerificationInput.model_validate(input, from_attributes=True)

        llm_findings = [finding for finding in parsed.findings if finding.source == "llm"]
        other_findings = [finding for finding in parsed.findings if finding.source != "llm"]

        if not llm_findings:
            # US-V3's second scenario: nothing to judge, so no LLM Gateway
            # call is made at all — static/other findings still pass
            # through unchanged (US-V6). Memory suppression has nothing to
            # compare against either, so it's never attempted here.
            return VerificationOutput(
                findings=other_findings,
                **_pass_through_job_fields(parsed),
            )

        memory_suppressed_count = 0
        if self._memory_suppression is not None and self._memory_suppression.enabled:
            llm_findings, memory_suppressed_count = await suppress_via_memory(
                llm_findings, config=self._memory_suppression, repo_id=parsed.repo_id
            )

        if not llm_findings:
            # Every candidate finding was memory-suppressed before the
            # rubric call — still logged (memory_suppressed_count, not
            # silently dropped), even though no LLM Gateway call happens;
            # static/other findings still pass through unchanged (US-V6).
            logger.info(
                "verification: batch judgment completed",
                extra={
                    "job_id": context.job_id,
                    "repo_id": parsed.repo_id,
                    "judged_count": 0,
                    "rejected_count": 0,
                    "unmatched_count": 0,
                    "memory_suppressed_count": memory_suppressed_count,
                    **{f"{criterion}_failures": 0 for criterion in _RUBRIC_CRITERIA},
                },
            )
            return VerificationOutput(
                findings=other_findings,
                **_pass_through_job_fields(parsed),
            )

        truncated_text = render_and_truncate_diff(
            parsed.diff,
            self._max_diff_bytes,
            logger=logger,
            job_id=context.job_id,
            step_name="verification",
        )

        model = (
            await self._model_resolver.resolve(self.name, parsed.org_id)
            if self._model_resolver is not None
            else self._model
        )
        request = build_completion_request(
            model,
            system_content=_SYSTEM_PROMPT,
            user_content=_build_user_message(truncated_text, llm_findings, parsed.context_bundle),
        )
        response = await self._llm_client.complete(request, timeout=self._timeout)
        if response.usage is not None:
            record_llm_token_usage(
                response.usage, model=response.model, stage=_PIPELINE_STAGE_NAME, logger=logger
            )
        verdicts = _parse_verdicts(response.content, job_id=context.job_id)
        verdict_by_index = _correlate_verdicts(
            verdicts, candidate_count=len(llm_findings), job_id=context.job_id
        )

        # Aggregate per-criterion failure counts over correlated verdicts
        # only, computed before filtering — never over an unmatched
        # finding, which has no verdict to fail a criterion at all. Looped
        # over `_RUBRIC_CRITERIA` (the same four criteria `_passes_rubric`
        # checks) rather than one hand-written `if not verdict.X: X_failures
        # += 1` block per criterion, so adding/renaming a criterion can't
        # leave one of four near-identical blocks out of sync with the rest.
        criterion_failures = dict.fromkeys(_RUBRIC_CRITERIA, 0)
        for verdict in verdict_by_index.values():
            for criterion in _RUBRIC_CRITERIA:
                if not getattr(verdict, criterion):
                    criterion_failures[criterion] += 1

        kept_llm_findings: list[Finding] = []
        rejected_count = 0
        unmatched_count = 0
        for index, finding in enumerate(llm_findings):
            correlated_verdict = verdict_by_index.get(index)
            if correlated_verdict is None:
                # Fail-closed per Assumption 5: no verdict correlated to
                # this finding's index at all — rejected, not silently kept.
                rejected_count += 1
                unmatched_count += 1
                continue
            if not _passes_rubric(correlated_verdict):
                rejected_count += 1
                continue
            kept_llm_findings.append(finding.model_copy(update={"verification_status": "verified"}))

        with suppress_telemetry_errors(
            logger, "verification telemetry recording failed (verification unaffected)"
        ):
            _judged_counter.add(len(llm_findings))
            _rejected_counter.add(rejected_count)
            _unmatched_counter.add(unmatched_count)

        log_fn = logger.warning if rejected_count > 0 else logger.info
        log_fn(
            "verification: batch judgment completed",
            extra={
                "job_id": context.job_id,
                "repo_id": parsed.repo_id,
                "judged_count": len(llm_findings),
                "rejected_count": rejected_count,
                "unmatched_count": unmatched_count,
                "memory_suppressed_count": memory_suppressed_count,
                **{
                    f"{criterion}_failures": count
                    for criterion, count in criterion_failures.items()
                },
            },
        )

        return VerificationOutput(
            findings=[*other_findings, *kept_llm_findings],
            **_pass_through_job_fields(parsed),
        )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[Any, VerificationOutput]] = VerificationStep
