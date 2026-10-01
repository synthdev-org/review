"""review.intent_summary — the ``intent_summary`` pipeline step (Unit A),
per docs/plans/plan-deliver-verification-pipeline.md's Phase 1 section and
docs/specs/spec-verification-pipeline.md's US-V1.

Sends the bare diff alone (Assumption 1 — no static findings, no context
bundle: "what is this PR trying to accomplish" is fully derivable from the
diff, and including the other sections would add real token cost to a
one-paragraph summarization call for no stated benefit) to the configured
LLM endpoint in exactly one completion call, and forwards the raw text
response unchanged (no JSON parsing — this step's response is plain
natural-language prose, not this codebase's usual structured-findings
schema) as a new ``intent_summary`` field on its own output.

**Why between ``context_builder`` and ``review_generation``:** this step's
own output feeds ``review_generation``'s new prompt section (Phase 2); it
has no dependency on any other phase of that plan.

**Import boundary:** mirrors ``review/review_generation.py``'s own established
pattern — ``_IntentSummaryInput`` below structurally mirrors whichever
upstream step's output actually reaches it
(``review.context_builder.ContextBuilderOutput``/
``review.static_analysis.StaticAnalysisOutput``/
``review.prefilter.PrefilterOutput``, depending on which upstream steps are
enabled) via ``from_attributes=True`` rather than importing any of those
types directly.

**No redaction call for this step's own text** (Assumption 4/the plan's
Phase 1 section): the intent summary never becomes a ``Finding.message`` or
any other posted/logged field directly — its only downstream consumer is
``review_generation``'s own prompt (Phase 2), and any content it echoes
from the diff that ends up quoted back in a ``source="llm"`` finding's
``message`` is already caught by that finding's own existing redaction
pass. Logged only as presence/length (``intent_summary_chars: int``), never
the text itself.

**Intent-summary text is capped at a fixed module-level constant**
(``MAX_INTENT_SUMMARY_CHARS``), not a new bootstrap-config field — matching
this codebase's existing pattern for a fixed-not-configurable ceiling
(``review.review_generation.REDACTION_FINDING_CEILING``,
``findings.merge.POSTED_FINDINGS_CEILING``); nothing in the spec asks for
this to be operator-tunable.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict, Field

from findings.models import Finding
from ingestion.diff import Diff
from llmgateway.client import LLMClient
from llmgateway.routing import FallbackModelRoutingResolver
from review.context_bundle import ContextBundle
from review.diff_prompt import cap_sequence_length, render_and_truncate_diff
from review.llm_response import build_completion_request
from review.step import JobIdentity, Step, StepContext, job_identity_fields

logger = logging.getLogger(__name__)

# A one-paragraph PR-intent summary has no legitimate reason to run long;
# capped here so a manipulated/verbose completion can't grow an unbounded
# amount of text into `review_generation`'s own downstream prompt. Excess
# is truncated and logged at `warning`, mirroring `REDACTION_FINDING_CEILING`/
# `POSTED_FINDINGS_CEILING`'s own fixed-not-configurable pattern.
MAX_INTENT_SUMMARY_CHARS = 2000

_SYSTEM_PROMPT = (
    "You are an automated code review assistant. You will be given a single "
    "pull request's unified diff. Respond with a concise, plain-language "
    "summary (2-4 sentences) of what this change is trying to accomplish — "
    "the PR's overall intent, not a line-by-line description of every edit. "
    "Respond with ONLY that summary as plain text — no JSON, no markdown "
    "formatting, no preamble or labels."
)


class _IntentSummaryInput(JobIdentity):
    """Mirrors whichever upstream step's output actually reaches this step
    (``ContextBuilderOutput``/``StaticAnalysisOutput``/``PrefilterOutput``,
    per the existing ``from_attributes=True``-mirroring pattern every step
    module already uses — see the module docstring's "Import boundary"
    note). ``model_config`` mirrors ``review.review_generation._ReviewGenerationInput``'s
    own exactly.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    # Defaults to empty, not required, so this step also validates off
    # `review.prefilter.PrefilterOutput` (which carries no `findings` of its
    # own) when `depends_on` falls back to it.
    findings: list[Finding] = Field(default_factory=list)
    # Optional, defaulting to `None`, so this step also validates off
    # `review.static_analysis.StaticAnalysisOutput`/`PrefilterOutput`
    # (neither carries a `context_bundle` field of its own).
    context_bundle: ContextBundle | None = None


