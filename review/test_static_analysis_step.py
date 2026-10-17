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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.gitleaks import GitleaksAnalyzer
from adapter.analyzers.semgrep import SemgrepAnalyzer
from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from findings.models import Finding, Source
from findings.redact import TextRedactor
from ingestion.diff import Diff, DiffFetcher, DiffFile
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.static_analysis import (
    REDACTION_FINDING_CEILING,
    ConfigOutcome,
    StaticAnalysisStep,
    gitleaks_semgrep_registrations,
)
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

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
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

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
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
        registrations=gitleaks_semgrep_registrations(gitleaks, semgrep),
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

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
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


# ============================================================================
# Phase 9 / Phase 1: registry-driven analyzers, language routing, changed-line
# filtering, ordering, timeout inheritance. Unit tier: fake analyzers at the
# StaticAnalyzer Protocol boundary, no real binaries.
# ============================================================================

from review.changed_lines import changed_lines  # noqa: E402
from review.file_matchers import match_python  # noqa: E402
from review.static_analysis import (  # noqa: E402
    AnalyzerRegistration,
    RepoConfigFetcher,
    RepoConfigFetchError,
)


class _RecordingAnalyzer:
    def __init__(self, findings: list[Finding] | None = None) -> None:
        self.findings = findings or []
        self.calls: list[tuple[list[str], float]] = []
        self.config_seen: list[dict[str, bytes]] = []

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        self.calls.append((sorted(content), timeout))
        self.config_seen.append(dict(config_files))
        return list(self.findings)


def _finding(
    source: Source, line: int | None, severity: str = "low", file: str = "a.py"
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        message=f"{source}@{line}",
        source=source,
    )


_PY_PATCH = "@@ -1,2 +1,3 @@\n keep = 1\n+added = 2\n keep2 = 3\n"


def _fake_step(
    registrations: list[AnalyzerRegistration],
    content: dict[str, bytes],
    *,
    patches: dict[str, str | None] | None = None,
    enabled: dict[str, bool] | None = None,
    timeouts: dict[str, float] | None = None,
    analyzer_slots: asyncio.Semaphore | None = None,
    config_fetcher: RepoConfigFetcher | None = None,
) -> tuple[StaticAnalysisStep, Diff]:
    patches = patches or {}
    diff = Diff(
        files=tuple(
            DiffFile(path=p, status="modified", patch=patches.get(p), mode="100644")
            for p in content
        )
    )
    step = StaticAnalysisStep(
        _FakeDiffFetcher(content),
        registrations=registrations,
        redactor=_RecordingTextRedactor(),
        enabled=enabled or {},
        timeouts=timeouts or {"static_analysis_analyzer": 9.0},
        analyzer_slots=analyzer_slots,
        config_fetcher=config_fetcher,
    )
    return step, diff


@pytest.mark.asyncio
async def test_us_a1_each_analyzer_receives_only_its_languages_and_is_skipped_otherwise() -> None:
    py = _RecordingAnalyzer()
    other = AnalyzerRegistration("ruff", py, matches=match_python)
    go = _RecordingAnalyzer()
    never = AnalyzerRegistration("golangci_lint", go, matches=lambda p, c: p.endswith(".go"))
    everything = _RecordingAnalyzer()
    step, diff = _fake_step(
        [other, never, AnalyzerRegistration("gitleaks", everything)],
        {"a.py": b"x", "notes.md": b"y"},
    )

    await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert [c[0] for c in py.calls] == [["a.py"]]
    assert go.calls == []  # no matching file: never invoked
    assert [c[0] for c in everything.calls] == [["a.py", "notes.md"]]  # unchanged behavior


@pytest.mark.asyncio
async def test_us_a3_changed_line_filter_drops_untouched_lines_and_patchless_files() -> None:
    lint = _RecordingAnalyzer(
        [
            _finding("ruff", 2),
            _finding("ruff", 1),
            _finding("ruff", 1, file="big.py"),
        ]
    )
    secret = _RecordingAnalyzer([_finding("gitleaks", 1)])
    step, diff = _fake_step(
        [
            AnalyzerRegistration("ruff", lint, changed_lines_only=True),
            AnalyzerRegistration("gitleaks", secret),
        ],
        {"a.py": b"x", "big.py": b"y"},
        patches={"a.py": _PY_PATCH, "big.py": None},
    )
    assert changed_lines(_PY_PATCH) == {2}

    output = await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert sorted((f.source, f.line) for f in output.findings) == [("gitleaks", 1), ("ruff", 2)]


