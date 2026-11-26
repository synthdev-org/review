"""Integration tests: `adapter.storage.memory_repository.PostgresMemoryRepository`
against real Postgres/pgvector (via `testcontainers-python`, per
`docs/rules/testing.md`'s mandatory DB-integration rule).

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

import math
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
    AdminRole,
    AdminUserRow,
    MemoryEntryRow,
    MemoryEntryStatus,
    Org,
    PostedFindingCommentRow,
    Repo,
    Team,
)
from memory.models import PostedComment
from memory.store import MemoryEntryNotFoundError, MemoryStoreError

pytestmark = pytest.mark.integration

_EMBEDDING_DIMENSION = 384


def _vector(fill: float = 0.0) -> tuple[float, ...]:
    return tuple(float(fill) for _ in range(_EMBEDDING_DIMENSION))


def _vector_with_similarity(cosine_similarity: float) -> tuple[float, ...]:
    """A `_EMBEDDING_DIMENSION`-dim vector whose cosine similarity to the
    all-ones query vector (`_vector(1.0)`) is exactly `cosine_similarity` —
    split half-and-half between `cos(theta) + sin(theta)` and
    `cos(theta) - sin(theta)` (`theta = acos(cosine_similarity)`), which
    works out to exactly `cos(theta)` cosine similarity against an all-ones
    vector of any dimension (the two halves are each other's orthogonal
    complement relative to the all-ones direction).
    """
    theta = math.acos(cosine_similarity)
    a = math.cos(theta) + math.sin(theta)
    b = math.cos(theta) - math.sin(theta)
    half = _EMBEDDING_DIMENSION // 2
    return tuple([a] * half + [b] * (_EMBEDDING_DIMENSION - half))


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
async def test_record_signal_returns_true_when_matched(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9102)

    found = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "matched"},
        github_comment_id=556,
    )

    assert found is True


@pytest.mark.asyncio
async def test_record_signal_returns_false_for_unknown_posted_finding_comment(
    repository: PostgresMemoryRepository,
) -> None:
    """A signal whose `posted_finding_comment_id` doesn't correlate to any row
    this platform posted is a routine, expected outcome — never an error —
    so `apps/webhook_api/webhook_routes.py`'s capture handler can log it as
    `"not_a_platform_comment"` and still accept (200) the webhook.
    """
    found = await repository.record_signal(
        posted_finding_comment_id=999_999,
        raw_signal_type="reply",
        raw_signal_detail={"body": "orphaned"},
        github_comment_id=1,
    )

    assert found is False


@pytest.mark.asyncio
async def test_record_signal_is_idempotent_on_redelivery(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """A GitHub redelivery of the identical reply event (same
    `posted_finding_comment_id`/`raw_signal_type`/`github_comment_id`
    triple) upserts via `ON CONFLICT DO NOTHING` rather than creating a
    second `pending` row every retry — redelivery-safe by design.
    """
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9103)

    first = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "first delivery"},
        github_comment_id=557,
    )
    second = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "redelivered"},
        github_comment_id=557,
    )

    assert (first, second) == (True, True)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryEntryRow).where(
                        MemoryEntryRow.posted_finding_comment_id == comment_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # The original delivery's content survives; the redelivery's own
    # (different) body never overwrote it — `ON CONFLICT DO NOTHING` skips
    # the row entirely rather than upserting new content over it.
    assert rows[0].raw_signal_detail == {"body": "first delivery"}


@pytest.mark.asyncio
async def test_record_signal_keeps_both_rows_when_a_thread_toggles_resolution(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """A thread `resolved` event followed by a later `unresolved` event on
    that *same* thread (identical `posted_finding_comment_id`,
    `raw_signal_type="thread_resolution"`, and `github_comment_id` — the
    thread's own last comment id never differs by action) are two distinct
    real-world state changes, not a redelivery of the same event, and must
    both be captured — regression test for the bug a review pass on the
    first implementation found (the original 3-column key silently
    dropped the second call's write via `ON CONFLICT DO NOTHING`, while
    still reporting `found=True`).
    """
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9104)

    resolved = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="thread_resolution",
        raw_signal_detail={"resolved": True},
        github_comment_id=558,
    )
    unresolved = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="thread_resolution",
        raw_signal_detail={"resolved": False},
        github_comment_id=558,
    )

    assert (resolved, unresolved) == (True, True)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryEntryRow)
                    .where(MemoryEntryRow.posted_finding_comment_id == comment_id)
                    .order_by(MemoryEntryRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [row.raw_signal_detail for row in rows] == [{"resolved": True}, {"resolved": False}]


@pytest.mark.asyncio
async def test_record_signal_keeps_both_rows_for_two_distinct_reactions_on_one_comment(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Two distinct reactions on the same posted comment (identical
    `posted_finding_comment_id`, `raw_signal_type="reaction"`, and
    `github_comment_id=None` — `memory.reaction_poll` always passes `None`
    for a reaction signal) are two distinct real-world signals, not a
    redelivery of the same one, and must both be captured — regression test
    for the bug a review pass found (without a per-reaction `signal_variant`,
    the second reaction's insert collided with the first's on the exact same
    `uq_memory_entry_signal` key and was silently dropped by
    `ON CONFLICT DO NOTHING`, while still reporting `found=True`).
    """
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9105)

    thumbs_up = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reaction",
        raw_signal_detail={"reaction_id": 1, "content": "+1"},
        github_comment_id=None,
    )
    thumbs_down = await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reaction",
        raw_signal_detail={"reaction_id": 2, "content": "-1"},
        github_comment_id=None,
    )

    assert (thumbs_up, thumbs_down) == (True, True)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryEntryRow)
                    .where(MemoryEntryRow.posted_finding_comment_id == comment_id)
                    .order_by(MemoryEntryRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [row.raw_signal_detail["reaction_id"] for row in rows] == [1, 2]


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
async def test_list_entries_filters_by_org_id(
    repository: PostgresMemoryRepository,
    session_factory: async_sessionmaker[Any],
) -> None:
    """A regression test for a gap an earlier draft of `MemoryAdminRepository.list_entries`
    dropped `org_id`/`team_id` down to `repo_id` alone. `record_signal`
    derives `memory_entry.org_id` from the comment's own repo's `org_id`
    (see its own docstring), so seeding two repos under two different orgs
    and recording a signal against each proves the filter genuinely
    isolates by org, not merely by repo."""
    async with session_factory() as session, session.begin():
        org_a = Org(name="org-a", slug=f"org-a-{uuid.uuid4().hex}")
        org_b = Org(name="org-b", slug=f"org-b-{uuid.uuid4().hex}")
        session.add_all([org_a, org_b])
        await session.flush()
        repo_a = Repo(org_id=org_a.id, name="repo-a", full_name=f"org-a/repo-a-{uuid.uuid4().hex}")
        repo_b = Repo(org_id=org_b.id, name="repo-b", full_name=f"org-b/repo-b-{uuid.uuid4().hex}")
        session.add_all([repo_a, repo_b])
        await session.flush()
        org_a_id, repo_a_id, repo_b_id = org_a.id, repo_a.id, repo_b.id

    comment_a = await _seed_posted_comment(session_factory, repo_id=repo_a_id, comment_id=9210)
    comment_b = await _seed_posted_comment(session_factory, repo_id=repo_b_id, comment_id=9211)

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
        org_id=str(org_a_id), repo_id=None, team_id=None, status=None, limit=50, offset=0
    )

    assert total == 1
    assert len(entries) == 1
    assert entries[0].posted_finding_comment_id == comment_a
    assert entries[0].raw_signal_detail == {"emoji": "+1"}


