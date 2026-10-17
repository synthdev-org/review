"""review.static_analysis — the static-analysis macro pipeline step (US4,
US9), per the plan's Phase 5 section: runs Gitleaks and Semgrep as
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

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

from findings.models import SEVERITY_RANK, Finding
from findings.redact import TextRedactor, apply_redaction_ceiling, redact_findings
from ingestion.diff import Diff, DiffFetcher
from review._telemetry import suppress_telemetry_errors
from review.changed_lines import filter_to_changed_lines
from review.file_matchers import FileMatcher, match_all
from review.pipeline import FailurePolicy, StepRegistry
from review.step import JobIdentity, Step, StepContext

logger = logging.getLogger(__name__)

# OTel API only, per docs/adr/0011 — no SDK/exporter construction here.
_meter = metrics.get_meter(__name__)
_runs_counter = _meter.create_counter(
    "review.static_analysis.analyzer.runs_total",
    description="Analyzer sub-step runs, by analyzer_name and outcome.",
)
_duration_histogram = _meter.create_histogram(
    "review.static_analysis.analyzer.duration_ms",
    unit="ms",
    description="Time an analyzer spent running (excludes time queued for a slot).",
)
_slot_wait_histogram = _meter.create_histogram(
    "review.static_analysis.analyzer.slot_wait_ms",
    unit="ms",
    description="Time an analyzer waited for a concurrency slot.",
)
_config_fallback_counter = _meter.create_counter(
    "review.static_analysis.config_fallback_total",
    description="Jobs whose repo-config fetch failed and fell back to default profiles.",
)
_skipped_counter = _meter.create_counter(
    "review.static_analysis.analyzer.skipped_total",
    description="Analyzer runs skipped because no changed file matched its routing.",
)
_findings_counter = _meter.create_counter(
    "review.static_analysis.findings_total",
    description="Findings returned by successful analyzer runs, by analyzer_name.",
)
_findings_dropped_counter = _meter.create_counter(
    "review.static_analysis.findings_dropped_total",
    description="Findings discarded before redaction, by reason (and analyzer_name for "
    "not_changed_line).",
)
_config_skipped_counter = _meter.create_counter(
    "review.static_analysis.config_skipped_total",
    description="Engine runs that ignored the repo's config and used the default profile, "
    "by analyzer_name and config_skip_reason.",
)
_in_flight = _meter.create_up_down_counter(
    "review.static_analysis.analyzers.in_flight",
    description="Analyzers currently holding a concurrency slot.",
)

# Combined across both analyzers — the same unbounded-subprocess-spawn risk
# Phase 6 caps (at a smaller ceiling) for LLM-sourced findings: each
# redaction call spawns its own Gitleaks subprocess, so an unredacted,
# uncapped finding count is an unbounded subprocess-spawn multiplier. Higher
# than Phase 6's own 20-finding cap since static-analysis findings are
# typically more numerous in a real PR but still bounded. Findings beyond
# this ceiling are dropped entirely, not redacted and not returned — never
# posted or logged with their raw text, since this is a security mechanism.
REDACTION_FINDING_CEILING = 50


# Repo-config discovery bounds (spec Unit B): small, data-only files at the
# repository root, so a hostile PR cannot make the Worker fetch large blobs.
MAX_CONFIG_FILE_BYTES = 256 * 1024
MAX_CONFIG_FILES = 16


class RepoConfigFetchError(Exception):
    """The repo's analyzer config could not be fetched (truncated tree,
    unreachable GitHub, ...). Never fatal: the step degrades to default
    profiles. ``reason`` is the ``config_skip_reason`` log value.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RepoConfigFetcher(Protocol):
    """Fetches allow-listed analyzer config files from the repo root at one
    commit. Implemented by ``adapter/github/repo_config.py``. Only names in
    ``names`` are ever fetched (never a path a config or the PR names);
    oversize files and files beyond ``max_files`` are omitted. Raises
    ``RepoConfigFetchError`` on any fetch failure.
    """

    async def fetch_config_files(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        commit_sha: str,
        names: Collection[str],
        max_file_bytes: int,
        max_files: int,
    ) -> dict[str, bytes]: ...


