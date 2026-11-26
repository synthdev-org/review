"""adapter.storage.queue — the Postgres-backed job queue's claim/enqueue
repository.

Claiming uses SQLAlchemy's native ``with_for_update(skip_locked=True)`` —
the ORM/Core primitive ADR-0002 already put in the stack for exactly this pattern —
never hand-rolled raw SQL. The SELECT is parameterized by SQLAlchemy Core
like every other query here, so there is no injection surface.

``mark_done``/``mark_failed`` apply the
``CLAIMED`` -> ``DONE``/``FAILED`` terminal-status transitions
that ``apps/worker/dispatch.py``'s dispatch loop requests once a claimed job's
pipeline run finishes or fails fatally.

US1 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) instruments
``enqueue``/``claim_next`` directly with the OTel *API* package, per that
spec's "Resolved decisions" note that domain/adapter code imports OTel's
API directly (no bespoke ``MetricsRecorder`` Protocol), mirroring how
``logging`` is already imported directly here. The meter is created via
``metrics.get_meter("review.queue")`` — the catalog's mandated literal
meter name, since this module's own dotted path (``adapter.storage.queue``)
doesn't match it. Every value recorded is a count, a duration, or a
bounded-cardinality tag (``job_type``) — never payload/job content, per the
spec's Privacy NFR.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from opentelemetry import metrics
from sqlalchemy import event, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from adapter.storage.models import JobQueueRow, JobStatus, JobType
from adapter.storage.schemas import ClaimedJob, claimed_job_from_row
from adapter.storage.unit_of_work import run_in_session
from adapter.telemetry.sdk import clamp_clock_skew_duration_seconds, suppress_telemetry_errors

logger = logging.getLogger(__name__)

# Meter name is the catalog's mandated "review.queue" literal (not `__name__`
# — this module's dotted path is `adapter.storage.queue`, which wouldn't
# match), the same explicit-literal pattern `review/output.py` and
# `review/review_generation.py` use whenever the module path doesn't already
# equal the catalog's meter name.
meter = metrics.get_meter("review.queue")

# Catalog names, verbatim (docs/specs/spec-observability.md's METRIC & SPAN
# CATALOG, US1) — attributes are bounded-cardinality tags only (`job_type`),
# never job payload content.
_enqueued_counter = meter.create_counter(
    "review.queue.enqueued_total", description="Jobs inserted into the queue."
)
_claimed_counter = meter.create_counter(
    "review.queue.claimed_total", description="Jobs successfully claimed off the queue."
)
_wait_histogram = meter.create_histogram(
    "review.queue.wait_seconds",
    unit="s",
    description="Time a job spent PENDING before being claimed (claimed_at - created_at).",
)
# Not a real retry mechanism (dispatch.py's own module docstring names the
# gap: no reclaim/retry logic exists yet) — this exports the one real
# existing signal, `JobQueueRow.attempts` (incremented once per successful
# claim), so an operator can at least see attempts-per-claim rather than
# nothing.
_job_attempts_histogram = meter.create_histogram(
    "review.queue.job_attempts",
    description="ClaimedJob.attempts at claim time — the real existing attempt-count signal, "
    "not a retry/reclaim mechanism (none exists yet).",
)


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

        ``session``, additive and optional: when omitted, this method keeps
        opening and committing its own session exactly as before — every
        existing call site is unaffected. When supplied (only
        ``apps/webhook_api/webhook_routes.py``'s enqueuing path does this),
        the insert is staged on the caller's own already-open session
        instead, so it commits (or rolls back) together with that caller's
        other writes in one shared transaction rather than opening a second,
        independent one.

        The ``review.queue.enqueued_total`` counter is recorded only once
        the transaction the insert lands in has actually committed — never
        on rollback, and never more than once. When this call owns (and
        therefore has already committed) its own session, that's
        straightforward: ``record_enqueued`` runs immediately after. When
        ``session`` is caller-supplied, this method has no way to know
        synchronously whether the caller's shared transaction will
        ultimately commit or roll back, so instead of relying on the
        caller to remember a separate follow-up call (a contract
        previously enforced only by this docstring, and easy for a future
        caller-supplied-session call site to forget), it registers an
        ``after_commit`` hook directly on the caller's own session —
        SQLAlchemy's session-level event, which fires only on an actual
        commit, never a rollback. Passes ``once=True`` (SQLAlchemy's own
        ``event.listen`` docs mark this deprecated/private and note it does
        *not* actually de-register the listener afterwards) rather than
        having the callback remove itself: a self-removing callback calling
        ``event.remove()`` from inside its own dispatch raises
        ``RuntimeError: deque mutated during iteration`` (verified against
        the installed SQLAlchemy's listener-dispatch implementation, which
        iterates the listener deque directly, not a snapshot). Every
        caller-supplied session in this codebase carries exactly one
        transaction for its whole lifetime (see ``_run_in_transaction``,
        ``apps/webhook_api/webhook_routes.py``) — opened, committed or
        rolled back once, then closed — so the listener never outlives that
        one commit regardless, and ``once=True``'s lack of de-registration
        has no practical effect here. Callers no longer need to call
        ``record_enqueued`` themselves.
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

        job_id = await run_in_session(self._session_factory, session, _insert)
        if session is None:
            self.record_enqueued(job_type)
        else:

            def _record_after_commit(_sync_session: Session) -> None:
                self.record_enqueued(job_type)

            event.listen(session.sync_session, "after_commit", _record_after_commit, once=True)
        return job_id

    def record_enqueued(self, job_type: JobType) -> None:
        """Records the ``review.queue.enqueued_total`` counter for one
        successfully, durably enqueued job. Called automatically by
        ``enqueue()`` itself — either immediately (when it owns and has
        committed its own transaction) or via the one-shot ``after_commit``
        hook it registers on a caller-supplied session (see ``enqueue()``'s
        own docstring) — so callers never need to call this directly.
        """
        with suppress_telemetry_errors(
            logger,
            "enqueue telemetry recording failed (job enqueued successfully)",
            job_type=job_type.value,
        ):
            _enqueued_counter.add(1, {"job_type": job_type.value})

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
            claimed = claimed_job_from_row(row)

        # `claimed_at` is always set (to `datetime.now(UTC)`) a few lines
        # above, before `claimed_job_from_row` builds `claimed` — asserting
        # the invariant (rather than branching on it) both narrows the type
        # for mypy and documents that the `None` case in `ClaimedJob.
        # claimed_at`'s type is for other callers, not this one. Kept
        # outside the `suppress_telemetry_errors` block below: this checks a
        # real programming invariant, not a telemetry-recording concern, so
        # a violation must surface loudly rather than being logged and
        # swallowed as a benign "telemetry recording failed" message.
        assert claimed.claimed_at is not None

        # Recorded only after the transaction above has already committed
        # (the claim is durable regardless of what happens here) — a
        # telemetry-recording failure must never roll back an otherwise
        # successful claim, and must never propagate to the caller as a
        # claim failure (that would leave the row stuck CLAIMED forever).
        with suppress_telemetry_errors(
            logger,
            "claim_next telemetry recording failed (claim unaffected)",
            job_id=str(claimed.id),
            job_type=claimed.job_type.value,
        ):
            attributes = {"job_type": claimed.job_type.value}
            _claimed_counter.add(1, attributes)
            wait_seconds = (claimed.claimed_at - claimed.created_at).total_seconds()
            # `claimed_at` is this process's own clock while `created_at`
            # came from the database server's clock — see
            # `clamp_clock_skew_duration_seconds`'s own docstring for why
            # this needs clamping.
            _wait_histogram.record(clamp_clock_skew_duration_seconds(wait_seconds), attributes)
            _job_attempts_histogram.record(claimed.attempts, attributes)
        return claimed

    async def mark_done(self, job_id: uuid.UUID) -> bool:
        """Transition a `CLAIMED` job to `DONE` — the pipeline completed
        successfully (or, for an `INDEX` job, "logged and left unimplemented" is itself
        this job's complete, correct handling). Called by
        ``apps/worker/dispatch.py``, never by the enqueue/claim paths
        above.

        Returns whether this call actually performed the transition (the
        row was ``CLAIMED``) — ``False`` means the row was already
        terminal (e.g. a previous ``mark_done``/``mark_failed`` for the
        same job already landed). Callers must use this to decide whether
        to record completion telemetry, so a job's completion is never
        counted more than once.
        """
        return await self._set_status(job_id, JobStatus.DONE)

    async def mark_failed(self, job_id: uuid.UUID) -> bool:
        """Transition a `CLAIMED` job to `FAILED` — a fatal macro-step
        failure (``review.pipeline.PipelineFailedError``) or an unhandled
        bug in the Worker's own dispatch loop. Called by ``apps/worker/dispatch.py``.

        Returns whether this call actually performed the transition — see
        ``mark_done``'s docstring for why callers must check this before
        recording completion telemetry.
        """
        return await self._set_status(job_id, JobStatus.FAILED)

    async def _set_status(self, job_id: uuid.UUID, status: JobStatus) -> bool:
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
                return False
            return True
