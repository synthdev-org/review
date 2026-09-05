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
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import JobQueueRow, JobStatus, JobType, Org, Repo
from adapter.storage.queue import JobQueueRepository
from apps.worker.dispatch import run_dispatch_loop
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.pipeline import IndexingJobDependencies
from indexing.repository import IndexCommitTracker, IndexRepository
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

    async def delete_file(self, **kwargs: object) -> None:
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
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    stuck_job_warning_threshold_seconds: float = 30.0,
    worker_id: str = _WORKER_ID,
) -> AsyncIterator[None]:
    """Run `run_dispatch_loop` as a background task for the duration of the
    `async with` block, then cancel it — `run_dispatch_loop` never returns
    on its own (see its own docstring), so every test needs this to observe
    its effects and still let the test process exit cleanly.

    `indexing_deps` defaults to `_default_indexing_deps()` (never touches
    Postgres) for every `REVIEW`-focused caller below; dedicated INDEX-job
    tests pass a real one built against `migrated_engine`.
    """
    task: asyncio.Task[None] = asyncio.create_task(
        run_dispatch_loop(
            job_queue=job_queue,
            review_registry=review_registry,
            indexing_deps=indexing_deps if indexing_deps is not None else _default_indexing_deps(),
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
