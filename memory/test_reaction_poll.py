"""Integration tests: `memory.reaction_poll.run_memory_reaction_poll_job`
against a fake `ReactionFetcher` and a real `PostgresMemoryRepository` (via
`testcontainers-python`, per `docs/rules/testing.md`'s mandatory DB-
integration rule), per `docs/plans/plan-deliver-memory.md`'s Phase 4
section (Unit D) Tests subsection.

Marked `@pytest.mark.integration` (not Unit) despite living in a domain
package: this function's own logic is a thin orchestration layer over real
Postgres I/O (`dedup_store`/`signal_recorder`), so proving it end to end
needs the real database, matching `testing.md`'s "no real I/O, adapters,
network, or database" Unit-tier definition.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.models import MemoryEntryRow, Org, PostedFindingCommentRow, Repo
from memory.models import ReactionRecord
from memory.reaction_poll import run_memory_reaction_poll_job
from memory.store import ReactionFetcher

pytestmark = pytest.mark.integration

_INSTALLATION_ID = 555
_REPO_FULL_NAME = "acme/widgets"


@dataclass
class _FakeReactionFetcher:
    """A `ReactionFetcher` fake returning a fixed, per-comment-id set of
    reactions — records every call it receives so a test can assert on the
    exact `comment_ids` a poll actually requested."""

    reactions_by_comment: dict[int, tuple[ReactionRecord, ...]] = field(default_factory=dict)
    calls: list[tuple[int, str, tuple[int, ...]]] = field(default_factory=list)

    async def fetch_reactions(
        self, *, installation_id: int, repo_full_name: str, comment_ids: Sequence[int]
    ) -> dict[int, tuple[ReactionRecord, ...]]:
        self.calls.append((installation_id, repo_full_name, tuple(comment_ids)))
        return {
            comment_id: self.reactions_by_comment.get(comment_id, ()) for comment_id in comment_ids
        }


_: type[ReactionFetcher] = _FakeReactionFetcher


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def repository(session_factory: async_sessionmaker[Any]) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(session_factory)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(
            org_id=org.id, name="widgets", full_name=f"{_REPO_FULL_NAME}-{uuid.uuid4().hex}"
        )
        session.add(repo)
        await session.flush()
        return repo.id


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


async def _memory_entries_for_comment(
    session_factory: async_sessionmaker[Any], *, comment_id: int
) -> list[MemoryEntryRow]:
    async with session_factory() as session:
        result = await session.execute(
            select(MemoryEntryRow).where(MemoryEntryRow.posted_finding_comment_id == comment_id)
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_no_candidate_comments_is_a_no_op(
    repository: PostgresMemoryRepository, repo_id: uuid.UUID
) -> None:
    """A PR with zero `posted_finding_comment` rows — nothing to poll,
    proven by the fetcher never being called."""
    fetcher = _FakeReactionFetcher()

    await run_memory_reaction_poll_job(
        repo_id=str(repo_id),
        pr_number=1,
        installation_id=_INSTALLATION_ID,
        repo_full_name=_REPO_FULL_NAME,
        reaction_fetcher=fetcher,
        signal_recorder=repository,
        dedup_store=repository,
        max_comments_per_poll=50,
    )

    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_a_new_reaction_creates_a_pending_memory_entry(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """US-M3's first `Given/When/Then`: a newly-observed reaction on a
    posted comment creates one `pending` `memory_entry`, deduped by
    `(comment_id, reaction_id)`."""
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=8001, pr_number=1)
    fetcher = _FakeReactionFetcher(
        reactions_by_comment={8001: (ReactionRecord(reaction_id=101, content="heart"),)}
    )

    await run_memory_reaction_poll_job(
        repo_id=str(repo_id),
        pr_number=1,
        installation_id=_INSTALLATION_ID,
        repo_full_name=_REPO_FULL_NAME,
        reaction_fetcher=fetcher,
        signal_recorder=repository,
        dedup_store=repository,
        max_comments_per_poll=50,
    )

    assert fetcher.calls == [(_INSTALLATION_ID, _REPO_FULL_NAME, (8001,))]
    entries = await _memory_entries_for_comment(session_factory, comment_id=8001)
    assert len(entries) == 1
    assert entries[0].raw_signal_type == "reaction"
    assert entries[0].raw_signal_detail == {"reaction_id": 101, "content": "heart"}
    assert entries[0].status.value == "pending"
    assert entries[0].github_comment_id is None


@pytest.mark.asyncio
async def test_a_previously_captured_reaction_is_not_recaptured_on_a_repeat_call(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """The same reaction observed across two separate polls (e.g. two later
    pushes to the same PR) is captured exactly once — Assumption 9's
    `captured_reaction` dedup, exercised through the whole job, not just
    `PostgresMemoryRepository.filter_new` directly."""
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=8002, pr_number=1)
    fetcher = _FakeReactionFetcher(
        reactions_by_comment={8002: (ReactionRecord(reaction_id=201, content="+1"),)}
    )

    async def _poll() -> None:
        await run_memory_reaction_poll_job(
            repo_id=str(repo_id),
            pr_number=1,
            installation_id=_INSTALLATION_ID,
            repo_full_name=_REPO_FULL_NAME,
            reaction_fetcher=fetcher,
            signal_recorder=repository,
            dedup_store=repository,
            max_comments_per_poll=50,
        )

    await _poll()
    await _poll()

    entries = await _memory_entries_for_comment(session_factory, comment_id=8002)
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_a_new_reaction_on_a_later_poll_is_captured_alongside_the_earlier_one(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """A second, genuinely new reaction added between two polls is captured
    on the later poll, while the first poll's own reaction is never
    recaptured — proving `filter_new` distinguishes "already captured" from
    "new" per reaction id, not just per comment."""
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=8003, pr_number=1)

    first_fetcher = _FakeReactionFetcher(
        reactions_by_comment={8003: (ReactionRecord(reaction_id=301, content="+1"),)}
    )
    await run_memory_reaction_poll_job(
        repo_id=str(repo_id),
        pr_number=1,
        installation_id=_INSTALLATION_ID,
        repo_full_name=_REPO_FULL_NAME,
        reaction_fetcher=first_fetcher,
        signal_recorder=repository,
        dedup_store=repository,
        max_comments_per_poll=50,
    )

    second_fetcher = _FakeReactionFetcher(
        reactions_by_comment={
            8003: (
                ReactionRecord(reaction_id=301, content="+1"),
                ReactionRecord(reaction_id=302, content="heart"),
            )
        }
    )
    await run_memory_reaction_poll_job(
        repo_id=str(repo_id),
        pr_number=1,
        installation_id=_INSTALLATION_ID,
        repo_full_name=_REPO_FULL_NAME,
        reaction_fetcher=second_fetcher,
        signal_recorder=repository,
        dedup_store=repository,
        max_comments_per_poll=50,
    )

    entries = await _memory_entries_for_comment(session_factory, comment_id=8003)
    assert {entry.raw_signal_detail["reaction_id"] for entry in entries} == {301, 302}


@pytest.mark.asyncio
async def test_max_comments_per_poll_bounds_which_comments_are_polled(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """The plan's own "bounded cost, not just bounded per-job" design: only
    the `max_comments_per_poll` most-recently-posted comments for this PR
    are ever polled, newest first."""
    for i in range(3):
        await _seed_posted_comment(
            session_factory, repo_id=repo_id, comment_id=8100 + i, pr_number=1
        )
    fetcher = _FakeReactionFetcher()

    await run_memory_reaction_poll_job(
        repo_id=str(repo_id),
        pr_number=1,
        installation_id=_INSTALLATION_ID,
        repo_full_name=_REPO_FULL_NAME,
        reaction_fetcher=fetcher,
        signal_recorder=repository,
        dedup_store=repository,
        max_comments_per_poll=2,
    )

    assert len(fetcher.calls) == 1
    polled_ids = fetcher.calls[0][2]
    assert len(polled_ids) == 2
    # Newest first: 8102 (last seeded) and 8101 — never the oldest, 8100.
    assert set(polled_ids) == {8101, 8102}


@pytest.mark.asyncio
async def test_comments_with_no_reactions_capture_nothing(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=8200, pr_number=1)
    fetcher = _FakeReactionFetcher(reactions_by_comment={8200: ()})

    await run_memory_reaction_poll_job(
        repo_id=str(repo_id),
        pr_number=1,
        installation_id=_INSTALLATION_ID,
        repo_full_name=_REPO_FULL_NAME,
        reaction_fetcher=fetcher,
        signal_recorder=repository,
        dedup_store=repository,
        max_comments_per_poll=50,
    )

    assert await _memory_entries_for_comment(session_factory, comment_id=8200) == []
