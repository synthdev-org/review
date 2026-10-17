"""Integration tests: the clone-fingerprint half of
`adapter.storage.indexing_repository.IndexingRepository` against real
Postgres/pgvector (via `testcontainers-python`), per
`docs/plans/plan-phase-10.md` Phase 2.

Covers US-C1 (clone rows written in the file's own transaction) and US-C2
(modify replaces, delete prunes, rename moves, `clone_index=None` clears), the
`pr_number` scoping that keeps a PR-scoped write or delete away from the
canonical `pr_number = 0` rows, `delete_file`'s removed-fingerprint count,
all-or-nothing rollback across chunk, graph and clone rows, and batched
inserts of a very large fingerprint set.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import (
    IndexChunkRow,
    IndexCloneFileRow,
    IndexCloneFingerprintRow,
    IndexGraphEdgeRow,
    IndexGraphNodeRow,
    Org,
    Repo,
)
from indexing.chunk import Chunk
from indexing.clone import CloneFileIndex, Fingerprint
from indexing.embedding import EmbeddingVector
from indexing.graph import GraphEdge, GraphNode
from indexing.repository import IndexingRepositoryError

pytestmark = pytest.mark.integration

_DIM = 384


def _vector() -> EmbeddingVector:
    return EmbeddingVector(values=tuple(0.0 for _ in range(_DIM)), dimension=_DIM)


def _chunk(file_path: str, content: str = "function foo() {}") -> Chunk:
    return Chunk(
        file_path=file_path,
        language="typescript",
        symbol_name="foo",
        byte_range=(0, len(content)),
        line_range=None,
        content=content,
        chunk_index=0,
    )


def _clone(
    file_path: str, hashes: tuple[int, ...] = (1, 2, 3), n_fingerprints: int = 2
) -> CloneFileIndex:
    # 2**40 exceeds int32: fingerprint hashes are bigint, token hashes int32-sized.
    return CloneFileIndex(
        file_path=file_path,
        language="typescript",
        token_hashes=hashes,
        line_first_token=(2, 3),
        fingerprints=tuple(
            Fingerprint(hash=2**40 + i, token_offset=i) for i in range(n_fingerprints)
        ),
    )


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repository(session_factory: async_sessionmaker[Any]) -> IndexingRepository:
    return IndexingRepository(session_factory)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _write(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    path: str,
    clone_index: CloneFileIndex | None,
    *,
    pr_number: int = 0,
    content: str = "function foo() {}",
    graph: tuple[str, str] | None = None,
) -> None:
    # graph = (defined symbol, called symbol): one node plus one call edge.
    nodes = (
        [GraphNode(file_path=path, symbol_name=graph[0], language="typescript")] if graph else []
    )
    edges = [GraphEdge(source_node=nodes[0], target_symbol_name=graph[1])] if graph else []
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=pr_number,
        file_path=path,
        chunks=[_chunk(path, content)],
        embeddings=[_vector()],
        nodes=nodes,
        edges=edges,
        clone_index=clone_index,
    )


async def _fingerprints(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> list[tuple[int, str, int, int]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(
                IndexCloneFingerprintRow.pr_number,
                IndexCloneFingerprintRow.file_path,
                IndexCloneFingerprintRow.token_offset,
                IndexCloneFingerprintRow.hash,
            )
            .where(IndexCloneFingerprintRow.repo_id == repo_id)
            .order_by(
                IndexCloneFingerprintRow.pr_number,
                IndexCloneFingerprintRow.file_path,
                IndexCloneFingerprintRow.token_offset,
            )
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]


async def _file_rows(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> list[IndexCloneFileRow]:
    async with session_factory() as session:
        result = await session.execute(
            select(IndexCloneFileRow)
            .where(IndexCloneFileRow.repo_id == repo_id)
            .order_by(IndexCloneFileRow.pr_number, IndexCloneFileRow.file_path)
        )
        return list(result.scalars())


@pytest.mark.asyncio
async def test_upsert_writes_clone_file_and_fingerprint_rows(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts"))

    files = await _file_rows(session_factory, repo_id)
    assert [(f.file_path, f.language, f.token_hashes, f.line_first_token) for f in files] == [
        ("src/a.ts", "typescript", [1, 2, 3], [2, 3])
    ]
    # Full 40-bit hashes survive the bigint column round trip.
    assert await _fingerprints(session_factory, repo_id) == [
        (0, "src/a.ts", 0, 2**40),
        (0, "src/a.ts", 1, 2**40 + 1),
    ]


@pytest.mark.asyncio
async def test_modify_replaces_prior_clone_rows(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts", n_fingerprints=3))
    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts", (9, 8), n_fingerprints=1))

    files = await _file_rows(session_factory, repo_id)
    assert [f.token_hashes for f in files] == [[9, 8]]
    assert len(await _fingerprints(session_factory, repo_id)) == 1


@pytest.mark.asyncio
async def test_clone_index_none_clears_existing_clone_rows_but_keeps_chunks(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts"))
    await _write(repository, repo_id, "src/a.ts", None, content="changed")

    assert await _file_rows(session_factory, repo_id) == []
    assert await _fingerprints(session_factory, repo_id) == []
    async with session_factory() as session:
        contents = (
            (
                await session.execute(
                    select(IndexChunkRow.content).where(IndexChunkRow.repo_id == repo_id)
                )
            )
            .scalars()
            .all()
        )
    assert list(contents) == ["changed"]


@pytest.mark.asyncio
async def test_clone_index_for_another_file_is_rejected_and_writes_nothing(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    with pytest.raises(ValueError, match="clone_index must belong to"):
        await _write(repository, repo_id, "src/a.ts", _clone("src/other.ts"))

    assert await _file_rows(session_factory, repo_id) == []
    assert await _fingerprints(session_factory, repo_id) == []


@pytest.mark.asyncio
async def test_delete_file_prunes_clone_rows_and_returns_fingerprint_count(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts", n_fingerprints=3))
    await _write(repository, repo_id, "src/b.ts", _clone("src/b.ts", n_fingerprints=2))

    removed = await repository.delete_file(repo_id=repo_id, pr_number=0, file_path="src/a.ts")

    assert removed == 3
    assert [f.file_path for f in await _file_rows(session_factory, repo_id)] == ["src/b.ts"]
    assert {fp[1] for fp in await _fingerprints(session_factory, repo_id)} == {"src/b.ts"}
    # Idempotent: a file with no clone rows reports zero.
    assert await repository.delete_file(repo_id=repo_id, pr_number=0, file_path="src/a.ts") == 0


@pytest.mark.asyncio
async def test_rename_moves_clone_rows_to_the_new_path(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(repository, repo_id, "src/old.ts", _clone("src/old.ts"))

    # A rename reaches the indexer as: new path added, old path removed.
    await _write(repository, repo_id, "src/new.ts", _clone("src/new.ts"))
    await repository.delete_file(repo_id=repo_id, pr_number=0, file_path="src/old.ts")

    assert [f.file_path for f in await _file_rows(session_factory, repo_id)] == ["src/new.ts"]
    assert {fp[1] for fp in await _fingerprints(session_factory, repo_id)} == {"src/new.ts"}


@pytest.mark.asyncio
async def test_pr_scoped_upsert_and_delete_never_touch_canonical_clone_rows(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts", n_fingerprints=2))

    await _write(repository, repo_id, "src/a.ts", _clone("src/a.ts", (7,), 1), pr_number=5)
    await _write(repository, repo_id, "src/a.ts", None, pr_number=6)
    removed = await repository.delete_file(repo_id=repo_id, pr_number=5, file_path="src/a.ts")

    assert removed == 1
    canonical = [fp for fp in await _fingerprints(session_factory, repo_id) if fp[0] == 0]
    assert len(canonical) == 2
    files = await _file_rows(session_factory, repo_id)
    assert [(f.pr_number, f.token_hashes) for f in files] == [(0, [1, 2, 3])]


@pytest.mark.asyncio
async def test_mid_write_failure_rolls_back_chunks_graph_and_clone_rows_together(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    await _write(
        repository, repo_id, "src/a.ts", _clone("src/a.ts"), content="v1", graph=("foo", "bar")
    )
    # A hash beyond bigint fails inside the clone insert, after the chunk write
    # and the clone delete have already been issued in the same transaction.
    bad = _clone("src/a.ts").model_copy(
        update={"fingerprints": (Fingerprint(hash=2**70, token_offset=0),)}
    )

    with pytest.raises(IndexingRepositoryError):
        await _write(repository, repo_id, "src/a.ts", bad, content="v2", graph=("baz", "qux"))

    async with session_factory() as session:
        contents = (
            (
                await session.execute(
                    select(IndexChunkRow.content).where(IndexChunkRow.repo_id == repo_id)
                )
            )
            .scalars()
            .all()
        )
    assert list(contents) == ["v1"]
    assert [f.token_hashes for f in await _file_rows(session_factory, repo_id)] == [[1, 2, 3]]
    assert len(await _fingerprints(session_factory, repo_id)) == 2
    async with session_factory() as session:
        symbols = (await session.execute(select(IndexGraphNodeRow.symbol_name))).scalars().all()
        targets = (
            (await session.execute(select(IndexGraphEdgeRow.target_symbol_name))).scalars().all()
        )
    assert list(symbols) == ["foo"]
    assert list(targets) == ["bar"]


@pytest.mark.asyncio
async def test_large_fingerprint_set_inserts_in_batches(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    # 12,000 rows x 5 columns is 60,000 bind parameters, over asyncpg's 32,767 cap
    # if sent as one statement.
    await _write(repository, repo_id, "gen.ts", _clone("gen.ts", n_fingerprints=12_000))

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(IndexCloneFingerprintRow)
            .where(IndexCloneFingerprintRow.repo_id == repo_id)
        )
    assert count == 12_000
