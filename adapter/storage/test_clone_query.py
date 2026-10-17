"""Integration tests: `adapter.storage.clone_query.PostgresCloneIndexQuery`
against real Postgres (testcontainers), per `docs/plans/plan-phase-10.md`
Phase 3. Rows are seeded straight through the ORM / SQL — the writer has its
own tests; this module's subject is the read path.
"""

from __future__ import annotations

import statistics
import time
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ARRAY, BigInteger, Integer, String, Uuid, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.clone_query import _FIND_CANDIDATES_QUERY, PostgresCloneIndexQuery
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.models import (
    IndexChunkRow,
    IndexCloneFileRow,
    IndexCloneFingerprintRow,
    Org,
    Repo,
)
from review.clone_detection import CloneQueryError

pytestmark = pytest.mark.integration

_TIMEOUT = 10.0
_MAX_SIGNED_63 = 2**63 - 1


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def query(session_factory: async_sessionmaker[Any]) -> PostgresCloneIndexQuery:
    return PostgresCloneIndexQuery(session_factory)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    return await _make_repo(session_factory)


async def _make_repo(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _seed(
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    file_path: str,
    *,
    fingerprints: dict[int, int],
    pr_number: int = 0,
    with_chunk: bool = True,
    token_hashes: tuple[int, ...] = (1, 2, 3),
) -> None:
    """One file: clone-file row, fingerprints (`{token_offset: hash}`), and
    (by default) the `index_chunk` row the reader's guard requires."""
    async with session_factory() as session, session.begin():
        session.add(
            IndexCloneFileRow(
                repo_id=repo_id,
                pr_number=pr_number,
                file_path=file_path,
                language="go",
                token_hashes=list(token_hashes),
                line_first_token=[1, 3],
            )
        )
        session.add_all(
            IndexCloneFingerprintRow(
                repo_id=repo_id,
                pr_number=pr_number,
                file_path=file_path,
                token_offset=offset,
                hash=hash_value,
            )
            for offset, hash_value in fingerprints.items()
        )
        if with_chunk:
            session.add(
                IndexChunkRow(
                    repo_id=repo_id,
                    pr_number=pr_number,
                    file_path=file_path,
                    chunk_index=0,
                    language="go",
                    symbol_name="fn",
                    byte_range_start=0,
                    byte_range_end=1,
                    content="x",
                    embedding=[0.0] * 384,
                )
            )


@pytest.mark.asyncio
async def test_candidates_found_by_hash(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _seed(session_factory, repo_id, "a.go", fingerprints={0: 11, 5: 22})
    await _seed(session_factory, repo_id, "b.go", fingerprints={3: 11, 9: 33})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[11, 22, 99],
        excluded_paths=[],
        per_hash_limit=20,
        timeout_seconds=_TIMEOUT,
    )

    assert [(h.hash, h.file_path, h.token_offset) for h in result.hits] == [
        (11, "a.go", 0),
        (11, "b.go", 3),
        (22, "a.go", 5),
    ]
    assert result.capped_hash_count == 0


@pytest.mark.asyncio
async def test_pr_scoped_and_other_repo_rows_never_returned(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    other_repo = await _make_repo(session_factory)
    await _seed(session_factory, repo_id, "pr.go", fingerprints={0: 7}, pr_number=4)
    await _seed(session_factory, other_repo, "other.go", fingerprints={0: 7})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[7],
        excluded_paths=[],
        per_hash_limit=20,
        timeout_seconds=_TIMEOUT,
    )

    assert result.hits == ()


@pytest.mark.asyncio
async def test_excluded_paths_honored(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _seed(session_factory, repo_id, "a.go", fingerprints={0: 5})
    await _seed(session_factory, repo_id, "b.go", fingerprints={0: 5})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[5],
        excluded_paths=["a.go"],
        per_hash_limit=20,
        timeout_seconds=_TIMEOUT,
    )

    assert [h.file_path for h in result.hits] == ["b.go"]


@pytest.mark.asyncio
async def test_per_hash_cap_applied_and_reported(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    for name in ("a.go", "b.go", "c.go"):
        await _seed(session_factory, repo_id, name, fingerprints={0: 5})
    await _seed(session_factory, repo_id, "d.go", fingerprints={0: 6})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[5, 6],
        excluded_paths=[],
        per_hash_limit=2,
        timeout_seconds=_TIMEOUT,
    )

    assert [(h.hash, h.file_path) for h in result.hits] == [(5, "a.go"), (5, "b.go"), (6, "d.go")]
    assert result.capped_hash_count == 1


@pytest.mark.asyncio
async def test_empty_hashes_and_empty_index_return_empty(
    query: PostgresCloneIndexQuery, repo_id: uuid.UUID
) -> None:
    empty = await query.find_candidates(
        repo_id=str(repo_id), hashes=[], excluded_paths=[], per_hash_limit=20, timeout_seconds=1
    )
    none_indexed = await query.find_candidates(
        repo_id=str(repo_id), hashes=[1, 2], excluded_paths=[], per_hash_limit=20, timeout_seconds=1
    )
    files = await query.load_files(repo_id=str(repo_id), file_paths=["x.go"], timeout_seconds=1)
    no_paths = await query.load_files(repo_id=str(repo_id), file_paths=[], timeout_seconds=1)

    assert empty.hits == () and none_indexed.hits == ()
    assert files == {} and no_paths == {}


@pytest.mark.asyncio
async def test_hashes_are_sent_in_batches(
    query: PostgresCloneIndexQuery,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("adapter.storage.clone_query.HASH_BATCH_SIZE", 2)
    await _seed(session_factory, repo_id, "a.go", fingerprints={i: i + 100 for i in range(5)})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[100, 101, 102, 103, 104, 105],
        excluded_paths=[],
        per_hash_limit=20,
        timeout_seconds=_TIMEOUT,
    )

    assert [h.hash for h in result.hits] == [100, 101, 102, 103, 104]


@pytest.mark.asyncio
async def test_top_of_signed_63_bit_range_round_trips(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _seed(session_factory, repo_id, "a.go", fingerprints={0: _MAX_SIGNED_63})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[_MAX_SIGNED_63],
        excluded_paths=[],
        per_hash_limit=20,
        timeout_seconds=_TIMEOUT,
    )
    files = await query.load_files(repo_id=str(repo_id), file_paths=["a.go"], timeout_seconds=1)

    assert [h.hash for h in result.hits] == [_MAX_SIGNED_63]
    assert files["a.go"].fingerprints[0].hash == _MAX_SIGNED_63


@pytest.mark.asyncio
async def test_file_without_chunk_row_is_never_returned(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _seed(session_factory, repo_id, "gone.go", fingerprints={0: 8}, with_chunk=False)
    await _seed(session_factory, repo_id, "kept.go", fingerprints={0: 8})

    result = await query.find_candidates(
        repo_id=str(repo_id),
        hashes=[8],
        excluded_paths=[],
        per_hash_limit=20,
        timeout_seconds=_TIMEOUT,
    )
    files = await query.load_files(
        repo_id=str(repo_id), file_paths=["gone.go", "kept.go"], timeout_seconds=_TIMEOUT
    )

    assert [h.file_path for h in result.hits] == ["kept.go"]
    assert set(files) == {"kept.go"}


@pytest.mark.asyncio
async def test_load_files_returns_stored_index_and_ignores_pr_rows(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _seed(session_factory, repo_id, "a.go", fingerprints={4: 1, 0: 2}, token_hashes=(9, 8))
    await _seed(session_factory, repo_id, "pr.go", fingerprints={0: 2}, pr_number=3)

    files = await query.load_files(
        repo_id=str(repo_id), file_paths=["a.go", "pr.go"], timeout_seconds=_TIMEOUT
    )

    assert set(files) == {"a.go"}
    loaded = files["a.go"]
    assert loaded.token_hashes == (9, 8)
    assert loaded.line_first_token == (1, 3)
    assert [(f.token_offset, f.hash) for f in loaded.fingerprints] == [(0, 2), (4, 1)]


@pytest.mark.asyncio
async def test_database_failure_becomes_clone_query_error() -> None:
    dead = create_storage_engine("postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none")
    try:
        dead_query = PostgresCloneIndexQuery(create_session_factory(dead))
        with pytest.raises(CloneQueryError) as found:
            await dead_query.find_candidates(
                repo_id=str(uuid.uuid4()),
                hashes=[1],
                excluded_paths=[],
                per_hash_limit=20,
                timeout_seconds=5,
            )
        assert found.value.operation == "find_candidates"
        with pytest.raises(CloneQueryError) as found_load:
            await dead_query.load_files(
                repo_id=str(uuid.uuid4()), file_paths=["a.go"], timeout_seconds=5
            )
        assert found_load.value.operation == "load_files"
    finally:
        await dead.dispose()


@pytest.mark.asyncio
async def test_invalid_repo_id_becomes_clone_query_error(query: PostgresCloneIndexQuery) -> None:
    with pytest.raises(CloneQueryError):
        await query.find_candidates(
            repo_id="not-a-uuid", hashes=[1], excluded_paths=[], per_hash_limit=1, timeout_seconds=1
        )


@pytest.mark.asyncio
async def test_load_files_invalid_repo_id_becomes_clone_query_error(
    query: PostgresCloneIndexQuery,
) -> None:
    with pytest.raises(CloneQueryError) as caught:
        await query.load_files(repo_id="not-a-uuid", file_paths=["a.go"], timeout_seconds=1)
    assert caught.value.operation == "load_files"


@pytest.mark.asyncio
async def test_blocked_database_times_out_as_clone_query_error(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    async with session_factory() as blocker, blocker.begin():
        await blocker.execute(text("LOCK TABLE index_clone_fingerprint IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(CloneQueryError, match="timed out"):
            await query.find_candidates(
                repo_id=str(repo_id),
                hashes=[1],
                excluded_paths=[],
                per_hash_limit=1,
                timeout_seconds=0.3,
            )


_FILES = 1_000
_FINGERPRINTS_PER_FILE = 100  # 100,000 fingerprints: the spec's latency fixture size.


async def _seed_large(session_factory: async_sessionmaker[Any], repo_id: uuid.UUID) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("SELECT setseed(0.42)"))
        await session.execute(
            text(
                """
                INSERT INTO index_chunk (id, repo_id, pr_number, file_path, chunk_index, language,
                    byte_range_start, byte_range_end, content, embedding)
                SELECT gen_random_uuid(), :repo, 0, 'f' || g || '.go', 0, 'go', 0, 1, 'x',
                    array_fill(0::real, ARRAY[384])::vector
                FROM generate_series(1, :files) g
                """
            ),
            {"repo": repo_id, "files": _FILES},
        )
        await session.execute(
            text(
                """
                INSERT INTO index_clone_fingerprint
                    (repo_id, pr_number, file_path, token_offset, hash)
                SELECT :repo, 0, 'f' || g || '.go', o, (random() * 1e12)::bigint
                FROM generate_series(1, :files) g, generate_series(0, :per_file - 1) o
                """
            ),
            {"repo": repo_id, "files": _FILES, "per_file": _FINGERPRINTS_PER_FILE},
        )
    # ANALYZE cannot run inside a transaction block on some drivers; use autocommit.
    engine = session_factory.kw["bind"]
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("ANALYZE index_clone_fingerprint"))
        await connection.execute(text("ANALYZE index_chunk"))


@pytest.mark.asyncio
async def test_lookup_uses_hash_index_and_sql_limit_at_100k_fingerprints(
    query: PostgresCloneIndexQuery, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _seed_large(session_factory, repo_id)
    async with session_factory() as session:
        sample = (
            (
                await session.execute(
                    text(
                        "SELECT hash FROM index_clone_fingerprint WHERE repo_id = :r "
                        "ORDER BY file_path, token_offset LIMIT 200"
                    ),
                    {"r": repo_id},
                )
            )
            .scalars()
            .all()
        )
    hashes = list(sample)

    explain = text("EXPLAIN (ANALYZE) " + str(_FIND_CANDIDATES_QUERY)).bindparams(
        bindparam("hashes", type_=ARRAY(BigInteger)),
        bindparam("repo_id", type_=Uuid()),
        bindparam("excluded", type_=ARRAY(String)),
        bindparam("fetch_limit", type_=Integer()),
    )
    async with session_factory() as session:
        plan_rows = await session.execute(
            explain,
            {"hashes": hashes, "repo_id": repo_id, "excluded": [], "fetch_limit": 21},
        )
        plan = "\n".join(row[0] for row in plan_rows.all())
    assert "ix_index_clone_fingerprint_repo_pr_hash" in plan, plan
    assert "Seq Scan on index_clone_fingerprint" not in plan, plan
    assert "Limit" in plan, plan  # per-hash cap applied inside the query

    durations = []
    for _ in range(10):
        started = time.perf_counter()
        result = await query.find_candidates(
            repo_id=str(repo_id),
            hashes=hashes,
            excluded_paths=[],
            per_hash_limit=20,
            timeout_seconds=_TIMEOUT,
        )
        durations.append((time.perf_counter() - started) * 1000)
    print(
        f"clone lookup, 200 hashes @ 100k fingerprints: "
        f"median {statistics.median(durations):.1f} ms, "
        f"max {max(durations):.1f} ms, hits {len(result.hits)}"
    )
    assert len(result.hits) >= len(hashes)
    assert statistics.median(durations) < 1000
