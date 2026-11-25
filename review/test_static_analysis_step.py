"""Unit tests for the static-analysis macro step's own *inner* registry —
the second of the framework's "two nested levels" (see
review/pipeline.py's module docstring): the same ``review.pipeline.StepRegistry``
class, constructed with ``FailurePolicy.ISOLATE`` instead of ``FATAL``, for
analyzer sub-steps that don't depend on each other's output.

Created in this phase against a generic fake sub-step registered purely
for test purposes, since ``review/static_analysis.py`` (the real
static-analysis macro step, wrapping this same mechanism around real
Gitleaks/Semgrep sub-steps) doesn't exist until Phase 5 — Phase 5 extends
this same file with its own additional real-analyzer cases rather than
duplicating it.

Covers US-F3 (sub-step registration), US-F4 (a disabled sub-step is never
invoked, and the remaining enabled sub-steps still complete the run), and
US-F5 (a raising *or* timed-out sub-step is isolated — logged and excluded,
never fatal to the run) — matching docs/specs/pipeline-step-framework.md's
own Testing table.

Unit-tier only in Phase 2 (a fake sub-step, no real binaries) — Phase 5
(below) extends this same file with real-binary Integration-tier cases for
US4/US9, per that phase's own "Marker note": ``@pytest.mark.integration``
is applied per-test to Phase 5's own additions specifically, not via a
module-level ``pytestmark``, since this file now straddles both tiers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.gitleaks import GitleaksAnalyzer
from adapter.analyzers.semgrep import SemgrepAnalyzer
from adapter.telemetry.conftest import (
    reset_otel_global_providers,  # noqa: F401
)
from findings.models import Finding, Source
from findings.redact import TextRedactor
from ingestion.diff import Diff, DiffFetcher, DiffFile
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.static_analysis import REDACTION_FINDING_CEILING, StaticAnalysisStep
from review.step import StepContext


@pytest.fixture(autouse=True)
def _reenable_static_analysis_logger() -> None:
    """Alembic's `env.py` (triggered by any `migrated_engine`-using test
    elsewhere in the same `pytest -m integration` process) disables every
    already-created logger not listed in `alembic.ini`, including this
    module's own `review.static_analysis` logger. Re-enabled here, autouse,
    so every `caplog`-asserting case below (Phase 5's real-binary
    Integration-tier additions) stays deterministic regardless of test
    order — mirroring `apps/webhook_api/test_webhook_routes.py`'s own
    per-test version of this same fix. Harmless for this file's own
    Unit-tier cases too (they never assert on `caplog`).
    """
    logging.getLogger("review.static_analysis").disabled = False


@dataclass
class _FakeSubStep:
    """A generic, in-memory fake analyzer sub-step — see
    review/test_step_registry.py's ``_FakeStep`` for the same shape at the
    macro level; duplicated here (not imported) so this file stays
    self-contained the same way `apps/worker`'s own test files do, per
    that module's own precedent."""

    name: str
    depends_on: tuple[str, ...] = ()
    delay_seconds: float = 0.0
    error: Exception | None = None
    calls: list[Any] = field(default_factory=list)

    async def run(self, input: Any, context: StepContext) -> list[str]:
        self.calls.append(input)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return [f"finding-from-{self.name}"]


def _context() -> StepContext:
    return StepContext(job_id="job-1")


def _all_enabled(*names: str) -> dict[str, bool]:
    return dict.fromkeys(names, True)


def _uniform_timeouts(seconds: float, *names: str) -> dict[str, float]:
    return dict.fromkeys(names, seconds)


# --- US-F3: a new sub-step registers with zero diff to existing ones -------


@pytest.mark.asyncio
async def test_new_sub_step_surfaces_in_merged_output_with_no_change_to_existing_ones() -> None:
    gitleaks = _FakeSubStep(name="gitleaks")
    semgrep = _FakeSubStep(name="semgrep")
    # A hypothetical new analyzer sub-step — a new registry entry only.
    new_analyzer = _FakeSubStep(name="new_analyzer")

    registry = StepRegistry(
        [gitleaks, semgrep, new_analyzer],
        enabled=_all_enabled("gitleaks", "semgrep", "new_analyzer"),
        timeouts=_uniform_timeouts(5, "gitleaks", "semgrep", "new_analyzer"),
        on_failure=FailurePolicy.ISOLATE,
    )

    outcomes = await registry.run(seed_input={"file.py": b"contents"}, context=_context())

    merged = [outcome.output[0] for outcome in outcomes.values() if outcome.status == "success"]
    assert "finding-from-gitleaks" in merged
    assert "finding-from-semgrep" in merged
    assert "finding-from-new_analyzer" in merged


