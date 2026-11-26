"""Integration tests for the Worker's model-routing read-path
(US-A16):

"seed a ModelRoutingConfig row, dispatch a real job against migrated_engine,
assert the fake LLMClient receives the configured model with no worker
restart; a second case with the flag disabled asserts the static default is
used even with a seeded row present (kill-switch proof); a third case
simulates the repository raising during resolve() and asserts the job still
completes using the static default, not a failed dispatch."

Real Postgres via `testcontainers-python` (`migrated_engine`, imported into
`apps/worker/conftest.py` from `adapter/storage/conftest.py`, per
`apps/worker/test_dispatch.py`'s own identical pattern), real claim/dispatch
against a directly-enqueued `REVIEW` job — no HTTP/webhook layer involved,
mirroring that module's own "Real Postgres... real claim/dispatch" module
docstring. The dispatch-loop-driving helpers below (`_running_dispatch_loop`,
`_wait_for_status`, the null `IndexingJobDependencies`/
`MemoryReactionPollJobDependencies` stand-ins) are kept small and
self-contained in this file rather than imported from
`apps/worker/test_dispatch.py`, the same "stays self-contained" convention
this package's other test modules already establish for their own stub
servers.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.llm_routing import PostgresModelRoutingRepository
from adapter.storage.models import JobQueueRow, JobStatus, JobType, Org
from adapter.storage.queue import JobQueueRepository
from apps.worker.dispatch import run_dispatch_loop
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.pipeline import IndexingJobDependencies
from indexing.repository import IndexCommitTracker, IndexRepository
from ingestion.diff import Diff, DiffFile
from llmgateway.models import CompletionRequest, CompletionResponse
from llmgateway.routing import (
    FallbackModelRoutingResolver,
    ModelRoutingConfigReader,
    ModelRoutingConfigRow,
)
from memory.reaction_poll import MemoryReactionPollJobDependencies
from memory.store import ReactionFetcher
from review.intent_summary import IntentSummaryStep
from review.pipeline import FailurePolicy, StepRegistry
from review.step import StepContext

pytestmark = pytest.mark.integration

_WORKER_ID = "test-worker"
_POLL_INTERVAL_SECONDS = 0.05
_WAIT_TIMEOUT_SECONDS = 5.0
_STAGE = "intent_summary"


@dataclass
class _NullIndexRepository:
    """Never touches Postgres — no test below dispatches an `INDEX` job."""

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
    null_repo = _NullIndexRepository()
    return IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(),
        embedding_client=FakeEmbeddingClient(),
        index_repository=null_repo,
        commit_tracker=null_repo,
    )


@dataclass
class _NullMemoryReactionPollDeps:
    """Never touches Postgres — no test below dispatches a
    `MEMORY_REACTION_POLL` job."""

    async def fetch_reactions(self, **kwargs: object) -> dict[int, tuple[Any, ...]]:
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
    null_deps = _NullMemoryReactionPollDeps()
    return MemoryReactionPollJobDependencies(
        reaction_fetcher=null_deps,
        signal_recorder=null_deps,
        dedup_store=null_deps,
        max_comments_per_poll=50,
    )


@dataclass
class _FakeLLMClient:
    """A fake `LLMClient` recording every request it receives — used to
    prove which model actually reached the completion request, with no
    real I/O."""

    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(content="a summary", model=request.model)


@dataclass
class _SeedStep:
    """Stands in for `context_builder` — `IntentSummaryStep`'s own default
    upstream dependency — so it can be registered into a real
    `StepRegistry` without needing the real, much heavier context-builder
    step. Returns a fixed output regardless of the raw job payload, mirrors
    `apps/worker/test_dispatch.py`'s own `_SucceedingStep`/`_RaisingStep`
    pattern for a minimal fake step."""

    output: Any
    name: str = "context_builder"
    depends_on: tuple[str, ...] = ()

    async def run(self, input: Any, context: StepContext) -> Any:
        return self.output


@dataclass
class _SeedOutput:
    """Structurally mirrors `review.context_builder.ContextBuilderOutput`
    closely enough for `IntentSummaryStep`'s own
    `_IntentSummaryInput.model_validate(..., from_attributes=True)` to read
    it — see `review/test_intent_summary_step.py`'s identical
    `_FakeContextBuilderOutput` for the same pattern."""

    diff: Diff
    org_id: str
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 1
    head_sha: str = "deadbeef"
    findings: list[Any] = field(default_factory=list)
    context_bundle: Any = None


def _diff_with_patch() -> Diff:
    return Diff(
        files=(DiffFile(path="app.py", status="modified", patch="@@ -1 +1 @@\n-old\n+new\n"),)
    )


def _registry(
    *, llm_client: _FakeLLMClient, org_id: str, model_resolver: FallbackModelRoutingResolver | None
) -> StepRegistry:
    seed_step = _SeedStep(output=_SeedOutput(diff=_diff_with_patch(), org_id=org_id))
    intent_summary_step = IntentSummaryStep(
        llm_client,
        model="static-default-model",
        timeout=5.0,
        max_diff_bytes=200_000,
        depends_on="context_builder",
        model_resolver=model_resolver,
    )
    return StepRegistry(
        [seed_step, intent_summary_step],
        enabled={"context_builder": True, "intent_summary": True},
        timeouts={"context_builder": 5.0, "intent_summary": 5.0},
        on_failure=FailurePolicy.FATAL,
        terminal_step="intent_summary",
    )


@contextlib.asynccontextmanager
async def _running_dispatch_loop(
    *, job_queue: JobQueueRepository, review_registry: StepRegistry
) -> AsyncIterator[None]:
    """Mirrors `apps/worker/test_dispatch.py`'s own identical fixture —
    `run_dispatch_loop` never returns on its own, so every test needs this
    to observe its effects and still let the test process exit cleanly."""
    task: asyncio.Task[None] = asyncio.create_task(
        run_dispatch_loop(
            job_queue=job_queue,
            review_registry=review_registry,
            indexing_deps=_default_indexing_deps(),
            memory_reaction_poll_deps=_default_memory_reaction_poll_deps(),
            worker_id=_WORKER_ID,
            poll_interval_seconds=_POLL_INTERVAL_SECONDS,
            stuck_job_warning_threshold_seconds=30.0,
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


async def _provision_org(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        return org.id


class _RaisingReader:
    """A `ModelRoutingConfigReader` that always raises — the
    explicitly-required case: 'simulates the repository raising during
    resolve() and asserts the job still completes using the static
    default, not a failed dispatch.'"""

    async def get(self, *, org_id: str | None, stage: str) -> ModelRoutingConfigRow | None:
        raise RuntimeError("connection refused")


