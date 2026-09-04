"""review.llm_review — the llm-review macro pipeline step (US5), per the
plan's Phase 6 section: sends the bare diff plus static-analysis findings
(as context) to the configured LLM endpoint in exactly one completion call,
parses the response against a fixed structured-findings schema, redacts
every LLM-sourced finding's own free-text fields, and forwards the combined
(static + LLM) finding list onward.

**Why here:** depends on Phase 2 (the framework), Phase 4 (consumes the
``Diff`` ``ingestion/diff.py`` already fetched, forwarded through
``review.static_analysis.StaticAnalysisOutput`` rather than re-fetched
here), and Phase 5 (consumes static-analysis findings as context, per the
PRD's US5) — not on Phase 7, whose own findings-merge-and-output step is
this step's *downstream* dependency, not the other way around.

**Protocol boundary:** this module binds ``llmgateway.client.LLMClient``
(unchanged, Phase 0) rather than ``adapter.llm`` directly — this is the
first real *caller* of that Protocol outside its own startup check
(``llmgateway/startup_check.py``). It also reuses Phase 5's
``findings.redact.TextRedactor`` Protocol and concrete wiring rather than
defining or wiring a second redaction mechanism — see
``findings/redact.py``'s own module docstring and Phase 5's
"Static-analysis-wide secret redaction" section.

**Import boundary, per US-F1 (``review/test_step_boundaries.py``):** this
module never imports ``review.static_analysis`` (its own upstream
dependency) directly, even though it depends on that step's output —
``_LLMReviewInput`` below mirrors ``review.static_analysis.StaticAnalysisOutput``'s
field shape structurally (``from_attributes=True``) rather than importing
that type, the same pattern ``review.static_analysis``'s own
``_StaticAnalysisInput`` already established for its own upstream
dependency (``review.prefilter.PrefilterOutput``).

**LLM-output secret redaction — reusing Phase 5's mechanism, not a second
one.** Gitleaks' own ``--redact`` flag (Phase 5) only affects Gitleaks' own
tool output — it says nothing about the LLM, which receives the same
unredacted diff as its prompt input and can echo a secret-shaped substring
back verbatim in its own finding prose. Uncaught, an LLM-sourced finding
quoting a live secret could be posted as a real PR review comment on the
customer's repo. Every ``source="llm"`` finding's ``message`` is redacted
before it is added to this step's own combined output — never only
Gitleaks' or Semgrep's findings. A redaction-call failure (subprocess
error, timeout, unparseable output) is **fatal** for this macro step, the
same fail-closed reasoning Phase 5 applies to its own call site — posting
an unredacted finding is a materially worse outcome than failing the one
job affected.

**Finding-count ceiling on the redaction loop** (``REDACTION_FINDING_CEILING``
below), closing the same unbounded-subprocess-spawn risk Phase 5 caps for
static-analysis findings, at a smaller ceiling: each redaction call spawns
its own Gitleaks subprocess, so an unbounded LLM-response finding count is
an unbounded subprocess-spawn multiplier bounded only by this step's own
timeout, not an explicit limit. Findings beyond the ceiling are dropped
entirely, not redacted and not returned — never posted or logged with
their raw text — logged at ``warning`` with the count dropped.

**Diff-size prompt ceiling.** The diff is capped at a fixed byte ceiling
(``max_diff_bytes``, sourced from ``WorkerSettings.llm_max_diff_bytes`` at
the composition root) before being included in the prompt — diff size is
fully PR-author-controlled and directly drives LLM token cost. A truncation
logs at ``warning`` with the original and truncated byte counts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from findings.models import Finding, Severity
from findings.redact import TextRedactor, apply_redaction_ceiling, redact_findings
from ingestion.diff import Diff
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, Message
from review.step import JobIdentity, Step, StepContext

logger = logging.getLogger(__name__)

# Combined across every `source: "llm"` finding a single completion returns —
# a smaller ceiling than Phase 5's own 50-finding static-analysis cap (see
# that module's own `REDACTION_FINDING_CEILING`), since a real code review
# has no legitimate reason to produce hundreds of individual findings from
# one LLM completion; a review with an implausibly large finding count is
# more likely a malformed/adversarial LLM response than a real one. Findings
# beyond this ceiling are dropped entirely, not redacted and not returned.
REDACTION_FINDING_CEILING = 20

_SYSTEM_PROMPT = (
    "You are an automated code review assistant reviewing a single pull "
    "request's unified diff. You will also be given the findings an earlier, "
    "deterministic static-analysis pass already produced for the same diff, "
    "as context — do not repeat those findings; focus on defects, risks, and "
    "quality issues static analysis would not catch.\n\n"
    "Respond with ONLY a single JSON object, no other text, matching exactly "
    "this shape:\n"
    '{"findings": [{"file": "<path>", "line": <int or null>, '
    '"severity": "critical" | "high" | "medium" | "low" | "info", '
    '"message": "<finding text>"}]}\n\n'
    'If you have no findings, respond with {"findings": []}.'
)


class LLMResponseParseError(Exception):
    """Raised when the LLM's completion response content does not parse as
    valid JSON matching the fixed structured-findings schema (invalid JSON,
    missing/extra-typed fields, an invalid ``severity`` value).

    Distinct from ``llmgateway.exceptions.LLMMalformedResponseError``: that
    one is raised by the concrete ``LLMClient`` implementation
    (``adapter/llm/litellm_client.py``) when the provider's own wire-level
    response shape is broken (no ``choices``, empty message content) —
    a transport/provider concern. This one is raised here, in the domain
    step, when the provider *did* return a well-formed completion but its
    own ``content`` string doesn't parse against this platform's structured
    findings schema — a prompt/schema-adherence concern specific to this
    step, not `llmgateway`'s.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(
            f"could not parse LLM response against the structured findings schema: {detail}"
        )
        self.detail = detail


