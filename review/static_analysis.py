"""review.static_analysis — the static-analysis macro pipeline step (US4,
US9): runs Gitleaks and Semgrep as
independent, concurrent sub-steps against pre-filtered file content,
normalizes their output into ``findings.models.Finding`` objects, and
redacts every finding's own free-text fields before returning them.

**Two nested registries, per pipeline-step-framework.md's own design** (see
``review/pipeline.py``'s module docstring): this module is one *macro* step
registered into the outer, job-level registry (``apps/worker/main.py``), and
internally builds a *second*, inner ``review.pipeline.StepRegistry``
instance over its own two analyzer sub-steps
(``static_analysis_gitleaks_enabled``/``static_analysis_semgrep_enabled``,
per US-F4) — the same class serves both levels; only ``on_failure`` differs
(``FailurePolicy.ISOLATE`` here, per US-F5: a raising or timed-out analyzer
sub-step is logged and excluded, never fatal to this macro step, so the
review still completes using the other analyzer's findings).

**Protocol boundary:** ``StaticAnalyzer`` (defined here, the consumer, per
architecture.md) is a narrower Protocol than ``review.step.Step`` —
``run(diff, content, *, timeout) -> list[Finding]`` — because a concrete
analyzer (``adapter/analyzers/gitleaks.py``, ``semgrep.py``) needs its own
explicit timeout value to bound its own subprocess call directly
(``adapter/analyzers/subprocess_runner.py``'s kill-and-reap), not only the
inner registry's own ``asyncio.wait_for`` wrapping — belt-and-braces
subprocess-level enforcement, since only the former actually kills the
underlying OS process. ``_AnalyzerSubStep`` (private to this module) adapts
each ``StaticAnalyzer`` into a ``review.step.Step`` for registration into
the inner registry.

**Import boundary, per US-F1 (``review/test_step_boundaries.py``):** this
module never imports ``review.prefilter`` (its own upstream dependency)
directly, even though it depends on that step's output — a step module may
only depend on ``review.step``'s ``Step`` Protocol and the pydantic models
passed between steps, never a sibling step module. ``_StaticAnalysisInput``
below mirrors ``review.prefilter.PrefilterOutput``'s field shape
structurally (``from_attributes=True``) rather than importing that type, so
``model_validate`` can read the real ``PrefilterOutput`` instance the outer
registry hands this step as its own ``input`` with no import needed.

**Zero infra imports, zero filesystem I/O of its own** (this step passes
``content: dict[str, bytes]`` data across the ``StaticAnalyzer`` Protocol
boundary, never a filesystem path — materializing that data to disk is
``adapter/analyzers/subprocess_runner.py``'s job, an adapter concern) — this
module never imports ``adapter.analyzers`` directly, only the
``StaticAnalyzer``/``findings.redact.TextRedactor`` Protocols; concrete
implementations are wired in only at the composition root
(``apps/worker/main.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

from findings.models import Finding
from findings.redact import TextRedactor, apply_redaction_ceiling, redact_findings
from ingestion.diff import Diff, DiffFetcher
from review._telemetry import suppress_telemetry_errors
from review.pipeline import FailurePolicy, StepRegistry
from review.step import JobIdentity, Step, StepContext

logger = logging.getLogger(__name__)

# Catalog: docs/specs/spec-observability.md. `analyzer` is a registry key (closed set),
# `status` ∈ {success, failed, timeout, skipped_oversize}. Analyzer failures are isolated,
# so without this a broken analyzer is visible only in ERROR logs.
_analyzer_outcomes = metrics.get_meter("review.static_analysis").create_counter(
    "review.static_analysis.analyzer_total",
    description="Static-analyzer sub-step outcomes, by analyzer and status.",
)

# Combined across both analyzers — the same unbounded-subprocess-spawn risk
# the LLM review step caps (at a smaller ceiling) for LLM-sourced findings: each
# redaction call spawns its own Gitleaks subprocess, so an unredacted,
# uncapped finding count is an unbounded subprocess-spawn multiplier. Higher
# than the LLM review step's own 20-finding cap since static-analysis findings are
# typically more numerous in a real PR but still bounded. Findings beyond
# this ceiling are dropped entirely, not redacted and not returned — never
# posted or logged with their raw text, since this is a security mechanism.
REDACTION_FINDING_CEILING = 50


class AnalyzerOversize(Exception):
    """Raised by an analyzer that refuses input over its hard size bound. It
    is isolated like any analyzer failure but logged as ``skipped_oversize``
    (a bound working as designed), not as a tool defect.
    """


class StaticAnalyzer(Protocol):
    """A single analyzer's own scan capability, per US4 — narrower than
    ``review.step.Step`` (see the module docstring for why). Implemented by
    ``adapter/analyzers/gitleaks.py``'s ``GitleaksAnalyzer`` and
    ``adapter/analyzers/semgrep.py``'s ``SemgrepAnalyzer``.
    """

    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        raise NotImplementedError


class _StaticAnalysisInput(JobIdentity):
    """Mirrors ``review.prefilter.PrefilterOutput``'s own field shape
    without importing it — see the module docstring's "Import boundary"
    note. ``from_attributes=True`` lets ``model_validate`` read this shape
    off *any* object with matching attributes, including the real
    ``PrefilterOutput`` instance the outer registry hands this step as its
    own ``input``.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    diff: Diff


