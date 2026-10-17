"""Integration tests for the Worker's free-tier usage counter write path
(Unit H, US-A19), per docs/plans/plan-deliver-admin-gui.md's Phase 7
"Tests" section:

"completed private-repo job increments the right row; public-repo job
never touches the table; two jobs either side of 04:00 land in different
rows; a fake UsageCounterRecorder that raises on increment() — asserting
the REVIEW job still completes successfully and the failure metric below
is incremented."

Real Postgres via `testcontainers-python` (`migrated_engine`, imported into
`apps/worker/conftest.py` from `adapter/storage/conftest.py`), real
claim/dispatch against directly-enqueued `REVIEW` jobs — no HTTP/webhook
layer involved, mirroring `apps/worker/test_dispatch.py`'s own module
docstring. The dispatch-loop-driving helpers below are kept small and
self-contained in this file rather than imported from
`apps/worker/test_dispatch.py`/`apps/worker/test_model_routing_integration.py`,
the same "stays self-contained" convention this package's other test
modules already establish for their own fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import (
    JobQueueRow,
    JobStatus,
    JobType,
    Org,
    PrivateRepoReviewCount,
    Repo,
)
from adapter.storage.queue import JobQueueRepository
from adapter.storage.tenancy import PostgresUsageCounterRepository, day_for
from apps.worker.dispatch import run_dispatch_loop
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.pipeline import IndexingJobDependencies
from indexing.repository import IndexCommitTracker, IndexRepository
from memory.reaction_poll import MemoryReactionPollJobDependencies
from memory.store import ReactionFetcher
from review.pipeline import FailurePolicy, StepRegistry
from tenancy.usage import UsageCounterRecorder

pytestmark = pytest.mark.integration

_WORKER_ID = "test-worker"
_POLL_INTERVAL_SECONDS = 0.05
_WAIT_TIMEOUT_SECONDS = 5.0


@dataclass
class _NullIndexRepository:
    """Never touches Postgres — no test below dispatches an `INDEX` job."""

    async def upsert_file(self, **kwargs: object) -> None:
        raise AssertionError("_NullIndexRepository.upsert_file should never be called")

    async def delete_file(self, **kwargs: object) -> int:
        raise AssertionError("_NullIndexRepository.delete_file should never be called")

    async def get_last_indexed_commit(self, **kwargs: object) -> str | None:
        raise AssertionError("_NullIndexRepository.get_last_indexed_commit should never be called")

    async def set_last_indexed_commit(self, **kwargs: object) -> None:
        raise AssertionError("_NullIndexRepository.set_last_indexed_commit should never be called")


_: type[IndexRepository] = _NullIndexRepository
__: type[IndexCommitTracker] = _NullIndexRepository


def _default_indexing_deps() -> IndexingJobDependencies:
    null_repo = _NullIndexRepository()
    return IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(),
        embedding_client=FakeEmbeddingClient(),
        index_repository=null_repo,
        commit_tracker=null_repo,
    )


@dataclass
class _NullMemoryReactionPollDeps:
    """Never touches Postgres — no test below dispatches a
    `MEMORY_REACTION_POLL` job."""

    async def fetch_reactions(self, **kwargs: object) -> dict[int, tuple[Any, ...]]:
        raise AssertionError("_NullMemoryReactionPollDeps.fetch_reactions should never be called")

    async def record_comment_provenance(self, **kwargs: object) -> None:
        raise AssertionError(
            "_NullMemoryReactionPollDeps.record_comment_provenance should never be called"
        )

    async def record_signal(self, **kwargs: object) -> bool:
        raise AssertionError("_NullMemoryReactionPollDeps.record_signal should never be called")

    async def recent_comment_ids(self, **kwargs: object) -> tuple[int, ...]:
        raise AssertionError(
            "_NullMemoryReactionPollDeps.recent_comment_ids should never be called"
        )

    async def filter_new(self, **kwargs: object) -> tuple[int, ...]:
        raise AssertionError("_NullMemoryReactionPollDeps.filter_new should never be called")


___: type[ReactionFetcher] = _NullMemoryReactionPollDeps


def _default_memory_reaction_poll_deps() -> MemoryReactionPollJobDependencies:
    null_deps = _NullMemoryReactionPollDeps()
    return MemoryReactionPollJobDependencies(
        reaction_fetcher=null_deps,
        signal_recorder=null_deps,
        dedup_store=null_deps,
        max_comments_per_poll=50,
    )


def _empty_registry() -> StepRegistry:
    """A trivial pipeline with zero steps — every test below only cares
    about the `REVIEW` job's terminal status and this phase's own
    usage-counter side effect, not any real review-pipeline output,
    mirroring `apps/worker/test_dispatch.py`'s own `_empty_registry`."""
    return StepRegistry([], enabled={}, timeouts={}, on_failure=FailurePolicy.FATAL)


