"""Unit tests for review.pipeline.StepRegistry, per docs/rules/testing.md's
tier rule and docs/adr/0005-pure-no-io-logic-unit-tested.md — pure
orchestration logic over fakes at the ``review.step.Step`` Protocol
boundary, no real I/O, adapters, network, or database.

Covers US-F1 (downstream tests stay unmodified across an upstream step's
internal-only change), US-F2 (independent steps run concurrently), US-F3
(a new macro step registers with zero diff to existing ones), US-F4 (a
disabled step is never invoked), and US-F6 (a raising *or* timed-out macro
step is fatal and short-circuits every downstream step) — matching
docs/specs/pipeline-step-framework.md's own Testing table.

Also covers US2 (docs/specs/spec-observability.md, Unit B): the
``review.job.run`` root span / ``review.step.{name}`` child span / the
``review.pipeline.step.duration_ms`` histogram ``StepRegistry.run()``/
``_run_one`` record around every step outcome (success, failed, timeout,
disabled) — proven here, against the same fakes the rest of this file
already uses, rather than in ``review/test_review_job.py``'s own single
"a review job" acceptance case (which proves the shape end to end, not
every outcome). Uses OTel's own in-memory SDK test components
(``InMemorySpanExporter``/``InMemoryMetricReader``), genuinely no I/O, per
``testing.md``'s Unit tier — and asserts the exact, fixed attribute
allowlist on every emitted span/metric per the Privacy/Compliance NFR
(never a step's own exception text).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from review import pipeline as pipeline_module
from review.pipeline import FailurePolicy, PipelineFailedError, StepFailureInfo, StepRegistry
from review.step import StepContext


@dataclass
class _FakeStep:
    """A generic, in-memory fake satisfying ``review.step.Step`` — records
    every ``input`` it was called with so tests can assert whether/how it
    ran, and can be configured to sleep, raise, or simply echo its input
    back as part of its output.
    """

    name: str
    depends_on: tuple[str, ...] = ()
    delay_seconds: float = 0.0
    error: Exception | None = None
    calls: list[Any] = field(default_factory=list)
    completed: bool = field(default=False, init=False)

    async def run(self, input: Any, context: StepContext) -> Any:
        self.calls.append(input)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        self.completed = True
        return {"produced_by": self.name, "input": input}


def _context(job_id: str = "job-1") -> StepContext:
    return StepContext(job_id=job_id)


def _all_enabled(*names: str) -> dict[str, bool]:
    return dict.fromkeys(names, True)


def _uniform_timeouts(seconds: int, *names: str) -> dict[str, int]:
    return dict.fromkeys(names, seconds)


# --- US-F2: independent steps run concurrently -----------------------------


@pytest.mark.asyncio
async def test_independent_steps_run_concurrently() -> None:
    step_a = _FakeStep(name="a", delay_seconds=0.2)
    step_b = _FakeStep(name="b", delay_seconds=0.2)
    registry = StepRegistry(
        [step_a, step_b],
        enabled=_all_enabled("a", "b"),
        timeouts=_uniform_timeouts(5, "a", "b"),
        on_failure=FailurePolicy.FATAL,
    )

    start = time.monotonic()
    await registry.run(seed_input="seed", context=_context())
    elapsed = time.monotonic() - start

    # Run sequentially this would take >= 0.4s; concurrently, comfortably
    # under that (with generous headroom for CI scheduling jitter).
    assert elapsed < 0.35


# --- US-F3: a new macro step registers with zero diff to existing ones -----


@pytest.mark.asyncio
async def test_registering_a_new_step_at_a_declared_position_needs_no_existing_step_changes() -> (
    None
):
    upstream = _FakeStep(name="prefilter")
    existing_downstream = _FakeStep(name="output", depends_on=("prefilter",))
    # A hypothetical new macro step inserted between them, declared purely
    # via `depends_on` — no change to `upstream` or `existing_downstream`.
    new_step = _FakeStep(name="context_builder", depends_on=("prefilter",))

    registry = StepRegistry(
        [upstream, new_step, existing_downstream],
        enabled=_all_enabled("prefilter", "context_builder", "output"),
        timeouts=_uniform_timeouts(5, "prefilter", "context_builder", "output"),
        on_failure=FailurePolicy.FATAL,
    )

    outcomes = await registry.run(seed_input="diff", context=_context())

    prefilter_output = {"produced_by": "prefilter", "input": "diff"}
    assert outcomes["context_builder"].status == "success"
    assert new_step.calls == [prefilter_output]
    assert existing_downstream.calls == [prefilter_output]


# --- US-F4: a disabled step is never invoked --------------------------------


@pytest.mark.asyncio
async def test_disabled_step_is_never_invoked() -> None:
    enabled_step = _FakeStep(name="prefilter")
    disabled_step = _FakeStep(name="static_analysis", depends_on=())

    registry = StepRegistry(
        [enabled_step, disabled_step],
        enabled={"prefilter": True, "static_analysis": False},
        timeouts=_uniform_timeouts(5, "prefilter", "static_analysis"),
        on_failure=FailurePolicy.FATAL,
    )

    outcomes = await registry.run(seed_input="diff", context=_context())

    assert disabled_step.calls == []
    assert outcomes["static_analysis"].status == "disabled"
    assert outcomes["prefilter"].status == "success"


@pytest.mark.asyncio
async def test_remaining_steps_still_run_when_a_non_essential_step_is_disabled() -> None:
    disabled_step = _FakeStep(name="static_analysis")
    other_step = _FakeStep(name="llm_review")

    registry = StepRegistry(
        [disabled_step, other_step],
        enabled={"static_analysis": False, "llm_review": True},
        timeouts=_uniform_timeouts(5, "static_analysis", "llm_review"),
        on_failure=FailurePolicy.FATAL,
    )

    outcomes = await registry.run(seed_input="diff", context=_context())

    assert outcomes["llm_review"].status == "success"
    assert other_step.calls == ["diff"]


# --- US-F1: a downstream step's own tests stay unmodified across an --------
# --- upstream step's internal-only change -----------------------------------


class _RichUpstreamOutput:
    """Stands in for a step's declared, unchanging output *model* — the
    thing a downstream step's own tests are written against, independent
    of whatever the upstream step's internals happen to do to produce it.
    """

    def __init__(self, changed_files: list[str]) -> None:
        self.changed_files = changed_files


@dataclass
class _UpstreamStepV1:
    """ "Version 1" of an upstream step's internal implementation."""

    name: str = "prefilter"
    depends_on: tuple[str, ...] = ()

    async def run(self, input: Any, context: StepContext) -> _RichUpstreamOutput:
        return _RichUpstreamOutput(changed_files=sorted(input))


