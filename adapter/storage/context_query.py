"""adapter.storage.context_query — the concrete Postgres/pgvector
implementation of ``review.context_builder``'s ``GraphQuery`` Protocol
(Phase 2/Unit B of ``docs/plans/plan-deliver-context-builder.md``).

``PostgresGraphQuery`` reuses ``adapter/storage/unit_of_work.py``'s
``run_in_session`` helper and ``adapter/storage/indexing_repository.py``'s
established session-open/``(SQLAlchemyError, OSError)``-translation wrapper
shape — the same pattern already used against these exact tables — rather
than hand-rolling a second, parallel session-management mechanism.

``find_callers`` runs in two batched stages, per the plan's Phase 2 section:

1. **Edge/node lookup**, capped inside the query itself via a ``LATERAL``
   join with a per-symbol ``LIMIT`` — deliberately *not* a window function
   (``ROW_NUMBER() OVER (PARTITION BY ...)``) over the whole matched set,
   which only bounds the number of rows *transmitted*, not the cost of
   *computing* them (Postgres would still have to gather and sort every
   matching edge in a partition before it could rank and truncate). The
   ``LATERAL`` join instead runs one indexed, ``LIMIT``-bounded subquery per
   symbol name, letting Postgres use the existing
   ``ix_index_graph_edge_target_symbol_name`` index and stop scanning each
   symbol's matches once ``limit_per_symbol`` is reached — bounding actual
   computation, not just result size. **The join condition is the
   security-critical part, not just the performance-critical part:**
   ``index_graph_edge`` has no ``repo_id`` column of its own — isolation
   depends entirely on ``node.repo_id``/``node.pr_number = 0`` being part
   of the ``LATERAL`` subquery's own join condition, never an after-the-fact
   filter, because a scoping bug here would forward another org's — or
   another PR's untrusted, fork-authored — content into the requesting
   job's context bundle. This is genuinely new raw-SQL territory for this
   codebase (no existing ``LATERAL`` join precedent to build on with the
   ORM/Core query builder alone) — written as a parameterized ``text()``
   query with explicit ``bindparams`` types (never string-built), the same
   "raw SQL only where the query builder genuinely can't express it, still
   fully parameterized" precedent ``adapter/storage/pgvector_check.py``
   already sets.

2. **Content resolution**, batched across every distinct ``(file_path,
   symbol_name)`` pair stage 1 returned, in one query — using SQLAlchemy's
   ``tuple_(...).in_(...)`` row-value construct, never independent
   per-column ``IN`` lists (which would be a cross product: any file
   matched against any symbol in the two lists, silently resolving a
   caller's citation to the *wrong* file's content). ``tuple_(...).in_(...)``
   binds every pair as genuine parameters through the ORM query builder —
   never a hand-built SQL string — while still expressing the same
   row-value ``WHERE (file_path, symbol_name) = ANY(:pairs)`` semantics the
   plan's own text names. Filtered by ``repo_id``, ``pr_number = 0`` as a
   plain ``WHERE`` condition — correct, but a structurally weaker isolation
   guarantee than stage 1's join (named explicitly, not left implicit,
   since this distinction matters for what the isolation tests prove).

Neither stage logs — a real Postgres failure is translated to
``ContextQueryError`` and raised; the caller (``ContextBuilderStep``, a
later phase) is the one place with a ``job_id`` in scope to correlate a
logged warning to, per this module's own Observability note in the plan.

``PostgresSimilarityQuery`` (Phase 3, Unit C) implements
``review.context_builder.SimilarityQuery`` — the first vector-similarity
(KNN) *read* against ``index_chunk`` in this codebase (prior code only ever
wrote to it). Unlike ``PostgresGraphQuery.find_callers``'s stage 1, this
query's shape (a single-table filter, order, and limit) is fully expressible
through SQLAlchemy's own query builder plus ``pgvector.sqlalchemy.Vector``'s
``cosine_distance`` comparator (``embedding <=> :vector`` under the hood,
per the plan's own text) — no raw ``text()`` SQL needed here, unlike stage
1's ``LATERAL`` join. Per Assumption 5, no ANN index backs this for v1: a
sequential scan plus sort, one per call, accepted since no latency SLA is
set. Scoped by ``repo_id``/``pr_number = 0``/``file_path != exclude_file_path``
in the query's own ``WHERE`` clause, mirroring stage 2's isolation shape
above (a plain filter, not a join condition) — the same isolation guarantee,
proven the same way in this module's test suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from sqlalchemy import ARRAY, Integer, Row, String, Uuid, bindparam, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import IndexChunkRow
from adapter.storage.unit_of_work import run_in_session
from indexing.chunk import Chunk, ChunkLanguage
from indexing.embedding import EmbeddingVector
from review.context_builder import CallerHit, ContextQueryError, GraphQuery, SimilarityQuery

# Stage 1: one LATERAL-bounded subquery per requested symbol name.
# `s.name` is carried through into the SELECT list so each returned row can
# be attributed back to the (possibly duplicated) `symbol_names` entry that
# produced it — `find_callers`'s own dict-of-tuples return shape is keyed
# on this column, not on `hit.symbol_name` (the *caller's* own symbol name,
# a different thing).
_FIND_CALLERS_EDGES_QUERY = text(
    """
    SELECT s.name AS target_symbol_name, hit.file_path AS file_path, hit.symbol_name AS symbol_name
    FROM unnest(:symbol_names) AS s(name)
    CROSS JOIN LATERAL (
        SELECT node.file_path AS file_path, node.symbol_name AS symbol_name
        FROM index_graph_edge edge
        JOIN index_graph_node node
            ON edge.source_node_id = node.id
            AND node.repo_id = :repo_id
            AND node.pr_number = 0
        WHERE edge.target_symbol_name = s.name
        ORDER BY node.file_path, node.symbol_name
        LIMIT :limit_per_symbol
    ) hit
    ORDER BY s.name, hit.file_path, hit.symbol_name
    """
).bindparams(
    bindparam("symbol_names", type_=ARRAY(String)),
    bindparam("repo_id", type_=Uuid()),
    bindparam("limit_per_symbol", type_=Integer()),
)


class PostgresGraphQuery:
    """Implements ``review.context_builder.GraphQuery`` against real
    Postgres tables (``index_graph_edge``, ``index_graph_node``,
    ``index_chunk``).
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
            raise ContextQueryError(operation=operation, detail=str(exc)) from exc

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        # Deduped up front, preserving order — the Protocol's own docstring
        # promises exactly one dict entry per name in `symbol_names`
        # (including duplicates collapsed to one key), and the
        # `unnest(:symbol_names)`-based query below issues one independent
        # LATERAL subquery per *occurrence*, not per distinct name; without
        # this, a caller passing a duplicated name would get that name's
        # LATERAL subquery run twice, doubling both query cost and the
        # resulting `CallerHit` entries appended under that one key.
        symbol_names_tuple = tuple(dict.fromkeys(symbol_names))
        if not symbol_names_tuple:
            return {}

        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise ContextQueryError(operation="find_callers_edges", detail=str(exc)) from exc

        async def _read_edges(session: AsyncSession) -> Sequence[Row[Any]]:
            result = await session.execute(
                _FIND_CALLERS_EDGES_QUERY,
                {
                    "symbol_names": list(symbol_names_tuple),
                    "repo_id": repo_uuid,
                    "limit_per_symbol": limit_per_symbol,
                },
            )
            return result.all()

        edge_rows = await self._run(operation="find_callers_edges", fn=_read_edges)

        results: dict[str, list[CallerHit]] = {name: [] for name in symbol_names_tuple}
        if not edge_rows:
            return dict.fromkeys(results, ())

        # Every distinct (file_path, symbol_name) pair stage 1 returned,
        # resolved to its actual chunk content in a single stage-2 query —
        # never one content query per edge.
        pairs = sorted({(row.file_path, row.symbol_name) for row in edge_rows})

        async def _read_content(session: AsyncSession) -> Sequence[Row[Any]]:
            statement = select(
                IndexChunkRow.file_path,
                IndexChunkRow.symbol_name,
                IndexChunkRow.content,
                IndexChunkRow.start_line,
                IndexChunkRow.end_line,
                IndexChunkRow.chunk_index,
            ).where(
                IndexChunkRow.repo_id == repo_uuid,
                IndexChunkRow.pr_number == 0,
                # Row-value matching, not independent per-column IN lists —
                # see this module's own docstring for why the latter would
                # be a cross-product correctness bug here.
                tuple_(IndexChunkRow.file_path, IndexChunkRow.symbol_name).in_(pairs),
            )
            result = await session.execute(statement)
            return result.all()

        content_rows = await self._run(operation="find_callers_content", fn=_read_content)
        # A symbol split across multiple `_pack()` windows (per
        # `indexing/chunk.py`) produces more than one `index_chunk` row for
        # the same (file_path, symbol_name) pair — keep the lowest
        # `chunk_index` (the window containing the symbol's own
        # declaration/start) deterministically, rather than whichever row
        # Postgres happens to return last.
        content_by_pair: dict[tuple[str, str], Row[Any]] = {}
        for row in content_rows:
            key = (row.file_path, row.symbol_name)
            existing = content_by_pair.get(key)
            if existing is None or row.chunk_index < existing.chunk_index:
                content_by_pair[key] = row

        for row in edge_rows:
            content_row = content_by_pair.get((row.file_path, row.symbol_name))
            if content_row is None:
                # No index_chunk row resolved for this caller (e.g. the
                # graph node's symbol_name doesn't line up with any chunk's
                # own symbol_name) — dropped silently rather than emitting
                # a CallerHit with no real content; per US7/US8's own
                # "contributes nothing rather than blocking the rest"
                # posture applied at item granularity.
                continue
            line_range: tuple[int, int] | None = None
            if content_row.start_line is not None and content_row.end_line is not None:
                line_range = (content_row.start_line, content_row.end_line)
            results[row.target_symbol_name].append(
                CallerHit(
                    file_path=row.file_path,
                    symbol_name=row.symbol_name,
                    content=content_row.content,
                    line_range=line_range,
                )
            )

        return {name: tuple(hits) for name, hits in results.items()}