# --- US-F4: a disabled sub-step is never invoked; the rest still complete --


@pytest.mark.asyncio
async def test_disabled_sub_step_is_never_invoked() -> None:
    gitleaks = _FakeSubStep(name="gitleaks")
    semgrep = _FakeSubStep(name="semgrep")

    registry = StepRegistry(
        [gitleaks, semgrep],
        enabled={"gitleaks": True, "semgrep": False},
        timeouts=_uniform_timeouts(5, "gitleaks", "semgrep"),
        on_failure=FailurePolicy.ISOLATE,
    )

    outcomes = await registry.run(seed_input={}, context=_context())

    assert semgrep.calls == []
    assert outcomes["semgrep"].status == "disabled"
    assert outcomes["gitleaks"].status == "success"


# --- US-F5: a raising or timed-out sub-step is isolated, never fatal -------


@pytest.mark.asyncio
async def test_raising_sub_step_is_isolated_others_still_appear() -> None:
    failing = _FakeSubStep(name="gitleaks", error=RuntimeError("parser crash"))
    healthy = _FakeSubStep(name="semgrep")

    registry = StepRegistry(
        [failing, healthy],
        enabled=_all_enabled("gitleaks", "semgrep"),
        timeouts=_uniform_timeouts(5, "gitleaks", "semgrep"),
        on_failure=FailurePolicy.ISOLATE,
    )

    # Isolation means run() completes normally — no exception propagates.
    outcomes = await registry.run(seed_input={}, context=_context())

    assert outcomes["gitleaks"].status == "failed"
    assert outcomes["semgrep"].status == "success"
    assert outcomes["semgrep"].output == ["finding-from-semgrep"]


@pytest.mark.asyncio
async def test_sub_step_exceeding_its_timeout_is_isolated_others_still_appear() -> None:
    slow = _FakeSubStep(name="gitleaks", delay_seconds=0.3)
    healthy = _FakeSubStep(name="semgrep")

    registry = StepRegistry(
        [slow, healthy],
        enabled=_all_enabled("gitleaks", "semgrep"),
        timeouts={"gitleaks": 0.05, "semgrep": 5},
        on_failure=FailurePolicy.ISOLATE,
    )

    outcomes = await registry.run(seed_input={}, context=_context())

    assert outcomes["gitleaks"].status == "timeout"
    assert outcomes["semgrep"].status == "success"
    assert outcomes["semgrep"].output == ["finding-from-semgrep"]


@pytest.mark.asyncio
async def test_every_sub_step_failing_still_completes_the_run_with_no_findings() -> None:
    """The registry itself never treats "zero surviving sub-steps" as
    fatal — whether that's an acceptable outcome for the *static-analysis
    step's own* merged result is Phase 5's concern, not this framework's."""
    first = _FakeSubStep(name="gitleaks", error=RuntimeError("boom"))
    second = _FakeSubStep(name="semgrep", error=RuntimeError("also boom"))

    registry = StepRegistry(
        [first, second],
        enabled=_all_enabled("gitleaks", "semgrep"),
        timeouts=_uniform_timeouts(5, "gitleaks", "semgrep"),
        on_failure=FailurePolicy.ISOLATE,
    )

    outcomes = await registry.run(seed_input={}, context=_context())

    assert outcomes["gitleaks"].status == "failed"
    assert outcomes["semgrep"].status == "failed"


# ============================================================================
# Phase 5: review.static_analysis.StaticAnalysisStep — real analyzer binaries
# on fixtures, per testing.md's "static-analyzer adapter against actual
# binaries on fixtures" convention and the PRD's own Testing table. Every
# case below is Integration-tier (@pytest.mark.integration applied
# per-test, not via a module-level pytestmark — see this file's own
# docstring for why).
# ============================================================================