@dataclass
class _UpstreamStepV2:
    """ "Version 2" — a deliberately different internal implementation
    (a different algorithm entirely) that still honors the same declared
    output type as V1."""

    name: str = "prefilter"
    depends_on: tuple[str, ...] = ()

    async def run(self, input: Any, context: StepContext) -> _RichUpstreamOutput:
        # A genuinely different algorithm from V1's `sorted(input)` — an
        # in-place `.sort()` on a copy — that still honors the same
        # declared output: sorted `changed_files`.
        files = list(input)
        files.sort()
        return _RichUpstreamOutput(changed_files=files)


@dataclass
class _DownstreamStep:
    """A downstream step whose own behavior — and whose own test below —
    depends only on `_RichUpstreamOutput`'s declared shape, never on which
    concrete upstream implementation produced it."""

    name: str = "static_analysis"
    depends_on: tuple[str, ...] = ("prefilter",)
    calls: list[_RichUpstreamOutput] = field(default_factory=list)

    async def run(self, input: _RichUpstreamOutput, context: StepContext) -> list[str]:
        self.calls.append(input)
        return input.changed_files


async def _run_downstream_against(upstream: Any) -> list[str]:
    """The one test body every version of the upstream step is proven
    against below — this is the "downstream step's own test" that must
    pass unmodified regardless of which upstream implementation runs."""
    downstream = _DownstreamStep()
    registry = StepRegistry(
        [upstream, downstream],
        enabled=_all_enabled("prefilter", "static_analysis"),
        timeouts=_uniform_timeouts(5, "prefilter", "static_analysis"),
        on_failure=FailurePolicy.FATAL,
    )
    outcomes = await registry.run(seed_input=["b.py", "a.py"], context=_context())
    result = outcomes["static_analysis"].output
    assert isinstance(result, list)
    return result