@pytest.mark.asyncio
async def test_list_entries_filters_by_team_id(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Nothing in this codebase writes `memory_entry.team_id` yet (per
    `MemoryEntryRow`'s own docstring — a documented placeholder, no writer
    exists), so unlike `org_id`/`repo_id` this filter can't be proven
    through `record_signal`. Seeds two rows directly, one scoped to a real
    `team_id` and one not, and proves `list_entries`'s `team_id` filter
    isolates the former without `repo_id`/`org_id` in play."""
    comment_a = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9212)
    comment_b = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9213)

    async with session_factory() as session, session.begin():
        org = (await session.execute(select(Repo.org_id).where(Repo.id == repo_id))).scalar_one()
        team = Team(org_id=org, name=f"team-{uuid.uuid4().hex}")
        session.add(team)
        await session.flush()
        team_id = team.id
        session.add(
            MemoryEntryRow(
                posted_finding_comment_id=comment_a,
                repo_id=repo_id,
                team_id=team_id,
                raw_signal_type="reaction",
                raw_signal_detail={"emoji": "+1"},
            )
        )
        session.add(
            MemoryEntryRow(
                posted_finding_comment_id=comment_b,
                repo_id=repo_id,
                team_id=None,
                raw_signal_type="reaction",
                raw_signal_detail={"emoji": "-1"},
            )
        )

    entries, total = await repository.list_entries(
        org_id=None, repo_id=None, team_id=str(team_id), status=None, limit=50, offset=0
    )

    assert total == 1
    assert len(entries) == 1
    assert entries[0].posted_finding_comment_id == comment_a
    assert entries[0].raw_signal_detail == {"emoji": "+1"}


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
        entry_id=entry_id,
        status="approved",
        entry_type="dismissed_finding",
        embedding=_vector(0.5),
        approved_by_user_id=None,
    )
    assert approved.status == "approved"
    assert approved.entry_type == "dismissed_finding"
    assert approved.embedding == _vector(0.5)
    assert approved.approved_at is not None

    rejected = await repository.set_status(
        entry_id=entry_id,
        status="rejected",
        entry_type=None,
        embedding=None,
        approved_by_user_id=None,
    )
    assert rejected.status == "rejected"
    assert rejected.entry_type is None
    assert rejected.embedding is None

    reapproved = await repository.set_status(
        entry_id=entry_id,
        status="approved",
        entry_type="correction",
        embedding=None,
        approved_by_user_id=None,
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
            approved_by_user_id=None,
        )


@pytest.mark.asyncio
async def test_set_status_records_the_approving_admin_and_clears_it_on_reject(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """`approved_by_user_id` is set from the acting Admin's id
    on approve, and — unlike `approved_at` — is cleared back to `None` on a
    later reject, since the entry's full state (including this field) is
    returned directly in list/get API responses regardless of status; a
    stale approver id on a rejected entry would misattribute an approval
    that's no longer in effect. A subsequent reapproval by a different
    admin re-attributes to that admin, not the original approver."""
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9402)
    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "not applicable"},
        github_comment_id=778,
    )
    entries, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=10, offset=0
    )
    entry_id = entries[0].id

    async with session_factory() as session, session.begin():
        admin_user = AdminUserRow(
            username=f"admin-{uuid.uuid4().hex}",
            password_hash="unused-hash",
            role=AdminRole.ADMIN,
        )
        session.add(admin_user)
        await session.flush()
        admin_user_id = str(admin_user.id)

    approved = await repository.set_status(
        entry_id=entry_id,
        status="approved",
        entry_type="dismissed_finding",
        embedding=_vector(0.5),
        approved_by_user_id=admin_user_id,
    )
    assert approved.approved_by_user_id == admin_user_id

    rejected = await repository.set_status(
        entry_id=entry_id,
        status="rejected",
        entry_type=None,
        embedding=None,
        approved_by_user_id=None,
    )
    assert rejected.approved_by_user_id is None

    async with session_factory() as session, session.begin():
        other_admin = AdminUserRow(
            username=f"admin-{uuid.uuid4().hex}",
            password_hash="unused-hash",
            role=AdminRole.ADMIN,
        )
        session.add(other_admin)
        await session.flush()
        other_admin_id = str(other_admin.id)

    reapproved = await repository.set_status(
        entry_id=entry_id,
        status="approved",
        entry_type="dismissed_finding",
        embedding=_vector(0.5),
        approved_by_user_id=other_admin_id,
    )
    assert reapproved.approved_by_user_id == other_admin_id


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
async def test_get_posted_comment_returns_the_linked_finding_fingerprint(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """The admin API's approve route (US-M4) reads a `dismissed_finding` entry's linked
    `posted_finding_comment` row via `get_posted_comment` to build the text
    it embeds — this proves that read against a real Postgres row.
    """
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9601)
    await repository.record_signal(
        posted_finding_comment_id=comment_id,
        raw_signal_type="reaction",
        raw_signal_detail={"emoji": "+1"},
        github_comment_id=None,
    )
    entries, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=10, offset=0
    )
    entry_id = entries[0].id

    posted_comment = await repository.get_posted_comment(entry_id=entry_id)

    assert posted_comment == PostedComment(
        comment_id=comment_id,
        file="app/main.py",
        line=10,
        rule_id="no-secrets",
        message="a secret was found",
    )


@pytest.mark.asyncio
async def test_get_posted_comment_raises_not_found_for_an_unknown_id(
    repository: PostgresMemoryRepository,
) -> None:
    with pytest.raises(MemoryEntryNotFoundError):
        await repository.get_posted_comment(entry_id=str(uuid.uuid4()))


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
        entry_id=entry_id,
        status="approved",
        entry_type="dismissed_finding",
        embedding=query_vector,
        approved_by_user_id=None,
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


async def _seed_approved_dismissed_finding(
    session_factory: async_sessionmaker[Any],
    *,
    repo_id: uuid.UUID,
    repository: PostgresMemoryRepository,
    comment_id: int,
    embedding: tuple[float, ...],
    approved_at: datetime,
) -> None:
    posted_comment_id = await _seed_posted_comment(
        session_factory, repo_id=repo_id, comment_id=comment_id
    )
    await repository.record_signal(
        posted_finding_comment_id=posted_comment_id,
        raw_signal_type="reply",
        raw_signal_detail={"body": "not applicable"},
        github_comment_id=comment_id,
    )
    entries, _ = await repository.list_entries(
        org_id=None, repo_id=str(repo_id), team_id=None, status=None, limit=100, offset=0
    )
    entry_id = next(
        entry.id for entry in entries if entry.posted_finding_comment_id == posted_comment_id
    )
    await repository.set_status(
        entry_id=entry_id,
        status="approved",
        entry_type="dismissed_finding",
        embedding=embedding,
        approved_by_user_id=None,
    )
    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(MemoryEntryRow).where(MemoryEntryRow.id == uuid.UUID(entry_id))
            )
        ).scalar_one()
        row.approved_at = approved_at


@pytest.mark.asyncio
async def test_best_effective_similarity_considers_every_candidate_not_just_the_raw_nearest(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Regression test for the bug a review pass found: the query used to
    order by raw `cosine_distance` and `LIMIT 1` *before* applying decay, so
    decay only ever got a chance to run on the single raw-nearest row. A
    slightly-less-similar but much more recently-approved entry can have a
    higher *decay-weighted* similarity than the raw-nearest one — this
    Protocol's own contract (`memory.store.DismissedFindingMatcher
    .best_effective_similarity`) is "the highest decay-weighted similarity
    ... any approved ... entry", not "the raw-nearest entry's decayed
    similarity".
    """
    now = datetime.now(UTC)
    half_life_days = 30.0

    # Raw-nearest (0.95) but approved 200 days ago -> heavily decayed.
    await _seed_approved_dismissed_finding(
        session_factory,
        repo_id=repo_id,
        repository=repository,
        comment_id=9602,
        embedding=_vector_with_similarity(0.95),
        approved_at=now - timedelta(days=200),
    )
    # Raw-further (0.85) but approved 2 days ago -> barely decayed, and its
    # decayed similarity ends up higher than the raw-nearest entry's.
    await _seed_approved_dismissed_finding(
        session_factory,
        repo_id=repo_id,
        repository=repository,
        comment_id=9603,
        embedding=_vector_with_similarity(0.85),
        approved_at=now - timedelta(days=2),
    )

    result = await repository.best_effective_similarity(
        repo_id=str(repo_id),
        embedding=_vector(1.0),
        half_life_days=half_life_days,
        now=now,
    )

    expected_from_recent_entry = 0.85 * 0.5 ** (2.0 / half_life_days)
    expected_from_old_entry = 0.95 * 0.5 ** (200.0 / half_life_days)
    assert expected_from_recent_entry > expected_from_old_entry  # sanity-check the fixture
    assert result == pytest.approx(expected_from_recent_entry, abs=1e-3)


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


# --- recent_comment_ids / filter_new (ReactionDedupStore) ---


@pytest.mark.asyncio
async def test_recent_comment_ids_returns_newest_first_bounded_by_limit(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    for i in range(3):
        await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9800 + i)

    ids = await repository.recent_comment_ids(repo_id=str(repo_id), pr_number=7, limit=2)

    assert len(ids) == 2
    # Newest first (highest `created_at`) — 9802 (seeded last) and 9801,
    # never the oldest, 9800.
    assert set(ids) == {9801, 9802}


@pytest.mark.asyncio
async def test_recent_comment_ids_scoped_to_repo_and_pr(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            PostedFindingCommentRow(
                id=9900,
                repo_id=repo_id,
                pr_number=99,
                commit_sha="a" * 40,
                finding_file="app/main.py",
                finding_line=10,
                finding_rule_id="no-secrets",
                finding_message="a secret was found",
            )
        )
    await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9901)

    ids = await repository.recent_comment_ids(repo_id=str(repo_id), pr_number=7, limit=50)

    assert ids == (9901,)