class StaticAnalysisOutput(JobIdentity):
    """This step's own output: the normalized, redacted findings plus the
    same job-identifying context ``_StaticAnalysisInput`` carried, forwarded
    onward the same way ``review.prefilter.PrefilterOutput`` forwards it —
    The LLM review step (this step's own downstream dependency)
    receives this as its own ``input``.

    ``diff`` is forwarded too (not part of this step's own output data, but
    passed through unchanged) — the LLM review step builds its own
    LLM prompt from the bare diff the prefilter step already fetched rather than
    re-fetching it, per that step's own "passed through StepContext/
    pipeline wiring — not re-fetched here" design; since
    ``review.pipeline.StepRegistry`` resolves a single dependency's *output*
    into the next step's own ``input`` (per ``review/step.py``'s own
    docstring), the diff has to ride along on this step's output the same
    way it already rides along on ``review.prefilter.PrefilterOutput``.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    diff: Diff
    findings: list[Finding]


class _AnalyzerSubStepInput(BaseModel):
    """The one bundled object passed as ``input`` to every analyzer
    sub-step's ``Step.run()`` call — ``review.pipeline.StepRegistry``'s own
    single-argument ``input`` design (per ``review/step.py``) needs both
    ``diff`` and ``content`` bundled together, since a ``StaticAnalyzer``
    Protocol implementation needs both.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    diff: Diff
    content: dict[str, bytes]


class _AnalyzerSubStep:
    """Adapts one ``StaticAnalyzer`` into a ``review.step.Step`` for
    registration into this macro step's own inner registry. ``timeouts`` is
    the same full ``Mapping[str, float]`` the outer macro-step registry
    already reads its own timeouts from (``review.config.StepTimeouts.as_mapping()``
    — it already carries ``"gitleaks"``/``"semgrep"`` keys, both mapped to
    the shared ``static_analysis_analyzer_timeout_seconds`` value, per that
    model's own docstring) — this sub-step looks up its own entry by name
    rather than needing a second, narrower mapping threaded through
    separately.
    """

    depends_on: tuple[str, ...] = ()

    def __init__(
        self, name: str, analyzer: StaticAnalyzer, *, timeouts: Mapping[str, float]
    ) -> None:
        self.name = name
        self._analyzer = analyzer
        self._timeouts = timeouts

    async def run(self, step_input: _AnalyzerSubStepInput, context: StepContext) -> list[Finding]:
        return await self._analyzer.run(
            step_input.diff, step_input.content, timeout=self._timeouts[self.name]
        )