@pytest.mark.asyncio
async def test_downstream_step_test_passes_unmodified_against_upstream_v1() -> None:
    assert await _run_downstream_against(_UpstreamStepV1()) == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_downstream_step_test_passes_unmodified_against_upstream_v2() -> None:
    # Same assertion, same helper, only the upstream implementation
    # changed — proving US-F1's "the other step's own tests still pass
    # unmodified" property directly, not merely asserting it in prose.
    assert await _run_downstream_against(_UpstreamStepV2()) == ["a.py", "b.py"]


# --- US-F6: a raising or timed-out macro step is fatal ----------------------


@pytest.mark.asyncio
async def test_raising_macro_step_is_fatal_and_short_circuits_downstream() -> None:
    failing = _FakeStep(name="llm_review", error=RuntimeError("boom"))
    downstream = _FakeStep(name="output", depends_on=("llm_review",))

    registry = StepRegistry(
        [failing, downstream],
        enabled=_all_enabled("llm_review", "output"),
        timeouts=_uniform_timeouts(5, "llm_review", "output"),
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError) as excinfo:
        await registry.run(seed_input="diff", context=_context())

    assert excinfo.value.step_name == "llm_review"
    assert downstream.calls == []


@pytest.mark.asyncio
async def test_macro_step_exceeding_its_timeout_is_fatal_and_short_circuits_downstream() -> None:
    slow = _FakeStep(name="static_analysis", delay_seconds=0.3)
    downstream = _FakeStep(name="llm_review", depends_on=("static_analysis",))

    registry = StepRegistry(
        [slow, downstream],
        enabled=_all_enabled("static_analysis", "llm_review"),
        timeouts={"static_analysis": 0.05, "llm_review": 5},
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError) as excinfo:
        await registry.run(seed_input="diff", context=_context())

    assert excinfo.value.step_name == "static_analysis"
    assert downstream.calls == []


@pytest.mark.asyncio
async def test_step_timeouts_are_genuinely_per_step_not_one_shared_value() -> None:
    """A `StepTimeouts`-equivalent mapping configured with two different
    values for two different fake steps in the same run: the short-timeout
    step is cancelled while the long-timeout step's identical-length sleep
    completes normally — proving per-step configurability, not just that
    *a* timeout exists."""
    short_timeout_step = _FakeStep(name="fast_budget", delay_seconds=0.3)
    long_timeout_step = _FakeStep(name="slow_budget", delay_seconds=0.3)

    registry = StepRegistry(
        [short_timeout_step, long_timeout_step],
        enabled=_all_enabled("fast_budget", "slow_budget"),
        timeouts={"fast_budget": 0.05, "slow_budget": 5},
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError) as excinfo:
        await registry.run(seed_input="diff", context=_context())

    # The short-timeout step is the one that actually failed the run...
    assert excinfo.value.step_name == "fast_budget"
    # ...while the long-timeout step's identical 0.3s sleep ran to
    # completion in the same gathered level, never forcibly cancelled by
    # its sibling's own timeout.
    assert long_timeout_step.completed is True


# --- Terminal step: US-F6's one stated exception ------------------


@pytest.mark.asyncio
async def test_terminal_step_runs_normally_on_the_happy_path() -> None:
    """The registered terminal step's happy-path behavior is completely
    unaffected by this mechanism — it still just receives its normal
    dependency's output, the same as any other step."""
    upstream = _FakeStep(name="llm_review")
    terminal = _FakeStep(name="output", depends_on=("llm_review",))

    registry = StepRegistry(
        [upstream, terminal],
        enabled=_all_enabled("llm_review", "output"),
        timeouts=_uniform_timeouts(5, "llm_review", "output"),
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )

    outcomes = await registry.run(seed_input="diff", context=_context())

    assert outcomes["output"].status == "success"
    assert terminal.calls == [{"produced_by": "llm_review", "input": "diff"}]