_TESTDATA_DIR = Path(__file__).parent / "testdata"


def _load_testdata(*filenames: str) -> dict[str, bytes]:
    return {name: (_TESTDATA_DIR / name).read_bytes() for name in filenames}


def _diff_for(*filenames: str) -> Diff:
    return Diff(
        files=tuple(
            DiffFile(
                path=name,
                status="added",
                mode="100644",
                blob_sha="f" * 40,
                blob_size_bytes=len((_TESTDATA_DIR / name).read_bytes()),
            )
            for name in filenames
        )
    )


@dataclass
class _FakePrefilterOutput:
    """Structurally mirrors ``review.prefilter.PrefilterOutput`` (never
    imported here — see ``review/static_analysis.py``'s own "Import
    boundary" note) closely enough for ``_StaticAnalysisInput.model_validate(...,
    from_attributes=True)`` to read it."""

    diff: Diff
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"
    base_sha: str | None = None
    pr_title: str | None = None
    pr_head_ref: str | None = None
    pr_issue_keys: tuple[str, ...] = ()
    pr_is_fork: bool | None = None
    private: bool = False


class _FakePrefilterStep:
    """Stands in for ``review.prefilter.PrefilterStep`` so
    ``StaticAnalysisStep`` (which declares ``depends_on = ("prefilter",)``)
    can be registered into a real, fully-wired outer ``StepRegistry`` — the
    only way to observe ``PipelineFailedError`` propagate, since that's
    raised by the registry wrapping a step's ``run()`` call, not by the step
    itself.
    """

    name = "prefilter"
    depends_on: tuple[str, ...] = ()

    def __init__(self, output: _FakePrefilterOutput) -> None:
        self._output = output

    async def run(self, input: Any, context: StepContext) -> _FakePrefilterOutput:
        return self._output


class _FakeDiffFetcher:
    """A minimal ``DiffFetcher`` fake: ``fetch_content`` returns exactly the
    already-loaded testdata bytes it was constructed with, regardless of the
    ``paths`` it's asked for — ``fetch()`` is never called by
    ``StaticAnalysisStep`` (Phase 4's own pre-filter step is the only
    ``fetch()`` caller), so it's left unimplemented here.
    """

    def __init__(self, content: dict[str, bytes]) -> None:
        self._content = content

    async def fetch(self, **kwargs: Any) -> Diff:
        raise NotImplementedError("StaticAnalysisStep never calls DiffFetcher.fetch()")

    async def fetch_content(self, **kwargs: Any) -> dict[str, bytes]:
        return dict(self._content)


class _RaisingStaticAnalyzer:
    """A fake ``StaticAnalyzer`` sub-step that raises immediately — stands
    in for "semgrep" in the US9 case, and (with
    ``AnalyzerOutputParseError`` specifically) for the "process exits 0 but
    emits malformed/unparseable JSON" failure mode a real adapter would
    itself raise in that situation.
    """

    def __init__(self, name: str, error: Exception) -> None:
        self.name = name
        self._error = error

    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        raise self._error


class _SleepingStaticAnalyzer:
    """A fake ``StaticAnalyzer`` sub-step that sleeps past its own
    configured timeout — the real-registry equivalent of
    ``_FakeSubStep(delay_seconds=...)`` above, proving US-F5's "exceeds its
    timeout" clause holds for a real ``StaticAnalysisStep``, not only the
    generic framework fake.
    """

    def __init__(self, name: str, delay_seconds: float) -> None:
        self.name = name
        self._delay_seconds = delay_seconds

    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        await asyncio.sleep(self._delay_seconds)
        return []