class StaticAnalysisStep:
    """``review.step.Step`` implementation for US4/US9 — see this module's
    own docstring for the full design.
    """

    name = "static_analysis"
    depends_on: tuple[str, ...] = ("prefilter",)

    def __init__(
        self,
        diff_fetcher: DiffFetcher,
        *,
        gitleaks_analyzer: StaticAnalyzer,
        semgrep_analyzer: StaticAnalyzer,
        extra_analyzers: Mapping[str, StaticAnalyzer] | None = None,
        redactor: TextRedactor,
        enabled: Mapping[str, bool],
        timeouts: Mapping[str, float],
    ) -> None:
        self._diff_fetcher = diff_fetcher
        self._analyzers: list[tuple[str, StaticAnalyzer]] = [
            ("gitleaks", gitleaks_analyzer),
            ("semgrep", semgrep_analyzer),
            *(extra_analyzers or {}).items(),
        ]
        self._redactor = redactor
        self._enabled = dict(enabled)
        self._timeouts = dict(timeouts)

        sub_steps: list[Step[Any, Any]] = [
            _AnalyzerSubStep(name, analyzer, timeouts=self._timeouts)
            for name, analyzer in self._analyzers
        ]
        self._inner_registry = StepRegistry(
            sub_steps,
            enabled=self._enabled,
            timeouts=self._timeouts,
            on_failure=FailurePolicy.ISOLATE,
            # Distinct from the outer per-job root span's default
            # "review.job.run" name — this inner registry's seed_input is
            # a static-analysis-specific input, not a job identity, so its
            # span must not collide with (and be mistaken for) the real
            # one-per-job root span the catalog expects.
            root_span_name="review.step.static_analysis.run",
        )

    async def run(self, step_input: Any, context: StepContext) -> StaticAnalysisOutput:
        # Any (not _StaticAnalysisInput) because the registry passes the
        # raw upstream step output here for validation; typing it as Any
        # matches the Step Protocol's generic `run(input: InputT, ...)`
        # boundary while `model_validate(..., from_attributes=True)` below
        # is what actually enforces the shape at runtime.
        parsed = _StaticAnalysisInput.model_validate(step_input, from_attributes=True)

        pre_filtered_paths = [file.path for file in parsed.diff.files]
        content = await self._diff_fetcher.fetch_content(
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            diff=parsed.diff,
            paths=pre_filtered_paths,
        )

        outcomes = await self._run_analyzer_sub_steps(parsed.diff, content, context)

        findings: list[Finding] = []
        for name, _analyzer in self._analyzers:
            outcome = outcomes.get(name)
            if outcome is not None and outcome.status == "success":
                findings.extend(outcome.output)

        surviving, dropped = apply_redaction_ceiling(findings, REDACTION_FINDING_CEILING)
        if dropped:
            self._warn_ceiling(context, dropped)

        redacted_findings = await self._redact_all(surviving, context)

        return StaticAnalysisOutput(
            diff=parsed.diff,
            findings=redacted_findings,
            org_id=parsed.org_id,
            repo_id=parsed.repo_id,
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
            base_sha=parsed.base_sha,
            pr_title=parsed.pr_title,
            pr_head_ref=parsed.pr_head_ref,
            pr_issue_keys=parsed.pr_issue_keys,
            pr_is_fork=parsed.pr_is_fork,
            private=parsed.private,
        )

    @staticmethod
    def _warn_ceiling(context: StepContext, dropped: int) -> None:
        logger.warning(
            "static-analysis redaction ceiling exceeded; dropping excess findings",
            extra={
                "job_id": context.job_id,
                "ceiling": REDACTION_FINDING_CEILING,
                "dropped_count": dropped,
            },
        )

    async def _run_analyzer_sub_steps(
        self, diff: Diff, content: dict[str, bytes], context: StepContext
    ) -> Mapping[str, Any]:
        sub_step_input = _AnalyzerSubStepInput(diff=diff, content=content)
        outcomes = await self._inner_registry.run(sub_step_input, context)
        self._log_analyzer_outcomes(outcomes, context)
        return outcomes

    # Extends the pipeline's generic per-step start/completion/failure
    # logging (``job_id``/``step_name``/``duration_ms``/``status``,
    # already emitted by ``review.pipeline.StepRegistry`` itself for
    # every sub-step) with the analyzer-specific fields
    # ``analyzer_name`` and, for a
    # successfully-completed analyzer, its own ``finding_count`` — fields
    # the generic framework can't emit itself, since ``review.pipeline``
    # never imports ``findings.models.Finding`` and has no concept of
    # "analyzer" (per this module's own Import boundary/Protocol-boundary
    # notes).
    #
    # A failed or timed-out analyzer sub-step logs here at ``error`` —
    # because it is "a confirmed defect in the tool
    # invocation, not merely absorbed noise", even though it's isolated
    # and doesn't fail this macro step — distinct from (and in addition
    # to) the generic ``warning``-level "step isolated: failed" line the
    # inner registry already logs for every ``ISOLATE``-policy failure
    # regardless of which concrete sub-step it was.
    def _log_analyzer_outcomes(self, outcomes: Mapping[str, Any], context: StepContext) -> None:
        for name, outcome in outcomes.items():
            status = (
                "skipped_oversize"
                if isinstance(outcome.error, AnalyzerOversize)
                else outcome.status
            )
            if status in ("success", "skipped_oversize", "failed", "timeout"):
                with suppress_telemetry_errors(logger, "analyzer metric recording failed"):
                    _analyzer_outcomes.add(1, {"analyzer": name, "status": status})
            if outcome.status == "success":
                logger.info(
                    "analyzer sub-step findings",
                    extra={
                        "job_id": context.job_id,
                        "analyzer_name": name,
                        "status": outcome.status,
                        "finding_count": len(outcome.output),
                    },
                )
            elif isinstance(outcome.error, AnalyzerOversize):
                logger.warning(
                    "analyzer sub-step skipped: input over size bound",
                    extra={
                        "job_id": context.job_id,
                        "analyzer_name": name,
                        "status": "skipped_oversize",
                        "error": str(outcome.error),
                    },
                )
            elif outcome.status in ("failed", "timeout"):
                logger.error(
                    "analyzer sub-step failed",
                    extra={
                        "job_id": context.job_id,
                        "analyzer_name": name,
                        "status": outcome.status,
                        "error": str(outcome.error),
                    },
                )

    async def _redact_all(self, findings: list[Finding], context: StepContext) -> list[Finding]:
        """Redacts every surviving finding's ``message`` field, per the
        module docstring's "Static-analysis-wide secret redaction" note.
        Applied uniformly to **either** analyzer's findings, not only
        Semgrep's, giving defense-in-depth over Gitleaks' own ``--redact``
        too. A redaction failure is fatal for this macro step.

        The redact-fan-out-and-log mechanics (previously duplicated
        byte-for-byte in ``review/review_generation.py``'s own ``_redact_all``) now
        live once in ``findings.redact.redact_findings`` — see that
        function's own docstring.
        """
        return await redact_findings(
            findings, self._redactor, job_id=context.job_id, log_prefix="static-analysis"
        )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[Any, StaticAnalysisOutput]] = StaticAnalysisStep
