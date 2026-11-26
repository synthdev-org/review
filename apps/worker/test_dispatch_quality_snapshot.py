"""Unit test: the per-process ``review_quality_v2_enabled`` switch reaches every
step of a ``REVIEW`` job as one snapshot on its ``StepContext`` (A8). No database:
``_dispatch_claimed_job``'s REVIEW path only builds the context and runs the
registry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from adapter.storage.models import JobStatus, JobType
from adapter.storage.schemas import ClaimedJob
from apps.worker.dispatch import _dispatch_claimed_job
from review.pipeline import FailurePolicy, StepRegistry
from review.step import StepContext


@dataclass
class _RecordingStep:
    name: str = "fake_step"
    depends_on: tuple[str, ...] = ()
    seen: list[StepContext] = field(default_factory=list)

    async def run(self, input: Any, context: StepContext) -> Any:
        self.seen.append(context)
        return {"ok": True}


def _job() -> ClaimedJob:
    now = datetime.now(UTC)
    return ClaimedJob(
        id=uuid.uuid4(),
        job_type=JobType.REVIEW,
        status=JobStatus.CLAIMED,
        payload={},
        org_id=None,
        repo_id=None,
        attempts=1,
        claimed_by="w",
        claimed_at=now,
        created_at=now,
    )


async def _dispatch(**kwargs: bool) -> StepContext:
    step = _RecordingStep()
    registry = StepRegistry(
        [step],
        enabled={"fake_step": True},
        timeouts={"fake_step": 5.0},
        on_failure=FailurePolicy.FATAL,
    )
    await _dispatch_claimed_job(
        _job(),
        review_registry=registry,
        indexing_deps=None,  # type: ignore[arg-type]  # REVIEW path never reads it
        memory_reaction_poll_deps=None,  # type: ignore[arg-type]
        dry_run=False,
        **kwargs,
    )
    return step.seen[0]


@pytest.mark.asyncio
async def test_the_switch_is_snapshotted_onto_the_job_context() -> None:
    assert (await _dispatch(review_quality_v2=True)).review_quality_v2 is True
    assert (await _dispatch(review_quality_v2=False)).review_quality_v2 is False


@pytest.mark.asyncio
async def test_a_missing_value_defaults_to_v1() -> None:
    assert (await _dispatch()).review_quality_v2 is False
