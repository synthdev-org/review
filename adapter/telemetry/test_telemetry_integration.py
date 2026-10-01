"""Integration test for adapter.telemetry.sdk — proves, against a real
(local, ephemeral) OTLP gRPC receiver, that:

(a) spans/metrics recorded in-process via ``init_telemetry`` genuinely
    reach it over the network — this is the "US1-US9 exporter integration,
    happy path" row in the spec's Testing table, which belongs to Unit A,
    not to whichever unit instruments a given metric.
(b) an unreachable receiver (nothing listening) never raises into caller
    code and never blocks a caller — this PRD's own highest-risk failure
    mode (the Reliability NFR). The "a job still completes against a dead
    collector" half of this same scenario is
    ``apps/worker/test_dispatch.py``'s own extension, not duplicated here
    — this file proves the lower-level guarantee
    (``adapter.telemetry.sdk``/OTel's own exporters never raise/block),
    ``test_dispatch.py`` proves a real dispatch loop built on top of it
    still completes a job.

Uses a real ``grpc`` server implementing the actual OTLP
TraceService/MetricsService protobuf interfaces (the same generated code
``opentelemetry-exporter-otlp-proto-grpc`` itself talks to over the wire)
— not an HTTP-layer mock — per testing.md's adapter-integration convention
("a real adapter against a real ... external system"). No new test-only
dependency: ``grpc``/the OTLP proto modules used below are already
transitive dependencies of this repo's own
``opentelemetry-exporter-otlp-proto-grpc`` (see pyproject.toml).
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest
from opentelemetry import metrics, trace
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.sdk.metrics import MeterProvider as SDKMeterProvider
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

from adapter.telemetry.sdk import init_telemetry

pytestmark = pytest.mark.integration


class _RecordingTraceServiceServicer(trace_service_pb2_grpc.TraceServiceServicer):
    """Records every ``Export`` call it receives — a real OTLP gRPC
    service implementation, not a mock at the HTTP/transport layer."""

    def __init__(self) -> None:
        self.requests: list[trace_service_pb2.ExportTraceServiceRequest] = []

    def Export(
        self,
        request: trace_service_pb2.ExportTraceServiceRequest,
        context: grpc.ServicerContext,
    ) -> trace_service_pb2.ExportTraceServiceResponse:
        self.requests.append(request)
        return trace_service_pb2.ExportTraceServiceResponse()


class _RecordingMetricsServiceServicer(metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self) -> None:
        self.requests: list[metrics_service_pb2.ExportMetricsServiceRequest] = []

    def Export(
        self,
        request: metrics_service_pb2.ExportMetricsServiceRequest,
        context: grpc.ServicerContext,
    ) -> metrics_service_pb2.ExportMetricsServiceResponse:
        self.requests.append(request)
        return metrics_service_pb2.ExportMetricsServiceResponse()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class _FakeOtlpReceiver:
    """A real, ephemeral, local gRPC server speaking the OTLP collector
    protocol — started/stopped per test via the ``fake_otlp_receiver``
    fixture below, never shared across tests."""

    def __init__(self) -> None:
        self.trace_servicer = _RecordingTraceServiceServicer()
        self.metrics_servicer = _RecordingMetricsServiceServicer()
        self._server = grpc.server(ThreadPoolExecutor(max_workers=2))
        # Both generated `add_*Servicer_to_server` functions ship with no
        # inline type hints (grpc_tools' own codegen, unlike the
        # message/stub classes in the same generated package, which do —
        # see pyproject.toml's `grpc.*` mypy override comment for the
        # broader "no stubs for the grpc-generated surface" gap this is
        # part of).
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(  # type: ignore[no-untyped-call]
            self.trace_servicer, self._server
        )
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(  # type: ignore[no-untyped-call]
            self.metrics_servicer, self._server
        )
        self._port = _free_port()
        self._server.add_insecure_port(f"127.0.0.1:{self._port}")

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop(None)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}"


@pytest.fixture
def fake_otlp_receiver() -> Iterator[_FakeOtlpReceiver]:
    receiver = _FakeOtlpReceiver()
    receiver.start()
    yield receiver
    receiver.stop()


def _wait_until(predicate: Callable[[], bool], *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_seconds}s")


def test_recorded_spans_and_metrics_reach_a_real_otlp_receiver(
    fake_otlp_receiver: _FakeOtlpReceiver,
    reset_otel_global_providers: None,
) -> None:
    init_telemetry(otlp_endpoint=fake_otlp_receiver.endpoint, service_name="review-test")

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("integration-test-span"):
        pass

    meter = metrics.get_meter(__name__)
    meter.create_counter("review.telemetry_integration_test.counter").add(1)

    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    assert isinstance(tracer_provider, SDKTracerProvider)
    assert isinstance(meter_provider, SDKMeterProvider)

    assert tracer_provider.force_flush(timeout_millis=5_000)
    meter_provider.force_flush(timeout_millis=5_000)

    _wait_until(lambda: len(fake_otlp_receiver.trace_servicer.requests) >= 1, timeout_seconds=5.0)
    _wait_until(lambda: len(fake_otlp_receiver.metrics_servicer.requests) >= 1, timeout_seconds=5.0)

    tracer_provider.shutdown()
    meter_provider.shutdown()


def test_init_telemetry_never_raises_or_blocks_when_receiver_is_unreachable(
    reset_otel_global_providers: None,
) -> None:
    """(b) above: nothing listens on this port at all (never started, not
    just killed) — the closest local proxy for "connection refused."
    Recording must never raise, and must never block waiting on the dead
    network target (only the background export thread ever touches the
    network — see sdk.py's own docstring)."""
    unreachable_port = _free_port()

    init_telemetry(otlp_endpoint=f"http://127.0.0.1:{unreachable_port}", service_name="review-test")

    tracer = trace.get_tracer(__name__)
    meter = metrics.get_meter(__name__)
    started_at = time.monotonic()
    with tracer.start_as_current_span("span-against-dead-receiver"):
        pass
    meter.create_counter("review.telemetry_integration_test.counter").add(1)
    elapsed_seconds = time.monotonic() - started_at

    # Recording is purely in-process/buffered (the Performance NFR's own
    # 1ms-per-call ceiling) — this is a coarse sanity bound proving it
    # didn't silently block on the dead network target, not a precise
    # benchmark.
    assert elapsed_seconds < 0.5

    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    assert isinstance(tracer_provider, SDKTracerProvider)
    assert isinstance(meter_provider, SDKMeterProvider)

    # A forced flush against a genuinely unreachable receiver must still
    # return normally rather than raise or hang past its own timeout —
    # OTel's own exporter failure handling (BatchSpanProcessor/
    # PeriodicExportingMetricReader both catch every export exception
    # internally; see sdk.py's own docstring for the source citation).
    tracer_provider.force_flush(timeout_millis=2_000)
    meter_provider.force_flush(timeout_millis=2_000)

    tracer_provider.shutdown()
    meter_provider.shutdown()