@pytest.mark.asyncio
async def test_terminal_step_still_invoked_when_an_earlier_step_fails() -> None:
    """The gap this mechanism closes: an *earlier* macro step's fatal
    failure would otherwise mean the terminal step never runs at all — no
    GitHub-visible signal. With
    `terminal_step` set, it still runs exactly once, with a
    `StepFailureInfo` built from the failed step, before
    `PipelineFailedError` is raised."""
    failing = _FakeStep(name="prefilter", error=RuntimeError("boom"))
    never_reached = _FakeStep(name="static_analysis", depends_on=("prefilter",))
    terminal = _FakeStep(name="output", depends_on=("static_analysis",))

    registry = StepRegistry(
        [failing, never_reached, terminal],
        enabled=_all_enabled("prefilter", "static_analysis", "output"),
        timeouts=_uniform_timeouts(5, "prefilter", "static_analysis", "output"),
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )

    with pytest.raises(PipelineFailedError) as excinfo:
        await registry.run(seed_input={"pr_number": 1}, context=_context())

    # The original failure is still what's raised — this mechanism doesn't
    # replace or mask it.
    assert excinfo.value.step_name == "prefilter"
    # The genuinely-downstream, dependency-needing step never ran (US-F6's
    # own rule, unaffected).
    assert never_reached.calls == []
    # ...but the terminal step *did* run, exactly once, with a
    # StepFailureInfo describing the original failure.
    assert len(terminal.calls) == 1
    failure_input = terminal.calls[0]
    assert isinstance(failure_input, StepFailureInfo)
    assert failure_input.failed_step_name == "prefilter"
    assert "boom" in failure_input.error
    assert failure_input.seed_input == {"pr_number": 1}


@pytest.mark.asyncio
async def test_terminal_step_not_invoked_twice_when_it_is_itself_the_failing_step() -> None:
    """If the terminal step is the one that fails on its own (an ordinary
    FATAL-policy failure, not an upstream one), it already ran once as a
    normal step in the loop — this mechanism must not invoke it a second
    time."""
    upstream = _FakeStep(name="llm_review")
    terminal = _FakeStep(name="output", depends_on=("llm_review",), error=RuntimeError("boom"))

    registry = StepRegistry(
        [upstream, terminal],
        enabled=_all_enabled("llm_review", "output"),
        timeouts=_uniform_timeouts(5, "llm_review", "output"),
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )

    with pytest.raises(PipelineFailedError) as excinfo:
        await registry.run(seed_input="diff", context=_context())

    assert excinfo.value.step_name == "output"
    assert len(terminal.calls) == 1


@pytest.mark.asyncio
async def test_disabled_terminal_step_is_never_invoked_on_the_failure_path() -> None:
    """US-F4 still applies on the failure path exactly as it does on the
    happy path — a terminal step an operator has disabled is never
    invoked, full stop, not even to report a failure."""
    failing = _FakeStep(name="prefilter", error=RuntimeError("boom"))
    terminal = _FakeStep(name="output", depends_on=("prefilter",))

    registry = StepRegistry(
        [failing, terminal],
        enabled={"prefilter": True, "output": False},
        timeouts=_uniform_timeouts(5, "prefilter", "output"),
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )

    with pytest.raises(PipelineFailedError):
        await registry.run(seed_input="diff", context=_context())

    assert terminal.calls == []


