"""Integration tests for the Worker's claim/dispatch loop
(`apps/worker/dispatch.py`), per the plan's Phase 3 "Tests" section.

Real Postgres via `testcontainers-python` (`migrated_engine`, imported into
this package's `conftest.py` from `adapter/storage/conftest.py`), real
claim/dispatch against directly-enqueued test jobs — no HTTP/webhook layer
involved, matching the PRD's own Unit B "testable with directly-enqueued
test jobs independent of Unit A" note.

`run_dispatch_loop` runs forever by design (see its own module docstring),
so every test here runs it as a background `asyncio.Task` via
`_running_dispatch_loop` and polls Postgres directly for the expected job
status, then cancels the task — proving the loop's real, ongoing behavior
(including "the loop survives to process a subsequent job") rather than
calling some bounded, test-only variant that doesn't exist in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.models import (
    JobQueueRow,
    JobStatus,
    JobType,
    MemoryEntryRow,
    Org,
    PostedFindingCommentRow,
    Repo,
)
from adapter.storage.queue import JobQueueRepository
from adapter.telemetry.sdk import init_telemetry
from apps.worker.dispatch import run_dispatch_loop
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.pipeline import IndexingJobDependencies
from indexing.repository import IndexCommitTracker, IndexRepository
from memory.models import ReactionRecord
from memory.reaction_poll import MemoryReactionPollJobDependencies
from memory.store import ReactionFetcher
from review.pipeline import FailurePolicy, StepFailureInfo, StepRegistry
from review.step import StepContext

pytestmark = pytest.mark.integration

_WORKER_ID = "test-worker"
_POLL_INTERVAL_SECONDS = 0.05
_WAIT_TIMEOUT_SECONDS = 5.0


@dataclass
class _NullIndexRepository:
    """A trivial `IndexRepository`/`IndexCommitTracker` stand-in for tests
    in this file that dispatch no `INDEX` job at all (every `REVIEW`-only
    test below) — never touches Postgres, just satisfies
    `IndexingJobDependencies`' own type. Dedicated INDEX-job tests further
    down build a real `IndexingRepository` against `migrated_engine`
    instead, so this fake's own (non-)behavior is never itself under test.
    """

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
    """The `IndexingJobDependencies` every `REVIEW`-focused test below
    passes — none of these tests dispatch an `INDEX` job, so this never
    needs to do real work (see `_NullIndexRepository`'s own docstring)."""
    null_repo = _NullIndexRepository()
    return IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(),
        embedding_client=FakeEmbeddingClient(),
        index_repository=null_repo,
        commit_tracker=null_repo,
    )


@dataclass
class _NullMemoryReactionPollDeps:
    """A trivial `ReactionFetcher`/`MemorySignalRecorder`/`ReactionDedupStore`
    stand-in for tests in this file that dispatch no `MEMORY_REACTION_POLL`
    job at all (every test below except the dedicated ones further down) —
    mirrors `_NullIndexRepository`'s own docstring/purpose exactly: never
    touches Postgres, just satisfies each Protocol's own type."""

    async def fetch_reactions(self, **kwargs: object) -> dict[int, tuple[ReactionRecord, ...]]:
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
    """The `MemoryReactionPollJobDependencies` every `REVIEW`/`INDEX`-focused
    test below passes — none of these tests dispatch a
    `MEMORY_REACTION_POLL` job, so this never needs to do real work.
    Dedicated `MEMORY_REACTION_POLL` tests further down build a real
    `PostgresMemoryRepository`-backed one against `migrated_engine` instead.
    """
    null_deps = _NullMemoryReactionPollDeps()
    return MemoryReactionPollJobDependencies(
        reaction_fetcher=null_deps,
        signal_recorder=null_deps,
        dedup_store=null_deps,
        max_comments_per_poll=50,
    )