@dataclass
class _RaisingUsageCounterRecorder:
    """A `UsageCounterRecorder` that always raises — the plan's own
    explicitly-required case: 'a fake UsageCounterRecorder that raises on
    increment() — asserting the REVIEW job still completes successfully
    and the failure metric below is incremented.'"""

    calls: list[tuple[int, datetime]]

    async def increment(self, *, installation_id: int, now: datetime) -> None:
        self.calls.append((installation_id, now))
        raise RuntimeError("connection refused")


@contextlib.asynccontextmanager
async def _running_dispatch_loop(
    *,
    job_queue: JobQueueRepository,
    usage_counter_recorder: UsageCounterRecorder | None,
) -> AsyncIterator[None]:
    """Mirrors `apps/worker/test_dispatch.py`'s own identical fixture —
    `run_dispatch_loop` never returns on its own, so every test needs this
    to observe its effects and still let the test process exit cleanly."""
    task: asyncio.Task[None] = asyncio.create_task(
        run_dispatch_loop(
            job_queue=job_queue,
            review_registry=_empty_registry(),
            indexing_deps=_default_indexing_deps(),
            memory_reaction_poll_deps=_default_memory_reaction_poll_deps(),
            worker_id=_WORKER_ID,
            poll_interval_seconds=_POLL_INTERVAL_SECONDS,
            stuck_job_warning_threshold_seconds=30.0,
            usage_counter_recorder=usage_counter_recorder,
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_for_status(
    engine: AsyncEngine,
    job_id: uuid.UUID,
    expected: JobStatus,
    *,
    timeout: float = _WAIT_TIMEOUT_SECONDS,
    poll: float = 0.02,
) -> None:
    session_factory = create_session_factory(engine)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_status: JobStatus | None = None
    while loop.time() < deadline:
        async with session_factory() as session:
            row = await session.get(JobQueueRow, job_id)
            assert row is not None, f"job {job_id} vanished from job_queue"
            last_status = row.status
            if row.status == expected:
                return
        await asyncio.sleep(poll)
    raise AssertionError(
        f"job {job_id} did not reach status {expected!r} within {timeout}s (last: {last_status!r})"
    )


@pytest.fixture
def in_memory_metric_reader() -> Iterator[InMemoryMetricReader]:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `apps.worker.dispatch`'s module-level
    `review.usage_counter` meter/counter (created once, at import time)
    picks it up via OTel's proxy-instrument migration mechanism — mirrors
    `apps/worker/test_dispatch.py`'s identical fixture.
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
    assert metrics_data is not None
    points: list[tuple[float, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append((data_point.value, dict(data_point.attributes or {})))
    return points


async def _row_count(engine: AsyncEngine, *, installation_id: int, day: Any) -> int | None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(PrivateRepoReviewCount).where(
                    PrivateRepoReviewCount.installation_id == installation_id,
                    PrivateRepoReviewCount.day == day,
                )
            )
        ).scalar_one_or_none()
        return row.count if row is not None else None


# =============================================================================
# US-A19: counted / not-counted, real dispatch loop, real Postgres.
# =============================================================================


@pytest.mark.asyncio
async def test_completed_private_repo_review_job_increments_the_right_row(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    usage_counter_recorder = PostgresUsageCounterRepository(session_factory)
    installation_id = 424242

    job_id = await job_queue.enqueue(
        JobType.REVIEW, {"pr_number": 1, "installation_id": installation_id, "private": True}
    )

    async with _running_dispatch_loop(
        job_queue=job_queue, usage_counter_recorder=usage_counter_recorder
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)
        # The dispatch loop's own increment call races this assertion
        # (the job is already DONE, but the fail-soft increment happens
        # just after) — poll briefly rather than asserting immediately.
        deadline = asyncio.get_running_loop().time() + _WAIT_TIMEOUT_SECONDS
        count = None
        while asyncio.get_running_loop().time() < deadline:
            count = await _row_count(
                migrated_engine, installation_id=installation_id, day=day_for(datetime.now(UTC))
            )
            if count is not None:
                break
            await asyncio.sleep(0.02)

    assert count == 1


@pytest.mark.asyncio
async def test_public_repo_review_job_never_touches_the_table(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    usage_counter_recorder = PostgresUsageCounterRepository(session_factory)
    installation_id = 515151

    job_id = await job_queue.enqueue(
        JobType.REVIEW, {"pr_number": 1, "installation_id": installation_id, "private": False}
    )

    async with _running_dispatch_loop(
        job_queue=job_queue, usage_counter_recorder=usage_counter_recorder
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)
        # Give the (should-never-fire) increment call a beat to have run
        # if the implementation were wrong, rather than asserting the
        # instant DONE is observed.
        await asyncio.sleep(_POLL_INTERVAL_SECONDS * 3)
        count = await _row_count(
            migrated_engine, installation_id=installation_id, day=day_for(datetime.now(UTC))
        )

    assert count is None


@pytest.mark.asyncio
async def test_review_job_completes_despite_usage_counter_recorder_raising(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """The plan's own explicitly-required fail-soft proof: a
    `UsageCounterRecorder` that always raises must never turn an
    otherwise-successful `REVIEW` job into a `FAILED` one, and the
    dedicated `review.usage_counter.increment_failed_total` counter must
    record the failure."""
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    raising_recorder = _RaisingUsageCounterRecorder(calls=[])
    installation_id = 606060

    job_id = await job_queue.enqueue(
        JobType.REVIEW, {"pr_number": 1, "installation_id": installation_id, "private": True}
    )

    async with _running_dispatch_loop(job_queue=job_queue, usage_counter_recorder=raising_recorder):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)
        deadline = asyncio.get_running_loop().time() + _WAIT_TIMEOUT_SECONDS
        while not raising_recorder.calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)

    assert len(raising_recorder.calls) == 1
    assert raising_recorder.calls[0][0] == installation_id

    failed_points = _sum_points(
        in_memory_metric_reader, "review.usage_counter.increment_failed_total"
    )
    assert sum(value for value, _attrs in failed_points) == 1


@pytest.mark.asyncio
async def test_no_recorder_wired_in_is_a_no_op(migrated_engine: AsyncEngine) -> None:
    """`usage_counter_recorder=None` (every pre-Phase-7 call site's
    default) must behave exactly like today: the job still completes, and
    nothing raises for lack of a recorder."""
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)

    job_id = await job_queue.enqueue(
        JobType.REVIEW, {"pr_number": 1, "installation_id": 707070, "private": True}
    )

    async with _running_dispatch_loop(job_queue=job_queue, usage_counter_recorder=None):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)


# =============================================================================
# US-A19's 04:00 reset boundary — exercised directly against the real
# repository/Postgres rather than through the full dispatch loop, since
# the dispatch loop always reads the wall clock (`datetime.now(UTC)`)
# internally with no seam to inject a fixed time; the repository's own
# `increment`/`day_for` accept an explicit `now`, which is the real unit
# of behavior this boundary case is about.
# =============================================================================


@pytest.mark.asyncio
async def test_two_increments_either_side_of_04_00_land_in_different_rows(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    repository = PostgresUsageCounterRepository(session_factory)
    installation_id = 808080

    just_before_reset = datetime(2026, 5, 1, 3, 59, 0, tzinfo=UTC)
    just_after_reset = datetime(2026, 5, 1, 4, 1, 0, tzinfo=UTC)

    await repository.increment(installation_id=installation_id, now=just_before_reset)
    await repository.increment(installation_id=installation_id, now=just_after_reset)

    previous_day_count = await _row_count(
        migrated_engine, installation_id=installation_id, day=day_for(just_before_reset)
    )
    new_day_count = await _row_count(
        migrated_engine, installation_id=installation_id, day=day_for(just_after_reset)
    )

    assert previous_day_count == 1
    assert new_day_count == 1
    assert day_for(just_before_reset) != day_for(just_after_reset)


@pytest.mark.asyncio
async def test_repeated_increments_on_the_same_day_accumulate_on_one_row(
    migrated_engine: AsyncEngine,
) -> None:
    """Sanity check for the atomic upsert itself — three increments for
    the same installation/day land on one row with count 3, not three
    separate rows."""
    session_factory = create_session_factory(migrated_engine)
    repository = PostgresUsageCounterRepository(session_factory)
    installation_id = 909090
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)

    await repository.increment(installation_id=installation_id, now=now)
    await repository.increment(installation_id=installation_id, now=now)
    await repository.increment(installation_id=installation_id, now=now)

    count = await _row_count(migrated_engine, installation_id=installation_id, day=day_for(now))
    assert count == 3


async def _provision_installation(
    engine: AsyncEngine, *, installation_id: int, org_name: str
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        org = Org(name=org_name, slug=f"{org_name}-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(
            org_id=org.id,
            name="widgets",
            full_name=f"{org_name}/widgets",
            installation_id=installation_id,
        )
        session.add(repo)


@pytest.mark.asyncio
async def test_list_current_day_counts_resolves_org_identity_via_repo(
    migrated_engine: AsyncEngine,
) -> None:
    """`PostgresUsageCounterRepository.list_current_day_counts` (the read
    side `apps/admin_api/usage_routes.py` consumes, US-A18) joins through
    `Repo` to resolve each installation's connected org — exercised here
    directly against the repository, complementing
    `apps/admin_api/test_usage_routes.py`'s own route-level coverage."""
    session_factory = create_session_factory(migrated_engine)
    repository = PostgresUsageCounterRepository(session_factory)
    installation_id = 111213
    await _provision_installation(migrated_engine, installation_id=installation_id, org_name="acme")

    now = datetime.now(UTC)
    await repository.increment(installation_id=installation_id, now=now)

    rows = await repository.list_current_day_counts(now=now)
    matching = [row for row in rows if row.installation_id == installation_id]
    assert len(matching) == 1
    assert matching[0].org_name == "acme"
    assert matching[0].count == 1
    assert matching[0].over_threshold is False