@pytest.mark.asyncio
async def test_recent_comment_ids_returns_empty_tuple_with_no_rows(
    repository: PostgresMemoryRepository, repo_id: uuid.UUID
) -> None:
    ids = await repository.recent_comment_ids(repo_id=str(repo_id), pr_number=1, limit=50)

    assert ids == ()


@pytest.mark.asyncio
async def test_filter_new_returns_and_records_previously_unseen_reaction_ids(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9910)

    new_ids = await repository.filter_new(
        posted_finding_comment_id=comment_id, reaction_ids=(1, 2, 3)
    )

    assert set(new_ids) == {1, 2, 3}


@pytest.mark.asyncio
async def test_filter_new_excludes_already_captured_reaction_ids(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """The check-then-record race this method closes: a
    reaction id already captured by an earlier poll is never returned
    again, even though it's still present in the fetcher's own response."""
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9911)

    first = await repository.filter_new(posted_finding_comment_id=comment_id, reaction_ids=(1, 2))
    second = await repository.filter_new(
        posted_finding_comment_id=comment_id, reaction_ids=(1, 2, 3)
    )

    assert set(first) == {1, 2}
    assert set(second) == {3}


@pytest.mark.asyncio
async def test_filter_new_with_no_reaction_ids_is_a_no_op(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    comment_id = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9912)

    new_ids = await repository.filter_new(posted_finding_comment_id=comment_id, reaction_ids=())

    assert new_ids == ()