def _assert_is_reader(reader: object) -> None:
    assert isinstance(reader, ModelRoutingConfigReader)


# =============================================================================
# US-A16: a live routing change takes effect on the next job dispatch, with
# no Worker restart — the resolver/registry are constructed exactly once,
# before the config row is written, proving this isn't just "a fresh
# resolver picks up a pre-seeded row."
# =============================================================================


@pytest.mark.asyncio
async def test_live_routing_change_reaches_the_next_dispatched_job_with_no_restart(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    org_id = await _provision_org(session_factory)
    job_queue = JobQueueRepository(session_factory)

    repository = PostgresModelRoutingRepository(session_factory)
    # The resolver is built once here — nothing below reconstructs it or
    # restarts anything — before the ModelRoutingConfig row it will read
    # even exists.
    resolver = FallbackModelRoutingResolver(repository, static_default_model="static-default-model")
    llm_client = _FakeLLMClient()
    registry = _registry(llm_client=llm_client, org_id=str(org_id), model_resolver=resolver)

    # The "live change": an Admin writes a routing row for this org/stage
    # *after* the resolver above was already constructed — simulating
    # apps/admin_api's own PUT /api/v1/model-routing/{stage} happening
    # while the Worker keeps running.
    await repository.upsert(org_id=str(org_id), stage=_STAGE, model_name="routed-model")

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})
    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    assert len(llm_client.calls) == 1
    assert llm_client.calls[0].model == "routed-model"


# =============================================================================
# Kill switch: model_routing_config_enabled=False (apps/worker/main.py
# passes model_resolver=None in that case) — the static default is used
# even with a seeded row present for that exact org/stage.
# =============================================================================


@pytest.mark.asyncio
async def test_resolver_disabled_uses_static_default_even_with_a_seeded_row_present(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    org_id = await _provision_org(session_factory)
    job_queue = JobQueueRepository(session_factory)

    repository = PostgresModelRoutingRepository(session_factory)
    await repository.upsert(org_id=str(org_id), stage=_STAGE, model_name="routed-model")

    llm_client = _FakeLLMClient()
    # model_resolver=None — the exact value apps/worker/main.py passes to
    # every LLM-calling step's constructor when
    # `model_routing_config_enabled` is `False`.
    registry = _registry(llm_client=llm_client, org_id=str(org_id), model_resolver=None)

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})
    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    assert len(llm_client.calls) == 1
    assert llm_client.calls[0].model == "static-default-model"


# =============================================================================
# Fail-open: a repository failure during resolve() degrades to the static
# default rather than failing the job dispatch.
# =============================================================================


@pytest.mark.asyncio
async def test_repository_raising_during_resolve_still_completes_the_job(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)

    reader = _RaisingReader()
    _assert_is_reader(reader)
    resolver = FallbackModelRoutingResolver(reader, static_default_model="static-default-model")
    llm_client = _FakeLLMClient()
    registry = _registry(llm_client=llm_client, org_id="org-1", model_resolver=resolver)

    job_id = await job_queue.enqueue(JobType.REVIEW, {"pr_number": 1})
    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(migrated_engine, job_id, JobStatus.DONE)

    assert len(llm_client.calls) == 1
    assert llm_client.calls[0].model == "static-default-model"