@pytest.mark.asyncio
async def test_us_a7_disabled_analyzer_is_never_invoked_and_timeout_is_inherited() -> None:
    on = _RecordingAnalyzer()
    off = _RecordingAnalyzer()
    own = _RecordingAnalyzer()
    step, diff = _fake_step(
        [
            AnalyzerRegistration("ruff", on),
            AnalyzerRegistration("eslint", off),
            AnalyzerRegistration("gitleaks", own),
        ],
        {"a.py": b"x"},
        enabled={"eslint": False},
        timeouts={"static_analysis_analyzer": 9.0, "gitleaks": 4.0},
    )

    await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert off.calls == []
    assert on.calls[0][1] == 9.0  # inherited default
    assert own.calls[0][1] == 4.0  # its own entry wins


def test_analyzer_without_any_timeout_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="no timeout configured"):
        StaticAnalysisStep(
            _FakeDiffFetcher({}),
            registrations=[AnalyzerRegistration("ruff", _RecordingAnalyzer())],
            redactor=_RecordingTextRedactor(),
            enabled={},
            timeouts={},
        )


@pytest.mark.asyncio
async def test_us_a10_ceiling_keeps_highest_severity_then_security_class_first() -> None:
    lint = _RecordingAnalyzer(
        [_finding("ruff", i, "low") for i in range(1, REDACTION_FINDING_CEILING + 1)]
        + [_finding("ruff", 1000, "critical")]
    )
    sec = _RecordingAnalyzer([_finding("semgrep", 7, "low")])
    step, diff = _fake_step(
        [
            AnalyzerRegistration("ruff", lint),  # registered first on purpose
            AnalyzerRegistration("semgrep", sec, security_class=True),
        ],
        {"a.py": b"x"},
    )

    output = await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert len(output.findings) == REDACTION_FINDING_CEILING
    assert output.findings[0].severity == "critical"
    # The security-class low finding survives; one lint-class low is dropped instead.
    assert any(f.source == "semgrep" for f in output.findings)
    assert sum(f.source == "ruff" for f in output.findings) == REDACTION_FINDING_CEILING - 1


# --- Phase 9 / Phase 2: repo-config discovery -------------------------------


class _FakeConfigFetcher:
    def __init__(
        self, files: dict[str, bytes] | None = None, error: RepoConfigFetchError | None = None
    ) -> None:
        self.files = files or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def fetch_config_files(self, **kwargs: Any) -> dict[str, bytes]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return dict(self.files)


def _config_registrations() -> tuple[_RecordingAnalyzer, _RecordingAnalyzer, list[Any]]:
    ruff, other = _RecordingAnalyzer(), _RecordingAnalyzer()
    return (
        ruff,
        other,
        [
            AnalyzerRegistration(
                "ruff", ruff, matches=match_python, config_filenames=("ruff.toml",)
            ),
            AnalyzerRegistration("gitleaks", other, config_filenames=("other.cfg",)),
        ],
    )


@pytest.mark.asyncio
async def test_each_engine_receives_only_its_own_allow_listed_config_files() -> None:
    ruff, other, regs = _config_registrations()
    fetcher = _FakeConfigFetcher({"ruff.toml": b"[x]", "other.cfg": b"o", "evil.sh": b"!"})
    step, diff = _fake_step(regs, {"a.py": b"x"}, config_fetcher=fetcher)

    await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert ruff.config_seen == [{"ruff.toml": b"[x]"}]
    assert other.config_seen == [{"other.cfg": b"o"}]
    assert len(fetcher.calls) == 1  # once per job
    assert set(fetcher.calls[0]["names"]) == {"ruff.toml", "other.cfg"}
    assert fetcher.calls[0]["commit_sha"] == _FakePrefilterOutput(diff=diff).head_sha