@dataclass(frozen=True)
class ConfigOutcome:
    """Honored-vs-skipped bookkeeping shared by every engine: the
    ``config_source`` / ``config_skip_reason`` log fields.
    """

    source: str  # "repo" | "default"
    skip_reason: str | None = None

    def log_fields(self) -> dict[str, str]:
        fields = {"config_source": self.source}
        if self.skip_reason is not None:
            fields["config_skip_reason"] = self.skip_reason
        return fields

    def record(self, analyzer_name: str) -> None:
        """Counts a skipped repo config (no-op when the repo config was honored)."""
        if self.skip_reason is None:
            return
        with suppress_telemetry_errors(logger, "config skip counter failed"):
            _config_skipped_counter.add(
                1, {"analyzer_name": analyzer_name, "reason": self.skip_reason}
            )


class StaticAnalyzer(Protocol):
    """A single analyzer's own scan capability, per US4 — narrower than
    ``review.step.Step`` (see the module docstring for why). Implemented by
    ``adapter/analyzers/gitleaks.py``'s ``GitleaksAnalyzer`` and
    ``adapter/analyzers/semgrep.py``'s ``SemgrepAnalyzer``.
    """

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]: ...


@dataclass(frozen=True)
class AnalyzerRegistration:
    """One analyzer as the step sees it: ``matches`` routes files to it (it is
    skipped when none match), ``changed_lines_only`` limits its findings to
    added/modified lines, and ``security_class`` ranks its findings ahead of
    lint-class ones at equal severity under the combined ceiling (US-A10).
    Gitleaks and Semgrep register with the defaults below, so behave as before.
    """

    name: str
    analyzer: StaticAnalyzer
    matches: FileMatcher = match_all
    changed_lines_only: bool = False
    security_class: bool = False
    config_filenames: tuple[str, ...] = ()


def gitleaks_semgrep_registrations(
    gitleaks: StaticAnalyzer, semgrep: StaticAnalyzer
) -> list[AnalyzerRegistration]:
    return [
        AnalyzerRegistration("gitleaks", gitleaks, security_class=True),
        AnalyzerRegistration("semgrep", semgrep, security_class=True),
    ]


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
    Phase 6's llm-review step (this step's own downstream dependency)
    receives this as its own ``input``.

    ``diff`` is forwarded too (not part of this step's own output data, but
    passed through unchanged) — Phase 6's llm-review step builds its own
    LLM prompt from Phase 4's already-fetched bare diff rather than
    re-fetching it, per that phase's own "passed through StepContext/
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
    config_files: dict[str, bytes] = {}


class _AnalyzerSubStep:
    """Adapts one ``AnalyzerRegistration`` into a ``review.step.Step`` for
    registration into this macro step's own inner registry: routes files to
    the analyzer (skipping it when none match), runs it, then applies the
    changed-line filter when the registration asks for one. ``timeouts`` is
    looked up by analyzer name (resolved by ``StaticAnalysisStep``, which fills
    in the inherited default for analyzers without their own entry).
    """

    depends_on: tuple[str, ...] = ()

    def __init__(
        self, registration: AnalyzerRegistration, *, timeouts: Mapping[str, float]
    ) -> None:
        self.name = registration.name
        self._registration = registration
        self._timeouts = timeouts

    async def run(self, input: _AnalyzerSubStepInput, context: StepContext) -> list[Finding]:
        registration = self._registration
        content = {
            path: data for path, data in input.content.items() if registration.matches(path, data)
        }
        # Unrouted analyzers (Gitleaks/Semgrep) run even on empty content, as before.
        if not content and registration.matches is not match_all:
            logger.info(
                "analyzer skipped: no matching files",
                extra={"job_id": context.job_id, "analyzer_name": self.name},
            )
            with suppress_telemetry_errors(logger, "analyzer skipped counter failed"):
                _skipped_counter.add(1, {"analyzer_name": self.name})
            return []
        diff = input.diff
        if len(content) != len(input.content):
            diff = Diff(files=tuple(f for f in diff.files if f.path in content))

        start = time.monotonic()
        try:
            findings = await registration.analyzer.run(
                diff,
                content,
                timeout=self._timeouts[self.name],
                # Only this engine's own allow-listed names, never the rest.
                config_files={
                    name: input.config_files[name]
                    for name in registration.config_filenames
                    if name in input.config_files
                },
            )
        finally:
            with suppress_telemetry_errors(logger, "analyzer duration recording failed"):
                _duration_histogram.record(
                    int((time.monotonic() - start) * 1000), {"analyzer_name": self.name}
                )

        if registration.changed_lines_only:
            findings, filtered = filter_to_changed_lines(
                findings, {f.path: f.patch for f in input.diff.files}
            )
            with suppress_telemetry_errors(logger, "findings dropped counter failed"):
                _findings_dropped_counter.add(
                    filtered, {"reason": "not_changed_line", "analyzer_name": self.name}
                )
            logger.info(
                "analyzer findings filtered to changed lines",
                extra={
                    "job_id": context.job_id,
                    "analyzer_name": self.name,
                    "filtered_count": filtered,
                },
            )
        return findings


