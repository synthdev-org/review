"""Unit tests for the Worker dispatch loop."""

# Fakes sit at the job queue, the review StepRegistry and the INDEX and
# MEMORY_REACTION_POLL job runners. The real-Postgres coverage lives in
# ``test_dispatch.py``.

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from adapter.storage.models import JobStatus, JobType
from adapter.storage.queue import JobQueueRepository
from adapter.storage.schemas import ClaimedJob
from apps.worker import dispatch
from indexing.pipeline import IndexingJobDependencies, IndexingJobFailedError
from memory.reaction_poll import (
    MemoryReactionPollJobDependencies,
    MemoryReactionPollJobFailedError,
)
from review.pipeline import PipelineFailedError, StepRegistry
from review.step import StepContext

pytestmark = pytest.mark.asyncio

_INDEXING = cast(IndexingJobDependencies, object())
_MEMORY = cast(
    MemoryReactionPollJobDependencies,
    type(
        "_Deps",
        (),
        {
            "reaction_fetcher": "fetcher",
            "signal_recorder": "recorder",
            "dedup_store": "dedup",
            "max_comments_per_poll": 9,
        },
    )(),
)


def _job(
    job_type: JobType = JobType.REVIEW, payload: dict[str, object] | None = None
) -> ClaimedJob:
    now = datetime.now(UTC)
    return ClaimedJob(
        id=uuid.uuid4(),
        job_type=job_type,
        status=JobStatus.CLAIMED,
        payload=payload or {},
        org_id=None,
        repo_id=None,
        attempts=1,
        claimed_by="w",
        claimed_at=now - timedelta(seconds=3),
        created_at=now - timedelta(seconds=5),
    )


class _Queue:
    def __init__(self, claims: list[ClaimedJob | BaseException | None] | None = None) -> None:
        self.claims = list(claims or [])
        self.done: list[uuid.UUID] = []
        self.failed: list[uuid.UUID] = []
        self.transition = True

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        item = self.claims.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def mark_done(self, job_id: uuid.UUID) -> bool:
        self.done.append(job_id)
        return self.transition

    async def mark_failed(self, job_id: uuid.UUID) -> bool:
        self.failed.append(job_id)
        return self.transition


class _Registry:
    def __init__(self, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.runs: list[tuple[dict[str, object], StepContext]] = []

    async def run(self, payload: dict[str, object], context: StepContext) -> None:
        self.runs.append((payload, context))
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


def _queue(q: _Queue) -> JobQueueRepository:
    return cast(JobQueueRepository, q)


async def _run(job: ClaimedJob, q: _Queue, registry: _Registry, threshold: float = 5.0) -> None:
    await dispatch._run_claimed_job(
        job,
        job_queue=_queue(q),
        review_registry=cast(StepRegistry, registry),
        indexing_deps=_INDEXING,
        memory_reaction_poll_deps=_MEMORY,
        stuck_job_warning_threshold_seconds=threshold,
        dry_run=True,
        review_quality_v2=True,
    )


async def test_review_job_runs_the_registry_with_the_snapshotted_flags_and_is_marked_done() -> None:
    job, q, registry = _job(payload={"k": "v"}), _Queue(), _Registry()

    await _run(job, q, registry)

    ((payload, context),) = registry.runs
    assert payload == {"k": "v"}
    assert (context.job_id, context.dry_run, context.review_quality_v2) == (str(job.id), True, True)
    assert q.done == [job.id] and q.failed == []


async def test_pipeline_failure_marks_the_job_failed_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job, q = _job(), _Queue()

    with caplog.at_level(logging.ERROR, logger=dispatch.logger.name):
        await _run(job, q, _Registry(error=PipelineFailedError("step", RuntimeError("boom"))))

    assert q.failed == [job.id] and q.done == []
    assert any(r.message == "job failed" for r in caplog.records)


async def test_a_bug_escaping_the_job_is_not_swallowed_by_run_claimed_job() -> None:
    q = _Queue()

    with pytest.raises(RuntimeError, match="bug"):
        await _run(_job(), q, _Registry(error=RuntimeError("bug")))

    assert q.done == [] and q.failed == []  # the loop, not this function, marks it failed


async def test_a_job_past_the_stuck_threshold_warns_once_and_still_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job, q = _job(), _Queue()

    with caplog.at_level(logging.WARNING, logger=dispatch.logger.name):
        await _run(job, q, _Registry(delay=0.05), threshold=0.01)

    warnings = [r for r in caplog.records if "stuck-job warning threshold" in r.message]
    assert len(warnings) == 1
    assert q.done == [job.id]


async def test_a_lost_transition_race_is_not_counted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        dispatch, "_record_job_completion", lambda job, *, outcome: recorded.append(outcome)
    )
    q = _Queue()
    q.transition = False

    await _run(_job(), q, _Registry())
    q.transition = True
    await _run(_job(), q, _Registry())

    assert recorded == ["done"]


async def test_index_job_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[dict[str, object], str]] = []

    async def fake_run_indexing_job(
        payload: dict[str, object], deps: object, *, job_id: str
    ) -> None:
        calls.append((payload, job_id))
        raise IndexingJobFailedError(detail="tree truncated")

    monkeypatch.setattr(dispatch, "run_indexing_job", fake_run_indexing_job)
    job, q = _job(JobType.INDEX, {"x": 1}), _Queue()

    await _run(job, q, _Registry())

    assert calls == [({"x": 1}, str(job.id))]
    assert q.failed == [job.id]


def _poll_payload() -> dict[str, object]:
    return {
        "org_id": str(uuid.uuid4()),
        "repo_id": str(uuid.uuid4()),
        "installation_id": 11,
        "repo_full_name": "acme/w",
        "pr_number": 7,
    }


async def test_memory_reaction_poll_job_is_parsed_and_wired_with_its_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_poll(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(dispatch, "run_memory_reaction_poll_job", fake_poll)
    payload = _poll_payload()
    job, q = _job(JobType.MEMORY_REACTION_POLL, payload), _Queue()

    await _run(job, q, _Registry())

    assert seen == {
        "repo_id": payload["repo_id"],
        "pr_number": 7,
        "installation_id": 11,
        "repo_full_name": "acme/w",
        "reaction_fetcher": "fetcher",
        "signal_recorder": "recorder",
        "dedup_store": "dedup",
        "max_comments_per_poll": 9,
    }
    assert q.done == [job.id]


async def test_memory_reaction_poll_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_poll(**_: Any) -> None:
        raise MemoryReactionPollJobFailedError(detail="db down")

    monkeypatch.setattr(dispatch, "run_memory_reaction_poll_job", fake_poll)
    job, q = _job(JobType.MEMORY_REACTION_POLL, _poll_payload()), _Queue()

    await _run(job, q, _Registry())

    assert q.failed == [job.id]


async def test_dispatch_loop_survives_claim_errors_idle_polls_and_job_bugs() -> None:
    bad = _job()
    stop = asyncio.CancelledError()
    q = _Queue([RuntimeError("db restarting"), None, bad, stop])

    with pytest.raises(asyncio.CancelledError):
        await dispatch.run_dispatch_loop(
            job_queue=_queue(q),
            review_registry=cast(StepRegistry, _Registry(error=RuntimeError("bug"))),
            indexing_deps=_INDEXING,
            memory_reaction_poll_deps=_MEMORY,
            worker_id="w",
            poll_interval_seconds=0,
            stuck_job_warning_threshold_seconds=5.0,
        )

    assert q.failed == [bad.id]  # the claimed job the bug hit is marked FAILED, not left CLAIMED
    assert q.done == []
