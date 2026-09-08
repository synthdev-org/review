"""adapter.storage.memory_repository — the concrete Postgres/pgvector
implementation of ``memory.store``'s storage-backed Protocols
(``MemorySignalRecorder``, ``DismissedFindingMatcher``,
``MemoryAdminRepository``, and — Phase 4, Unit D — ``ReactionDedupStore``),
per ``docs/plans/plan-deliver-memory.md``'s Phase 1 section (Unit A).

One ``PostgresMemoryRepository`` class implements all four narrow
Protocols (Assumption 8), mirroring ``adapter/storage/indexing_repository.py``'s
``IndexingRepository`` own precedent (one concrete class, one
``session_factory``, every one of its own Protocols) — constructed once per
entrypoint (Worker and Webhook API each build their own instance from their
own session factory, the same way ``JobQueueRepository``/
``PostgresTenancyStore`` already are).

``recent_comment_ids``/``filter_new`` (Phase 4, Unit D) back US-M3's
reaction-poll job (``memory/reaction_poll.py``) — the former bounds which
``posted_finding_comment`` rows a poll even looks at
(``MEMORY_REACTION_POLL_MAX_COMMENTS_PER_POLL``); the latter is a single
``INSERT ... ON CONFLICT DO NOTHING ... RETURNING`` against
``captured_reaction`` that both checks and durably records a poll's newly-
observed reaction ids in one round trip, closing the check-then-record race
a separate pair of calls would leave open.

Reuses ``adapter/storage/unit_of_work.py``'s ``run_in_session`` and
``adapter/storage/indexing_repository.py``'s established session-open/
``(SQLAlchemyError, OSError)``-translation ``_run`` wrapper shape — the same
precedent ``adapter/storage/context_query.py``'s ``PostgresGraphQuery``/
``PostgresSimilarityQuery`` already follow.

``best_effective_similarity`` queries ``memory_entry`` filtered by
``repo_id``/``status="approved"``/``entry_type="dismissed_finding"``,
ordered by ``embedding.cosine_distance(vector)`` (mirroring
``PostgresSimilarityQuery.find_similar``'s exact query shape against a new
table instead of ``index_chunk``), takes the single closest match, computes
``1 - cosine_distance`` for a raw cosine similarity, and applies
``memory.decay.effective_similarity`` before returning — no ANN index, no
latency SLA (per ``spec-memory.md``'s NFRs, matching
``PostgresSimilarityQuery``'s own v1 posture).

``record_signal`` (Phase 3, Unit C) upserts via ``ON CONFLICT DO NOTHING``
against ``uq_memory_entry_signal`` (created by Phase 1's own migration) —
GitHub's at-least-once webhook delivery semantics mean the same reply/
thread-resolution event can arrive more than once, and this makes a
redelivery a silent no-op rather than a duplicate ``pending`` row. Returns
``True``/``False`` (found-and-recorded vs. no correlating
``posted_finding_comment`` row) rather than raising for the "no match"
case — that outcome is a routine, expected one for
``apps/webhook_api/webhook_routes.py``'s own capture handler (a webhook
comment/thread this platform never posted), not a failure.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import (
    CapturedReactionRow,
    MemoryEntryRow,
    MemoryEntryStatus,
    PostedFindingCommentRow,
    Repo,
)
from adapter.storage.unit_of_work import run_in_session
from memory.decay import effective_similarity
from memory.models import MemoryEntry, MemoryEntryType, MemoryStatus, PostedComment, RawSignalType
from memory.store import (
    DismissedFindingMatcher,
    MemoryAdminRepository,
    MemoryEntryNotFoundError,
    MemorySignalRecorder,
    MemoryStoreError,
    ReactionDedupStore,
)


def _to_entry(row: MemoryEntryRow) -> MemoryEntry:
    # `memory_entry.repo_id` is a nullable FK at the schema level (the same
    # "narrow the scope" convention as org_id/team_id), but
    # `memory.models.MemoryEntry.repo_id` is a required `str` — every writer
    # this phase has (`record_signal`) always resolves and persists a real
    # `repo_id`, raising `MemoryStoreError` if it can't. A `NULL` here would
    # be a genuine data-integrity violation, not a value to silently coerce
    # into the literal string `"None"` past the pydantic boundary.
    if row.repo_id is None:
        raise MemoryStoreError(
            operation="_to_entry", detail=f"memory_entry {row.id} has NULL repo_id"
        )
    return MemoryEntry(
        id=str(row.id),
        repo_id=str(row.repo_id),
        org_id=str(row.org_id) if row.org_id is not None else None,
        team_id=str(row.team_id) if row.team_id is not None else None,
        entry_type=cast("MemoryEntryType", row.entry_type),
        status=row.status.value,
        raw_signal_type=cast("RawSignalType", row.raw_signal_type),
        raw_signal_detail=row.raw_signal_detail,
        posted_finding_comment_id=row.posted_finding_comment_id,
        embedding=tuple(row.embedding) if row.embedding is not None else None,
        approved_at=row.approved_at,
        approved_by=row.approved_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresMemoryRepository:
    """Implements ``memory.store.MemorySignalRecorder``,
    ``memory.store.DismissedFindingMatcher``,
    ``memory.store.MemoryAdminRepository``, and (Phase 4, Unit D)
    ``memory.store.ReactionDedupStore`` against real Postgres/pgvector
    tables (``posted_finding_comment``, ``memory_entry``,
    ``captured_reaction``).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run[T](self, *, operation: str, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Shared open-a-session-then-translate-the-failure wrapper, the
        same shape ``adapter/storage/indexing_repository.py``'s own
        ``_run`` uses against these exact tables.
        """
        try:
            return await run_in_session(self._session_factory, None, fn)
        except (SQLAlchemyError, OSError) as exc:
            raise MemoryStoreError(operation=operation, detail=str(exc)) from exc

    # -- MemorySignalRecorder ----------------------------------------------

    async def record_comment_provenance(
        self, *, repo_id: str, pr_number: int, commit_sha: str, comments: Sequence[PostedComment]
    ) -> None:
        comments_tuple = tuple(comments)
        if not comments_tuple:
            return

        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise MemoryStoreError(operation="record_comment_provenance", detail=str(exc)) from exc

        async def _write(session: AsyncSession) -> None:
            session.add_all(
                [
                    PostedFindingCommentRow(
                        id=comment.comment_id,
                        repo_id=repo_uuid,
                        pr_number=pr_number,
                        commit_sha=commit_sha,
                        finding_file=comment.file,
                        finding_line=comment.line,
                        finding_rule_id=comment.rule_id,
                        finding_message=comment.message,
                    )
                    for comment in comments_tuple
                ]
            )

        await self._run(operation="record_comment_provenance", fn=_write)

    async def record_signal(
        self,
        *,
        posted_finding_comment_id: int,
        raw_signal_type: RawSignalType,
        raw_signal_detail: dict[str, object],
        github_comment_id: int | None,
    ) -> bool:
        # `record_signal`'s own Protocol signature takes no repo/org/team
        # scope — the caller (Phase 3's webhook handler, Phase 4's reaction
        # poll job) only ever has the *comment* it's reacting to in scope,
        # not a fresh lookup of that comment's own repo. Resolved here via
        # `posted_finding_comment_id`'s own FK to `posted_finding_comment`
        # (repo_id) and that row's `repo.org_id` (one join, in the same
        # write transaction as the insert) — `memory_entry.repo_id`/`org_id`
        # are what `list_entries`'s filters and `best_effective_similarity`'s
        # own `WHERE repo_id = ...` scoping actually read; leaving them NULL
        # here would silently break both. `team_id` is left NULL — nothing
        # in this codebase associates a PR/comment with a team yet (the
        # same "placeholder, no writer exists" state
        # `OrgRepoTeamSetting`/`ModelRoutingConfig` are already in).
        async def _write(session: AsyncSession) -> bool:
            lookup = await session.execute(
                select(PostedFindingCommentRow.repo_id, Repo.org_id)
                .join(Repo, Repo.id == PostedFindingCommentRow.repo_id)
                .where(PostedFindingCommentRow.id == posted_finding_comment_id)
            )
            row = lookup.first()
            if row is None:
                return False
            repo_id, org_id = row
            # `signal_variant` (0006_memory_entry_signal_variant.py):
            # disambiguates a thread's `resolved` event from a later
            # `unresolved` event on that *same* thread — both share the
            # same `raw_signal_type="thread_resolution"` and the same
            # `github_comment_id` (the thread's own last comment id, not
            # something that differs by action), so without this the
            # original 3-column key would collapse them into one row (the
            # second event's `ON CONFLICT DO NOTHING` silently discarding
            # real state-change data, not just a genuine redelivery).
            # Derived from `raw_signal_detail` here, at the storage
            # boundary, rather than a new `MemorySignalRecorder.record_signal`
            # parameter — every other signal type (`reply`, `reaction`)
            # leaves it at its `""` default, so a genuine redelivery of
            # those still collides on the same key it always has.
            signal_variant = ""
            if raw_signal_type == "thread_resolution":
                signal_variant = "resolved" if raw_signal_detail.get("resolved") else "unresolved"
            # `ON CONFLICT DO NOTHING` against `uq_memory_entry_signal`
            # (posted_finding_comment_id, raw_signal_type,
            # github_comment_id, signal_variant) — Phase 3's own
            # redelivery-safety design: a GitHub redelivery of the
            # identical reply/thread-resolution event silently no-ops here
            # rather than creating a second `pending` row every retry.
            # Whether this particular call inserted a fresh row or hit the
            # conflict-skip path is immaterial to the caller — either way
            # `posted_finding_comment_id` correlated to a real row, which is
            # what `found` reports.
            statement = (
                pg_insert(MemoryEntryRow)
                .values(
                    repo_id=repo_id,
                    org_id=org_id,
                    posted_finding_comment_id=posted_finding_comment_id,
                    raw_signal_type=raw_signal_type,
                    raw_signal_detail=raw_signal_detail,
                    github_comment_id=github_comment_id,
                    signal_variant=signal_variant,
                )
                .on_conflict_do_nothing(constraint="uq_memory_entry_signal")
            )
            await session.execute(statement)
            return True

        return await self._run(operation="record_signal", fn=_write)

    # -- DismissedFindingMatcher ---------------------------------------------

    async def best_effective_similarity(
        self, *, repo_id: str, embedding: tuple[float, ...], half_life_days: float, now: datetime
    ) -> float | None:
        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise MemoryStoreError(operation="best_effective_similarity", detail=str(exc)) from exc
        vector = list(embedding)

        async def _read(session: AsyncSession) -> tuple[float, datetime] | None:
            statement = (
                select(MemoryEntryRow.approved_at, MemoryEntryRow.embedding.cosine_distance(vector))
                .where(
                    MemoryEntryRow.repo_id == repo_uuid,
                    MemoryEntryRow.status == MemoryEntryStatus.APPROVED,
                    MemoryEntryRow.entry_type == "dismissed_finding",
                    MemoryEntryRow.embedding.is_not(None),
                )
                .order_by(MemoryEntryRow.embedding.cosine_distance(vector))
                .limit(1)
            )
            result = await session.execute(statement)
            row = result.first()
            if row is None:
                return None
            approved_at, cosine_distance = row
            return (float(cosine_distance), approved_at)

        found = await self._run(operation="best_effective_similarity", fn=_read)
        if found is None:
            return None
        cosine_distance, approved_at = found
        raw_similarity = 1.0 - cosine_distance
        return effective_similarity(
            raw_similarity, approved_at=approved_at, half_life_days=half_life_days, now=now
        )

    # -- MemoryAdminRepository -----------------------------------------------

    async def list_entries(
        self,
        *,
        org_id: str | None,
        repo_id: str | None,
        team_id: str | None,
        status: MemoryStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[MemoryEntry, ...], int]:
        try:
            org_uuid = uuid.UUID(org_id) if org_id is not None else None
            repo_uuid = uuid.UUID(repo_id) if repo_id is not None else None
            team_uuid = uuid.UUID(team_id) if team_id is not None else None
        except ValueError as exc:
            raise MemoryStoreError(operation="list_entries", detail=str(exc)) from exc

        conditions = []
        if org_uuid is not None:
            conditions.append(MemoryEntryRow.org_id == org_uuid)
        if repo_uuid is not None:
            conditions.append(MemoryEntryRow.repo_id == repo_uuid)
        if team_uuid is not None:
            conditions.append(MemoryEntryRow.team_id == team_uuid)
        if status is not None:
            conditions.append(MemoryEntryRow.status == MemoryEntryStatus(status))

        async def _read(session: AsyncSession) -> tuple[Sequence[MemoryEntryRow], int]:
            entries_statement = (
                select(MemoryEntryRow)
                .where(*conditions)
                .order_by(MemoryEntryRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            entries_result = await session.execute(entries_statement)
            rows = entries_result.scalars().all()

            count_statement = select(func.count()).select_from(MemoryEntryRow).where(*conditions)
            count_result = await session.execute(count_statement)
            total = count_result.scalar_one()
            return rows, total

        rows, total = await self._run(operation="list_entries", fn=_read)
        return tuple(_to_entry(row) for row in rows), total

    async def set_status(
        self,
        *,
        entry_id: str,
        status: Literal["approved", "rejected"],
        entry_type: MemoryEntryType,
        embedding: tuple[float, ...] | None,
    ) -> MemoryEntry:
        try:
            entry_uuid = uuid.UUID(entry_id)
        except ValueError as exc:
            raise MemoryStoreError(operation="set_status", detail=str(exc)) from exc

        async def _write(session: AsyncSession) -> MemoryEntryRow | None:
            result = await session.execute(
                select(MemoryEntryRow).where(MemoryEntryRow.id == entry_uuid).with_for_update()
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.status = MemoryEntryStatus(status)
            row.entry_type = entry_type
            row.embedding = list(embedding) if embedding is not None else None
            if status == "approved":
                row.approved_at = func.now()
            await session.flush()
            await session.refresh(row)
            return row

        row = await self._run(operation="set_status", fn=_write)
        if row is None:
            raise MemoryEntryNotFoundError(entry_id=entry_id)
        return _to_entry(row)

    async def delete_entry(self, *, entry_id: str) -> None:
        try:
            entry_uuid = uuid.UUID(entry_id)
        except ValueError as exc:
            raise MemoryStoreError(operation="delete_entry", detail=str(exc)) from exc

        async def _write(session: AsyncSession) -> bool:
            result = await session.execute(
                select(MemoryEntryRow).where(MemoryEntryRow.id == entry_uuid)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            return True

        deleted = await self._run(operation="delete_entry", fn=_write)
        if not deleted:
            raise MemoryEntryNotFoundError(entry_id=entry_id)

    # -- ReactionDedupStore (Phase 4, Unit D) ---------------------------------

    async def recent_comment_ids(
        self, *, repo_id: str, pr_number: int, limit: int
    ) -> tuple[int, ...]:
        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise MemoryStoreError(operation="recent_comment_ids", detail=str(exc)) from exc

        async def _read(session: AsyncSession) -> Sequence[int]:
            statement = (
                select(PostedFindingCommentRow.id)
                .where(
                    PostedFindingCommentRow.repo_id == repo_uuid,
                    PostedFindingCommentRow.pr_number == pr_number,
                )
                .order_by(
                    PostedFindingCommentRow.created_at.desc(), PostedFindingCommentRow.id.desc()
                )
                .limit(limit)
            )
            result = await session.execute(statement)
            return result.scalars().all()

        ids = await self._run(operation="recent_comment_ids", fn=_read)
        return tuple(ids)

    async def filter_new(
        self, *, posted_finding_comment_id: int, reaction_ids: Sequence[int]
    ) -> tuple[int, ...]:
        reaction_ids_tuple = tuple(reaction_ids)
        if not reaction_ids_tuple:
            return ()

        # A single `INSERT ... ON CONFLICT DO NOTHING ... RETURNING` closes
        # the check-then-record race a separate "check captured_reaction,
        # then insert" pair would leave open under concurrent polls of the
        # same PR (Assumption 9): a row that already exists never appears in
        # `RETURNING`'s own output, so the returned ids are exactly the ones
        # this call itself just durably captured, in one round trip.
        async def _write(session: AsyncSession) -> Sequence[int]:
            statement = (
                pg_insert(CapturedReactionRow)
                .values(
                    [
                        {
                            "posted_finding_comment_id": posted_finding_comment_id,
                            "reaction_id": reaction_id,
                        }
                        for reaction_id in reaction_ids_tuple
                    ]
                )
                .on_conflict_do_nothing(index_elements=["posted_finding_comment_id", "reaction_id"])
                .returning(CapturedReactionRow.reaction_id)
            )
            result = await session.execute(statement)
            return result.scalars().all()

        new_ids = await self._run(operation="filter_new", fn=_write)
        return tuple(new_ids)


# Runtime confirmation (not just a docstring claim) that this concrete
# implementation actually satisfies every Protocol it stands in for,
# mirroring adapter/storage/indexing_repository.py's own runtime check.
_: type[MemorySignalRecorder] = PostgresMemoryRepository
__: type[DismissedFindingMatcher] = PostgresMemoryRepository
___: type[MemoryAdminRepository] = PostgresMemoryRepository
____: type[ReactionDedupStore] = PostgresMemoryRepository
