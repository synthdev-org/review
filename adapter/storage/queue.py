"""adapter.storage.queue — the Postgres-backed job queue's claim/enqueue
repository.

Per the plan's Phase 2 section and Security review: claiming uses
SQLAlchemy's native ``with_for_update(skip_locked=True)`` — the ORM/Core
primitive ADR-0002 already put in the stack for exactly this pattern —
never hand-rolled raw SQL. The SELECT is parameterized by SQLAlchemy Core
like every other query here, so there is no injection surface.

Phase 3 of the delivery plan adds ``mark_done``/``mark_failed`` — the
``CLAIMED`` -> ``DONE``/``FAILED`` terminal-status transitions
``apps/worker/dispatch.py``'s dispatch loop applies once a claimed job's
pipeline run finishes or fails fatally.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import JobQueueRow, JobStatus, JobType
from adapter.storage.schemas import ClaimedJob, claimed_job_from_row
from adapter.storage.unit_of_work import run_in_session

logger = logging.getLogger(__name__)


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
        session: AsyncSession | None = None,
    ) -> uuid.UUID:
        """Insert a new pending job and return its id.

        ``session``, additive and optional (per the plan's Phase 1
        "Transaction boundary" section): when omitted, this method keeps
        opening and committing its own session exactly as before — every
        existing call site is unaffected. When supplied (only
        ``apps/webhook_api/webhook_routes.py``'s enqueuing path does this),
        the insert is staged on the caller's own already-open session
        instead, so it commits (or rolls back) together with that caller's
        other writes in one shared transaction rather than opening a second,
        independent one.
        """
        row = JobQueueRow(
            job_type=job_type,
            status=JobStatus.PENDING,
            payload=payload,
            org_id=org_id,
            repo_id=repo_id,
        )

        async def _insert(active_session: AsyncSession) -> uuid.UUID:
            active_session.add(row)
            await active_session.flush()
            return row.id

        return await run_in_session(self._session_factory, session, _insert)

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

    async def mark_done(self, job_id: uuid.UUID) -> None:
        """Transition a `CLAIMED` job to `DONE` — the pipeline completed
        successfully (or, for an `INDEX` job, per the plan's Phase 3
        section, "logged and left unimplemented" is itself this job's
        complete, correct handling for this phase's scope). Called by
        ``apps/worker/dispatch.py``, never by the enqueue/claim paths
        above.
        """
        await self._set_status(job_id, JobStatus.DONE)

    async def mark_failed(self, job_id: uuid.UUID) -> None:
        """Transition a `CLAIMED` job to `FAILED` — a fatal macro-step
        failure (``review.pipeline.PipelineFailedError``) or an unhandled
        bug in the Worker's own dispatch loop, per the plan's Phase 3
        section. Called by ``apps/worker/dispatch.py``.
        """
        await self._set_status(job_id, JobStatus.FAILED)

    async def _set_status(self, job_id: uuid.UUID, status: JobStatus) -> None:
        async with self._session_factory() as session, session.begin():
            # Guarded by the row's current status (only a CLAIMED job may
            # transition to DONE/FAILED) so a stray or duplicate call can
            # never silently overwrite an already-terminal row — e.g. a
            # second `mark_failed` racing a `mark_done` that already landed.
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(JobQueueRow)
                    .where(JobQueueRow.id == job_id, JobQueueRow.status == JobStatus.CLAIMED)
                    .values(status=status)
                ),
            )
            if result.rowcount == 0:
                logger.warning(
                    "job status transition skipped: row not found or not CLAIMED",
                    extra={"job_id": str(job_id), "target_status": status.value},
                )