class _LLMFindingItem(BaseModel):
    """One item of the LLM's own structured-findings JSON response — the
    fixed schema stated in ``_SYSTEM_PROMPT`` above."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int | None = None
    severity: Severity
    message: str


class _LLMFindingsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    findings: list[_LLMFindingItem]


class _LLMReviewInput(JobIdentity):
    """Mirrors ``review.static_analysis.StaticAnalysisOutput``'s own field
    shape without importing it — see the module docstring's "Import
    boundary" note. ``from_attributes=True`` lets ``model_validate`` read
    this shape off *any* object with matching attributes, including the
    real ``StaticAnalysisOutput`` instance the outer registry hands this
    step as its own ``input``.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    # Defaults to empty, not required, so this step can also validate off
    # ``review.prefilter.PrefilterOutput`` (which carries no ``findings`` of
    # its own) when the composition root wires ``depends_on="prefilter"``
    # because ``static_analysis`` is disabled — see ``LLMReviewStep.__init__``.
    findings: list[Finding] = Field(default_factory=list)


class LLMReviewOutput(JobIdentity):
    """This step's own output: the **combined** static-analysis findings
    (forwarded, already redacted by Phase 5) plus this step's own
    LLM-sourced findings (redacted here) — one single list, since
    ``review.pipeline.StepRegistry`` resolves a single dependency's output
    into the next step's own ``input`` (per ``review/step.py``'s own
    docstring), so Phase 7's findings-merge-and-output step (this step's
    own downstream dependency) receives everything it needs to dedupe/merge
    from this one ``input`` rather than needing two upstream dependencies
    the registry doesn't support.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    findings: list[Finding]
    head_sha: str


def _render_diff(diff: Diff) -> str:
    """Renders ``diff`` as the bare-diff text included in the LLM prompt —
    per-file header (path, status) plus unified-diff patch text, in file
    order. No AST parsing, no index — bare diff only, per the roadmap."""
    parts = []
    for file in diff.files:
        patch = file.patch if file.patch is not None else "(no patch available)"
        parts.append(f"--- {file.path} ({file.status})\n{patch}")
    return "\n\n".join(parts)


def _truncate_diff_text(diff_text: str, max_bytes: int) -> tuple[str, int, int]:
    """Caps ``diff_text`` at ``max_bytes`` UTF-8 bytes, returning
    ``(possibly-truncated text, original byte count, final byte count)``.
    Exactly ``max_bytes`` is not truncated (boundary is exclusive); one byte
    over is. ``errors="ignore"`` on the final decode drops a multi-byte
    UTF-8 character that the byte-boundary truncation may have split
    mid-sequence, rather than raising.
    """
    encoded = diff_text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return diff_text, len(encoded), len(encoded)
    truncated_text = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated_text, len(encoded), len(truncated_text.encode("utf-8"))


def _render_static_findings_context(findings: list[Finding]) -> str:
    """Renders the static-analysis findings supplied as prompt *context* —
    per US5, "supplying the bare diff plus the static-analysis findings as
    context" — as JSON, the same structured shape the LLM is itself asked
    to respond in."""
    return json.dumps([finding.model_dump(mode="json") for finding in findings])


def _build_user_message(diff_text: str, static_findings: list[Finding]) -> str:
    context_json = _render_static_findings_context(static_findings)
    return (
        f"Static-analysis findings (context, already reported — do not repeat):\n"
        f"{context_json}\n\n"
        f"Diff:\n{diff_text}"
    )


def _parse_llm_findings(content: str) -> list[Finding]:
    """Parses ``response.content`` against the fixed structured-findings
    schema, raising ``LLMResponseParseError`` (never an unhandled
    exception) on any malformed/unparseable response."""
    try:
        raw = json.loads(content)
        parsed = _LLMFindingsResponse.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise LLMResponseParseError(detail=str(exc)) from exc

    return [
        Finding(
            file=item.file,
            line=item.line,
            severity=item.severity,
            message=item.message,
            source="llm",
        )
        for item in parsed.findings
    ]


class LLMReviewStep:
    """``review.step.Step`` implementation for US5 — see this module's own
    docstring for the full design."""

    name = "llm_review"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        redactor: TextRedactor,
        model: str,
        timeout: float,
        max_diff_bytes: int,
        depends_on: str = "static_analysis",
    ) -> None:
        """``depends_on`` names this step's single upstream dependency —
        ``"static_analysis"`` by default (the ordinary, both-steps-enabled
        case), or ``"prefilter"`` when the composition root
        (``apps/worker/main.py``) determines ``static_analysis`` can never
        produce findings (disabled, or both its analyzer sub-flags are).

        **Why this is a constructor parameter, not a fixed class attribute
        (mirrors ``review.output.FindingsOutputStep``'s own fix for the
        symmetric problem — see that class's docstring):**
        ``review.pipeline.StepRegistry`` resolves this step's ``input`` from
        exactly its declared dependency's output; a disabled step never
        produces one. With a fixed ``depends_on=("static_analysis",)``,
        disabling ``static_analysis`` while leaving ``llm_review`` enabled —
        a configuration ``review.config.check_step_configuration_viable``
        explicitly treats as viable — left this step with no satisfiable
        dependency, and the registry's own generic "no defined behavior for
        that combination" ``RuntimeError`` failed every single review job.
        ``PrefilterOutput`` carries every field ``_LLMReviewInput`` needs
        except ``findings`` (which defaults to empty), so falling back to it
        is always safe.
        """
        self._llm_client = llm_client
        self._redactor = redactor
        self._model = model
        self._timeout = timeout
        self._max_diff_bytes = max_diff_bytes
        self.depends_on: tuple[str, ...] = (depends_on,)

    async def run(self, input: Any, context: StepContext) -> LLMReviewOutput:
        # Any (not _LLMReviewInput) because the registry passes the raw
        # upstream step output here for validation — it may be
        # StaticAnalysisOutput or, when static analysis produced no
        # findings, PrefilterOutput (see depends_on fallback above).
        # `model_validate(..., from_attributes=True)` below enforces the
        # actual shape at runtime.
        parsed = _LLMReviewInput.model_validate(input, from_attributes=True)

        diff_text = _render_diff(parsed.diff)
        truncated_text, original_bytes, final_bytes = _truncate_diff_text(
            diff_text, self._max_diff_bytes
        )
        if final_bytes < original_bytes:
            logger.warning(
                "llm-review: diff truncated to fit prompt byte ceiling",
                extra={
                    "job_id": context.job_id,
                    "ceiling_bytes": self._max_diff_bytes,
                    "original_bytes": original_bytes,
                    "truncated_bytes": final_bytes,
                },
            )

        request = CompletionRequest(
            model=self._model,
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=_build_user_message(truncated_text, parsed.findings)),
            ],
        )
        response = await self._llm_client.complete(request, timeout=self._timeout)
        llm_findings = _parse_llm_findings(response.content)

        surviving, dropped = apply_redaction_ceiling(llm_findings, REDACTION_FINDING_CEILING)
        if dropped:
            logger.warning(
                "llm-review redaction ceiling exceeded; dropping excess findings",
                extra={
                    "job_id": context.job_id,
                    "ceiling": REDACTION_FINDING_CEILING,
                    "dropped_count": dropped,
                },
            )

        redacted_llm_findings = await self._redact_all(surviving, context)

        return LLMReviewOutput(
            findings=[*parsed.findings, *redacted_llm_findings],
            org_id=parsed.org_id,
            repo_id=parsed.repo_id,
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
        )

    async def _redact_all(self, findings: list[Finding], context: StepContext) -> list[Finding]:
        """Redacts every ``source: "llm"`` finding's ``message`` field
        before it exists anywhere else in the pipeline, per the module
        docstring's "LLM-output secret redaction" note.

        The redact-fan-out-and-log mechanics (previously duplicated
        byte-for-byte from ``review/static_analysis.py``'s own
        ``_redact_all``) now live once in
        ``findings.redact.redact_findings`` — see that function's own
        docstring.
        """
        return await redact_findings(
            findings, self._redactor, job_id=context.job_id, log_prefix="llm-review"
        )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[Any, LLMReviewOutput]] = LLMReviewStep
