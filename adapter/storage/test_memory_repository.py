"""Integration tests: `adapter.storage.memory_repository.PostgresMemoryRepository`
against real Postgres/pgvector (via `testcontainers-python`, per
`docs/rules/testing.md`'s mandatory DB-integration rule), per
`docs/plans/plan-deliver-memory.md`'s Phase 1 (Unit A) Tests section.

Covers `record_comment_provenance`/`record_signal` persisting correctly,
`list_entries` filtering/pagination, `set_status` transitioning in both
directions and raising `MemoryEntryNotFoundError` for a missing id,
`delete_entry` removing a row and raising the same error on a repeat call,
`best_effective_similarity` returning `None` with zero approved entries and
the correct decayed value against a hand-seeded approved entry at a known
age, and a real Postgres-level failure surfacing as `MemoryStoreError`
(never a raw `sqlalchemy`/`asyncpg` exception).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.models import (
    MemoryEntryRow,
    MemoryEntryStatus,
    Org,
    PostedFindingCommentRow,
    Repo,
)
from memory.models import PostedComment
from memory.store import MemoryEntryNotFoundError, MemoryStoreError

pytestmark = pytest.mark.integration

_EMBEDDING_DIMENSION = 384


def _vector(fill: float = 0.0) -> tuple[float, ...]:
    return tuple(float(fill) for _ in range(_EMBEDDING_DIMENSION))


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repository(session_factory: async_sessionmaker[Any]) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(session_factory)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    """Provisions a real `org`/`repo` row pair so this module's own FK-
    constrained columns have something valid to reference."""
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _seed_posted_comment(
    session_factory: async_sessionmaker[Any], *, repo_id: uuid.UUID, comment_id: int
) -> int:
    async with session_factory() as session, session.begin():
        session.add(
            PostedFindingCommentRow(
                id=comment_id,
                repo_id=repo_id,
                pr_number=7,
                commit_sha="a" * 40,
                finding_file="app/main.py",
                finding_line=10,
                finding_rule_id="no-secrets",
                finding_message="a secret was found",
            )
        )
    return comment_id


@pytest_asyncio.fixture
async def broken_repository() -> AsyncIterator[PostgresMemoryRepository]:
    """A `PostgresMemoryRepository` pointed at an unreachable Postgres port
    — a real connection failure through the real driver/SQLAlchemy code
    path (never a mock), for proving the Postgres-level-failure ->
    `MemoryStoreError` translation without depending on triggering a
    specific constraint violation.
    """
    engine = create_storage_engine("postgresql+asyncpg://user:pass@127.0.0.1:1/nonexistent")
    try:
        yield PostgresMemoryRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_comment_provenance_persists_one_row_per_comment(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comments = (
        PostedComment(comment_id=9001, file="app/a.py", line=1, rule_id="r1", message="m1"),
        PostedComment(comment_id=9002, file="app/b.py", line=None, rule_id=None, message="m2"),
    )

    await repository.record_comment_provenance(
        repo_id=str(repo_id), pr_number=42, commit_sha="deadbeef", comments=comments
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(PostedFindingCommentRow).where(
                        PostedFindingCommentRow.id.in_([9001, 9002])
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {row.id: row for row in rows}
    assert by_id[9001].repo_id == repo_id
    assert by_id[9001].pr_number == 42
    assert by_id[9001].commit_sha == "deadbeef"
    assert by_id[9001].finding_file == "app/a.py"
    assert by_id[9001].finding_line == 1
    assert by_id[9001].finding_rule_id == "r1"
    assert by_id[9001].finding_message == "m1"
    assert by_id[9002].finding_line is None
    assert by_id[9002].finding_rule_id is None


@pytest.mark.asyncio
async def test_record_comment_provenance_with_no_comments_is_a_no_op(
    repository: PostgresMemoryRepository, repo_id: uuid.UUID
) -> None:
    # Should not raise even though `pr_number`/`commit_sha` are otherwise
    # unused when `comments` is empty.
    await repository.record_comment_provenance(
        repo_id=str(repo_id), pr_number=1, commit_sha="x", comments=()
    )


@pytest.mark.asyncio
async def test_record_signal_persists_a_pending_unclassified_entry(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9101)

    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "I don't think this applies here"},
        github_comment_id=555,
    )

    async with session_factory() as session:
        row = (
            await session.execute(
                select(MemoryEntryRow).where(MemoryEntryRow.posted_finding_comment_id == comment_id)
            )
        ).scalar_one()
    assert row.status == MemoryEntryStatus.PENDING
    assert row.entry_type is None
    assert row.raw_signal_type == "reply"
    assert row.raw_signal_detail == {"body": "I don't think this applies here"}
    assert row.github_comment_id == 555
    assert row.embedding is None


@pytest.mark.asyncio
async def test_list_entries_filters_by_repo_and_status(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    other_repo_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        org = Org(name="other-org", slug=f"other-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        other_repo = Repo(
            id=other_repo_id,
            org_id=org.id,
            name="other",
            full_name=f"other/other-{uuid.uuid4().hex}",
        )
        session.add(other_repo)

    comment_a = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9201)
    comment_b = await _seed_posted_comment(session_factory, repo_id=other_repo_id, comment_id=9202)

    await repository.record_signal(
        posted_finding_comment_id=comment_a,
        raw_signal_type="reaction",
        raw_signal_detail={"emoji": "+1"},
        github_comment_id=None,
    )
    await repository.record_signal(
        posted_finding_comment_id=comment_b,
        raw_signal_type="reaction",
        raw_signal_detail={"emoji": "-1"},
        github_comment_id=None,
    )

    entries, total = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=50, offset=0
    )

    assert total == 1
    assert len(entries) == 1
    assert entries[0].posted_finding_comment_id == comment_a
    assert entries[0].raw_signal_detail == {"emoji": "+1"}

    entries, total = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status="approved", limit=50, offset=0
    )
    assert total == 0
    assert entries == ()


@pytest.mark.asyncio
async def test_list_entries_paginates(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    for i in range(5):
        comment_id = await _seed_posted_comment(
            session_factory, repo_id=repo_id, comment_id=9300 + i
        )
        await repository.record_signal(
            posted_finding_comment_id=comment_id,
            raw_signal_type="reaction",
            raw_signal_detail={"i": i},
            github_comment_id=None,
        )

    first_page, total = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=2, offset=0
    )
    second_page, total_again = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=2, offset=2
    )

    assert total == 5
    assert total_again == 5
    assert len(first_page) == 2
    assert len(second_page) == 2
    assert {e.id for e in first_page}.isdisjoint({e.id for e in second_page})


@pytest.mark.asyncio
async def test_set_status_approves_then_rejects_then_reapproves(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9401)
    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "not applicable"},
        github_comment_id=777,
    )
    entries, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=10, offset=0
    )
    entry_id = entries[0].id

    approved = await repository.set_status(
        entry_id=entry_id, status="approved", entry_type="dismissed_finding", embedding=_vector(0.5)
    )
    assert approved.status == "approved"
    assert approved.entry_type == "dismissed_finding"
    assert approved.embedding == _vector(0.5)
    assert approved.approved_at is not None

    rejected = await repository.set_status(
        entry_id=entry_id, status="rejected", entry_type=None, embedding=None
    )
    assert rejected.status == "rejected"
    assert rejected.entry_type is None
    assert rejected.embedding is None

    reapproved = await repository.set_status(
        entry_id=entry_id, status="approved", entry_type="correction", embedding=None
    )
    assert reapproved.status == "approved"
    assert reapproved.entry_type == "correction"


@pytest.mark.asyncio
async def test_set_status_raises_not_found_for_an_unknown_id(
    repository: PostgresMemoryRepository,
) -> None:
    with pytest.raises(MemoryEntryNotFoundError):
        await repository.set_status(
            entry_id=str(uuid.uuid4()),
            status="approved",
            entry_type="dismissed_finding",
            embedding=None,
        )


@pytest.mark.asyncio
async def test_delete_entry_removes_the_row_and_raises_on_a_repeat_call(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9501)
    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="thread_resolution",
        raw_signal_detail={"resolved": True},
        github_comment_id=888,
    )
    entries, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=10, offset=0
    )
    entry_id = entries[0].id

    await repository.delete_entry(entry_id=entry_id)

    entries_after, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=10, offset=0
    )
    assert entry_id not in {e.id for e in entries_after}

    with pytest.raises(MemoryEntryNotFoundError):
        await repository.delete_entry(entry_id=entry_id)


@pytest.mark.asyncio
async def test_best_effective_similarity_returns_none_with_zero_approved_entries(
    repository: PostgresMemoryRepository, repo_id: uuid.UUID
) -> None:
    result = await repository.best_effective_similarity(
        repo_id=str(repo_id), embedding=_vector(0.5), half_life_days=180.0, now=datetime.now(UTC)
    )

    assert result is None


@pytest.mark.asyncio
async def test_best_effective_similarity_returns_the_correct_decayed_value(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9601)
    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "yeah, that's fine, not a real issue"},
        github_comment_id=999,
    )
    entries, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=10, offset=0
    )
    entry_id = entries[0].id

    query_vector = _vector(0.5)
    now = datetime.now(UTC)
    approved_180_days_ago = now - timedelta(days=180)

    await repository.set_status(
        entry_id=entry_id, status="approved", entry_type="dismissed_finding", embedding=query_vector
    )
    # `set_status` writes `approved_at = now()` server-side; hand-set it to
    # a known age directly so this test can assert an exact, hand-computed
    # decayed value rather than an unpredictable near-zero one.
    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(MemoryEntryRow).where(MemoryEntryRow.id == uuid.UUID(entry_id))
            )
        ).scalar_one()
        row.approved_at = approved_180_days_ago

    result = await repository.best_effective_similarity(
        repo_id=str(repo_id), embedding=query_vector, half_life_days=180.0, now=now
    )

    # Identical vectors -> cosine_distance == 0 -> raw_cosine_similarity == 1.0.
    # One half-life (180 / 180) -> 1.0 * 0.5 ** 1 == 0.5.
    assert result == pytest.approx(0.5, abs=1e-4)


@pytest.mark.asyncio
async def test_best_effective_similarity_ignores_pending_entries(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9701)
    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "still pending, never classified"},
        github_comment_id=1001,
    )

    result = await repository.best_effective_similarity(
        repo_id=str(repo_id), embedding=_vector(0.5), half_life_days=180.0, now=datetime.now(UTC)
    )

    assert result is None


@pytest.mark.asyncio
async def test_record_signal_raises_memory_store_error_on_connection_failure(
    broken_repository: PostgresMemoryRepository,
) -> None:
    with pytest.raises(MemoryStoreError):
        await broken_repository.record_signal(
            posted_finding_comment_id=1,
            raw_signal_type="reaction",
            raw_signal_detail={},
            github_comment_id=None,
        )


@pytest.mark.asyncio
async def test_list_entries_raises_memory_store_error_on_connection_failure(
    broken_repository: PostgresMemoryRepository,
) -> None:
    with pytest.raises(MemoryStoreError):
        await broken_repository.list_entries(
            org_id=None, repo_id=None, team_id=None, status=None, limit=10, offset=0
        )