@pytest.mark.asyncio
async def test_terminal_step_double_failure_is_logged_distinctly_and_does_not_mask_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The residual gap: if the
    terminal step's own failure-path invocation itself fails (e.g. posting
    a failure-conclusion check-run needs the same broken GitHub auth that
    caused the original failure), that's a double failure — logged
    distinctly, never allowed to replace or hide the original
    `PipelineFailedError`."""
    failing = _FakeStep(name="prefilter", error=RuntimeError("original failure"))
    terminal = _FakeStep(
        name="output", depends_on=("prefilter",), error=RuntimeError("terminal also failed")
    )

    registry = StepRegistry(
        [failing, terminal],
        enabled=_all_enabled("prefilter", "output"),
        timeouts=_uniform_timeouts(5, "prefilter", "output"),
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )

    with caplog.at_level("ERROR"), pytest.raises(PipelineFailedError) as excinfo:
        await registry.run(seed_input="diff", context=_context())

    # The originally-failed step is still what's reported — not "output".
    assert excinfo.value.step_name == "prefilter"
    double_failure_logs = [r for r in caplog.records if "double failure" in r.getMessage()]
    assert len(double_failure_logs) == 1
    assert double_failure_logs[0].__dict__["failed_step_name"] == "prefilter"
    assert double_failure_logs[0].__dict__["terminal_step_name"] == "output"


@pytest.mark.asyncio
async def test_no_terminal_step_configured_preserves_pre_phase_7_behavior() -> None:
    """`terminal_step=None` (the default) is exactly the original
    behavior — every caller without a terminal step (including this file's own other
    tests) is unaffected by this mechanism's existence."""
    failing = _FakeStep(name="llm_review", error=RuntimeError("boom"))
    downstream = _FakeStep(name="output", depends_on=("llm_review",))

    registry = StepRegistry(
        [failing, downstream],
        enabled=_all_enabled("llm_review", "output"),
        timeouts=_uniform_timeouts(5, "llm_review", "output"),
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError):
        await registry.run(seed_input="diff", context=_context())

    assert downstream.calls == []


def test_terminal_step_must_be_a_registered_step_name() -> None:
    with pytest.raises(ValueError, match="not a registered step name"):
        StepRegistry(
            [_FakeStep(name="a")],
            enabled=_all_enabled("a"),
            timeouts=_uniform_timeouts(5, "a"),
            on_failure=FailurePolicy.FATAL,
            terminal_step="missing",
        )


# --- Registry construction: defensive validation ----------------------------


def test_duplicate_step_name_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="duplicate step name"):
        StepRegistry(
            [_FakeStep(name="a"), _FakeStep(name="a")],
            enabled=_all_enabled("a"),
            timeouts=_uniform_timeouts(5, "a"),
            on_failure=FailurePolicy.FATAL,
        )


def test_dependency_on_unregistered_step_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unregistered step"):
        StepRegistry(
            [_FakeStep(name="a", depends_on=("missing",))],
            enabled=_all_enabled("a"),
            timeouts=_uniform_timeouts(5, "a"),
            on_failure=FailurePolicy.FATAL,
        )


def test_more_than_one_declared_dependency_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="single dependency"):
        StepRegistry(
            [
                _FakeStep(name="a"),
                _FakeStep(name="b"),
                _FakeStep(name="c", depends_on=("a", "b")),
            ],
            enabled=_all_enabled("a", "b", "c"),
            timeouts=_uniform_timeouts(5, "a", "b", "c"),
            on_failure=FailurePolicy.FATAL,
        )


def test_circular_dependency_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="circular dependency"):
        StepRegistry(
            [
                _FakeStep(name="a", depends_on=("b",)),
                _FakeStep(name="b", depends_on=("a",)),
            ],
            enabled=_all_enabled("a", "b"),
            timeouts=_uniform_timeouts(5, "a", "b"),
            on_failure=FailurePolicy.FATAL,
        )


# --- US2: review.job.run root span / review.step.{name} child span / ------
# --- review.pipeline.step.duration_ms histogram -----------------------------