@pytest.mark.asyncio
async def test_no_config_fetch_when_no_engine_with_config_names_is_due() -> None:
    ruff = _RecordingAnalyzer()
    fetcher = _FakeConfigFetcher({"ruff.toml": b"x"})
    regs = [
        AnalyzerRegistration("ruff", ruff, matches=match_python, config_filenames=("ruff.toml",))
    ]
    step, diff = _fake_step(regs, {"notes.md": b"x"}, config_fetcher=fetcher)
    await step.run(_FakePrefilterOutput(diff=diff), _context())

    disabled_step, diff2 = _fake_step(
        regs, {"a.py": b"x"}, enabled={"ruff": False}, config_fetcher=fetcher
    )
    await disabled_step.run(_FakePrefilterOutput(diff=diff2), _context())

    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_config_fetch_failure_degrades_to_no_config_and_job_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ruff, _, regs = _config_registrations()
    fetcher = _FakeConfigFetcher(error=RepoConfigFetchError("tree_truncated"))
    step, diff = _fake_step(regs, {"a.py": b"x"}, config_fetcher=fetcher)

    with caplog.at_level("WARNING", logger="review.static_analysis"):
        await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert ruff.config_seen == [{}]
    fallback = [r for r in caplog.records if hasattr(r, "config_skip_reason")]
    assert [(r.config_source, r.config_skip_reason) for r in fallback] == [  # type: ignore[attr-defined]
        ("default", "tree_truncated")
    ]


@pytest.fixture
def metric_reader(reset_otel_global_providers: None) -> Any:
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    otel_metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _counter_points(
    reader: InMemoryMetricReader, name: str
) -> dict[tuple[tuple[str, Any], ...], int]:
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[tuple[tuple[str, Any], ...], int] = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    assert isinstance(metric.data, Sum)
                    for point in metric.data.data_points:
                        points[tuple(sorted((point.attributes or {}).items()))] = point.value
    return points


@pytest.mark.asyncio
async def test_skipped_findings_and_changed_line_drops_are_counted(
    metric_reader: InMemoryMetricReader,
) -> None:
    lint = _RecordingAnalyzer([_finding("ruff", 2), _finding("ruff", 1)])
    go = _RecordingAnalyzer()
    step, diff = _fake_step(
        [
            AnalyzerRegistration("ruff", lint, matches=match_python, changed_lines_only=True),
            AnalyzerRegistration("golangci_lint", go, matches=lambda p, c: p.endswith(".go")),
        ],
        {"a.py": b"x"},
        patches={"a.py": _PY_PATCH},
    )

    await step.run(_FakePrefilterOutput(diff=diff), _context())

    assert _counter_points(metric_reader, "review.static_analysis.analyzer.skipped_total") == {
        (("analyzer_name", "golangci_lint"),): 1
    }
    assert _counter_points(metric_reader, "review.static_analysis.findings_total") == {
        (("analyzer_name", "golangci_lint"),): 0,
        (("analyzer_name", "ruff"),): 1,
    }
    assert _counter_points(metric_reader, "review.static_analysis.findings_dropped_total") == {
        (("analyzer_name", "ruff"), ("reason", "not_changed_line")): 1
    }


@pytest.mark.asyncio
async def test_redaction_ceiling_drops_are_counted(metric_reader: InMemoryMetricReader) -> None:
    step = _static_analysis_step(
        diff_fetcher=_FakeDiffFetcher({}),
        gitleaks=_ReturningStaticAnalyzer(
            "gitleaks", _synthetic_findings(REDACTION_FINDING_CEILING + 3)
        ),
        semgrep=_ReturningStaticAnalyzer("semgrep", []),
        redactor=_RecordingTextRedactor(),
    )

    await step.run(_FakePrefilterOutput(diff=Diff(files=())), _context())

    assert _counter_points(metric_reader, "review.static_analysis.findings_dropped_total") == {
        (("reason", "redaction_ceiling"),): 3
    }


def test_config_skip_is_counted_only_when_the_repo_config_was_not_honored(
    metric_reader: InMemoryMetricReader,
) -> None:
    ConfigOutcome("repo").record("ruff")
    ConfigOutcome("default").record("ruff")
    ConfigOutcome("default", "rejected_by_tool").record("ruff")

    assert _counter_points(metric_reader, "review.static_analysis.config_skipped_total") == {
        (("analyzer_name", "ruff"), ("reason", "rejected_by_tool")): 1
    }
