"""Integration test: two concurrent claimers against a fixture-seeded
`job_queue` table never both claim the same row — the `SKIP LOCKED`
guarantee `adapter/storage/queue.py`'s `with_for_update(skip_locked=True)`
claim query relies on. Per the plan's Phase 2 Tests/Acceptance sections,
this runs against a real Postgres (testcontainers), not a mock.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_concurrent_claimers_never_claim_the_same_row(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    job_id = await repository.enqueue(JobType.REVIEW, {"pr_number": 1})

    results = await asyncio.gather(
        repository.claim_next("worker-a"),
        repository.claim_next("worker-b"),
    )

    claimed = [job for job in results if job is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert claimed[0].claimed_by in {"worker-a", "worker-b"}


@pytest.mark.asyncio
async def test_claim_next_returns_none_when_queue_is_empty(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    result = await repository.claim_next("worker-a")

    assert result is None


@pytest.mark.asyncio
async def test_many_concurrent_claimers_each_get_a_distinct_job(
    migrated_engine: AsyncEngine,
) -> None:
    """A stronger form of the SKIP LOCKED guarantee: with N pending jobs and
    N concurrent claimers, every job is claimed exactly once."""
    session_factory = create_session_factory(migrated_engine)
    repository = JobQueueRepository(session_factory)

    job_ids = {await repository.enqueue(JobType.INDEX, {"n": i}) for i in range(5)}

    results = await asyncio.gather(*(repository.claim_next(f"worker-{i}") for i in range(5)))

    claimed_ids = {job.id for job in results if job is not None}
    assert claimed_ids == job_ids
    assert all(job is not None for job in results)
