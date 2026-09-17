"""adapter.storage.schemas — pydantic models for data crossing the storage
adapter's boundary, per docs/rules/architecture.md's "validate every
boundary ... DB rows ... with pydantic models — never trust a raw dict (or
ORM row) past the adapter that received it" rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from adapter.storage.models import JobQueueRow, JobStatus, JobType


class ClaimedJob(BaseModel):
    """A job_queue row handed back to a Worker after a successful claim."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    job_type: JobType
    status: JobStatus
    payload: dict[str, object]
    org_id: uuid.UUID | None
    repo_id: uuid.UUID | None
    attempts: int
    claimed_by: str | None
    claimed_at: datetime | None
    created_at: datetime
    """When the row was inserted (``JobQueueRow.created_at``) — added for
    US1 (docs/specs/spec-observability.md) so callers can compute
    enqueue-to-claim wait time (``claimed_at - created_at``) without a
    second query; a narrow, justified schema addition, not scope creep."""


def claimed_job_from_row(row: JobQueueRow) -> ClaimedJob:
    """Map a ``JobQueueRow`` ORM instance to the pydantic ``ClaimedJob``
    handed back across the adapter boundary.

    Kept as a pure function (no I/O) so it can be unit-tested without a
    database, per testing.md's "Unit tests for any pure Python (e.g.
    queue-row-to-pydantic-model mapping) live in adapter/storage/" note.
    """
    return ClaimedJob(
        id=row.id,
        job_type=row.job_type,
        status=row.status,
        payload=row.payload,
        org_id=row.org_id,
        repo_id=row.repo_id,
        attempts=row.attempts,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        created_at=row.created_at,
    )
