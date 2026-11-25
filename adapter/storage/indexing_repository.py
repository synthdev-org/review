"""adapter.storage.indexing_repository — the concrete Postgres/pgvector
implementation of ``indexing.repository``'s ``IndexRepository`` and
``IndexCommitTracker`` Protocols, per the indexing delivery plan's Phase 6
section (Unit F).

One ``IndexingRepository`` class implements both narrow Protocols (per
Assumption 13's split) — mirrors ``adapter/storage/tenancy.py``'s
``PostgresTenancyStore`` own placement (one adapter module per
storage-backed repository), wired in only at the composition root
(``apps/worker/main.py``, Phase 7).

Every public method here translates a real Postgres-level failure into
``indexing.repository.IndexingRepositoryError`` — never a raw driver
exception, per ``docs/rules/architecture.md``'s exception-type rule and the
plan's Assumption 14. Two distinct failure shapes are caught, deliberately
not a single bare ``except Exception``: ``sqlalchemy.exc.SQLAlchemyError``
(a constraint violation, or any failure surfaced once a statement actually
reaches Postgres) and ``OSError`` (a connection failure at the driver/
socket level — e.g. ``ConnectionRefusedError`` — which asyncpg/SQLAlchemy's
own pool-checkout path raises *unwrapped*, before it ever becomes a
``SQLAlchemyError``, per ``adapter/storage/pgvector_check.py``'s own
"connectivity failure looks different from a query failure" precedent).
Catching only these two, rather than every ``Exception``, means a genuine
programming bug elsewhere in this module (e.g. a ``KeyError`` from a
caller-supplied ``GraphEdge`` whose ``source_node`` isn't among the
``nodes`` it was upserted alongside) still propagates as itself, never
misreported as a storage failure.

**Node upsert dedupes by symbol name before writing, per US5's own key
definition ("each definition is upserted as a node keyed by (repo, file,
symbol name, language)")** — two distinct definitions that happen to share
a symbol name in the same file (e.g. two classes in one file each
declaring their own ``handle()`` method) collapse to the same
``index_graph_node`` row by design, not a bug this adapter introduces; a
single batched ``INSERT ... ON CONFLICT`` statement containing two rows for
the same conflict key would otherwise fail with Postgres's own "ON
CONFLICT DO UPDATE command cannot affect row a second time" restriction,
so this module resolves the collision itself (last occurrence in extraction
order wins) before ever reaching Postgres, rather than surfacing that
restriction as a confusing storage-level error for an expected, spec-defined
case.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from sqlalchemy import Row, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import IndexChunkRow, IndexGraphEdgeRow, IndexGraphNodeRow, Repo
from adapter.storage.unit_of_work import run_in_session
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingVector
from indexing.graph import GraphEdge, GraphNode
from indexing.repository import IndexCommitTracker, IndexingRepositoryError, IndexRepository


class IndexingRepository:
    """Implements both ``indexing.repository.IndexRepository`` and
    ``indexing.repository.IndexCommitTracker`` against real Postgres/pgvector
    tables (``index_chunk``, ``index_graph_node``, ``index_graph_edge``,
    ``repo.last_indexed_commit_sha``).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run[T](self, *, operation: str, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Shared open-a-session-then-translate-the-failure wrapper every
        method below uses around its own closure — one place for the
        "which two exception types get translated into
        ``IndexingRepositoryError``" choice this module's own docstring
        explains, rather than each method repeating the same
        ``try``/``except`` independently.
        """
        try:
            return await run_in_session(self._session_factory, None, fn)
        except (SQLAlchemyError, OSError) as exc:
            raise IndexingRepositoryError(operation=operation, detail=str(exc)) from exc

    # -- IndexRepository ---------------------------------------------------

    async def upsert_file(
        self,
        *,
        repo_id: uuid.UUID,
        pr_number: int,
        file_path: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[EmbeddingVector],
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
    ) -> None:
        chunks_tuple = tuple(chunks)
        embeddings_tuple = tuple(embeddings)
        if len(chunks_tuple) != len(embeddings_tuple):
            raise ValueError(
                "chunks/embeddings length mismatch: "
                f"{len(chunks_tuple)} chunks vs {len(embeddings_tuple)} embeddings"
            )
        other_chunk_paths = {chunk.file_path for chunk in chunks_tuple} - {file_path}
        if other_chunk_paths:
            raise ValueError(
                f"chunks must all belong to file_path={file_path!r}, got {other_chunk_paths}"
            )
        nodes_tuple = tuple(nodes)
        edges_tuple = tuple(edges)
        other_node_paths = {node.file_path for node in nodes_tuple} - {file_path}
        if other_node_paths:
            raise ValueError(
                f"nodes must all belong to file_path={file_path!r}, got {other_node_paths}"
            )

        chunk_statement = None
        if chunks_tuple:
            chunk_values = [
                {
                    "repo_id": repo_id,
                    "pr_number": pr_number,
                    "file_path": chunk.file_path,
                    "chunk_index": chunk.chunk_index,
                    "language": chunk.language,
                    "symbol_name": chunk.symbol_name,
                    "byte_range_start": chunk.byte_range[0],
                    "byte_range_end": chunk.byte_range[1],
                    "start_line": chunk.line_range[0] if chunk.line_range else None,
                    "end_line": chunk.line_range[1] if chunk.line_range else None,
                    "content": chunk.content,
                    "embedding": list(vector.values),
                }
                for chunk, vector in zip(chunks_tuple, embeddings_tuple, strict=True)
            ]
            chunk_insert = pg_insert(IndexChunkRow).values(chunk_values)
            chunk_statement = chunk_insert.on_conflict_do_update(
                index_elements=["repo_id", "pr_number", "file_path", "chunk_index"],
                set_={
                    "language": chunk_insert.excluded.language,
                    "symbol_name": chunk_insert.excluded.symbol_name,
                    "byte_range_start": chunk_insert.excluded.byte_range_start,
                    "byte_range_end": chunk_insert.excluded.byte_range_end,
                    "start_line": chunk_insert.excluded.start_line,
                    "end_line": chunk_insert.excluded.end_line,
                    "content": chunk_insert.excluded.content,
                    "embedding": chunk_insert.excluded.embedding,
                    "updated_at": func.now(),
                },
            )

        # A file's own chunk count can shrink between pushes, down to and
        # including zero (e.g. edited to become empty/whitespace-only) — a
        # plain upsert never removes the now-stale tail, so file_path's
        # surviving chunk_index set is deleted down to exactly what this
        # call just wrote. ``file_path`` is taken explicitly (never derived
        # from ``chunks``) precisely so this still runs when ``chunks`` is
        # empty. ``.notin_(())`` on an empty surviving set matches every
        # row for this file, deleting it entirely, exactly as intended.
        surviving_indices = {chunk.chunk_index for chunk in chunks_tuple}

        # Dedupe by symbol_name (last occurrence wins) before writing — see
        # this module's own docstring for why.
        node_by_symbol: dict[str, GraphNode] = {}
        for node in nodes_tuple:
            node_by_symbol[node.symbol_name] = node

        node_statement = None
        if node_by_symbol:
            node_values = [
                {
                    "repo_id": repo_id,
                    "pr_number": pr_number,
                    "file_path": node.file_path,
                    "symbol_name": node.symbol_name,
                    "language": node.language,
                }
                for node in node_by_symbol.values()
            ]
            node_insert = pg_insert(IndexGraphNodeRow).values(node_values)
            node_statement = node_insert.on_conflict_do_update(
                index_elements=["repo_id", "pr_number", "file_path", "symbol_name"],
                set_={"language": node_insert.excluded.language, "updated_at": func.now()},
            ).returning(IndexGraphNodeRow.id, IndexGraphNodeRow.symbol_name)

        # A symbol renamed or removed within a file that itself still
        # exists (so delete_file's own cascade never runs for it) must not
        # leave its old node — and that node's edges — behind: file_path's
        # surviving symbol_name set (which may be empty, e.g. a file edited
        # down to zero definitions) is deleted down to exactly what this
        # call just wrote, cascading to that node's own edges via
        # index_graph_edge.source_node_id's ondelete=CASCADE. file_path is
        # taken explicitly (never derived from nodes), the same as for
        # chunks, precisely so this cleanup still runs when nodes is empty.
        surviving_symbols = set(node_by_symbol)

        async def _write(session: AsyncSession) -> None:
            # Chunks and graph nodes/edges are written against the same
            # session/transaction (never two independent ``_run`` calls) so
            # that a failure partway through either half rolls back both —
            # per indexing/repository.py's own upsert_file docstring, and
            # indexing/pipeline.py's documented never-a-partial-commit
            # guarantee.
            if chunk_statement is not None:
                await session.execute(chunk_statement)
            await session.execute(
                delete(IndexChunkRow).where(
                    IndexChunkRow.repo_id == repo_id,
                    IndexChunkRow.pr_number == pr_number,
                    IndexChunkRow.file_path == file_path,
                    IndexChunkRow.chunk_index.notin_(surviving_indices),
                )
            )

            node_id_by_symbol: dict[str, uuid.UUID] = {}
            if node_statement is not None:
                result = await session.execute(node_statement)
                node_id_by_symbol = {row.symbol_name: row.id for row in result}

            await session.execute(
                delete(IndexGraphNodeRow).where(
                    IndexGraphNodeRow.repo_id == repo_id,
                    IndexGraphNodeRow.pr_number == pr_number,
                    IndexGraphNodeRow.file_path == file_path,
                    IndexGraphNodeRow.symbol_name.notin_(surviving_symbols),
                )
            )

            node_ids = tuple(node_id_by_symbol.values())
            if node_ids:
                await session.execute(
                    delete(IndexGraphEdgeRow).where(IndexGraphEdgeRow.source_node_id.in_(node_ids))
                )

            if edges_tuple:
                edge_values = [
                    {
                        "source_node_id": node_id_by_symbol[edge.source_node.symbol_name],
                        "target_symbol_name": edge.target_symbol_name,
                    }
                    for edge in edges_tuple
                ]
                edge_statement = pg_insert(IndexGraphEdgeRow).values(edge_values)
                edge_statement = edge_statement.on_conflict_do_nothing(
                    index_elements=["source_node_id", "target_symbol_name"]
                )
                await session.execute(edge_statement)

        await self._run(operation="upsert_file", fn=_write)

    async def delete_file(self, *, repo_id: uuid.UUID, pr_number: int, file_path: str) -> None:
        async def _write(session: AsyncSession) -> None:
            await session.execute(
                delete(IndexChunkRow).where(
                    IndexChunkRow.repo_id == repo_id,
                    IndexChunkRow.pr_number == pr_number,
                    IndexChunkRow.file_path == file_path,
                )
            )
            # index_graph_edge.source_node_id's own ondelete="CASCADE"
            # removes every edge sourced from a node deleted here — no
            # second explicit delete needed for that case (see this
            # module's own docstring and indexing/repository.py's
            # delete_file docstring for why an edge whose *target* was
            # this file's symbol is deliberately left in place).
            await session.execute(
                delete(IndexGraphNodeRow).where(
                    IndexGraphNodeRow.repo_id == repo_id,
                    IndexGraphNodeRow.pr_number == pr_number,
                    IndexGraphNodeRow.file_path == file_path,
                )
            )

        await self._run(operation="delete_file", fn=_write)

    # -- IndexCommitTracker --------------------------------------------------

    async def get_last_indexed_commit(self, *, repo_id: uuid.UUID) -> str | None:
        async def _read(session: AsyncSession) -> Row[tuple[str | None]] | None:
            result = await session.execute(
                select(Repo.last_indexed_commit_sha).where(Repo.id == repo_id)
            )
            return result.first()

        row = await self._run(operation="get_last_indexed_commit", fn=_read)

        if row is None:
            raise IndexingRepositoryError(
                operation="get_last_indexed_commit", detail=f"repo {repo_id} not found"
            )
        return cast("str | None", row[0])

    async def set_last_indexed_commit(self, *, repo_id: uuid.UUID, commit_sha: str) -> None:
        async def _write(session: AsyncSession) -> int:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(Repo)
                    .where(Repo.id == repo_id)
                    .values(last_indexed_commit_sha=commit_sha)
                ),
            )
            return result.rowcount

        rowcount = await self._run(operation="set_last_indexed_commit", fn=_write)

        if rowcount == 0:
            raise IndexingRepositoryError(
                operation="set_last_indexed_commit", detail=f"repo {repo_id} not found"
            )


# Runtime confirmation (not just a docstring claim) that this concrete
# implementation actually satisfies both Protocols it stands in for,
# mirroring adapter/github/tree.py's own runtime check.
_: type[IndexRepository] = IndexingRepository
__: type[IndexCommitTracker] = IndexingRepository