async def _provision_repo(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    """Provisions a real `org`/`repo` row pair, per
    `adapter/storage/test_indexing_repository.py`'s own identical fixture —
    an `INDEX` job's `set_last_indexed_commit` call needs a real `repo_id`
    to update, and `upsert_file` needs one for its own FK-constrained
    `repo_id` column.
    """
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


def _reenable_dispatch_logger() -> None:
    """`adapter/storage`'s Alembic `env.py` calls
    `logging.config.fileConfig()` on every `migrated_engine` fixture setup
    (default `disable_existing_loggers=True`), which disables every
    already-created logger not explicitly listed in `alembic.ini` —
    including this module's own `apps.worker.dispatch` logger (and, for the
    dedicated `INDEX`-job dispatch tests further down,
    `indexing.pipeline`'s own logger too). Called at the top of every test
    below that asserts on `caplog`, mirroring
    `apps/webhook_api/test_webhook_routes.py`'s own per-test version of
    this same fix.
    """
    logging.getLogger("apps.worker.dispatch").disabled = False
    logging.getLogger("indexing.pipeline").disabled = False


@dataclass
class _SucceedingStep:
    name: str = "fake_step"
    depends_on: tuple[str, ...] = ()

    async def run(self, input: Any, context: StepContext) -> Any:
        return {"ok": True}


@dataclass
class _RaisingStep:
    name: str = "fake_step"
    depends_on: tuple[str, ...] = ()

    async def run(self, input: Any, context: StepContext) -> Any:
        raise RuntimeError("boom: fake step always fails")


@dataclass
class _SlowThenSucceedingStep:
    name: str = "fake_step"
    depends_on: tuple[str, ...] = ()
    delay_seconds: float = 0.0

    async def run(self, input: Any, context: StepContext) -> Any:
        await asyncio.sleep(self.delay_seconds)
        return {"ok": True}


@dataclass
class _SlowThenFailingStep:
    name: str = "fake_step"
    depends_on: tuple[str, ...] = ()
    delay_seconds: float = 0.0

    async def run(self, input: Any, context: StepContext) -> Any:
        await asyncio.sleep(self.delay_seconds)
        raise RuntimeError("boom: fake step always fails, eventually")


def _registry(step: Any, *, step_timeout_seconds: float = 5.0) -> StepRegistry:
    return StepRegistry(
        [step],
        enabled={"fake_step": True},
        timeouts={"fake_step": step_timeout_seconds},
        on_failure=FailurePolicy.FATAL,
    )


def _empty_registry() -> StepRegistry:
    return StepRegistry([], enabled={}, timeouts={}, on_failure=FailurePolicy.FATAL)


@contextlib.asynccontextmanager
async def _running_dispatch_loop(
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies | None = None,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies | None = None,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    stuck_job_warning_threshold_seconds: float = 30.0,
    worker_id: str = _WORKER_ID,
) -> AsyncIterator[None]:
    """Run `run_dispatch_loop` as a background task for the duration of the
    `async with` block, then cancel it — `run_dispatch_loop` never returns
    on its own (see its own docstring), so every test needs this to observe
    its effects and still let the test process exit cleanly.

    `indexing_deps`/`memory_reaction_poll_deps` default to
    `_default_indexing_deps()`/`_default_memory_reaction_poll_deps()` (never
    touch Postgres) for every `REVIEW`-focused caller below; dedicated
    INDEX-job/`MEMORY_REACTION_POLL`-job tests each pass a real one built
    against `migrated_engine`.
    """
    task: asyncio.Task[None] = asyncio.create_task(
        run_dispatch_loop(
            job_queue=job_queue,
            review_registry=review_registry,
            indexing_deps=indexing_deps if indexing_deps is not None else _default_indexing_deps(),
            memory_reaction_poll_deps=(
                memory_reaction_poll_deps
                if memory_reaction_poll_deps is not None
                else _default_memory_reaction_poll_deps()
            ),
            worker_id=worker_id,
            poll_interval_seconds=poll_interval_seconds,
            stuck_job_warning_threshold_seconds=stuck_job_warning_threshold_seconds,
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


# --- Happy path / fatal-failure job status transitions ----------------------


@pytest.mark.asyncio
async def test_claimed_job_with_trivial_pipeline_transitions_to_done(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(
        job_queue=job_queue, review_registry=_registry(_SucceedingStep())
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)


@pytest.mark.asyncio
async def test_claimed_job_with_raising_pipeline_transitions_to_failed(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(
        job_queue=job_queue, review_registry=_registry(_RaisingStep())
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.FAILED)


# --- Bug-resilience: an unhandled exception in the loop itself --------------


@pytest.mark.asyncio
async def test_unhandled_dispatch_loop_exception_marks_job_failed_and_loop_survives(
    migrated_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake pipeline runner monkeypatched to raise *outside* the normal
    success/`PipelineFailedError` path — the already-`CLAIMED` job is still
    marked `FAILED`, the `error` log fires, and the loop survives to
    process a subsequent job normally.
    """
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    registry = _registry(_SucceedingStep())

    async def _raise_directly(self: StepRegistry, seed_input: Any, context: StepContext) -> Any:
        raise RuntimeError("simulated bug in the dispatch loop's own call, not the pipeline")

    monkeypatch.setattr(StepRegistry, "run", _raise_directly)

    buggy_job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    with caplog.at_level("ERROR"):
        async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
            await _wait_for_status(migrated_engine, buggy_job_id, JobStatus.FAILED)

    assert any(
        "unhandled exception in dispatch loop" in record.getMessage() for record in caplog.records
    )

    # The loop survives: undo the monkeypatch and prove a subsequent job
    # still processes normally.
    monkeypatch.undo()
    healthy_job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 2})
    async with _running_dispatch_loop(
        job_queue=job_queue, review_registry=_registry(_SucceedingStep())
    ):
        await _wait_for_status(migrated_engine, healthy_job_id, JobStatus.DONE)


# --- Stuck-job warning: surfaces the signal, doesn't alter the outcome ------


@pytest.mark.asyncio
async def test_stuck_job_warning_fires_then_job_still_completes_normally(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    slow_step = _SlowThenSucceedingStep(delay_seconds=0.3)
    registry = _registry(slow_step, step_timeout_seconds=5.0)

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    with caplog.at_level("WARNING"):
        async with _running_dispatch_loop(
            job_queue=job_queue,
            review_registry=registry,
            stuck_job_warning_threshold_seconds=0.05,
        ):
            await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    assert any(
        "stuck-job warning threshold" in record.getMessage()
        and getattr(record, "job_id", None) == str(job_id)
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_stuck_job_that_then_fails_is_marked_failed_via_the_normal_path(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A job that crosses the stuck-job warning threshold and then fails
    must still go through `_run_claimed_job`'s own `PipelineFailedError`
    handling (the "job failed" log + `mark_failed`) — not have that
    exception re-raised straight out of the post-threshold wait, which
    would let it escape to `run_dispatch_loop`'s generic handler and log
    the misleading "unhandled exception in dispatch loop" message for what
    is really just an ordinary, if slow, pipeline failure.
    """
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    slow_failing_step = _SlowThenFailingStep(delay_seconds=0.3)
    registry = _registry(slow_failing_step, step_timeout_seconds=5.0)

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    with caplog.at_level("WARNING"):
        async with _running_dispatch_loop(
            job_queue=job_queue,
            review_registry=registry,
            stuck_job_warning_threshold_seconds=0.05,
        ):
            await _wait_for_status(migrated_engine, job_id, JobStatus.FAILED)

    assert any(
        record.getMessage() == "job failed" and getattr(record, "job_id", None) == str(job_id)
        for record in caplog.records
    )
    assert not any(
        "unhandled exception in dispatch loop" in record.getMessage() for record in caplog.records
    )


# --- claim_next() itself failing ---------------------------------------------


@pytest.mark.asyncio
async def test_claim_next_failure_logs_and_loop_recovers_on_a_later_job(
    migrated_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    original_claim_next = JobQueueRepository.claim_next
    call_count = 0

    async def _flaky_claim_next(self: JobQueueRepository, worker_id: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated Postgres blip on the first claim_next() call")
        return await original_claim_next(self, worker_id)

    monkeypatch.setattr(JobQueueRepository, "claim_next", _flaky_claim_next)

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    with caplog.at_level("ERROR"):
        async with _running_dispatch_loop(
            job_queue=job_queue, review_registry=_registry(_SucceedingStep())
        ):
            await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    assert any("claim_next failed" in record.getMessage() for record in caplog.records)
    assert call_count >= 2


# --- INDEX job dispatch: a real call into indexing.pipeline.run_indexing_job ---
# (indexing delivery plan's Phase 7 — replaces the old "log unimplemented,
# mark DONE" stub the walking-skeleton plan shipped.)


def _index_payload(
    *, repo_id: uuid.UUID, force_full_tree_fallback: bool = False
) -> dict[str, object]:
    return {
        "org_id": str(uuid.uuid4()),
        "repo_id": str(repo_id),
        "installation_id": 1,
        "repo_full_name": "acme/widgets",
        "pr_number": 0,
        "commit_sha": "a" * 40,
        "added": [],
        "removed": [],
        "modified": [],
        "force_full_tree_fallback": force_full_tree_fallback,
    }


@pytest.mark.asyncio
async def test_index_job_runs_real_pipeline_and_marks_done(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A trivial (no added/modified/removed files) `INDEX` job dispatches
    through `indexing.pipeline.run_indexing_job` for real, against a real
    `IndexingRepository`/Postgres — proving the stub replacement, not just
    that dispatch.py compiles against the new signature.
    """
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    repo_id = await _provision_repo(session_factory)
    indexing_deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(),
        embedding_client=FakeEmbeddingClient(),
        index_repository=IndexingRepository(session_factory),
        commit_tracker=IndexingRepository(session_factory),
    )

    index_job_id = await job_queue.enqueue(JobType.INDEX, _index_payload(repo_id=repo_id))
    review_job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    with caplog.at_level("INFO"):
        async with _running_dispatch_loop(
            job_queue=job_queue,
            review_registry=_registry(_SucceedingStep()),
            indexing_deps=indexing_deps,
        ):
            await _wait_for_status(migrated_engine, index_job_id, JobStatus.DONE)
            await _wait_for_status(migrated_engine, review_job_id, JobStatus.DONE)

    assert any(
        record.getMessage() == "index job completed"
        and getattr(record, "job_id", None) == str(index_job_id)
        for record in caplog.records
    )

    async with session_factory() as session:
        row = await session.get(Repo, repo_id)
        assert row is not None
        assert row.last_indexed_commit_sha == "a" * 40


@pytest.mark.asyncio
async def test_index_job_fatal_failure_marks_job_failed(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A truncated Git Trees API response (`RepoTreeTruncatedError`) is a
    whole-job-fatal failure (`indexing.pipeline.IndexingJobFailedError`) —
    proving `_run_claimed_job` catches it the same way it already catches
    `PipelineFailedError` for `REVIEW` jobs, not the generic bug-resilience
    catch-all.
    """
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    repo_id = await _provision_repo(session_factory)
    null_repo = _NullIndexRepository()
    indexing_deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(truncated=True),
        embedding_client=FakeEmbeddingClient(),
        index_repository=null_repo,
        commit_tracker=null_repo,
    )

    index_job_id = await job_queue.enqueue(JobType.INDEX, _index_payload(repo_id=repo_id))

    with caplog.at_level("ERROR"):
        async with _running_dispatch_loop(
            job_queue=job_queue,
            review_registry=_registry(_SucceedingStep()),
            indexing_deps=indexing_deps,
        ):
            await _wait_for_status(migrated_engine, index_job_id, JobStatus.FAILED)

    assert any(
        record.getMessage() == "job failed" and getattr(record, "job_id", None) == str(index_job_id)
        for record in caplog.records
    )
    assert not any(
        "unhandled exception in dispatch loop" in record.getMessage() for record in caplog.records
    )


# --- MEMORY_REACTION_POLL job dispatch (docs/plans/plan-deliver-memory.md's --
# --- Phase 4 section, Unit D, US-M3) -----------------------------------------


@dataclass
class _FakeReactionFetcher:
    """A `ReactionFetcher` fake returning a fixed, per-comment-id set of
    reactions — records every call it receives so tests can assert on the
    exact `comment_ids` this job actually polled."""

    reactions_by_comment: dict[int, tuple[ReactionRecord, ...]] = field(default_factory=dict)
    calls: list[tuple[int, str, tuple[int, ...]]] = field(default_factory=list)

    async def fetch_reactions(
        self, *, installation_id: int, repo_full_name: str, comment_ids: Sequence[int]
    ) -> dict[int, tuple[ReactionRecord, ...]]:
        self.calls.append((installation_id, repo_full_name, tuple(comment_ids)))
        return {
            comment_id: self.reactions_by_comment.get(comment_id, ()) for comment_id in comment_ids
        }


____: type[ReactionFetcher] = _FakeReactionFetcher


@dataclass
class _RaisingReactionFetcher:
    """A `ReactionFetcher` fake standing in for a whole-job-fatal failure
    (e.g. the installation token exchange itself failing, before any
    per-comment fetch even starts) — `memory/reaction_poll.py` has no
    dedicated whole-job-failure exception type of its own (see that
    module's own docstring), so this should surface via
    `apps/worker/dispatch.py`'s generic bug-resilience handling, the same as
    any other unanticipated bug in the loop."""

    async def fetch_reactions(self, **kwargs: object) -> dict[int, tuple[ReactionRecord, ...]]:
        raise RuntimeError("boom: fake reaction fetcher always fails")


def _memory_reaction_poll_payload(*, repo_id: uuid.UUID, pr_number: int = 7) -> dict[str, object]:
    return {
        "org_id": str(uuid.uuid4()),
        "repo_id": str(repo_id),
        "installation_id": 1,
        "repo_full_name": "acme/widgets",
        "pr_number": pr_number,
    }


async def _seed_posted_comment(
    session_factory: async_sessionmaker[Any], *, repo_id: uuid.UUID, comment_id: int, pr_number: int
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            PostedFindingCommentRow(
                id=comment_id,
                repo_id=repo_id,
                pr_number=pr_number,
                commit_sha="a" * 40,
                finding_file="app/main.py",
                finding_line=10,
                finding_rule_id="no-secrets",
                finding_message="a secret was found",
            )
        )


@pytest.mark.asyncio
async def test_memory_reaction_poll_job_captures_a_new_reaction_and_marks_done(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A `MEMORY_REACTION_POLL` job runs through
    `memory.reaction_poll.run_memory_reaction_poll_job` for real, against a
    real `PostgresMemoryRepository`/Postgres — proving the dispatch-loop
    wiring, not just that `dispatch.py` compiles against the new job type.
    """
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    repo_id = await _provision_repo(session_factory)
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=7001, pr_number=7)

    memory_repository = PostgresMemoryRepository(session_factory)
    reaction_fetcher = _FakeReactionFetcher(
        reactions_by_comment={7001: (ReactionRecord(reaction_id=1, content="+1"),)}
    )
    memory_reaction_poll_deps = MemoryReactionPollJobDependencies(
        reaction_fetcher=reaction_fetcher,
        signal_recorder=memory_repository,
        dedup_store=memory_repository,
        max_comments_per_poll=50,
    )

    job_id = await job_queue.enqueue(
        JobType.MEMORY_REACTION_POLL, _memory_reaction_poll_payload(repo_id=repo_id)
    )

    with caplog.at_level("INFO"):
        async with _running_dispatch_loop(
            job_queue=job_queue,
            review_registry=_registry(_SucceedingStep()),
            memory_reaction_poll_deps=memory_reaction_poll_deps,
        ):
            await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    assert reaction_fetcher.calls == [(1, "acme/widgets", (7001,))]

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryEntryRow).where(MemoryEntryRow.posted_finding_comment_id == 7001)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].raw_signal_type == "reaction"
    assert rows[0].raw_signal_detail == {"reaction_id": 1, "content": "+1"}


@pytest.mark.asyncio
async def test_memory_reaction_poll_job_fatal_failure_marks_job_failed(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A whole-job-fatal failure (the fake `ReactionFetcher` raising
    unconditionally, standing in for a real token-exchange failure) is
    wrapped as `MemoryReactionPollJobFailedError` and caught by
    `_run_claimed_job`'s own typed-exception handling — the same "job
    failed" path `PipelineFailedError`/`IndexingJobFailedError` already get
    — marking the job `FAILED`, never falling through to the generic
    dispatch-loop "unhandled exception" bug-resilience log line.
    """
    _reenable_dispatch_logger()

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    repo_id = await _provision_repo(session_factory)
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=7002, pr_number=8)

    memory_repository = PostgresMemoryRepository(session_factory)
    memory_reaction_poll_deps = MemoryReactionPollJobDependencies(
        reaction_fetcher=_RaisingReactionFetcher(),
        signal_recorder=memory_repository,
        dedup_store=memory_repository,
        max_comments_per_poll=50,
    )

    job_id = await job_queue.enqueue(
        JobType.MEMORY_REACTION_POLL, _memory_reaction_poll_payload(repo_id=repo_id, pr_number=8)
    )

    with caplog.at_level("ERROR"):
        async with _running_dispatch_loop(
            job_queue=job_queue,
            review_registry=_registry(_SucceedingStep()),
            memory_reaction_poll_deps=memory_reaction_poll_deps,
        ):
            await _wait_for_status(migrated_engine, job_id, JobStatus.FAILED)

    assert any(
        record.getMessage() == "job failed"
        and getattr(record, "job_id", None) == str(job_id)
        and getattr(record, "job_type", None) == JobType.MEMORY_REACTION_POLL.value
        for record in caplog.records
    )


# --- Production wiring: zero registered steps is a correct no-op -----------


@pytest.mark.asyncio
async def test_review_job_against_an_empty_registry_completes_done(
    migrated_engine: AsyncEngine,
) -> None:
    """The shape `apps/worker/main.py` actually wires in this phase (no
    concrete steps registered yet — Phases 4-7 each add one): a `REVIEW`
    job's pipeline run is a genuine, correct no-op, not a bug."""
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=_empty_registry()):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)


# --- Phase 7: an earlier (pre-llm-review) macro-step failure still ----------
# --- invokes the terminal output step, not only the llm-review case --------
# --- US8's own PRD wording names --------------------------------------------


@dataclass
class _RecordingTerminalStep:
    """Stands in for `review.output.FindingsOutputStep` for this file's own
    purposes (this file's scope is the dispatch loop, not the real output
    step's own posting behavior — that's `review/test_findings_output_step.py`'s
    and `review/test_review_job.py`'s job): records every `input` it's
    called with, so this test can assert it received a `StepFailureInfo`
    describing the *earlier* step's own failure."""

    name: str = "output"
    depends_on: tuple[str, ...] = ("fake_step",)
    calls: list[Any] = field(default_factory=list)

    async def run(self, input: Any, context: StepContext) -> Any:
        self.calls.append(input)
        return {"posted": True}


@pytest.mark.asyncio
async def test_earlier_step_failure_still_invokes_terminal_output_step(
    migrated_engine: AsyncEngine,
) -> None:
    """Proves the Phase 7 fix closing the "zero GitHub-visible signal" gap
    for a fatal failure *before* llm-review (e.g. pre-filter/diff-fetch),
    not only the llm-review case US8's own PRD wording names: a
    directly-enqueued job wired to a pipeline whose first (pre-filter-
    equivalent) step fails still results in the terminal output step being
    invoked — with a `StepFailureInfo` describing that failure — before the
    job is marked `FAILED`.
    """
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    terminal = _RecordingTerminalStep()
    registry = StepRegistry(
        [_RaisingStep(), terminal],
        enabled={"fake_step": True, "output": True},
        timeouts={"fake_step": 5.0, "output": 5.0},
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(migrated_engine, job_id, JobStatus.FAILED)

    assert len(terminal.calls) == 1
    failure_input = terminal.calls[0]
    assert isinstance(failure_input, StepFailureInfo)
    assert failure_input.failed_step_name == "fake_step"


# --- docs/specs/spec-observability.md's Unit A: Reliability NFR (exporter
# unavailability must never fail/slow/block a job) --------------------------


@pytest.mark.asyncio
async def test_job_still_completes_when_otlp_collector_is_unreachable(
    migrated_engine: AsyncEngine,
    reset_otel_global_providers: None,
) -> None:
    """The "job still completes" half of US14's own named highest-risk
    failure mode — `adapter/telemetry/test_telemetry_integration.py`
    proves the lower-level guarantee (recording/flushing against a dead
    receiver never raises or blocks `adapter.telemetry.sdk` itself); this
    test proves it holds all the way up through a real dispatch loop: a
    directly-enqueued job still reaches `DONE` on its usual schedule even
    though `init_telemetry` was pointed at an OTLP endpoint nothing is
    listening on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        unreachable_port = sock.getsockname()[1]
    init_telemetry(
        otlp_endpoint=f"http://127.0.0.1:{unreachable_port}",
        service_name="review-worker-test",
    )
    try:
        session_factory = create_session_factory(migrated_engine)
        job_queue = JobQueueRepository(session_factory)
        job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

        async with _running_dispatch_loop(
            job_queue=job_queue, review_registry=_registry(_SucceedingStep())
        ):
            await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)
    finally:
        tracer_provider = trace.get_tracer_provider()
        meter_provider = metrics.get_meter_provider()
        assert isinstance(tracer_provider, TracerProvider)
        assert isinstance(meter_provider, MeterProvider)
        tracer_provider.shutdown()
        meter_provider.shutdown()


# --- US1 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG):
# `review.queue.completed_total` / `review.job.duration_seconds`, recorded
# by `_record_job_completion` on every terminal DONE/FAILED transition.
# Uses OTel's own in-memory metric reader — see
# `review/test_output_telemetry.py`'s identical fixture pattern. ----------


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `apps.worker.dispatch`'s module-level `meter`/
    counter/histogram (created once, at import time) pick it up via OTel's
    proxy-instrument migration mechanism."""
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


def _histogram_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[tuple[int, float, dict[str, object]]]:
    """Returns `(count, sum, attributes)` per recorded data point."""
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    points: list[tuple[int, float, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Histogram)
                for data_point in metric.data.data_points:
                    attrs: dict[str, object] = dict(data_point.attributes or {})
                    points.append((data_point.count, data_point.sum, attrs))
    return points


@pytest.mark.asyncio
async def test_successful_job_records_completed_total_and_duration_done(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(
        job_queue=job_queue, review_registry=_registry(_SucceedingStep())
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    completed_points = _sum_points(in_memory_metric_reader, "review.queue.completed_total")
    assert completed_points == [(1, {"job_type": "review", "outcome": "done"})]

    duration_points = _histogram_points(in_memory_metric_reader, "review.job.duration_seconds")
    assert len(duration_points) == 1
    count, total, attrs = duration_points[0]
    assert count == 1
    assert total >= 0.0
    assert attrs == {"job_type": "review", "outcome": "done"}


@pytest.mark.asyncio
async def test_failed_pipeline_job_records_completed_total_and_duration_failed(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(
        job_queue=job_queue, review_registry=_registry(_RaisingStep())
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.FAILED)

    completed_points = _sum_points(in_memory_metric_reader, "review.queue.completed_total")
    assert completed_points == [(1, {"job_type": "review", "outcome": "failed"})]

    duration_points = _histogram_points(in_memory_metric_reader, "review.job.duration_seconds")
    assert len(duration_points) == 1
    count, total, attrs = duration_points[0]
    assert count == 1
    assert total >= 0.0
    assert attrs == {"job_type": "review", "outcome": "failed"}


@pytest.mark.asyncio
async def test_unhandled_loop_exception_also_records_completed_total_as_failed(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_dispatch_loop`'s own bug-resilience path (an exception escaping
    `_run_claimed_job` entirely, not a normal `PipelineFailedError`) is the
    other call site of `_record_job_completion` — proves it's covered too,
    not just the two paths inside `_run_claimed_job` itself."""
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    registry = _registry(_SucceedingStep())

    async def _raise_directly(self: StepRegistry, seed_input: Any, context: StepContext) -> Any:
        raise RuntimeError("simulated bug in the dispatch loop's own call, not the pipeline")

    monkeypatch.setattr(StepRegistry, "run", _raise_directly)
    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(migrated_engine, job_id, JobStatus.FAILED)

    completed_points = _sum_points(in_memory_metric_reader, "review.queue.completed_total")
    assert completed_points == [(1, {"job_type": "review", "outcome": "failed"})]


@pytest.mark.asyncio
async def test_job_completion_metric_attributes_are_bounded_and_privacy_safe(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Per this spec's hard privacy requirement: attribute values are never
    job payload content — only the bounded `job_type`/`outcome`
    vocabulary."""
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(
        JobType.REVIEW, {"pr_number": 1, "secret": "definitely-not-a-metric-label"}
    )

    async with _running_dispatch_loop(
        job_queue=job_queue, review_registry=_registry(_SucceedingStep())
    ):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    for _value, attrs in _sum_points(in_memory_metric_reader, "review.queue.completed_total"):
        assert set(attrs.keys()) == {"job_type", "outcome"}
        assert attrs["job_type"] in {"review", "index"}
        assert attrs["outcome"] in {"done", "failed"}

    for _count, _total, attrs in _histogram_points(
        in_memory_metric_reader, "review.job.duration_seconds"
    ):
        assert set(attrs.keys()) == {"job_type", "outcome"}
        assert attrs["job_type"] in {"review", "index"}
        assert attrs["outcome"] in {"done", "failed"}
