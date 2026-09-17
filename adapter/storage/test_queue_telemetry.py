"""Integration tests for US1's queue-health telemetry
(docs/specs/spec-observability.md's METRIC & SPAN CATALOG), recorded
directly inside `adapter/storage/queue.py`'s `enqueue`/`claim_next`. Real
Postgres via `testcontainers-python` (`migrated_engine`, per
`test_queue_skip_locked.py`'s own identical pattern in this package) plus
OTel's own in-memory metric reader (`InMemoryMetricReader`) — no mocking of
either boundary.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `adapter.storage.queue`'s module-level `meter`/
    counters/histograms (created once, at import time) pick it up via
    OTel's proxy-instrument migration mechanism — see
    `review/test_output_telemetry.py`'s identical fixture docstring.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-worker-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _sum_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[tuple[float, dict[str, object]]]:
    metrics_data = reader.get_metrics_data()
    points: list[tuple[float, dict[str, object]]] = []
    # `get_metrics_data()` returns `None` (not an empty structure) when
    # this reader's `MeterProvider` has never collected a single data point
    # across *any* instrument — a real, expected state for a fresh reader
    # before anything has been recorded, not a test setup bug.
    if metrics_data is None:
        return points
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append((data_point.value, dict(data_point.attributes or {})))
    return points


def _histogram_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[tuple[int, float, float, dict[str, object]]]:
    """Returns `(count, sum, max, attributes)` per recorded data point."""
    metrics_data = reader.get_metrics_data()
    points: list[tuple[int, float, float, dict[str, object]]] = []
    # See `_sum_points`'s own comment: `None` here means "nothing recorded
    # yet", not a setup bug.
    if metrics_data is None:
        return points
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Histogram)
                for data_point in metric.data.data_points:
                    points.append(
                        (
                            data_point.count,
                            data_point.sum,
                            data_point.max,
                            dict(data_point.attributes or {}),
                        )
                    )
    return points


@pytest.mark.asyncio
async def test_enqueue_records_enqueued_total_by_job_type(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    await repository.enqueue(JobType.REVIEW, {"pr_number": 1})
    await repository.enqueue(JobType.REVIEW, {"pr_number": 2})
    await repository.enqueue(JobType.INDEX, {"commit": "abc"})

    points = _sum_points(in_memory_metric_reader, "review.queue.enqueued_total")
    by_job_type = {attrs.get("job_type"): value for value, attrs in points}
    assert by_job_type == {"review": 2, "index": 1}


@pytest.mark.asyncio
async def test_enqueue_with_caller_supplied_session_records_only_after_commit(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """When a caller passes its own session, ``enqueue()`` must record the
    ``review.queue.enqueued_total`` counter itself once that caller's
    transaction actually commits — a one-shot ``after_commit`` hook it
    registers on the caller's session (see ``JobQueueRepository.enqueue``'s
    own docstring), not something the caller has to remember to do."""
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    async with session_factory() as session, session.begin():
        await repository.enqueue(JobType.REVIEW, {"pr_number": 1}, session=session)
        # Not yet committed: nothing recorded.
        assert _sum_points(in_memory_metric_reader, "review.queue.enqueued_total") == []

    points = _sum_points(in_memory_metric_reader, "review.queue.enqueued_total")
    assert points == [(1, {"job_type": "review"})]


@pytest.mark.asyncio
async def test_enqueue_with_caller_supplied_session_records_nothing_on_rollback(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """A caller-supplied session that rolls back (e.g. a sibling candidate
    in the same request got rate-limited, per
    ``apps/webhook_api/webhook_routes.py``'s ``_handle_matched_triggers``)
    must never record the counter — the ``after_commit`` hook only fires on
    an actual commit."""
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    async with session_factory() as session:
        await session.begin()
        await repository.enqueue(JobType.REVIEW, {"pr_number": 1}, session=session)
        await session.rollback()

    assert _sum_points(in_memory_metric_reader, "review.queue.enqueued_total") == []


@pytest.mark.asyncio
async def test_claim_next_records_claimed_total_wait_seconds_and_job_attempts(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)
    await repository.enqueue(JobType.REVIEW, {"pr_number": 1})

    claimed = await repository.claim_next("worker-1")

    assert claimed is not None
    claimed_points = _sum_points(in_memory_metric_reader, "review.queue.claimed_total")
    assert claimed_points == [(1, {"job_type": "review"})]

    wait_points = _histogram_points(in_memory_metric_reader, "review.queue.wait_seconds")
    assert len(wait_points) == 1
    wait_count, wait_sum, _wait_max, wait_attrs = wait_points[0]
    assert wait_count == 1
    assert wait_sum >= 0.0
    assert wait_attrs == {"job_type": "review"}

    attempts_points = _histogram_points(in_memory_metric_reader, "review.queue.job_attempts")
    assert len(attempts_points) == 1
    attempts_count, attempts_sum, _attempts_max, attempts_attrs = attempts_points[0]
    assert attempts_count == 1
    assert attempts_sum == 1.0
    assert attempts_attrs == {"job_type": "review"}


@pytest.mark.asyncio
async def test_claim_next_on_empty_queue_records_nothing(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    result = await repository.claim_next("worker-1")

    assert result is None
    assert _sum_points(in_memory_metric_reader, "review.queue.claimed_total") == []
    assert _histogram_points(in_memory_metric_reader, "review.queue.wait_seconds") == []
    assert _histogram_points(in_memory_metric_reader, "review.queue.job_attempts") == []


@pytest.mark.asyncio
async def test_queue_metric_attributes_are_bounded_and_privacy_safe(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Per this spec's hard privacy requirement: attribute values are never
    job payload content — only the bounded `job_type` vocabulary."""
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)
    await repository.enqueue(
        JobType.REVIEW, {"pr_number": 1, "secret": "definitely-not-a-metric-label"}
    )

    await repository.claim_next("worker-1")

    for metric_name in (
        "review.queue.enqueued_total",
        "review.queue.claimed_total",
    ):
        for _value, attrs in _sum_points(in_memory_metric_reader, metric_name):
            assert set(attrs.keys()) == {"job_type"}
            assert attrs["job_type"] in {"review", "index"}

    for metric_name in ("review.queue.wait_seconds", "review.queue.job_attempts"):
        for _count, _sum, _max, attrs in _histogram_points(in_memory_metric_reader, metric_name):
            assert set(attrs.keys()) == {"job_type"}
            assert attrs["job_type"] in {"review", "index"}
