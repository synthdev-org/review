"""adapter.storage.queue — the Postgres-backed job queue's claim/enqueue
repository.

Per the plan's Phase 2 section and Security review: claiming uses
SQLAlchemy's native ``with_for_update(skip_locked=True)`` — the ORM/Core
primitive ADR-0002 already put in the stack for exactly this pattern —
never hand-rolled raw SQL. The SELECT is parameterized by SQLAlchemy Core
like every other query here, so there is no injection surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import JobQueueRow, JobStatus, JobType
from adapter.storage.schemas import ClaimedJob, claimed_job_from_row


class JobQueueRepository:
    """Enqueue and claim rows in the ``job_queue`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        job_type: JobType,
        payload: dict[str, object],
        *,
        org_id: uuid.UUID | None = None,
        repo_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Insert a new pending job and return its id."""
        row = JobQueueRow(
            job_type=job_type,
            status=JobStatus.PENDING,
            payload=payload,
            org_id=org_id,
            repo_id=repo_id,
        )
        async with self._session_factory() as session, session.begin():
            session.add(row)
            await session.flush()
            job_id = row.id
        return job_id

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        """Atomically claim the oldest pending job, or return ``None`` if
        the queue is empty.

        ``with_for_update(skip_locked=True)`` is what makes concurrent
        Worker processes calling this safe: a row already locked by another
        claimer is skipped rather than blocking this transaction, so two
        concurrent claimers never claim the same row (see
        ``test_queue_skip_locked.py``).
        """
        async with self._session_factory() as session, session.begin():
            statement = (
                select(JobQueueRow)
                .where(JobQueueRow.status == JobStatus.PENDING)
                .order_by(JobQueueRow.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                return None

            row.status = JobStatus.CLAIMED
            row.claimed_by = worker_id
            row.claimed_at = datetime.now(UTC)
            row.attempts += 1
            await session.flush()
            return claimed_job_from_row(row)
