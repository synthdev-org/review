"""Unit tests for adapter.telemetry.heartbeat — US14.

Uses OTel's own in-memory metric reader (no I/O, per testing.md's Unit
tier) and ``run_heartbeat_loop``'s own ``interval_seconds`` override (a
short interval, not the real 30s) so the test runs fast without faking
``asyncio``'s clock.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from adapter.telemetry import heartbeat


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real ``MeterProvider`` backed by
    ``InMemoryMetricReader`` as the *global* one — required for
    ``heartbeat.py``'s module-level ``_heartbeat_counter`` (created once,
    at import time, against whatever provider was global then) to pick
    this test's provider up: OTel's proxy-instrument mechanism migrates
    already-created instruments to a real backing instrument the moment a
    real provider is registered via ``metrics.set_meter_provider`` (see
    ``opentelemetry.metrics._internal._ProxyMeter.on_set_meter_provider``)
    — the same mechanism that lets domain code call
    ``meter = metrics.get_meter(__name__)`` at module scope safely, ahead
    of ``adapter.telemetry.sdk.init_telemetry`` ever running.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _heartbeat_total_and_attributes(
    reader: InMemoryMetricReader,
) -> tuple[float, dict[str, object]]:
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "review.process.heartbeat_total":
                    continue
                assert isinstance(metric.data, Sum)
                (data_point,) = metric.data.data_points
                return data_point.value, dict(data_point.attributes or {})
    raise AssertionError("review.process.heartbeat_total was not recorded")


@pytest.mark.asyncio
async def test_run_heartbeat_loop_increments_counter_every_interval(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    task = asyncio.create_task(heartbeat.run_heartbeat_loop(interval_seconds=0.01))
    try:
        await asyncio.sleep(0.055)  # ~5 intervals' worth of headroom
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    total, _ = _heartbeat_total_and_attributes(in_memory_metric_reader)
    assert total >= 2


@pytest.mark.asyncio
async def test_run_heartbeat_loop_records_no_attributes(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Privacy/Compliance + cardinality NFR: the heartbeat carries no
    attributes at all (CATALOG: "no extra attributes") — nothing that
    could ever become source code, diff content, or free text."""
    task = asyncio.create_task(heartbeat.run_heartbeat_loop(interval_seconds=0.01))
    try:
        await asyncio.sleep(0.025)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    _, attributes = _heartbeat_total_and_attributes(in_memory_metric_reader)
    assert attributes == {}


@pytest.mark.asyncio
async def test_run_heartbeat_loop_cancels_cleanly_mid_sleep() -> None:
    """Reliability: a composition root cancels this task on shutdown
    (apps/webhook_api/main.py's lifespan, apps/worker/main.py's
    ``asyncio.gather``) — cancellation during the ``asyncio.sleep`` must
    propagate as plain ``CancelledError``, not hang or raise anything
    else."""
    task = asyncio.create_task(heartbeat.run_heartbeat_loop(interval_seconds=10.0))
    await asyncio.sleep(0)  # let the task actually start and reach the sleep
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