@pytest.mark.asyncio
async def test_filter_new_scopes_dedup_per_comment(
    repository: PostgresMemoryRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """The same `reaction_id` value on two different comments is captured
    independently — the dedup key is the `(posted_finding_comment_id,
    reaction_id)` pair, never `reaction_id` alone."""
    comment_a = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9913)
    comment_b = await _seed_posted_comment(session_factory, repo_id=repo_id, comment_id=9914)

    await repository.filter_new(posted_finding_comment_id=comment_a, reaction_ids=(1,))
    new_for_b = await repository.filter_new(posted_finding_comment_id=comment_b, reaction_ids=(1,))

    assert new_for_b == (1,)


@pytest.mark.asyncio
async def test_recent_comment_ids_raises_memory_store_error_on_connection_failure(
    broken_repository: PostgresMemoryRepository,
) -> None:
    with pytest.raises(MemoryStoreError):
        await broken_repository.recent_comment_ids(repo_id=str(uuid.uuid4()), pr_number=1, limit=10)


@pytest.mark.asyncio
async def test_filter_new_raises_memory_store_error_on_connection_failure(
    broken_repository: PostgresMemoryRepository,
) -> None:
    with pytest.raises(MemoryStoreError):
        await broken_repository.filter_new(posted_finding_comment_id=1, reaction_ids=(1,))
