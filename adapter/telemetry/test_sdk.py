"""Unit tests for adapter.telemetry.sdk — genuinely no I/O (per
testing.md's Unit tier): ``init_telemetry``'s own recording path never
touches the network (see that module's docstring), and every test below
either exercises the ``otlp_endpoint=None`` no-op path or, when a real
endpoint is passed, only inspects the constructed
``TracerProvider``/``MeterProvider`` objects' own attributes — it never
waits for or asserts on an actual export happening over the wire (that's
``test_telemetry_integration.py``'s job, against a real receiver).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

import pytest
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from adapter.telemetry import sdk

# Unused endpoint, deliberately: nothing in this file ever waits for an
# export to actually reach it (see module docstring above) — this is only
# ever passed to a constructor, never dialed synchronously.
_FAKE_ENDPOINT = "http://127.0.0.1:4317"


@pytest.fixture
def _shutdown_global_providers_after_test() -> Iterator[None]:
    """Every test that registers a real provider via
    ``init_telemetry(otlp_endpoint=...)`` leaves a
    ``PeriodicExportingMetricReader`` background thread running (it starts
    one at construction time, independent of whether any export has
    happened yet) — shut both providers down on teardown so it doesn't
    outlive the test.
    """
    yield
    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    assert isinstance(tracer_provider, TracerProvider)
    assert isinstance(meter_provider, MeterProvider)
    tracer_provider.shutdown()
    meter_provider.shutdown()


def test_init_telemetry_is_a_true_noop_when_endpoint_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracer_provider_before = trace.get_tracer_provider()
    meter_provider_before = metrics.get_meter_provider()

    with caplog.at_level(logging.DEBUG):
        sdk.init_telemetry(otlp_endpoint=None, service_name="review-test")

    # US10: no SDK/exporter constructed at all — the global providers are
    # untouched (identity, not just equality), and nothing was logged.
    assert trace.get_tracer_provider() is tracer_provider_before
    assert metrics.get_meter_provider() is meter_provider_before
    assert caplog.records == []


def test_init_telemetry_is_a_true_noop_regardless_of_headers_argument() -> None:
    """``otlp_headers`` must never itself trigger construction — only
    ``otlp_endpoint`` gates US10's no-op."""
    tracer_provider_before = trace.get_tracer_provider()

    sdk.init_telemetry(
        otlp_endpoint=None, service_name="review-test", otlp_headers="authorization=Bearer x"
    )

    assert trace.get_tracer_provider() is tracer_provider_before


@pytest.mark.usefixtures("reset_otel_global_providers", "_shutdown_global_providers_after_test")
def test_init_telemetry_registers_real_providers_when_endpoint_set() -> None:
    sdk.init_telemetry(otlp_endpoint=_FAKE_ENDPOINT, service_name="review-webhook-api")

    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    assert isinstance(tracer_provider, TracerProvider)
    assert isinstance(meter_provider, MeterProvider)
    assert tracer_provider.resource.attributes["service.name"] == "review-webhook-api"
    assert meter_provider._sdk_config.resource.attributes["service.name"] == "review-webhook-api"


@pytest.mark.usefixtures("reset_otel_global_providers", "_shutdown_global_providers_after_test")
def test_init_telemetry_wires_the_same_endpoint_and_headers_into_both_exporters() -> None:
    headers = "authorization=Bearer test-token"

    sdk.init_telemetry(
        otlp_endpoint=_FAKE_ENDPOINT, service_name="review-worker", otlp_headers=headers
    )

    tracer_provider = trace.get_tracer_provider()
    assert isinstance(tracer_provider, TracerProvider)
    (span_processor,) = tracer_provider._active_span_processor._span_processors
    assert isinstance(span_processor, BatchSpanProcessor)
    span_exporter = span_processor.span_exporter
    assert isinstance(span_exporter, OTLPSpanExporter)
    assert span_exporter._endpoint == "127.0.0.1:4317"
    assert isinstance(span_exporter._headers, tuple)
    assert ("authorization", "Bearer test-token") in span_exporter._headers

    meter_provider = metrics.get_meter_provider()
    assert isinstance(meter_provider, MeterProvider)
    (metric_reader,) = meter_provider._sdk_config.metric_readers
    metric_exporter = metric_reader._exporter
    assert isinstance(metric_exporter, OTLPMetricExporter)
    assert metric_exporter._endpoint == "127.0.0.1:4317"
    assert isinstance(metric_exporter._headers, tuple)
    assert ("authorization", "Bearer test-token") in metric_exporter._headers


def test_rate_limited_log_filter_allows_first_record_then_blocks_within_window() -> None:
    log_filter = sdk._RateLimitedLogFilter(window_seconds=60.0)
    record = logging.LogRecord("otel.test", logging.ERROR, __file__, 1, "export failed", (), None)

    assert log_filter.filter(record) is True
    assert log_filter.filter(record) is False
    assert log_filter.filter(record) is False


def test_rate_limited_log_filter_allows_again_after_window_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_filter = sdk._RateLimitedLogFilter(window_seconds=10.0)
    record = logging.LogRecord("otel.test", logging.ERROR, __file__, 1, "export failed", (), None)
    fake_now = 1_000.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)

    assert log_filter.filter(record) is True
    assert log_filter.filter(record) is False

    fake_now += 10.0
    assert log_filter.filter(record) is True