class StaticAnalysisStep:
    """``review.step.Step`` implementation for US4/US9 — see this module's
    own docstring for the full design.

    ``analyzer_slots`` is the Worker-process-wide semaphore bounding
    simultaneously running analyzers (US-A8), created by the composition
    root. A slot is taken *before* an analyzer's timed region (via the inner
    registry's ``admission`` hook), so queue time never consumes its timeout.
    """

    name = "static_analysis"
    depends_on: tuple[str, ...] = ("prefilter",)

    def __init__(
        self,
        diff_fetcher: DiffFetcher,
        *,
        registrations: Sequence[AnalyzerRegistration],
        redactor: TextRedactor,
        enabled: Mapping[str, bool],
        timeouts: Mapping[str, float],
        analyzer_slots: asyncio.Semaphore | None = None,
        config_fetcher: RepoConfigFetcher | None = None,
    ) -> None:
        self._diff_fetcher = diff_fetcher
        self._config_fetcher = config_fetcher
        self._registrations = list(registrations)
        self._security_sources = frozenset(r.name for r in self._registrations if r.security_class)
        self._redactor = redactor
        self._enabled = dict(enabled)
        self._timeouts = dict(timeouts)
        self._analyzer_slots = analyzer_slots

        default_timeout = self._timeouts.get("static_analysis_analyzer")
        analyzer_timeouts: dict[str, float] = {}
        for registration in self._registrations:
            timeout = self._timeouts.get(registration.name, default_timeout)
            if timeout is None:
                raise ValueError(f"no timeout configured for analyzer {registration.name!r}")
            analyzer_timeouts[registration.name] = timeout

        sub_steps: list[Step[Any, Any]] = [
            _AnalyzerSubStep(registration, timeouts=analyzer_timeouts)
            for registration in self._registrations
        ]
        self._inner_registry = StepRegistry(
            sub_steps,
            enabled=self._enabled,
            timeouts=analyzer_timeouts,
            on_failure=FailurePolicy.ISOLATE,
            # Distinct from the outer per-job root span's default
            # "review.job.run" name — this inner registry's seed_input is
            # a static-analysis-specific input, not a job identity, so its
            # span must not collide with (and be mistaken for) the real
            # one-per-job root span the catalog expects.
            root_span_name="review.step.static_analysis.run",
            admission=self._admit if analyzer_slots is not None else None,
        )

    @asynccontextmanager
    async def _admit(self, step: Step[Any, Any]) -> AsyncIterator[None]:
        assert self._analyzer_slots is not None
        attributes = {"analyzer_name": step.name}
        waited_from = time.monotonic()
        async with self._analyzer_slots:
            with suppress_telemetry_errors(logger, "analyzer slot telemetry failed"):
                _slot_wait_histogram.record(
                    int((time.monotonic() - waited_from) * 1000), attributes
                )
                _in_flight.add(1, attributes)
            try:
                yield
            finally:
                with suppress_telemetry_errors(logger, "analyzer slot telemetry failed"):
                    _in_flight.add(-1, attributes)

    async def run(self, input: Any, context: StepContext) -> StaticAnalysisOutput:
        # Any (not _StaticAnalysisInput) because the registry passes the
        # raw upstream step output here for validation; typing it as Any
        # matches the Step Protocol's generic `run(input: InputT, ...)`
        # boundary while `model_validate(..., from_attributes=True)` below
        # is what actually enforces the shape at runtime.
        parsed = _StaticAnalysisInput.model_validate(input, from_attributes=True)

        pre_filtered_paths = [file.path for file in parsed.diff.files]
        content = await self._diff_fetcher.fetch_content(
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            diff=parsed.diff,
            paths=pre_filtered_paths,
        )

        config_files = await self._fetch_config_files(parsed, content, context)
        outcomes = await self._run_analyzer_sub_steps(parsed.diff, content, config_files, context)

        findings: list[Finding] = []
        for registration in self._registrations:
            outcome = outcomes.get(registration.name)
            if outcome is not None and outcome.status == "success":
                findings.extend(outcome.output)

        # US-A10: the ceiling drops from the tail, so order severity first,
        # then security-class sources ahead of lint-class (stable sort).
        findings.sort(
            key=lambda f: (
                SEVERITY_RANK[f.severity],
                0 if f.source in self._security_sources else 1,
            )
        )

        surviving, dropped = apply_redaction_ceiling(findings, REDACTION_FINDING_CEILING)
        if dropped:
            with suppress_telemetry_errors(logger, "findings dropped counter failed"):
                _findings_dropped_counter.add(dropped, {"reason": "redaction_ceiling"})
            logger.warning(
                "static-analysis redaction ceiling exceeded; dropping excess findings",
                extra={
                    "job_id": context.job_id,
                    "ceiling": REDACTION_FINDING_CEILING,
                    "dropped_count": dropped,
                },
            )

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

    async def _fetch_config_files(
        self, parsed: _StaticAnalysisInput, content: dict[str, bytes], context: StepContext
    ) -> dict[str, bytes]:
        """One fetch per job, and only when an enabled engine with config
        names has files routed to it. Any failure degrades to no config.
        """
        names = {
            name
            for registration in self._registrations
            if registration.config_filenames
            and self._enabled.get(registration.name, True)
            and any(registration.matches(path, data) for path, data in content.items())
            for name in registration.config_filenames
        }
        if self._config_fetcher is None or not names:
            return {}
        try:
            files = await self._config_fetcher.fetch_config_files(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                commit_sha=parsed.head_sha,
                names=names,
                max_file_bytes=MAX_CONFIG_FILE_BYTES,
                max_files=MAX_CONFIG_FILES,
            )
        except Exception as exc:
            # Degrade-by-design: an unexpected fetcher error (timeout, transport
            # failure the adapter did not wrap) must not fail the whole step.
            reason = exc.reason if isinstance(exc, RepoConfigFetchError) else "config_fetch_failed"
            outcome = ConfigOutcome("default", reason)
            with suppress_telemetry_errors(logger, "config fallback counter failed"):
                _config_fallback_counter.add(1, {"reason": reason})
            logger.warning(
                "analyzer repo config fetch failed; using default profiles",
                extra={"job_id": context.job_id, **outcome.log_fields()},
            )
            return {}
        logger.info(
            "analyzer repo config fetched",
            extra={"job_id": context.job_id, "config_files_fetched": len(files)},
        )
        return files

    async def _run_analyzer_sub_steps(
        self,
        diff: Diff,
        content: dict[str, bytes],
        config_files: dict[str, bytes],
        context: StepContext,
    ) -> Mapping[str, Any]:
        sub_step_input = _AnalyzerSubStepInput(
            diff=diff, content=content, config_files=config_files
        )
        outcomes = await self._inner_registry.run(sub_step_input, context)
        self._log_analyzer_outcomes(outcomes, context)
        return outcomes

    def _log_analyzer_outcomes(self, outcomes: Mapping[str, Any], context: StepContext) -> None:
        """Extends Phase 2's generic per-step start/completion/failure
        logging (``job_id``/``step_name``/``duration_ms``/``status``,
        already emitted by ``review.pipeline.StepRegistry`` itself for
        every sub-step) with the analyzer-specific fields the plan's
        Observability section calls for: ``analyzer_name`` and, for a
        successfully-completed analyzer, its own ``finding_count`` — fields
        the generic framework can't emit itself, since ``review.pipeline``
        never imports ``findings.models.Finding`` and has no concept of
        "analyzer" (per this module's own Import boundary/Protocol-boundary
        notes).

        A failed or timed-out analyzer sub-step logs here at ``error`` —
        per the plan's own wording, "a confirmed defect in the tool
        invocation, not merely absorbed noise", even though it's isolated
        and doesn't fail this macro step — distinct from (and in addition
        to) the generic ``warning``-level "step isolated: failed" line the
        inner registry already logs for every ``ISOLATE``-policy failure
        regardless of which concrete sub-step it was.
        """
        for name, outcome in outcomes.items():
            with suppress_telemetry_errors(logger, "analyzer run counter failed"):
                _runs_counter.add(1, {"analyzer_name": name, "outcome": outcome.status})
            if outcome.status == "success":
                with suppress_telemetry_errors(logger, "analyzer findings counter failed"):
                    _findings_counter.add(len(outcome.output), {"analyzer_name": name})
                logger.info(
                    "analyzer sub-step findings",
                    extra={
                        "job_id": context.job_id,
                        "analyzer_name": name,
                        "status": outcome.status,
                        "finding_count": len(outcome.output),
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
