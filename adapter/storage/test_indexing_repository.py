"""Integration tests: `adapter.storage.indexing_repository.IndexingRepository`
against real Postgres/pgvector (via `testcontainers-python`, per
`docs/rules/testing.md`'s mandatory DB-integration rule).

Covers US6 (delta upsert), US7 (deletion + dangling edges), the edge-table
uniqueness/delete-then-reinsert behavior, `pr_number`-scoped isolation
between canonical and PR-branch rows (closing the fork-PR data-poisoning
gap), a byte-identical embedding-vector round-trip, and
a real Postgres-level failure surfacing as `IndexingRepositoryError` (never
a raw `sqlalchemy`/`asyncpg` exception) from each of
`upsert_file`/`delete_file`, and that `upsert_file`'s chunk and graph
writes land in a single transaction (never a partial commit of one without
the other).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import IndexChunkRow, IndexGraphEdgeRow, IndexGraphNodeRow, Org, Repo
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingVector
from indexing.graph import GraphEdge, GraphNode
from indexing.repository import IndexingRepositoryError

pytestmark = pytest.mark.integration

_EMBEDDING_DIMENSION = 384


def _vector(fill: float = 0.0) -> EmbeddingVector:
    """A deterministic, fixed-dimension vector whose values (small
    integers-as-floats) are exactly representable in the ``float32``
    pgvector stores under the hood, so a round-trip through Postgres never
    loses precision — see ``test_upsert_chunks_embedding_round_trips``.
    """
    values = tuple(float(fill) for _ in range(_EMBEDDING_DIMENSION))
    return EmbeddingVector(values=values, dimension=_EMBEDDING_DIMENSION)


def _chunk(
    *,
    file_path: str = "src/foo.ts",
    chunk_index: int = 0,
    symbol_name: str | None = "foo",
    content: str = "function foo() {}",
    line_range: tuple[int, int] | None = None,
) -> Chunk:
    return Chunk(
        file_path=file_path,
        language="typescript",
        symbol_name=symbol_name,
        byte_range=(0, len(content)),
        line_range=line_range,
        content=content,
        chunk_index=chunk_index,
    )


def _node(*, file_path: str = "src/foo.ts", symbol_name: str = "foo") -> GraphNode:
    return GraphNode(file_path=file_path, symbol_name=symbol_name, language="typescript")


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repository(session_factory: async_sessionmaker[Any]) -> IndexingRepository:
    return IndexingRepository(session_factory)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    """Provisions a real `org`/`repo` row pair so `index_chunk`'s/
    `index_graph_node`'s FK-constrained `repo_id` column has something
    valid to reference."""
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


@pytest_asyncio.fixture
async def broken_repository() -> AsyncIterator[IndexingRepository]:
    """An `IndexingRepository` pointed at an unreachable Postgres port — a
    real connection failure through the real driver/SQLAlchemy code path
    (never a mock), for proving the Postgres-level-failure ->
    `IndexingRepositoryError` translation without depending on triggering a
    specific constraint violation.
    """
    engine = create_storage_engine("postgresql+asyncpg://user:pass@127.0.0.1:1/nonexistent")
    try:
        yield IndexingRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_chunks_stores_and_updates_row_in_place(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(content="v1")],
        embeddings=[_vector(1.0)],
        nodes=[],
        edges=(),
    )
    # US6/US11: a retried or redelivered job for the same file overwrites
    # the same (repo_id, pr_number, file_path, chunk_index) row cleanly
    # rather than duplicating it.
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(content="v2")],
        embeddings=[_vector(2.0)],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        rows = (
            (await session.execute(select(IndexChunkRow).where(IndexChunkRow.repo_id == repo_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].content == "v2"
    assert rows[0].embedding[0] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_upsert_chunks_writes_start_line_and_end_line_columns(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """`IndexChunkRow` gaining `start_line`/`end_line` columns is
    only half the fix — `upsert_file`'s own explicit per-column insert/
    on-conflict-update values dict must also actually write
    `chunk.line_range` into them, or the new columns silently stay `NULL`
    for every chunk indexed after deploy with no error. Proven here against
    real Postgres, including that a delta upsert's on-conflict path updates
    them too, not just the initial insert.
    """
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(content="v1", line_range=(10, 12))],
        embeddings=[_vector(1.0)],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        row = (
            (await session.execute(select(IndexChunkRow).where(IndexChunkRow.repo_id == repo_id)))
            .scalars()
            .one()
        )
    assert (row.start_line, row.end_line) == (10, 12)

    # On-conflict-update path also carries line_range forward, not just the
    # initial insert.
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(content="v2", line_range=(20, 25))],
        embeddings=[_vector(2.0)],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        row = (
            (await session.execute(select(IndexChunkRow).where(IndexChunkRow.repo_id == repo_id)))
            .scalars()
            .one()
        )
    assert (row.start_line, row.end_line) == (20, 25)


@pytest.mark.asyncio
async def test_upsert_chunks_embedding_round_trips_exactly(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    values = tuple(float(i % 7) for i in range(_EMBEDDING_DIMENSION))
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk()],
        embeddings=[EmbeddingVector(values=values, dimension=_EMBEDDING_DIMENSION)],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT embedding FROM index_chunk WHERE repo_id = :repo_id"),
                {"repo_id": repo_id},
            )
        ).scalar_one()

    stored = [float(v) for v in row.strip("[]").split(",")]
    assert stored == list(values)


@pytest.mark.asyncio
async def test_upsert_chunks_pr_number_scoping_never_overwrites_canonical_row(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(content="canonical")],
        embeddings=[_vector(0.0)],
        nodes=[],
        edges=(),
    )
    # A fork-PR-triggered job (pr_number > 0) for the *same* file path must
    # land in a separate row, never touching the pr_number=0 canonical row
    # — the core fork-PR isolation fix.
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=42,
        file_path="src/foo.ts",
        chunks=[_chunk(content="pr-branch-content")],
        embeddings=[_vector(0.0)],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(IndexChunkRow)
                    .where(IndexChunkRow.repo_id == repo_id)
                    .order_by(IndexChunkRow.pr_number)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 2
    assert rows[0].pr_number == 0
    assert rows[0].content == "canonical"
    assert rows[1].pr_number == 42
    assert rows[1].content == "pr-branch-content"


@pytest.mark.asyncio
async def test_upsert_chunks_shrinking_file_deletes_stale_tail_chunks(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    # A file first indexed with 3 chunks (chunk_index 0-2)...
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(chunk_index=i, content=f"v{i}") for i in range(3)],
        embeddings=[_vector() for _ in range(3)],
        nodes=[],
        edges=(),
    )
    # ...then shrinks to 1 chunk on a later push. The now-stale
    # chunk_index 1-2 rows must be removed, not left behind.
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(chunk_index=0, content="v0-updated")],
        embeddings=[_vector()],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(IndexChunkRow)
                    .where(IndexChunkRow.repo_id == repo_id)
                    .order_by(IndexChunkRow.chunk_index)
                )
            )
            .scalars()
            .all()
        )
    assert [row.chunk_index for row in rows] == [0]
    assert rows[0].content == "v0-updated"


@pytest.mark.asyncio
async def test_upsert_chunks_shrinking_to_zero_chunks_deletes_all_rows_for_file(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    # A file first indexed with content...
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[_chunk(chunk_index=i, content=f"v{i}") for i in range(2)],
        embeddings=[_vector() for _ in range(2)],
        nodes=[],
        edges=(),
    )
    # ...then edited down to empty/whitespace-only content on a later push
    # — `chunk_file` produces zero chunks for this case (see
    # `indexing/chunk.py`'s own `_pack_whole_file`), so this call's own
    # `chunks`/`embeddings` are both empty. `file_path` is taken explicitly
    # (never derived from `chunks`) precisely so the now-fully-stale rows
    # for this file are still found and removed here, not left behind
    # forever.
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[],
        embeddings=[],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        rows = (
            (await session.execute(select(IndexChunkRow).where(IndexChunkRow.repo_id == repo_id)))
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_upsert_graph_edge_uniqueness_and_delete_then_reinsert(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    source = _node(symbol_name="caller")
    # Two identical candidate edges (e.g. two calls to the same target from
    # the same enclosing symbol) must not violate the edge table's own
    # uniqueness constraint — ON CONFLICT DO NOTHING absorbs the duplicate.
    edges = (
        GraphEdge(source_node=source, target_symbol_name="helper"),
        GraphEdge(source_node=source, target_symbol_name="helper"),
        GraphEdge(source_node=source, target_symbol_name="other"),
    )
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[],
        embeddings=[],
        nodes=[source],
        edges=edges,
    )

    async with session_factory() as session:
        node_id = (
            await session.execute(
                select(IndexGraphNodeRow.id).where(IndexGraphNodeRow.repo_id == repo_id)
            )
        ).scalar_one()
        targets = sorted(
            (
                await session.execute(
                    select(IndexGraphEdgeRow.target_symbol_name).where(
                        IndexGraphEdgeRow.source_node_id == node_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert targets == ["helper", "other"]

    # A later push where "caller"'s outgoing edges shrink to just one target
    # must remove the now-stale edge, not just add to what's already there
    # — the delete-then-reinsert step, not a plain upsert.
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/foo.ts",
        chunks=[],
        embeddings=[],
        nodes=[source],
        edges=(GraphEdge(source_node=source, target_symbol_name="other"),),
    )

    async with session_factory() as session:
        targets = sorted(
            (
                await session.execute(
                    select(IndexGraphEdgeRow.target_symbol_name).where(
                        IndexGraphEdgeRow.source_node_id == node_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert targets == ["other"]


@pytest.mark.asyncio
async def test_upsert_graph_renamed_symbol_deletes_stale_node_and_cascades_its_edges(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    # "handle" is defined in a file that stays present, with its own
    # outgoing edge.
    old_node = _node(file_path="src/a.ts", symbol_name="handle")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/a.ts",
        chunks=[],
        embeddings=[],
        nodes=[old_node],
        edges=(GraphEdge(source_node=old_node, target_symbol_name="helper"),),
    )

    # A later push renames "handle" to "process" within the *same* file —
    # delete_file is never called for a file that still exists, so the old
    # node (and its own edge) must be pruned here instead of left orphaned.
    new_node = _node(file_path="src/a.ts", symbol_name="process")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/a.ts",
        chunks=[],
        embeddings=[],
        nodes=[new_node],
        edges=(),
    )

    async with session_factory() as session:
        node_rows = (
            (
                await session.execute(
                    select(IndexGraphNodeRow).where(IndexGraphNodeRow.repo_id == repo_id)
                )
            )
            .scalars()
            .all()
        )
        edge_rows = (
            (
                await session.execute(
                    select(IndexGraphEdgeRow).join(
                        IndexGraphNodeRow, IndexGraphEdgeRow.source_node_id == IndexGraphNodeRow.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert [row.symbol_name for row in node_rows] == ["process"]
    assert edge_rows == []


@pytest.mark.asyncio
async def test_upsert_graph_with_empty_nodes_still_deletes_stale_nodes_for_file(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    # A file that still exists is edited down to zero definitions (e.g. all
    # functions/classes removed) — delete_file is never called for it, so
    # upsert_file's own graph cleanup must still prune its now-stale node (and
    # cascaded edges) even though the fresh `nodes` it's called with is
    # empty. `file_path` is passed explicitly precisely so this case has
    # somewhere to anchor the cleanup delete.
    old_node = _node(file_path="src/a.ts", symbol_name="handle")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/a.ts",
        chunks=[],
        embeddings=[],
        nodes=[old_node],
        edges=(GraphEdge(source_node=old_node, target_symbol_name="helper"),),
    )

    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/a.ts",
        chunks=[],
        embeddings=[],
        nodes=[],
        edges=(),
    )

    async with session_factory() as session:
        node_rows = (
            (
                await session.execute(
                    select(IndexGraphNodeRow).where(IndexGraphNodeRow.repo_id == repo_id)
                )
            )
            .scalars()
            .all()
        )
        edge_rows = (
            (
                await session.execute(
                    select(IndexGraphEdgeRow).join(
                        IndexGraphNodeRow, IndexGraphEdgeRow.source_node_id == IndexGraphNodeRow.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert node_rows == []
    assert edge_rows == []


@pytest.mark.asyncio
async def test_delete_file_deletes_chunks_and_nodes_cascades_source_edges_leaves_dangling_target(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    # File A defines "helper" and is called by file B's "caller" node —
    # deleting file A must prune A's own chunk/node rows and A's own
    # outgoing edges, but B's edge *targeting* A's now-deleted "helper"
    # symbol is a dangling reference left in place (US7's own text).
    helper_node = _node(file_path="src/a.ts", symbol_name="helper")
    caller_node = _node(file_path="src/b.ts", symbol_name="caller")

    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/a.ts",
        chunks=[_chunk(file_path="src/a.ts", symbol_name="helper")],
        embeddings=[_vector()],
        nodes=[helper_node],
        edges=(),
    )
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/b.ts",
        chunks=[],
        embeddings=[],
        nodes=[caller_node],
        edges=(GraphEdge(source_node=caller_node, target_symbol_name="helper"),),
    )

    await repository.delete_file(repo_id=repo_id, pr_number=0, file_path="src/a.ts")

    async with session_factory() as session:
        chunk_rows = (
            (await session.execute(select(IndexChunkRow).where(IndexChunkRow.repo_id == repo_id)))
            .scalars()
            .all()
        )
        node_rows = (
            (
                await session.execute(
                    select(IndexGraphNodeRow).where(IndexGraphNodeRow.repo_id == repo_id)
                )
            )
            .scalars()
            .all()
        )
        edge_rows = (
            (
                await session.execute(
                    select(IndexGraphEdgeRow).join(
                        IndexGraphNodeRow, IndexGraphEdgeRow.source_node_id == IndexGraphNodeRow.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert chunk_rows == []
    assert [row.file_path for row in node_rows] == ["src/b.ts"]
    # "caller"'s own edge targeting "helper" survives (dangling), since its
    # *source* node ("caller", in b.ts) was never deleted.
    assert [row.target_symbol_name for row in edge_rows] == ["helper"]


@pytest.mark.asyncio
async def test_get_last_indexed_commit_defaults_to_none_then_round_trips(
    repository: IndexingRepository, repo_id: uuid.UUID
) -> None:
    assert await repository.get_last_indexed_commit(repo_id=repo_id) is None

    await repository.set_last_indexed_commit(repo_id=repo_id, commit_sha="a" * 40)

    assert await repository.get_last_indexed_commit(repo_id=repo_id) == "a" * 40


@pytest.mark.asyncio
async def test_get_last_indexed_commit_unknown_repo_raises(repository: IndexingRepository) -> None:
    with pytest.raises(IndexingRepositoryError):
        await repository.get_last_indexed_commit(repo_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_set_last_indexed_commit_unknown_repo_raises(repository: IndexingRepository) -> None:
    with pytest.raises(IndexingRepositoryError):
        await repository.set_last_indexed_commit(repo_id=uuid.uuid4(), commit_sha="a" * 40)


@pytest.mark.asyncio
async def test_upsert_chunks_length_mismatch_raises_value_error(
    repository: IndexingRepository, repo_id: uuid.UUID
) -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        await repository.upsert_file(
            repo_id=repo_id,
            pr_number=0,
            file_path="src/foo.ts",
            chunks=[_chunk()],
            embeddings=[],
            nodes=[],
            edges=(),
        )


@pytest.mark.asyncio
async def test_upsert_chunks_file_path_mismatch_raises_value_error(
    repository: IndexingRepository, repo_id: uuid.UUID
) -> None:
    with pytest.raises(ValueError, match=re.escape("src/foo.ts")):
        await repository.upsert_file(
            repo_id=repo_id,
            pr_number=0,
            file_path="src/foo.ts",
            chunks=[_chunk(file_path="src/other.ts")],
            embeddings=[_vector()],
            nodes=[],
            edges=(),
        )


@pytest.mark.asyncio
async def test_upsert_chunks_raises_indexing_repository_error_on_real_postgres_failure(
    broken_repository: IndexingRepository,
) -> None:
    with pytest.raises(IndexingRepositoryError):
        await broken_repository.upsert_file(
            repo_id=uuid.uuid4(),
            pr_number=0,
            file_path="src/foo.ts",
            chunks=[_chunk()],
            embeddings=[_vector()],
            nodes=[],
            edges=(),
        )


@pytest.mark.asyncio
async def test_upsert_graph_raises_indexing_repository_error_on_real_postgres_failure(
    broken_repository: IndexingRepository,
) -> None:
    node = _node()
    with pytest.raises(IndexingRepositoryError):
        await broken_repository.upsert_file(
            repo_id=uuid.uuid4(),
            pr_number=0,
            file_path="src/foo.ts",
            chunks=[],
            embeddings=[],
            nodes=[node],
            edges=(),
        )


@pytest.mark.asyncio
async def test_delete_file_raises_indexing_repository_error_on_real_postgres_failure(
    broken_repository: IndexingRepository,
) -> None:
    with pytest.raises(IndexingRepositoryError):
        await broken_repository.delete_file(
            repo_id=uuid.uuid4(), pr_number=0, file_path="src/foo.ts"
        )


@pytest.mark.asyncio
async def test_upsert_chunks_fk_violation_on_unknown_repo_raises_indexing_repository_error(
    repository: IndexingRepository,
) -> None:
    """A dedicated real constraint-violation case (as opposed to
    `broken_repository`'s connection-failure case above) — an unknown
    `repo_id` violates `index_chunk`'s own FK, a genuine Postgres-level
    failure through the real schema, not a simulated one.
    """
    with pytest.raises(IndexingRepositoryError):
        await repository.upsert_file(
            repo_id=uuid.uuid4(),
            pr_number=0,
            file_path="src/foo.ts",
            chunks=[_chunk()],
            embeddings=[_vector()],
            nodes=[],
            edges=(),
        )


@pytest.mark.asyncio
async def test_upsert_file_rolls_back_chunk_write_when_graph_write_fails(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Proves `upsert_file` writes chunks and graph data in one transaction,
    never a partial commit of one without the other — the real bug this
    method's merge of the former separate `upsert_chunks`/`upsert_graph`
    calls fixes. `edges` here references a `source_node` symbol absent from
    `nodes`, so the graph half fails (a genuine caller bug, left uncaught
    per this module's own docstring) only *after* the chunk statement has
    already been queued against the same session — if the two writes were
    still independently committed, the chunk row below would survive.
    """
    ghost_source = _node(symbol_name="ghost")
    with pytest.raises(KeyError):
        await repository.upsert_file(
            repo_id=repo_id,
            pr_number=0,
            file_path="src/foo.ts",
            chunks=[_chunk()],
            embeddings=[_vector()],
            nodes=[],
            edges=(GraphEdge(source_node=ghost_source, target_symbol_name="whatever"),),
        )

    async with session_factory() as session:
        rows = (
            (await session.execute(select(IndexChunkRow).where(IndexChunkRow.repo_id == repo_id)))
            .scalars()
            .all()
        )
    assert rows == []