class IntentSummaryOutput(JobIdentity):
    """This step's own output: ``diff``/``findings``/``context_bundle``
    forwarded unchanged, plus the new ``intent_summary`` field —
    ``review_generation`` (this step's own downstream dependency) receives
    this as its own ``input``, per ``review.pipeline.StepRegistry``'s
    single-dependency-input design.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    findings: list[Finding]
    context_bundle: ContextBundle | None = None
    intent_summary: str


def _cap_intent_summary(text: str, *, max_chars: int) -> tuple[str, int, int]:
    """Caps ``text`` at ``max_chars`` characters, returning
    ``(possibly-truncated text, original char count, final char count)``.
    Exactly ``max_chars`` is not truncated (boundary is exclusive); one
    character over is (this step's own ceiling, ``MAX_INTENT_SUMMARY_CHARS``,
    is stated in characters, not bytes). Delegates to
    ``review.diff_prompt.cap_sequence_length`` — the same boundary-slicing
    primitive ``truncate_diff_text`` itself now builds on — rather than a
    second, independent copy of the same cap-and-report-lengths algorithm.
    """
    capped, original_chars, final_chars = cap_sequence_length(text, max_chars)
    assert isinstance(capped, str)  # cap_sequence_length preserves the input type
    return capped, original_chars, final_chars


class IntentSummaryStep:
    """``review.step.Step`` implementation for US-V1 — see this module's own
    docstring for the full design."""

    name = "intent_summary"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model: str,
        timeout: float,
        max_diff_bytes: int,
        depends_on: str = "context_builder",
        model_resolver: FallbackModelRoutingResolver | None = None,
    ) -> None:
        """``depends_on`` names this step's single upstream dependency —
        ``"context_builder"`` by default, its normal immediate predecessor
        in the six-step chain (``context_builder -> intent_summary ->
        review_generation -> verification -> output``).

        A constructor parameter, not a fixed class attribute, mirrors
        ``review.review_generation.ReviewGenerationStep``'s own identical reasoning for
        the symmetric problem: ``review.pipeline.StepRegistry`` resolves
        this step's ``input`` from exactly its declared dependency's
        output, and the composition root (``apps/worker/main.py``, Phase 5)
        determines the actual value once it knows which upstream steps are
        enabled for a given deployment.

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
        self._model_resolver = model_resolver
        self.depends_on: tuple[str, ...] = (depends_on,)

    async def run(self, input: Any, context: StepContext) -> IntentSummaryOutput:
        # Any (not _IntentSummaryInput) because the registry passes the raw
        # upstream step output here for validation — see the module
        # docstring's "Import boundary" note.
        parsed = _IntentSummaryInput.model_validate(input, from_attributes=True)

        truncated_text = render_and_truncate_diff(
            parsed.diff,
            self._max_diff_bytes,
            logger=logger,
            job_id=context.job_id,
            step_name="intent-summary",
        )

        model = (
            await self._model_resolver.resolve(self.name, parsed.org_id)
            if self._model_resolver is not None
            else self._model
        )
        request = build_completion_request(
            model,
            system_content=_SYSTEM_PROMPT,
            user_content=f"Diff:\n{truncated_text}",
        )
        response = await self._llm_client.complete(request, timeout=self._timeout)

        intent_summary, original_chars, final_chars = _cap_intent_summary(
            response.content, max_chars=MAX_INTENT_SUMMARY_CHARS
        )
        if final_chars < original_chars:
            logger.warning(
                "intent-summary: response truncated to fit summary char ceiling",
                extra={
                    "job_id": context.job_id,
                    "ceiling_chars": MAX_INTENT_SUMMARY_CHARS,
                    "original_chars": original_chars,
                    "truncated_chars": final_chars,
                },
            )

        # Never the summary text itself — only its length, per the module
        # docstring's own "never logged in full" note.
        logger.info(
            "intent-summary: completed",
            extra={"job_id": context.job_id, "intent_summary_chars": final_chars},
        )

        return IntentSummaryOutput(
            diff=parsed.diff,
            findings=parsed.findings,
            context_bundle=parsed.context_bundle,
            intent_summary=intent_summary,
            **job_identity_fields(parsed),
        )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[Any, IntentSummaryOutput]] = IntentSummaryStep