class _RecordingTextRedactor:
    """A fake ``TextRedactor`` recording every call, isolating
    ``StaticAnalysisStep``'s own "call every finding" behavior from
    ``GitleaksTextRedactor``'s own redaction-correctness test
    (``adapter/analyzers/test_gitleaks.py``) — per the plan's Phase 5 Tests
    section.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def redact(self, text: str) -> str:
        self.calls.append(text)
        return f"REDACTED[{text}]"


class _RaisingTextRedactor:
    async def redact(self, text: str) -> str:
        raise RuntimeError("redaction backend unavailable")


def _static_analysis_step(
    *,
    diff_fetcher: DiffFetcher,
    gitleaks: Any,
    semgrep: Any,
    redactor: TextRedactor,
    enabled: dict[str, bool] | None = None,
    timeouts: dict[str, float] | None = None,
) -> StaticAnalysisStep:
    return StaticAnalysisStep(
        diff_fetcher,
        gitleaks_analyzer=gitleaks,
        semgrep_analyzer=semgrep,
        redactor=redactor,
        enabled=enabled or {"gitleaks": True, "semgrep": True},
        timeouts=timeouts or {"gitleaks": 30.0, "semgrep": 30.0},
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us4_both_analyzers_run_against_real_binaries_and_findings_are_normalized() -> None:
    """US4: Given a claimed REVIEW job for a PR with a real diff, When the
    static-analysis step runs, Then Gitleaks and Semgrep both run against
    the changed files and their findings are normalized into the findings
    domain model."""
    content = _load_testdata("planted_secret.py", "planted_pattern.py")
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher(content),
        gitleaks=GitleaksAnalyzer(),
        semgrep=SemgrepAnalyzer(),
        redactor=_RecordingTextRedactor(),
    )

    output = await step.run(_FakePrefilterOutput(diff=_diff_for(*content)), _context())

    sources = {f.source for f in output.findings}
    assert sources == {"gitleaks", "semgrep"}
    assert any(f.file == "planted_secret.py" and f.source == "gitleaks" for f in output.findings)
    assert any(f.file == "planted_pattern.py" and f.source == "semgrep" for f in output.findings)
    assert output.org_id == "org-1"
    assert output.pr_number == 42


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us9_one_analyzer_raising_does_not_prevent_the_review_from_completing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US9: Given one analyzer raises mid-run, When static-analysis runs,
    Then the review still completes using the other analyzer's findings —
    real Gitleaks against the planted-secret fixture; a fake, broken
    "semgrep" standing in for the failing analyzer. Also asserts each
    analyzer sub-step's own completion/failure log carries the
    ``analyzer_name`` field (extending Phase 2's per-step logging, per this
    step's own Observability section) and the successful analyzer's log
    carries its own ``finding_count``.
    """
    content = _load_testdata("planted_secret.py")
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher(content),
        gitleaks=GitleaksAnalyzer(),
        semgrep=_RaisingStaticAnalyzer("semgrep", RuntimeError("semgrep parser crash")),
        redactor=_RecordingTextRedactor(),
    )

    with caplog.at_level("INFO", logger="review.static_analysis"):
        output = await step.run(_FakePrefilterOutput(diff=_diff_for(*content)), _context())

    assert any(f.source == "gitleaks" for f in output.findings)
    assert not any(f.source == "semgrep" for f in output.findings)

    gitleaks_records = [
        r
        for r in caplog.records
        if getattr(r, "analyzer_name", None) == "gitleaks" and r.levelname == "INFO"
    ]
    assert len(gitleaks_records) == 1
    assert gitleaks_records[0].finding_count == len(output.findings)  # type: ignore[attr-defined]

    semgrep_records = [
        r
        for r in caplog.records
        if getattr(r, "analyzer_name", None) == "semgrep" and r.levelname == "ERROR"
    ]
    assert len(semgrep_records) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_analyzer_exceeding_its_timeout_is_isolated_the_other_still_completes() -> None:
    """The real-``StaticAnalysisStep`` equivalent of US-F5's "exceeds its
    timeout" clause (the "raises" half is US9 above) — a fake "semgrep"
    that sleeps past a deliberately short configured timeout is isolated
    the same way a raising analyzer is.
    """
    content = _load_testdata("planted_secret.py")
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher(content),
        gitleaks=GitleaksAnalyzer(),
        semgrep=_SleepingStaticAnalyzer("semgrep", delay_seconds=0.3),
        redactor=_RecordingTextRedactor(),
        timeouts={"gitleaks": 30.0, "semgrep": 0.05},
    )

    output = await step.run(_FakePrefilterOutput(diff=_diff_for(*content)), _context())

    assert any(f.source == "gitleaks" for f in output.findings)
    assert not any(f.source == "semgrep" for f in output.findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_analyzer_malformed_output_is_isolated_not_an_unhandled_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A distinct failure mode from raises/times-out: a tool that exits 0
    but emits malformed/unparseable JSON (tool-version drift, truncated
    output) is treated as a typed, isolated sub-step failure — the exact
    ``AnalyzerOutputParseError`` a real adapter raises in this situation,
    not an unhandled parse exception escaping the step's own error
    handling. Also asserts this case's failure log fires at ``error``, per
    this step's own Observability convention (Phase 5's plan section):
    isolated doesn't mean absorbed-as-noise.
    """
    content = _load_testdata("planted_secret.py")
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher(content),
        gitleaks=GitleaksAnalyzer(),
        semgrep=_RaisingStaticAnalyzer(
            "semgrep", AnalyzerOutputParseError(tool="semgrep", detail="truncated JSON")
        ),
        redactor=_RecordingTextRedactor(),
    )

    with caplog.at_level("ERROR", logger="review.static_analysis"):
        output = await step.run(_FakePrefilterOutput(diff=_diff_for(*content)), _context())

    assert any(f.source == "gitleaks" for f in output.findings)
    assert not any(f.source == "semgrep" for f in output.findings)
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    assert error_records[0].analyzer_name == "semgrep"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_semgrep_finding_message_is_redacted_before_the_step_returns() -> None:
    """Proves the gap Static-analysis-wide redaction closes (not only
    Gitleaks' own ``--redact``): a Semgrep finding's own ``message`` is
    passed through ``TextRedactor`` before the step returns, using a fake
    redactor that isolates the "call every finding" behavior from
    ``GitleaksTextRedactor``'s own redaction-correctness test.
    """
    fake_semgrep_finding = Finding(
        file="app.py", line=1, severity="high", message="raw message", source="semgrep"
    )
    redactor = _RecordingTextRedactor()
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher({}),
        gitleaks=_RaisingStaticAnalyzer("gitleaks", RuntimeError("disabled for this test")),
        semgrep=_ReturningStaticAnalyzer("semgrep", [fake_semgrep_finding]),
        redactor=redactor,
    )

    output = await step.run(_FakePrefilterOutput(diff=Diff(files=())), _context())

    assert redactor.calls == ["raw message"]
    assert output.findings == [
        fake_semgrep_finding.model_copy(update={"message": "REDACTED[raw message]"})
    ]


@pytest.mark.asyncio
async def test_redaction_failure_produces_the_fatal_pipeline_failed_error_path() -> None:
    """Mirrors Phase 6's equivalent test for its own call site into the same
    mechanism — this macro step's own redaction-failure propagation is
    proven directly, not left as an unverified extrapolation. Requires a
    real, fully-wired outer registry (``FailurePolicy.FATAL``) to actually
    observe ``PipelineFailedError`` — that's raised by the registry
    wrapping a step's own ``run()`` call, not by the step itself.
    """
    finding = Finding(file="app.py", line=1, severity="high", message="x", source="semgrep")
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher({}),
        gitleaks=_ReturningStaticAnalyzer("gitleaks", []),
        semgrep=_ReturningStaticAnalyzer("semgrep", [finding]),
        redactor=_RaisingTextRedactor(),
    )
    prefilter_output = _FakePrefilterOutput(diff=Diff(files=()))
    outer_registry = StepRegistry(
        [_FakePrefilterStep(prefilter_output), step],
        enabled={"prefilter": True, "static_analysis": True, "gitleaks": True, "semgrep": True},
        timeouts={"prefilter": 5.0, "static_analysis": 30.0, "gitleaks": 30.0, "semgrep": 30.0},
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError):
        await outer_registry.run(seed_input=None, context=_context())


class _ReturningStaticAnalyzer:
    """A fake ``StaticAnalyzer`` sub-step that returns a fixed list of
    findings unconditionally — used where the test's own point is the
    redaction/ceiling behavior downstream of the analyzer sub-steps, not
    the analyzer sub-steps' own scanning behavior.
    """

    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        return list(self._findings)


def _synthetic_findings(count: int, *, source: Source = "gitleaks") -> list[Finding]:
    return [
        Finding(file=f"file{i}.py", line=1, severity="high", message=f"finding {i}", source=source)
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_redaction_ceiling_exactly_at_cap_all_findings_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    findings = _synthetic_findings(REDACTION_FINDING_CEILING)
    redactor = _RecordingTextRedactor()
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher({}),
        gitleaks=_ReturningStaticAnalyzer("gitleaks", findings),
        semgrep=_ReturningStaticAnalyzer("semgrep", []),
        redactor=redactor,
    )

    with caplog.at_level("WARNING", logger="review.static_analysis"):
        output = await step.run(_FakePrefilterOutput(diff=Diff(files=())), _context())

    assert len(output.findings) == REDACTION_FINDING_CEILING
    assert len(redactor.calls) == REDACTION_FINDING_CEILING
    assert not any("ceiling" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_redaction_ceiling_one_over_drops_exactly_one_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    findings = _synthetic_findings(REDACTION_FINDING_CEILING + 1)
    redactor = _RecordingTextRedactor()
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher({}),
        gitleaks=_ReturningStaticAnalyzer("gitleaks", findings),
        semgrep=_ReturningStaticAnalyzer("semgrep", []),
        redactor=redactor,
    )

    with caplog.at_level("WARNING", logger="review.static_analysis"):
        output = await step.run(_FakePrefilterOutput(diff=Diff(files=())), _context())

    assert len(output.findings) == REDACTION_FINDING_CEILING
    assert len(redactor.calls) == REDACTION_FINDING_CEILING
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].dropped_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_product_knowledge_capture_fields_are_forwarded_to_output() -> None:
    """
    Optional ``JobIdentity`` fields must be forwarded explicitly at this site.

    Phase 2 of docs/plans/plan-deliver-product-knowledge.md: optional
    ``JobIdentity`` fields are not auto-carried, so this hand-constructing
    site must forward them explicitly.
    """
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher({}),
        gitleaks=_SleepingStaticAnalyzer("gitleaks", 0),
        semgrep=_SleepingStaticAnalyzer("semgrep", 0),
        redactor=_RecordingTextRedactor(),
    )
    upstream = _FakePrefilterOutput(
        diff=Diff(files=()),
        base_sha="base-1",
        pr_title="Fix PROJ-9",
        pr_head_ref="bugfix/PROJ-9",
        pr_issue_keys=("PROJ-9", "#41"),
        pr_is_fork=False,
        private=True,
    )

    output = await step.run(upstream, _context())

    assert output.base_sha == "base-1"
    assert output.pr_title == "Fix PROJ-9"
    assert output.pr_head_ref == "bugfix/PROJ-9"
    assert output.pr_issue_keys == ("PROJ-9", "#41")
    assert output.pr_is_fork is False
    assert output.private is True  # fail-closed field: a dropped forward kills the feature


@pytest.mark.asyncio
async def test_analyzer_outcomes_are_counted_by_analyzer_and_status(
    reset_otel_global_providers: None,  # noqa: F811
) -> None:
    """A failing analyzer is isolated (the job succeeds), so the counter is the
    only non-log signal that it is broken."""
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    try:
        content = _load_testdata("planted_secret.py")
        step = _static_analysis_step(
            diff_fetcher=_FakeDiffFetcher(content),
            gitleaks=_SleepingStaticAnalyzer("gitleaks", 0),
            semgrep=_RaisingStaticAnalyzer("semgrep", RuntimeError("boom")),
            redactor=_RecordingTextRedactor(),
        )
        await step.run(_FakePrefilterOutput(diff=_diff_for(*content)), _context())

        data = reader.get_metrics_data()
        assert data is not None
        seen = {}
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == "review.static_analysis.analyzer_total":
                        assert isinstance(m.data, Sum)
                        for p in m.data.data_points:
                            seen[
                                (
                                    str((p.attributes or {})["analyzer"]),
                                    str((p.attributes or {})["status"]),
                                )
                            ] = p.value
        assert seen == {("gitleaks", "success"): 1, ("semgrep", "failed"): 1}
    finally:
        provider.shutdown()
