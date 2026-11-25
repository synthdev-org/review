"""Unit test for the pure ORM-row -> pydantic mapping in schemas.py.

No database involved — per testing.md, this is the "unit tests for any
pure Python (e.g. queue-row-to-pydantic-model mapping)" case, kept separate
from the integration tests in this package that need a real Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from adapter.storage.models import JobQueueRow, JobStatus, JobType
from adapter.storage.schemas import ClaimedJob, claimed_job_from_row


def test_claimed_job_from_row_maps_every_field() -> None:
    job_id = uuid.uuid4()
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    claimed_at = datetime.now(UTC)
    row = JobQueueRow(
        id=job_id,
        job_type=JobType.REVIEW,
        status=JobStatus.CLAIMED,
        payload={"pr_number": 42},
        org_id=org_id,
        repo_id=repo_id,
        attempts=1,
        claimed_by="worker-1",
        claimed_at=claimed_at,
        created_at=created_at,
    )

    claimed = claimed_job_from_row(row)

    assert claimed == ClaimedJob(
        id=job_id,
        job_type=JobType.REVIEW,
        status=JobStatus.CLAIMED,
        payload={"pr_number": 42},
        org_id=org_id,
        repo_id=repo_id,
        attempts=1,
        claimed_by="worker-1",
        claimed_at=claimed_at,
        created_at=created_at,
    )


def test_claimed_job_from_row_handles_unscoped_job() -> None:
    """A job with no org/repo (e.g. a platform-level job) maps cleanly."""
    row = JobQueueRow(
        id=uuid.uuid4(),
        job_type=JobType.INDEX,
        status=JobStatus.PENDING,
        payload={},
        org_id=None,
        repo_id=None,
        attempts=0,
        claimed_by=None,
        claimed_at=None,
        created_at=datetime.now(UTC),
    )

    claimed = claimed_job_from_row(row)

    assert claimed.org_id is None
    assert claimed.repo_id is None
    assert claimed.claimed_by is None
