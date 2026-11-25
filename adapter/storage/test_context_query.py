"""Integration tests: `adapter.storage.context_query.PostgresGraphQuery` and
`PostgresSimilarityQuery` against real Postgres/pgvector (via
`testcontainers-python`, per `docs/rules/testing.md`'s mandatory
DB-integration rule), per the plan's Phase 2 (Unit B) and Phase 3 (Unit C)
Tests sections.

`PostgresGraphQuery` coverage: US2 (blast-radius edges), US3 (caller content
resolution), US9 (per-symbol truncation), US10 (pre-change-name lookup is
just an ordinary `find_callers` call — this module doesn't do rename
resolution itself, only the query), the row-value-matching fix's own
correctness (a fixture whose two callers' `(file_path, symbol_name)` values
overlap across columns but not as a row), all four isolation dimensions
(`repo_id` x2 stages, `pr_number` x2 stages), and `ContextQueryError`
wrapping with the correct `operation` per stage.

`PostgresSimilarityQuery` coverage (US4): a real fixture chunk, embedded
with the real local embedding model (`adapter.embedding.local_onnx`, no
network at inference time — per `docs/rules/testing.md`'s adapter
convention), finds a real similar chunk elsewhere in the repo; the querying
chunk's own file is excluded; `repo_id` isolation (a real
cross-tenant-exfiltration boundary the Phase 3 plan section itself flagged
as previously untested); `pr_number` isolation; and `ContextQueryError`
wrapping with `operation="find_similar"`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import Org, Repo
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingVector
from indexing.graph import GraphEdge, GraphNode
from review.context_builder import ContextQueryError

pytestmark = pytest.mark.integration

_EMBEDDING_DIMENSION = 384

# Same real, checksum-verified, no-network bundled model path
# `adapter/embedding/test_local_onnx.py` already uses — see that module's
# own docstring for provenance. Real inference, never a fake/mocked vector,
# per `docs/rules/testing.md`'s adapter convention: US4's own Given/When/Then
# is about a *real* embedding finding a *real* similar chunk, not a
# hand-picked vector that begs the question.
_REAL_MODEL_PATH = "/app/models/all-MiniLM-L6-v2"


def _vector(fill: float = 0.0) -> EmbeddingVector:
    values = tuple(float(fill) for _ in range(_EMBEDDING_DIMENSION))
    return EmbeddingVector(values=values, dimension=_EMBEDDING_DIMENSION)


def _chunk(
    *,
    file_path: str,
    symbol_name: str,
    content: str,
    chunk_index: int = 0,
    line_range: tuple[int, int] | None = (1, 1),
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


def _node(*, file_path: str, symbol_name: str) -> GraphNode:
    return GraphNode(file_path=file_path, symbol_name=symbol_name, language="typescript")


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repository(session_factory: async_sessionmaker[Any]) -> IndexingRepository:
    """The write path (`IndexRepository`) used only to seed fixture graph
    data — this test module's actual subject is the read path
    (`PostgresGraphQuery`), exercised separately below.
    """
    return IndexingRepository(session_factory)


@pytest_asyncio.fixture
async def query(session_factory: async_sessionmaker[Any]) -> PostgresGraphQuery:
    return PostgresGraphQuery(session_factory)


@pytest_asyncio.fixture
async def similarity_query(session_factory: async_sessionmaker[Any]) -> PostgresSimilarityQuery:
    return PostgresSimilarityQuery(session_factory)


@pytest.fixture(scope="module")
def embedding_client() -> LocalONNXEmbeddingClient:
    """The real, no-network, bundled local embedding model — module-scoped
    since loading it is real (if fast) inference-engine setup, not
    per-test-cheap state, and it's read-only from every test's point of
    view."""
    return LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)


async def _make_repo(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    return await _make_repo(session_factory)


async def _write_caller(
    repository: IndexingRepository,
    *,
    repo_id: uuid.UUID,
    pr_number: int = 0,
    caller_file: str,
    caller_symbol: str,
    caller_content: str,
    target_symbol: str,
    line_range: tuple[int, int] | None = (1, 1),
) -> None:
    """Writes one caller: a file containing one function (`caller_symbol`)
    whose one outgoing edge targets `target_symbol` — the fixture shape
    `find_callers` resolves back to a `CallerHit`.
    """
    node = _node(file_path=caller_file, symbol_name=caller_symbol)
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=pr_number,
        file_path=caller_file,
        chunks=[
            _chunk(
                file_path=caller_file,
                symbol_name=caller_symbol,
                content=caller_content,
                line_range=line_range,
            )
        ],
        embeddings=[_vector()],
        nodes=[node],
        edges=[GraphEdge(source_node=node, target_symbol_name=target_symbol)],
    )


async def _write_chunk(
    repository: IndexingRepository,
    *,
    repo_id: uuid.UUID,
    pr_number: int = 0,
    file_path: str,
    content: str,
    embedding: EmbeddingVector,
    symbol_name: str = "fn",
) -> None:
    """Writes a single bare chunk (no graph nodes/edges) — the fixture shape
    `PostgresSimilarityQuery.find_similar`'s tests below resolve back to a
    `Chunk`. Unlike `_write_caller`, this never touches
    `index_graph_node`/`index_graph_edge` at all.
    """
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=pr_number,
        file_path=file_path,
        chunks=[_chunk(file_path=file_path, symbol_name=symbol_name, content=content)],
        embeddings=[embedding],
        nodes=[],
        edges=[],
    )


@pytest.mark.asyncio
async def test_find_callers_returns_real_edge_with_content(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    await _write_caller(
        repository,
        repo_id=repo_id,
        caller_file="src/bar.ts",
        caller_symbol="bar",
        caller_content="function bar() { foo(); }",
        target_symbol="foo",
        line_range=(10, 12),
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["foo"], limit_per_symbol=10
    )

    assert set(result) == {"foo"}
    hits = result["foo"]
    assert len(hits) == 1
    assert hits[0].file_path == "src/bar.ts"
    assert hits[0].symbol_name == "bar"
    assert hits[0].content == "function bar() { foo(); }"
    assert hits[0].line_range == (10, 12)


@pytest.mark.asyncio
async def test_find_callers_zero_edges_returns_empty_tuple(
    query: PostgresGraphQuery,
    repo_id: uuid.UUID,
) -> None:
    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["never_called"], limit_per_symbol=10
    )

    assert result == {"never_called": ()}


@pytest.mark.asyncio
async def test_find_callers_pre_change_name_returns_real_existing_callers(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """US10: this module never resolves a rename itself — it just proves
    that a query issued under a symbol's *pre-change* name (as it appears
    in the default-branch graph, e.g. before this PR renamed it to
    something else) returns that name's real existing callers exactly as
    any other symbol name would.
    """
    await _write_caller(
        repository,
        repo_id=repo_id,
        caller_file="src/caller.ts",
        caller_symbol="caller",
        caller_content="function caller() { oldName(); }",
        target_symbol="oldName",
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["oldName"], limit_per_symbol=10
    )

    hits = result["oldName"]
    assert len(hits) == 1
    assert hits[0].file_path == "src/caller.ts"
    assert hits[0].symbol_name == "caller"


@pytest.mark.asyncio
async def test_find_callers_row_value_matching_not_cross_product(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """The test that actually distinguishes the row-value `= ANY(:pairs)`
    fix from the independent-`IN`-lists cross-product bug: two real
    callers, `(fileX.ts, symbolY)` and `(fileY.ts, symbolX)`, whose
    `file_path`/`symbol_name` values overlap across columns but never as
    an actual row — plus two decoy chunks at the *other* two combinations
    (`(fileX.ts, symbolX)`, `(fileY.ts, symbolY)`) that are not callers of
    anything and must never be attributed to either real caller.
    """
    node_x = _node(file_path="src/fileX.ts", symbol_name="symbolY")
    # A decoy chunk sharing fileX.ts's own file_path with symbolX's own
    # symbol_name — not a caller of anything, deliberately given a
    # never-expected content string so a cross-product leak is obvious.
    node_x2 = _node(file_path="src/fileX.ts", symbol_name="symbolX")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/fileX.ts",
        chunks=[
            _chunk(
                file_path="src/fileX.ts",
                symbol_name="symbolY",
                content="real caller A content",
                chunk_index=0,
            ),
            _chunk(
                file_path="src/fileX.ts",
                symbol_name="symbolX",
                content="DECOY should never surface",
                chunk_index=1,
            ),
        ],
        embeddings=[_vector(), _vector()],
        nodes=[node_x, node_x2],
        edges=[GraphEdge(source_node=node_x, target_symbol_name="targetA")],
    )

    node_y = _node(file_path="src/fileY.ts", symbol_name="symbolX")
    node_y2 = _node(file_path="src/fileY.ts", symbol_name="symbolY")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/fileY.ts",
        chunks=[
            _chunk(
                file_path="src/fileY.ts",
                symbol_name="symbolX",
                content="real caller B content",
                chunk_index=0,
            ),
            # Decoy sharing fileY.ts's file_path with symbolY's symbol_name.
            _chunk(
                file_path="src/fileY.ts",
                symbol_name="symbolY",
                content="DECOY should never surface",
                chunk_index=1,
            ),
        ],
        embeddings=[_vector(), _vector()],
        nodes=[node_y, node_y2],
        edges=[GraphEdge(source_node=node_y, target_symbol_name="targetB")],
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["targetA", "targetB"], limit_per_symbol=10
    )

    assert len(result["targetA"]) == 1
    assert result["targetA"][0].file_path == "src/fileX.ts"
    assert result["targetA"][0].symbol_name == "symbolY"
    assert result["targetA"][0].content == "real caller A content"

    assert len(result["targetB"]) == 1
    assert result["targetB"][0].file_path == "src/fileY.ts"
    assert result["targetB"][0].symbol_name == "symbolX"
    assert result["targetB"][0].content == "real caller B content"


@pytest.mark.asyncio
async def test_find_callers_truncates_deterministically_to_limit(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """Three callers of the same symbol, `limit_per_symbol=2` — asserts
    the *exact* surviving row identity (ordered by `(file_path,
    symbol_name)`), not just that some truncation happened.
    """
    for file_path, symbol_name in (
        ("src/a.ts", "callerA"),
        ("src/b.ts", "callerB"),
        ("src/c.ts", "callerC"),
    ):
        await _write_caller(
            repository,
            repo_id=repo_id,
            caller_file=file_path,
            caller_symbol=symbol_name,
            caller_content=f"content for {symbol_name}",
            target_symbol="hot",
        )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["hot"], limit_per_symbol=2
    )

    hits = result["hot"]
    assert len(hits) == 2
    assert [(hit.file_path, hit.symbol_name) for hit in hits] == [
        ("src/a.ts", "callerA"),
        ("src/b.ts", "callerB"),
    ]


@pytest.mark.asyncio
async def test_find_callers_deduplicates_a_repeated_symbol_name_argument(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """`find_callers` itself, not just its one real caller
    (`ContextBuilderStep._fetch_caller_hits`), must honor the
    `GraphQuery` Protocol's own documented contract of "exactly one dict
    entry per name in `symbol_names` (including duplicates collapsed to
    one key)" — passing `["hot", "hot"]` must not run `"hot"`'s own
    LATERAL subquery twice and double the resulting `CallerHit` entries."""
    await _write_caller(
        repository,
        repo_id=repo_id,
        caller_file="src/a.ts",
        caller_symbol="callerA",
        caller_content="content for callerA",
        target_symbol="hot",
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["hot", "hot"], limit_per_symbol=10
    )

    assert list(result.keys()) == ["hot"]
    assert len(result["hot"]) == 1


@pytest.mark.asyncio
async def test_find_callers_isolates_by_repo_id_at_edge_stage(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """Two repos share the identical `target_symbol_name` — stage 1's
    `LATERAL` join's own `node.repo_id` condition must exclude the other
    repo's caller entirely, never surface it as if it belonged to the
    requesting repo.
    """
    other_repo_id = await _make_repo(session_factory)

    await _write_caller(
        repository,
        repo_id=repo_id,
        caller_file="src/mine.ts",
        caller_symbol="mineCaller",
        caller_content="my repo's caller",
        target_symbol="shared",
    )
    await _write_caller(
        repository,
        repo_id=other_repo_id,
        caller_file="src/theirs.ts",
        caller_symbol="theirCaller",
        caller_content="their repo's caller",
        target_symbol="shared",
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["shared"], limit_per_symbol=10
    )

    hits = result["shared"]
    assert len(hits) == 1
    assert hits[0].file_path == "src/mine.ts"


@pytest.mark.asyncio
async def test_find_callers_isolates_by_repo_id_at_content_stage(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """Two repos share the identical `(file_path, symbol_name)` pair in
    `index_chunk` — stage 2's plain `WHERE repo_id = ...` condition must
    resolve the requesting repo's own content, never the other repo's,
    even though the row-value pair alone can't tell them apart.
    """
    other_repo_id = await _make_repo(session_factory)

    await _write_caller(
        repository,
        repo_id=repo_id,
        caller_file="src/shared_name.ts",
        caller_symbol="sharedCaller",
        caller_content="MY repo's real content",
        target_symbol="target",
    )
    # Same (file_path, symbol_name) pair in a completely different repo,
    # with deliberately distinguishable content.
    other_node = _node(file_path="src/shared_name.ts", symbol_name="sharedCaller")
    await repository.upsert_file(
        repo_id=other_repo_id,
        pr_number=0,
        file_path="src/shared_name.ts",
        chunks=[
            _chunk(
                file_path="src/shared_name.ts",
                symbol_name="sharedCaller",
                content="OTHER repo's content -- must never surface",
            )
        ],
        embeddings=[_vector()],
        nodes=[other_node],
        edges=[GraphEdge(source_node=other_node, target_symbol_name="target")],
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["target"], limit_per_symbol=10
    )

    hits = result["target"]
    assert len(hits) == 1
    assert hits[0].content == "MY repo's real content"


@pytest.mark.asyncio
async def test_find_callers_isolates_by_pr_number_at_edge_stage(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """A colliding `target_symbol_name` where the *caller node itself*
    exists only at `pr_number > 0` — proves stage 1's `LATERAL` join's own
    `node.pr_number = 0` condition excludes it, never surfacing a
    PR-branch (potentially fork-authored) caller as if it were canonical.
    """
    await _write_caller(
        repository,
        repo_id=repo_id,
        pr_number=7,
        caller_file="src/fork_caller.ts",
        caller_symbol="forkCaller",
        caller_content="fork-authored caller, must never surface",
        target_symbol="target",
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["target"], limit_per_symbol=10
    )

    assert result == {"target": ()}


@pytest.mark.asyncio
async def test_find_callers_isolates_by_pr_number_at_content_stage(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """A colliding `(file_path, symbol_name)` pair in `index_chunk`
    present at both `pr_number = 0` and `pr_number > 0` — proves stage 2's
    row never surfaces the PR-scoped (potentially fork-authored) content,
    even though the real edge (at `pr_number = 0`) resolves the pair
    correctly.
    """
    await _write_caller(
        repository,
        repo_id=repo_id,
        pr_number=0,
        caller_file="src/caller.ts",
        caller_symbol="caller",
        caller_content="canonical content",
        target_symbol="target",
    )
    # A PR-branch row for the exact same (file_path, symbol_name) pair,
    # with no graph node/edge of its own at pr_number=0 needed — this
    # write only needs to land a colliding index_chunk row.
    pr_node = _node(file_path="src/caller.ts", symbol_name="caller")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=3,
        file_path="src/caller.ts",
        chunks=[
            _chunk(
                file_path="src/caller.ts",
                symbol_name="caller",
                content="PR-branch content -- must never surface",
            )
        ],
        embeddings=[_vector()],
        nodes=[pr_node],
        edges=[GraphEdge(source_node=pr_node, target_symbol_name="target")],
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["target"], limit_per_symbol=10
    )

    hits = result["target"]
    assert len(hits) == 1
    assert hits[0].content == "canonical content"


@pytest.mark.asyncio
async def test_find_callers_content_resolves_split_symbol_deterministically(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """A caller function split across multiple `_pack()` windows
    (`indexing/chunk.py`) produces more than one `index_chunk` row sharing
    the same `(file_path, symbol_name)` pair, distinguished only by
    `chunk_index` — stage 2's content resolution must deterministically
    resolve the lowest `chunk_index` (the window containing the symbol's
    own declaration), never whichever row Postgres happens to return last.
    The higher-`chunk_index` row is written first in the `chunks` list to
    bias against accidentally passing due to lucky insertion/scan order.
    """
    node = _node(file_path="src/big.ts", symbol_name="bigCaller")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path="src/big.ts",
        chunks=[
            _chunk(
                file_path="src/big.ts",
                symbol_name="bigCaller",
                content="second window content -- must never surface",
                chunk_index=1,
                line_range=(50, 90),
            ),
            _chunk(
                file_path="src/big.ts",
                symbol_name="bigCaller",
                content="first window content -- the real declaration",
                chunk_index=0,
                line_range=(1, 49),
            ),
        ],
        embeddings=[_vector(), _vector()],
        nodes=[node],
        edges=[GraphEdge(source_node=node, target_symbol_name="target")],
    )

    result = await query.find_callers(
        repo_id=str(repo_id), symbol_names=["target"], limit_per_symbol=10
    )

    hits = result["target"]
    assert len(hits) == 1
    assert hits[0].content == "first window content -- the real declaration"
    assert hits[0].line_range == (1, 49)


@pytest_asyncio.fixture
async def broken_query() -> AsyncIterator[PostgresGraphQuery]:
    """A `PostgresGraphQuery` pointed at an unreachable Postgres port — a
    real connection failure through the real driver/SQLAlchemy code path
    (never a mock), for proving stage 1's Postgres-level-failure ->
    `ContextQueryError` translation.
    """
    engine = create_storage_engine("postgresql+asyncpg://user:pass@127.0.0.1:1/nonexistent")
    try:
        yield PostgresGraphQuery(create_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_find_callers_wraps_edge_stage_failure(broken_query: PostgresGraphQuery) -> None:
    with pytest.raises(ContextQueryError) as exc_info:
        await broken_query.find_callers(
            repo_id=str(uuid.uuid4()), symbol_names=["anything"], limit_per_symbol=10
        )

    assert exc_info.value.operation == "find_callers_edges"


@pytest.mark.asyncio
async def test_find_callers_malformed_repo_id_raises_context_query_error(
    query: PostgresGraphQuery,
) -> None:
    """A malformed `repo_id` (never a real possibility in production — the
    value is always a validated UUID string by the time it reaches this
    adapter — but a latent gap worth closing) must degrade the same way a
    real Postgres failure does, not raise a bare `ValueError` that escapes
    `ContextBuilderStep.run()`'s typed catch tuple and fails the job
    fatally instead of gracefully. Never opens a session at all (the
    `UUID(...)` parse fails first), so a real, working `query` fixture
    (not `broken_query`) proves this doesn't depend on the DB being
    unreachable."""
    with pytest.raises(ContextQueryError) as exc_info:
        await query.find_callers(
            repo_id="not-a-real-uuid", symbol_names=["anything"], limit_per_symbol=10
        )

    assert exc_info.value.operation == "find_callers_edges"


@pytest.mark.asyncio
async def test_find_callers_wraps_content_stage_failure(
    query: PostgresGraphQuery,
    repository: IndexingRepository,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """Stage 1 succeeds (a real edge resolves), but `index_chunk` is
    temporarily renamed out of the way before stage 2 runs — a real
    Postgres-level failure specific to the content-resolution query,
    proving `ContextQueryError` carries `operation="find_callers_content"`,
    not `"find_callers_edges"`. Renamed back in a `finally`, not dropped
    outright, so this test doesn't corrupt the schema `migrated_engine`'s
    own teardown (`alembic downgrade base`) still needs to run cleanly.
    """
    await _write_caller(
        repository,
        repo_id=repo_id,
        caller_file="src/bar.ts",
        caller_symbol="bar",
        caller_content="function bar() { foo(); }",
        target_symbol="foo",
    )

    async with session_factory() as session, session.begin():
        await session.execute(text("ALTER TABLE index_chunk RENAME TO index_chunk_hidden_for_test"))

    try:
        with pytest.raises(ContextQueryError) as exc_info:
            await query.find_callers(
                repo_id=str(repo_id), symbol_names=["foo"], limit_per_symbol=10
            )

        assert exc_info.value.operation == "find_callers_content"
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("ALTER TABLE index_chunk_hidden_for_test RENAME TO index_chunk")
            )


# --- PostgresSimilarityQuery (Phase 3, Unit C) ---


@pytest.mark.asyncio
async def test_find_similar_finds_real_similar_chunk_in_another_file(
    similarity_query: PostgresSimilarityQuery,
    repository: IndexingRepository,
    embedding_client: LocalONNXEmbeddingClient,
    repo_id: uuid.UUID,
) -> None:
    """US4's own Given/When/Then: a changed chunk's post-change content,
    embedded with the real local model, finds a real similar chunk
    elsewhere in the repo — not a hand-picked vector that begs the
    question. `similar_content` is a near-paraphrase of `query_content`
    (same shape, different identifier names); `dissimilar_content` is
    unrelated Go code, included so the assertion actually distinguishes a
    real nearest-neighbor ranking from a "returns everything" stub.
    """
    query_content = "function add(a: number, b: number): number { return a + b; }"
    similar_content = "function sum(x: number, y: number): number { return x + y; }"
    dissimilar_content = 'package main\n\nfunc main() {\n\tfmt.Println("hello world")\n}'

    (query_vector, similar_vector, dissimilar_vector) = embedding_client.embed(
        (query_content, similar_content, dissimilar_content)
    )

    await _write_chunk(
        repository,
        repo_id=repo_id,
        file_path="src/similar.ts",
        content=similar_content,
        embedding=similar_vector,
    )
    await _write_chunk(
        repository,
        repo_id=repo_id,
        file_path="src/dissimilar.go",
        content=dissimilar_content,
        embedding=dissimilar_vector,
    )

    result = await similarity_query.find_similar(
        repo_id=str(repo_id),
        embedding=query_vector,
        exclude_file_path="src/query.ts",
        limit=1,
    )

    assert len(result) == 1
    assert result[0].file_path == "src/similar.ts"
    assert result[0].content == similar_content


@pytest.mark.asyncio
async def test_find_similar_excludes_the_querying_chunks_own_file(
    similarity_query: PostgresSimilarityQuery,
    repository: IndexingRepository,
    embedding_client: LocalONNXEmbeddingClient,
    repo_id: uuid.UUID,
) -> None:
    """The nearest possible match — the querying chunk's own, identical
    content, in its own file — must never be returned; `exclude_file_path`
    is the querying chunk's own file, per US4's "excluding the chunk's own
    file" requirement.
    """
    content = "function add(a: number, b: number): number { return a + b; }"
    (vector,) = embedding_client.embed((content,))

    await _write_chunk(
        repository,
        repo_id=repo_id,
        file_path="src/query.ts",
        content=content,
        embedding=vector,
    )

    result = await similarity_query.find_similar(
        repo_id=str(repo_id),
        embedding=vector,
        exclude_file_path="src/query.ts",
        limit=10,
    )

    assert result == ()


@pytest.mark.asyncio
async def test_find_similar_isolates_by_repo_id(
    similarity_query: PostgresSimilarityQuery,
    repository: IndexingRepository,
    session_factory: async_sessionmaker[Any],
    embedding_client: LocalONNXEmbeddingClient,
    repo_id: uuid.UUID,
) -> None:
    """A real cross-tenant-exfiltration boundary, per the plan's Phase 3
    Tests section (flagging this dimension as previously untested): two
    repos hold matching/near-identical chunk content. Querying repo A,
    with repo A's own single chunk excluded by file path, must never
    surface repo B's chunk — even though repo B's file path doesn't match
    `exclude_file_path` and its content is maximally similar (embedded from
    the exact same text).
    """
    other_repo_id = await _make_repo(session_factory)
    content = "function isolationTarget() { return 42; }"
    (vector,) = embedding_client.embed((content,))

    await _write_chunk(
        repository,
        repo_id=repo_id,
        file_path="src/a.ts",
        content=content,
        embedding=vector,
    )
    await _write_chunk(
        repository,
        repo_id=other_repo_id,
        file_path="src/b.ts",
        content=content,
        embedding=vector,
    )

    result = await similarity_query.find_similar(
        repo_id=str(repo_id),
        embedding=vector,
        exclude_file_path="src/a.ts",
        limit=10,
    )

    assert result == ()


@pytest.mark.asyncio
async def test_find_similar_isolates_by_pr_number(
    similarity_query: PostgresSimilarityQuery,
    repository: IndexingRepository,
    embedding_client: LocalONNXEmbeddingClient,
    repo_id: uuid.UUID,
) -> None:
    """A colliding chunk present at both `pr_number = 0` and `pr_number >
    0` in the same repo — the same third isolation dimension Phase 2's
    tests apply to `find_callers`, applied here to `find_similar`. The
    `pr_number = 0` row is excluded by file path; the `pr_number > 0` row
    (a potentially fork-authored push) must never surface regardless of its
    own file path.
    """
    content = "function isolationTarget() { return 42; }"
    (vector,) = embedding_client.embed((content,))

    await _write_chunk(
        repository,
        repo_id=repo_id,
        pr_number=0,
        file_path="src/a.ts",
        content=content,
        embedding=vector,
    )
    await _write_chunk(
        repository,
        repo_id=repo_id,
        pr_number=7,
        file_path="src/fork.ts",
        content=content,
        embedding=vector,
    )

    result = await similarity_query.find_similar(
        repo_id=str(repo_id),
        embedding=vector,
        exclude_file_path="src/a.ts",
        limit=10,
    )

    assert result == ()


@pytest_asyncio.fixture
async def broken_similarity_query() -> AsyncIterator[PostgresSimilarityQuery]:
    """A `PostgresSimilarityQuery` pointed at an unreachable Postgres port —
    a real connection failure through the real driver/SQLAlchemy code path
    (never a mock), mirroring `broken_query` above, for proving
    `find_similar`'s own Postgres-level-failure -> `ContextQueryError`
    translation.
    """
    engine = create_storage_engine("postgresql+asyncpg://user:pass@127.0.0.1:1/nonexistent")
    try:
        yield PostgresSimilarityQuery(create_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_find_similar_wraps_failure_as_context_query_error(
    broken_similarity_query: PostgresSimilarityQuery,
) -> None:
    with pytest.raises(ContextQueryError) as exc_info:
        await broken_similarity_query.find_similar(
            repo_id=str(uuid.uuid4()),
            embedding=_vector(),
            exclude_file_path="src/anything.ts",
            limit=10,
        )

    assert exc_info.value.operation == "find_similar"


@pytest.mark.asyncio
async def test_find_similar_malformed_repo_id_raises_context_query_error(
    similarity_query: PostgresSimilarityQuery,
) -> None:
    """See `test_find_callers_malformed_repo_id_raises_context_query_error`'s
    own docstring for the full reasoning — same fix, same rationale,
    applied to `find_similar`'s own `repo_id` parse."""
    with pytest.raises(ContextQueryError) as exc_info:
        await similarity_query.find_similar(
            repo_id="not-a-real-uuid",
            embedding=_vector(),
            exclude_file_path="src/anything.ts",
            limit=10,
        )

    assert exc_info.value.operation == "find_similar"