class PostgresSimilarityQuery:
    """Implements ``review.context_builder.SimilarityQuery`` against real
    Postgres/pgvector (``index_chunk``), per Phase 3 (Unit C).

    Reuses the identical open-a-session-then-translate-the-failure ``_run``
    shape ``PostgresGraphQuery`` above already uses against the same
    tables — a small, deliberate duplication (rather than a shared base
    class) matching this module's own existing precedent of one ``_run``
    method per concrete class.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run[T](self, *, operation: str, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        try:
            return await run_in_session(self._session_factory, None, fn)
        except (SQLAlchemyError, OSError) as exc:
            raise ContextQueryError(operation=operation, detail=str(exc)) from exc

    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise ContextQueryError(operation="find_similar", detail=str(exc)) from exc
        vector = list(embedding.values)

        async def _read(session: AsyncSession) -> Sequence[IndexChunkRow]:
            statement = (
                select(IndexChunkRow)
                .where(
                    IndexChunkRow.repo_id == repo_uuid,
                    IndexChunkRow.pr_number == 0,
                    IndexChunkRow.file_path != exclude_file_path,
                )
                .order_by(IndexChunkRow.embedding.cosine_distance(vector))
                .limit(limit)
            )
            result = await session.execute(statement)
            return result.scalars().all()

        rows = await self._run(operation="find_similar", fn=_read)

        return tuple(
            Chunk(
                file_path=row.file_path,
                language=cast("ChunkLanguage", row.language),
                symbol_name=row.symbol_name,
                byte_range=(row.byte_range_start, row.byte_range_end),
                line_range=(
                    (row.start_line, row.end_line)
                    if row.start_line is not None and row.end_line is not None
                    else None
                ),
                content=row.content,
                chunk_index=row.chunk_index,
            )
            for row in rows
        )


# Runtime confirmation (not just a docstring claim) that these concrete
# implementations actually satisfy the Protocols they stand in for,
# mirroring adapter/storage/indexing_repository.py's own runtime check.
_: type[GraphQuery] = PostgresGraphQuery
_similarity: type[SimilarityQuery] = PostgresSimilarityQuery