@pytest.fixture
def in_memory_span_exporter(
    reset_otel_global_providers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[InMemorySpanExporter]:
    """Registers a real ``TracerProvider`` backed by ``InMemorySpanExporter``
    as the *global* one — required for ``review.pipeline``'s module-level
    ``tracer`` (created once, at import time) to pick this test's provider
    up.

    Unlike ``MeterProvider``'s own proxy-instrument migration (see
    ``adapter/telemetry/test_heartbeat.py``'s identical fixture docstring),
    the installed ``opentelemetry-api``'s own ``ProxyTracer`` caches its
    resolved real ``Tracer`` permanently on first use
    (``opentelemetry.trace.ProxyTracer._tracer``'s own ``if
    self._real_tracer: return self._real_tracer`` — no per-set-provider
    re-migration callback the way ``ProxyMeter`` has) — verified against
    the installed ``opentelemetry-api`` source. Left alone, only the first
    test in the whole process to use this fixture would ever see
    ``review.pipeline.tracer`` pick up its own provider; every later test
    would keep exporting to the first test's already-``shutdown()``
    exporter. This fixture resets that cache directly (the same "reach
    into OTel's own private state between tests" approach
    ``adapter/telemetry/conftest.py``'s own fixture already takes for the
    module-level globals) so every test using this fixture gets a fresh
    migration."""
    monkeypatch.setattr(pipeline_module.tracer, "_real_tracer", None, raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "review-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real ``MeterProvider`` backed by ``InMemoryMetricReader``
    as the *global* one — same reasoning as ``in_memory_span_exporter``
    above, for ``review.pipeline``'s module-level ``_step_duration_histogram``."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    otel_metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _histogram_data_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[tuple[float, int, dict[str, object]]]:
    """Returns ``(sum, count, attributes)`` per data point recorded against
    ``metric_name``."""
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    points: list[tuple[float, int, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Histogram)
                for data_point in metric.data.data_points:
                    points.append(
                        (data_point.sum, data_point.count, dict(data_point.attributes or {}))
                    )
    return points


@dataclass
class _SeedWithIdentity:
    """A minimal stand-in for a ``review.step.JobIdentity``-derived
    ``seed_input`` — only the two string attributes ``run()``'s own
    best-effort org/repo span attributes look for, per this module's own
    "set org/repo attributes too if/when available in that scope" note."""

    org_id: str
    repo_id: str


@pytest.mark.asyncio
async def test_run_produces_root_span_tagged_with_job_id_only_when_seed_has_no_identity(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    registry = StepRegistry(
        [_FakeStep(name="a")],
        enabled=_all_enabled("a"),
        timeouts=_uniform_timeouts(5, "a"),
        on_failure=FailurePolicy.FATAL,
    )

    await registry.run(seed_input="diff", context=_context(job_id="job-1"))

    spans = in_memory_span_exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "review.job.run")
    # Privacy/Compliance NFR: the root span's attribute set is a fixed
    # allowlist — a plain-string seed_input has no org_id/repo_id, so only
    # the job ID is present, never anything derived from the seed itself.
    assert dict(root.attributes or {}) == {"review.job.id": "job-1"}


@pytest.mark.asyncio
async def test_run_root_span_includes_org_and_repo_id_when_present_on_seed_input(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    registry = StepRegistry(
        [_FakeStep(name="a")],
        enabled=_all_enabled("a"),
        timeouts=_uniform_timeouts(5, "a"),
        on_failure=FailurePolicy.FATAL,
    )

    await registry.run(
        seed_input=_SeedWithIdentity(org_id="org-1", repo_id="repo-2"),
        context=_context(job_id="job-2"),
    )

    spans = in_memory_span_exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "review.job.run")
    assert dict(root.attributes or {}) == {
        "review.job.id": "job-2",
        "review.job.org_id": "org-1",
        "review.job.repo_id": "repo-2",
    }


@pytest.mark.asyncio
async def test_run_root_span_ignores_non_string_org_and_repo_id(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    """A ``seed_input`` whose ``org_id``/``repo_id`` aren't strings (any
    other shape than a real ``JobIdentity``-derived model) never blocks or
    fails the trace — per ``run()``'s own docstring, this is best-effort."""

    @dataclass
    class _WronglyTypedSeed:
        org_id: int
        repo_id: int

    registry = StepRegistry(
        [_FakeStep(name="a")],
        enabled=_all_enabled("a"),
        timeouts=_uniform_timeouts(5, "a"),
        on_failure=FailurePolicy.FATAL,
    )

    await registry.run(seed_input=_WronglyTypedSeed(org_id=1, repo_id=2), context=_context())

    spans = in_memory_span_exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "review.job.run")
    assert dict(root.attributes or {}) == {"review.job.id": "job-1"}


@pytest.mark.asyncio
async def test_run_produces_a_child_span_per_step_nested_under_the_root(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    step_a = _FakeStep(name="a")
    step_b = _FakeStep(name="b")
    registry = StepRegistry(
        [step_a, step_b],
        enabled=_all_enabled("a", "b"),
        timeouts=_uniform_timeouts(5, "a", "b"),
        on_failure=FailurePolicy.FATAL,
    )

    await registry.run(seed_input="diff", context=_context())

    spans = in_memory_span_exporter.get_finished_spans()
    by_name = {span.name: span for span in spans}
    assert set(by_name) == {"review.job.run", "review.step.a", "review.step.b"}
    root = by_name["review.job.run"]

    for step_name in ("a", "b"):
        child = by_name[f"review.step.{step_name}"]
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id
        # Privacy/Compliance NFR: exactly this fixed allowlist.
        attrs = dict(child.attributes or {})
        assert set(attrs) == {
            "review.step.name",
            "review.step.status",
            "review.step.duration_ms",
        }
        assert attrs["review.step.name"] == step_name
        assert attrs["review.step.status"] == "success"
        assert isinstance(attrs["review.step.duration_ms"], int)
        assert attrs["review.step.duration_ms"] >= 0


@pytest.mark.asyncio
async def test_run_child_span_records_failed_status_without_leaking_exception_text(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    failing = _FakeStep(name="a", error=RuntimeError("do-not-leak-this-secret"))
    registry = StepRegistry(
        [failing],
        enabled=_all_enabled("a"),
        timeouts=_uniform_timeouts(5, "a"),
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError):
        await registry.run(seed_input="diff", context=_context())

    spans = in_memory_span_exporter.get_finished_spans()
    child = next(span for span in spans if span.name == "review.step.a")
    attrs = dict(child.attributes or {})
    # Privacy/Compliance NFR: exactly this fixed allowlist — no "error"
    # key, and the exception's own text never appears in any value.
    assert set(attrs) == {"review.step.name", "review.step.status", "review.step.duration_ms"}
    assert attrs["review.step.status"] == "failed"
    assert "do-not-leak-this-secret" not in " ".join(str(v) for v in attrs.values())
    assert child.status.status_code != otel_trace.StatusCode.ERROR


@pytest.mark.asyncio
async def test_run_child_span_records_timeout_status(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    slow = _FakeStep(name="a", delay_seconds=0.3)
    registry = StepRegistry(
        [slow],
        enabled=_all_enabled("a"),
        timeouts={"a": 0.05},
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError):
        await registry.run(seed_input="diff", context=_context())

    spans = in_memory_span_exporter.get_finished_spans()
    child = next(span for span in spans if span.name == "review.step.a")
    assert dict(child.attributes or {})["review.step.status"] == "timeout"


@pytest.mark.asyncio
async def test_run_disabled_step_never_gets_a_child_span(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    enabled_step = _FakeStep(name="a")
    disabled_step = _FakeStep(name="b")
    registry = StepRegistry(
        [enabled_step, disabled_step],
        enabled={"a": True, "b": False},
        timeouts=_uniform_timeouts(5, "a", "b"),
        on_failure=FailurePolicy.FATAL,
    )

    await registry.run(seed_input="diff", context=_context())

    span_names = {span.name for span in in_memory_span_exporter.get_finished_spans()}
    assert "review.step.a" in span_names
    assert "review.step.b" not in span_names


@pytest.mark.asyncio
async def test_step_duration_histogram_records_bounded_attributes_matching_span(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    succeeding = _FakeStep(name="a")
    failing = _FakeStep(name="b", error=RuntimeError("do-not-leak-this-either"))
    registry = StepRegistry(
        [succeeding, failing],
        enabled=_all_enabled("a", "b"),
        timeouts=_uniform_timeouts(5, "a", "b"),
        on_failure=FailurePolicy.ISOLATE,
    )

    await registry.run(seed_input="diff", context=_context())

    points = _histogram_data_points(in_memory_metric_reader, "review.pipeline.step.duration_ms")
    by_attrs = {tuple(sorted(attrs.items())): (total, count) for total, count, attrs in points}

    success_key = (("status", "success"), ("step_name", "a"))
    failed_key = (("status", "failed"), ("step_name", "b"))
    assert success_key in by_attrs
    assert failed_key in by_attrs
    for total, count in by_attrs.values():
        assert count == 1
        assert total >= 0

    # Privacy/Compliance NFR: every recorded attribute set is exactly
    # {"step_name", "status"} — never the failing step's own exception text.
    for _, _, attrs in points:
        assert set(attrs) == {"step_name", "status"}
        assert "do-not-leak-this-either" not in " ".join(str(v) for v in attrs.values())