def test_install_rate_limited_exporter_failure_logging_attaches_filter_to_known_loggers() -> None:
    sdk._install_rate_limited_exporter_failure_logging(window_seconds=5.0)

    for logger_name in sdk._EXPORTER_FAILURE_LOGGER_NAMES:
        installed = logging.getLogger(logger_name).filters
        assert any(isinstance(f, sdk._RateLimitedLogFilter) for f in installed)


@pytest.mark.asyncio
async def test_shutdown_telemetry_never_raises_when_a_provider_shutdown_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``MeterProvider.shutdown``/``.force_flush`` re-raise a bare
    ``Exception`` when a metric reader fails (e.g. a timeout flushing to an
    unreachable collector) — the exact scenario this module's own docstring
    names as its highest-risk failure mode. ``shutdown_telemetry()`` must
    swallow that from either provider independently, not let it propagate
    into a composition root's own shutdown path."""

    class _RaisingProvider:
        def force_flush(self) -> bool:
            raise Exception("collector unreachable: flush timed out")

        def shutdown(self) -> None:
            raise Exception("collector unreachable: shutdown timed out")

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: _RaisingProvider())
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: _RaisingProvider())

    with caplog.at_level(logging.WARNING):
        await sdk.shutdown_telemetry()

    assert sum("telemetry shutdown failed" in record.message for record in caplog.records) == 2


@pytest.mark.asyncio
async def test_shutdown_telemetry_runs_providers_concurrently_not_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each provider's flush/shutdown is bounded by its own timeout and run
    off the event loop — asserts the two providers' blocking work overlaps
    rather than summing, so a slow/unreachable collector for one provider
    doesn't double the total shutdown wait."""
    import time as time_module

    class _SlowProvider:
        def force_flush(self) -> bool:
            time_module.sleep(0.2)
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: _SlowProvider())
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: _SlowProvider())

    started = time_module.monotonic()
    await sdk.shutdown_telemetry()
    elapsed = time_module.monotonic() - started

    assert elapsed < 0.35


@pytest.mark.asyncio
async def test_shutdown_telemetry_bounds_wall_time_even_when_the_underlying_call_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the gap `shutdown_telemetry`'s own docstring
    names: `asyncio.wait_for` alone only stops *awaiting* a blocking call,
    it doesn't cancel the thread running it — if that thread were the
    event loop's own default executor (the old `asyncio.to_thread`
    implementation), `asyncio.run()`'s own teardown
    (`loop.shutdown_default_executor()`) would still block on joining it
    past `_SHUTDOWN_TIMEOUT_SECONDS`. `_flush_and_shutdown` now runs on a
    plain daemon thread instead (`_run_in_daemon_thread`), so this asserts
    both that the await genuinely returns within the timeout bound *and*
    that the thread it abandoned is a daemon thread the interpreter won't
    wait to join at process exit.
    """
    monkeypatch.setattr(sdk, "_SHUTDOWN_TIMEOUT_SECONDS", 0.1)

    class _HangingProvider:
        def force_flush(self, timeout_millis: float | None = None) -> bool:
            time.sleep(5.0)
            return True

        def shutdown(self, timeout_millis: float | None = None) -> None:
            return None

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: _HangingProvider())
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: _HangingProvider())

    spawned_threads: list[threading.Thread] = []
    real_thread_cls = threading.Thread

    def _capturing_thread(*args: object, **kwargs: object) -> threading.Thread:
        thread = real_thread_cls(*args, **kwargs)  # type: ignore[arg-type]
        spawned_threads.append(thread)
        return thread

    monkeypatch.setattr(threading, "Thread", _capturing_thread)

    started = time.monotonic()
    await sdk.shutdown_telemetry()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert spawned_threads
    assert all(thread.daemon for thread in spawned_threads)


@pytest.mark.asyncio
async def test_run_in_daemon_thread_does_not_raise_when_abandoned_thread_finishes_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for a bug the previous test's own hanging-provider
    thread was silently triggering (visible only as a
    ``PytestUnhandledThreadExceptionWarning`` at end-of-session, since that
    thread outlives its test by several seconds): once ``asyncio.wait_for``
    times out and abandons the await, the ``concurrent.futures.Future`` it
    was wrapping is left cancelled. The abandoned thread keeps running and,
    on completion, used to call ``future.set_result``/``set_exception``
    unconditionally — raising ``concurrent.futures.InvalidStateError`` on
    an already-cancelled future, unhandled, on that background thread.
    ``_run_in_daemon_thread`` must swallow that specific race instead of
    letting it surface as an unhandled thread exception.
    """
    uncaught: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)

    finished = threading.Event()

    def _slow_then_done() -> None:
        time.sleep(0.2)
        finished.set()

    with pytest.raises(TimeoutError):
        await sdk._run_in_daemon_thread(_slow_then_done, timeout_seconds=0.01)

    assert finished.wait(timeout=1.0), "abandoned thread never actually finished"
    assert not uncaught, f"abandoned daemon thread raised unhandled: {uncaught}"
