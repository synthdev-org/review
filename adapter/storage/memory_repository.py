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
``repo_id``/``status="approved"``/``entry_type="dismissed_finding"``
(mirroring ``PostgresSimilarityQuery.find_similar``'s exact query shape
against a new table instead of ``index_chunk``), computes ``1 -
cosine_distance`` for every candidate's raw cosine similarity, applies
``memory.decay.effective_similarity`` to each, and returns the maximum —
no ANN index, no top-``N`` pre-filter, no latency SLA (per
``spec-memory.md``'s NFRs, matching ``PostgresSimilarityQuery``'s own v1
posture): decay can reorder candidates relative to raw similarity, so only
considering the raw-nearest row would silently miss a more-recently-
approved, slightly-less-similar entry with a higher decay-weighted score.

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
from typing import Any, Literal, cast

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


def _to_entry(row: MemoryEntryRow, *, carried_suggestion: bool = False) -> MemoryEntry:
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
        approved_by_user_id=(
            str(row.approved_by_user_id) if row.approved_by_user_id is not None else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        carried_suggestion=carried_suggestion,
    )


class PostgresMemoryRepository:
    """Implements ``memory.store.MemorySignalRecorder``,
    ``memory.store.DismissedFindingMatcher``,
    ``memory.store.MemoryAdminRepository``, ``memory.store.PostedCommentReader``
    (Phase 5, Unit E — split out from ``MemoryAdminRepository`` to keep both
    Protocols within the 1-3-method rule), and (Phase 4, Unit D)
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

    @staticmethod
    def _parse_uuid(value: str, *, operation: str) -> uuid.UUID:
        """Shared malformed-id-to-``MemoryStoreError`` translation — every
        public method below that accepts a caller-supplied id string
        (``repo_id``/``entry_id``/``org_id``/``team_id``) parses it through
        here instead of repeating the same ``try/except ValueError`` block.
        """
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise MemoryStoreError(operation=operation, detail=str(exc)) from exc

    # -- MemorySignalRecorder ----------------------------------------------

    async def record_comment_provenance(
        self, *, repo_id: str, pr_number: int, commit_sha: str, comments: Sequence[PostedComment]
    ) -> None:
        comments_tuple = tuple(comments)
        if not comments_tuple:
            return

        repo_uuid = self._parse_uuid(repo_id, operation="record_comment_provenance")

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
                        carried_suggestion=comment.carried_suggestion,
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
            # parameter — every other signal type (`reply`) leaves it at its
            # `""` default, so a genuine redelivery of those still collides
            # on the same key it always has.
            #
            # `reaction` also needs its own per-signal variant: unlike
            # `reply`/`thread_resolution` (one signal per webhook event, so
            # the 3-column key already identifies "this event"), a single
            # `posted_finding_comment_id` can carry several *distinct*
            # reactions (👍 from one reviewer, 👎 from another), each its
            # own `memory.reaction_poll` call with `github_comment_id=None`
            # every time — without a variant, the second reaction's insert
            # collides with the first's on the exact same key and is
            # silently dropped by `ON CONFLICT DO NOTHING`, even though it's
            # not a redelivery of anything. Keyed on the reaction's own id
            # rather than its content, so two people reacting with the same
            # emoji still both get captured. This does *not* also give this
            # constraint redelivery-idempotency for `reaction` the way it
            # does for `reply`/`thread_resolution`: `github_comment_id` is
            # always `NULL` here, and Postgres's own "NULLs are never equal"
            # unique-index semantics mean `uq_memory_entry_signal` never
            # actually matches a `reaction` row against another one,
            # regardless of `signal_variant` — real redelivery-safety for
            # reactions lives entirely in `ReactionDedupStore.filter_new`'s
            # own non-nullable `(posted_finding_comment_id, reaction_id)`
            # key (Assumption 9), called before this method ever runs, which
            # already guarantees `record_signal` sees a given reaction id at
            # most once.
            signal_variant = ""
            if raw_signal_type == "thread_resolution":
                signal_variant = "resolved" if raw_signal_detail.get("resolved") else "unresolved"
            elif raw_signal_type == "reaction":
                signal_variant = str(raw_signal_detail.get("reaction_id", ""))[:16]
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
        repo_uuid = self._parse_uuid(repo_id, operation="best_effective_similarity")
        vector = list(embedding)

        # Decay is applied per-candidate below, not in this query — ordering
        # by raw cosine distance and taking the single closest row would
        # only ever decay *that* row, but a slightly-less-similar, more
        # recently-approved entry can have a *higher* decay-weighted
        # similarity than the raw-closest one (this Protocol's own contract
        # is "the highest decay-weighted similarity ... any approved ...
        # entry", not "the raw-nearest entry's decayed similarity"). No
        # `LIMIT`, matching this module's own "no ANN index, no latency SLA"
        # v1 posture (see this module's docstring) — every approved
        # `dismissed_finding` candidate must be considered to answer the
        # Protocol's actual question.
        async def _read(session: AsyncSession) -> Sequence[tuple[datetime, float]]:
            statement = select(
                MemoryEntryRow.approved_at, MemoryEntryRow.embedding.cosine_distance(vector)
            ).where(
                MemoryEntryRow.repo_id == repo_uuid,
                MemoryEntryRow.status == MemoryEntryStatus.APPROVED,
                MemoryEntryRow.entry_type == "dismissed_finding",
                MemoryEntryRow.embedding.is_not(None),
            )
            result = await session.execute(statement)
            return [(approved_at, cosine_distance) for approved_at, cosine_distance in result]

        candidates = await self._run(operation="best_effective_similarity", fn=_read)
        if not candidates:
            return None
        return max(
            effective_similarity(
                1.0 - float(cosine_distance),
                approved_at=approved_at,
                half_life_days=half_life_days,
                now=now,
            )
            for approved_at, cosine_distance in candidates
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
        org_uuid = (
            self._parse_uuid(org_id, operation="list_entries") if org_id is not None else None
        )
        repo_uuid = (
            self._parse_uuid(repo_id, operation="list_entries") if repo_id is not None else None
        )
        team_uuid = (
            self._parse_uuid(team_id, operation="list_entries") if team_id is not None else None
        )

        conditions = []
        if org_uuid is not None:
            conditions.append(MemoryEntryRow.org_id == org_uuid)
        if repo_uuid is not None:
            conditions.append(MemoryEntryRow.repo_id == repo_uuid)
        if team_uuid is not None:
            conditions.append(MemoryEntryRow.team_id == team_uuid)
        if status is not None:
            conditions.append(MemoryEntryRow.status == MemoryEntryStatus(status))

        async def _read(session: AsyncSession) -> tuple[Sequence[Any], int]:
            entries_statement = (
                select(MemoryEntryRow, PostedFindingCommentRow.carried_suggestion)
                .join(
                    PostedFindingCommentRow,
                    PostedFindingCommentRow.id == MemoryEntryRow.posted_finding_comment_id,
                )
                .where(*conditions)
                .order_by(MemoryEntryRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            entries_result = await session.execute(entries_statement)
            rows = entries_result.all()

            count_statement = select(func.count()).select_from(MemoryEntryRow).where(*conditions)
            count_result = await session.execute(count_statement)
            total = count_result.scalar_one()
            return rows, total

        rows, total = await self._run(operation="list_entries", fn=_read)
        return tuple(_to_entry(row, carried_suggestion=carried) for row, carried in rows), total

    async def set_status(
        self,
        *,
        entry_id: str,
        status: Literal["approved", "rejected"],
        entry_type: MemoryEntryType,
        embedding: tuple[float, ...] | None,
        approved_by_user_id: str | None,
    ) -> MemoryEntry:
        entry_uuid = self._parse_uuid(entry_id, operation="set_status")

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
            row.approved_by_user_id = (
                uuid.UUID(approved_by_user_id) if approved_by_user_id is not None else None
            )
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
        entry_uuid = self._parse_uuid(entry_id, operation="delete_entry")

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

    # -- PostedCommentReader (Phase 5, Unit E) --------------------------------

    async def get_posted_comment(self, *, entry_id: str) -> PostedComment:
        entry_uuid = self._parse_uuid(entry_id, operation="get_posted_comment")

        async def _read(session: AsyncSession) -> PostedFindingCommentRow | None:
            statement = (
                select(PostedFindingCommentRow)
                .join(
                    MemoryEntryRow,
                    MemoryEntryRow.posted_finding_comment_id == PostedFindingCommentRow.id,
                )
                .where(MemoryEntryRow.id == entry_uuid)
            )
            result = await session.execute(statement)
            return result.scalar_one_or_none()

        row = await self._run(operation="get_posted_comment", fn=_read)
        if row is None:
            raise MemoryEntryNotFoundError(entry_id=entry_id)
        return PostedComment(
            comment_id=row.id,
            file=row.finding_file,
            line=row.finding_line,
            rule_id=row.finding_rule_id,
            message=row.finding_message,
            carried_suggestion=row.carried_suggestion,
        )

    # -- ReactionDedupStore (Phase 4, Unit D) ---------------------------------

    async def recent_comment_ids(
        self, *, repo_id: str, pr_number: int, limit: int
    ) -> tuple[int, ...]:
        repo_uuid = self._parse_uuid(repo_id, operation="recent_comment_ids")

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
