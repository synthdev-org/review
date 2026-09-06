"""Integration tests for review.context_builder.ContextBuilderStep, per
docs/plans/plan-deliver-context-builder.md's Phase 5 section (Unit E) and
docs/specs/spec-context-builder.md's US1/US5/US7/US8/US9/US10.

Real Postgres/pgvector via testcontainers (`migrated_engine`/
`pgvector_container`, imported from `review/conftest.py`) and the real,
no-network, bundled local embedding model — the same "real adapter against
a real system" convention `adapter/storage/test_context_query.py` already
established for Units B/C. `DiffFetcher`/`RepoTreeFetcher` are fakes (per
this phase's own Tests section) since this module's own subject is
`ContextBuilderStep`'s orchestration, not GitHub's own API shape (already
proven elsewhere).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.engine import create_session_factory
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import Org, Repo
from findings.models import Finding
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingClient, EmbeddingError, EmbeddingVector
from indexing.graph import GraphEdge, GraphNode
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)
from ingestion.diff import (
    Diff,
    DiffContentMalformedError,
    DiffContentUnavailableError,
    DiffFetcher,
    DiffFile,
)
from review.context_builder import (
    CallerHit,
    ContextBuilderStep,
    ContextQueryError,
    GraphQuery,
    SimilarityQuery,
)
from review.context_bundle import MAX_CALLER_EDGES_PER_SYMBOL, ContextItemKind, serialized_item_size
from review.pipeline import FailurePolicy, StepRegistry
from review.step import StepContext

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reenable_context_builder_logger() -> None:
    """See `review/test_llm_review_step.py`'s identical fixture docstring
    (`_reenable_pipeline_logger`): Alembic's `env.py` (triggered by this
    module's own `migrated_engine`-using fixtures) disables every
    already-created logger not listed in `alembic.ini`, including
    `review.context_builder` — the logger every ``caplog`` assertion below
    reads from. Re-enabled here, autouse, so these tests stay deterministic
    regardless of test collection/execution order."""
    logging.getLogger("review.context_builder").disabled = False


_EMBEDDING_DIMENSION = 384
_REAL_MODEL_PATH = "/app/models/all-MiniLM-L6-v2"

# --- Fixture repo content: a TS file with an `add` function -----------------

_APP_PATH = "src/app.ts"

_APP_PRE = b"""\
import { Logger } from "./logger";

export function add(a: number, b: number): number {
  return a + b;
}

export class Widget {
  greet(): string {
    return "hi";
  }
}
"""

_APP_POST_MODIFIED = b"""\
import { Logger } from "./logger";

export function add(a: number, b: number): number {
  return a + b + 1;
}

export class Widget {
  greet(): string {
    return "hi";
  }
}
"""

_APP_POST_RENAMED = b"""\
import { Logger } from "./logger";

export function sum(a: number, b: number): number {
  return a + b + 1;
}

export class Widget {
  greet(): string {
    return "hi";
  }
}
"""

# Padded with trailing unified-diff *context* lines (a leading space, per
# convention) beyond the real hunk — `_parse_hunk_headers` only reads
# `@@ ... @@` header lines, so this padding never changes which lines
# resolve to which symbol; it exists purely so this fixture's own
# `diff_byte_size` comfortably exceeds a real `ContextBundleItem`'s own
# serialized size, since the proportional-to-diff budget (Non-Functional
# Requirements) would otherwise truncate every real hit away for a diff
# this trivially small.
_APP_PATCH = "@@ -4,1 +4,1 @@\n-  return a + b;\n+  return a + b + 1;\n" + (
    " // padding line to keep this fixture's diff comfortably above the byte budget\n" * 40
)

_README_PATH = "README.md"
_README_PATCH = "@@ -1,1 +1,1 @@\n-old\n+new\n"

# Two distinct files, each independently touched by the same diff, whose
# changed (pre-change) chunk resolves to the *same* bare symbol name
# ("run") — the fixture the duplicate-`symbol_names`-dedup test below needs.
_RUN_A_PATH = "src/a.ts"
_RUN_A_PRE = b"""\
export function run(): number {
  return 1;
}
"""
_RUN_A_POST = b"""\
export function run(): number {
  return 2;
}
"""
_RUN_B_PATH = "src/b.ts"
_RUN_B_PRE = b"""\
export function run(): number {
  return 10;
}
"""
_RUN_B_POST = b"""\
export function run(): number {
  return 20;
}
"""
_RUN_PATCH = "@@ -2,1 +2,1 @@\n-  return 1;\n+  return 2;\n" + (
    " // padding line to keep this fixture's diff comfortably above the byte budget\n" * 40
)


def _diff(*files: DiffFile) -> Diff:
    return Diff(files=tuple(files))


# --- Fakes at the DiffFetcher/RepoTreeFetcher Protocol boundaries -----------


class _FakeDiffFetcher:
    """A fake ``DiffFetcher`` — only ``fetch_content`` is ever exercised by
    ``ContextBuilderStep`` (``fetch`` is ``PrefilterStep``'s own concern,
    per ``ingestion.diff.DiffFetcher``'s own docstring)."""

    def __init__(
        self,
        post_content: dict[str, bytes] | None = None,
        *,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._post_content = post_content or {}
        self._error = error
        self._delay_seconds = delay_seconds

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        raise AssertionError("fetch() is prefilter's own concern, never called here")

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
        return {path: content for path, content in self._post_content.items() if path in paths}


class _FakeTreeFetcher:
    def __init__(
        self,
        pre_content: dict[str, bytes] | None = None,
        *,
        fetch_tree_error: Exception | None = None,
        fetch_content_error: Exception | None = None,
    ) -> None:
        self._pre_content = pre_content or {}
        self._fetch_tree_error = fetch_tree_error
        self._fetch_content_error = fetch_content_error

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        if self._fetch_tree_error is not None:
            raise self._fetch_tree_error
        return tuple(
            RepoTreeEntry(path=path, mode="100644", blob_sha=f"sha-{path}")
            for path in self._pre_content
        )

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        if self._fetch_content_error is not None:
            raise self._fetch_content_error
        wanted = {entry.path for entry in paths_with_blob_sha}
        return {path: content for path, content in self._pre_content.items() if path in wanted}


class _RaisingGraphQuery:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        raise self._error


class _RaisingSimilarityQuery:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        raise self._error


class _EmptyGraphQuery:
    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        return dict.fromkeys(symbol_names, ())


class _RecordingGraphQuery:
    """A fake ``GraphQuery`` recording every ``symbol_names`` argument it's
    called with (and returning empty results) — isolates whether
    ``ContextBuilderStep._orchestrate`` deduplicates ``symbol_names`` before
    calling ``find_callers``, without needing real Postgres/graph data."""

    def __init__(self) -> None:
        self.symbol_names_calls: list[tuple[str, ...]] = []

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        self.symbol_names_calls.append(tuple(symbol_names))
        return dict.fromkeys(symbol_names, ())


class _EmptySimilarityQuery:
    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        return ()


class _DistinctPerFileSimilarityQuery:
    """A fake ``SimilarityQuery`` returning a result uniquely identifiable
    by which changed chunk's own ``exclude_file_path`` requested it —
    isolates whether ``_fetch_similarity_hits``'s concurrent
    ``asyncio.gather`` dispatch (over per-chunk ``find_similar`` calls)
    still pairs each changed chunk with *its own* result, never another
    chunk's, now that the calls no longer run strictly one at a time."""

    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        return (
            Chunk(
                file_path=f"match-for-{exclude_file_path}",
                language="typescript",
                symbol_name="match",
                byte_range=(0, 1),
                line_range=(1, 1),
                content=f"similar chunk matched for {exclude_file_path}",
                chunk_index=0,
            ),
        )


class _RaisingEmbeddingClient:
    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        raise EmbeddingError(detail="embedding backend unavailable")


class _ZeroVectorEmbeddingClient:
    """A fake ``EmbeddingClient`` returning a fixed, deterministic vector —
    used only where the *query* embedding's real value doesn't matter (the
    similarity query itself is faked in those same tests)."""

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        return tuple(
            EmbeddingVector(
                values=tuple(0.0 for _ in range(_EMBEDDING_DIMENSION)),
                dimension=_EMBEDDING_DIMENSION,
            )
            for _ in texts
        )


@pytest.fixture
def upstream_output() -> Any:
    """Structurally mirrors ``review.static_analysis.StaticAnalysisOutput``
    closely enough for ``ContextBuilderStep``'s own
    ``_ContextBuilderInput.model_validate(..., from_attributes=True)`` to
    read it — see ``review/test_llm_review_step.py``'s identical fake for
    the same pattern."""

    class _FakeUpstreamOutput:
        def __init__(
            self,
            diff: Diff,
            *,
            findings: list[Finding] | None = None,
            base_sha: str | None = "base-sha",
            repo_id: str = str(uuid.uuid4()),
        ) -> None:
            self.diff = diff
            self.findings = findings or []
            self.org_id = "org-1"
            self.repo_id = repo_id
            self.installation_id = 1
            self.repo_full_name = "acme/widgets"
            self.pr_number = 42
            self.head_sha = "head-sha"
            self.base_sha = base_sha

    return _FakeUpstreamOutput


def _context() -> StepContext:
    return StepContext(job_id="job-1")


# --- Real Postgres fixtures --------------------------------------------------


@pytest_asyncio.fixture
async def session_factory(migrated_engine: Any) -> async_sessionmaker[Any]:
    # `migrated_engine` is function-scoped (a fresh `alembic upgrade head`
    # per test) and Alembic's own `env.py` disables every pre-existing
    # logger not listed in `alembic.ini`, including `review.context_builder`
    # — the same effect `_reenable_context_builder_logger` above exists to
    # undo. That autouse fixture has no dependency on this one, so for any
    # test that also requests this fixture (transitively, via `repository`/
    # `real_repo_id`), pytest may set it up *after* the autouse fixture
    # already ran, re-disabling the logger for the rest of the test. Since
    # `session_factory` always runs strictly after `migrated_engine` (a hard
    # fixture dependency), re-enabling here as well guarantees the logger is
    # enabled by the time any test body actually runs, regardless of
    # fixture-setup ordering between the two independent fixtures above.
    logging.getLogger("review.context_builder").disabled = False
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repository(session_factory: async_sessionmaker[Any]) -> IndexingRepository:
    return IndexingRepository(session_factory)


@pytest_asyncio.fixture
async def real_repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


@pytest.fixture(scope="module")
def real_embedding_client() -> LocalONNXEmbeddingClient:
    return LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)


def _vector(fill: float = 0.0) -> EmbeddingVector:
    return EmbeddingVector(
        values=tuple(float(fill) for _ in range(_EMBEDDING_DIMENSION)),
        dimension=_EMBEDDING_DIMENSION,
    )


async def _write_caller(
    repository: IndexingRepository,
    *,
    repo_id: uuid.UUID,
    caller_file: str,
    caller_symbol: str,
    caller_content: str,
    target_symbol: str,
    embedding: EmbeddingVector | None = None,
) -> None:
    """``embedding`` defaults to an all-zero placeholder vector — fine for
    every test whose own similarity query is faked/empty, but a genuine,
    real embedding must be supplied whenever a real `PostgresSimilarityQuery`
    is also in play in the same test: an all-zero vector is a degenerate
    cosine-similarity case that pgvector can rank as a spurious top match
    regardless of the actual query vector, which would make this caller
    fixture accidentally satisfy a *different* test's own similarity
    assertion for the wrong reason.
    """
    node = GraphNode(file_path=caller_file, symbol_name=caller_symbol, language="typescript")
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path=caller_file,
        chunks=[
            Chunk(
                file_path=caller_file,
                language="typescript",
                symbol_name=caller_symbol,
                byte_range=(0, len(caller_content)),
                line_range=(1, 1),
                content=caller_content,
                chunk_index=0,
            )
        ],
        embeddings=[embedding if embedding is not None else _vector()],
        nodes=[node],
        edges=[GraphEdge(source_node=node, target_symbol_name=target_symbol)],
    )


async def _write_similar_chunk(
    repository: IndexingRepository,
    embedding_client: EmbeddingClient,
    *,
    repo_id: uuid.UUID,
    file_path: str,
    content: str,
    symbol_name: str = "similarFn",
) -> None:
    (embedding,) = embedding_client.embed([content])
    await repository.upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path=file_path,
        chunks=[
            Chunk(
                file_path=file_path,
                language="typescript",
                symbol_name=symbol_name,
                byte_range=(0, len(content)),
                line_range=(1, 1),
                content=content,
                chunk_index=0,
            )
        ],
        embeddings=[embedding],
        nodes=[],
        edges=[],
    )


def _step(
    *,
    diff_fetcher: DiffFetcher,
    tree_fetcher: RepoTreeFetcher,
    graph_query: GraphQuery,
    similarity_query: SimilarityQuery,
    embedding_client: EmbeddingClient,
    internal_timeout_seconds: float = 25.0,
    max_total_bytes: int = 200_000,
) -> ContextBuilderStep:
    return ContextBuilderStep(
        diff_fetcher,
        tree_fetcher,
        graph_query,
        similarity_query,
        embedding_client,
        internal_timeout_seconds=internal_timeout_seconds,
        max_total_bytes=max_total_bytes,
    )


# =============================================================================
# US1/US2/US3/US4/US10: full Phase 1->4 orchestration at two commits.
# =============================================================================


@pytest.mark.asyncio
async def test_modified_function_blast_radius_and_similarity_both_reach_the_bundle(
    upstream_output: Any,
    repository: IndexingRepository,
    real_repo_id: uuid.UUID,
    real_embedding_client: LocalONNXEmbeddingClient,
    session_factory: async_sessionmaker[Any],
) -> None:
    caller_content = "function bar() { add(1, 2); }"
    (caller_embedding,) = real_embedding_client.embed([caller_content])
    await _write_caller(
        repository,
        repo_id=real_repo_id,
        caller_file="src/caller.ts",
        caller_symbol="bar",
        caller_content=caller_content,
        target_symbol="add",
        embedding=caller_embedding,
    )
    await _write_similar_chunk(
        repository,
        real_embedding_client,
        repo_id=real_repo_id,
        file_path="src/other.ts",
        content=_APP_POST_MODIFIED.decode(),
    )

    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    diff_fetcher = _FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED})
    tree_fetcher = _FakeTreeFetcher({_APP_PATH: _APP_PRE})
    step = _step(
        diff_fetcher=diff_fetcher,
        tree_fetcher=tree_fetcher,
        graph_query=PostgresGraphQuery(session_factory),
        similarity_query=PostgresSimilarityQuery(session_factory),
        embedding_client=real_embedding_client,
    )

    output = await step.run(
        upstream_output(diff, base_sha="base-sha-1", repo_id=str(real_repo_id)), _context()
    )

    caller_items = [i for i in output.context_bundle.items if i.kind == ContextItemKind.CALLER]
    similarity_items = [
        i for i in output.context_bundle.items if i.kind == ContextItemKind.SIMILARITY
    ]
    assert {i.file_path for i in caller_items} == {"src/caller.ts"}
    assert {i.symbol_name for i in caller_items} == {"bar"}
    # Similarity search isn't scoped to a single "the" match — with only
    # two other-file candidates indexed and a limit of
    # MAX_SIMILARITY_MATCHES_PER_CHUNK (5), both can legitimately come back
    # (US4: "fewer than limit matches ... returns however many exist"). The
    # real assertion is that the deliberately near-duplicate chunk
    # (`src/other.ts`, identical post-change function body) is present.
    assert "src/other.ts" in {i.file_path for i in similarity_items}
    assert output.diff == diff
    assert output.base_sha == "base-sha-1"
    assert output.org_id == "org-1"


@pytest.mark.asyncio
async def test_duplicate_symbol_name_across_two_changed_files_is_deduped_before_find_callers(
    upstream_output: Any,
) -> None:
    """Two different changed files both touching a function named `run`
    must not pass `"run"` twice into `GraphQuery.find_callers` — a
    duplicated name would otherwise issue one independent LATERAL subquery
    per *occurrence*, not per distinct name (see
    `adapter/storage/context_query.py`'s own `unnest(:symbol_names)`-based
    query), doubling both the query work and the resulting `CallerHit`
    entries appended under that one key, potentially crowding out distinct
    callers at Phase 4's own per-symbol truncation."""
    diff = _diff(
        DiffFile(path=_RUN_A_PATH, status="modified", patch=_RUN_PATCH),
        DiffFile(path=_RUN_B_PATH, status="modified", patch=_RUN_PATCH),
    )
    graph_query = _RecordingGraphQuery()
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_RUN_A_PATH: _RUN_A_POST, _RUN_B_PATH: _RUN_B_POST}),
        tree_fetcher=_FakeTreeFetcher({_RUN_A_PATH: _RUN_A_PRE, _RUN_B_PATH: _RUN_B_PRE}),
        graph_query=graph_query,
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert len(graph_query.symbol_names_calls) == 1
    (called_names,) = graph_query.symbol_names_calls
    assert called_names.count("run") == 1


@pytest.mark.asyncio
async def test_concurrent_similarity_dispatch_pairs_each_chunk_with_its_own_match(
    upstream_output: Any,
) -> None:
    """`_fetch_similarity_hits` dispatches one `find_similar` call per
    changed chunk concurrently via `asyncio.gather` rather than one at a
    time — this must not scramble which result belongs to which changed
    chunk. Two changed files, each producing a distinct post-change chunk,
    each get a `SimilarityQuery` result identifiable by which chunk asked
    for it (`_DistinctPerFileSimilarityQuery`); both must reach the bundle
    correctly attributed, never swapped."""
    diff = _diff(
        DiffFile(path=_RUN_A_PATH, status="modified", patch=_RUN_PATCH),
        DiffFile(path=_RUN_B_PATH, status="modified", patch=_RUN_PATCH),
    )
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_RUN_A_PATH: _RUN_A_POST, _RUN_B_PATH: _RUN_B_POST}),
        tree_fetcher=_FakeTreeFetcher({_RUN_A_PATH: _RUN_A_PRE, _RUN_B_PATH: _RUN_B_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_DistinctPerFileSimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    similarity_items = [
        i for i in output.context_bundle.items if i.kind == ContextItemKind.SIMILARITY
    ]
    assert {i.file_path for i in similarity_items} == {
        f"match-for-{_RUN_A_PATH}",
        f"match-for-{_RUN_B_PATH}",
    }


@pytest.mark.asyncio
async def test_renamed_function_blast_radius_uses_pre_change_name(
    upstream_output: Any,
    repository: IndexingRepository,
    real_repo_id: uuid.UUID,
    real_embedding_client: LocalONNXEmbeddingClient,
    session_factory: async_sessionmaker[Any],
) -> None:
    """US10: a diff that renames ``add`` to ``sum`` still surfaces the
    caller indexed under ``add`` (the pre-change name), never a lookup
    under the new name ``sum``."""
    caller_content = "function bar() { add(1, 2); }"
    (caller_embedding,) = real_embedding_client.embed([caller_content])
    await _write_caller(
        repository,
        repo_id=real_repo_id,
        caller_file="src/caller.ts",
        caller_symbol="bar",
        caller_content=caller_content,
        target_symbol="add",
        embedding=caller_embedding,
    )

    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    diff_fetcher = _FakeDiffFetcher({_APP_PATH: _APP_POST_RENAMED})
    tree_fetcher = _FakeTreeFetcher({_APP_PATH: _APP_PRE})
    step = _step(
        diff_fetcher=diff_fetcher,
        tree_fetcher=tree_fetcher,
        graph_query=PostgresGraphQuery(session_factory),
        similarity_query=PostgresSimilarityQuery(session_factory),
        embedding_client=real_embedding_client,
    )

    output = await step.run(
        upstream_output(diff, base_sha="base-sha-1", repo_id=str(real_repo_id)), _context()
    )

    caller_items = [i for i in output.context_bundle.items if i.kind == ContextItemKind.CALLER]
    assert {i.file_path for i in caller_items} == {"src/caller.ts"}


# =============================================================================
# US7: every typed failure individually forced -> degraded_error, completed
# (not failed) job.
# =============================================================================

_FAILURE_CASES: list[tuple[str, Exception]] = [
    (
        "diff_content_unavailable",
        DiffContentUnavailableError(repo_full_name="acme/widgets", detail="boom"),
    ),
    (
        "diff_content_malformed",
        DiffContentMalformedError(repo_full_name="acme/widgets", detail="boom"),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("_name", "error"), _FAILURE_CASES, ids=[c[0] for c in _FAILURE_CASES])
async def test_diff_fetch_failures_degrade_gracefully(
    upstream_output: Any, _name: str, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher(error=error),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff), _context())

    assert output.context_bundle.items == ()
    degraded = [r for r in caplog.records if "degraded" in r.message]
    assert len(degraded) == 1
    assert degraded[0].outcome == "degraded_error"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_repo_tree_fetch_error_degrades_gracefully(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher(
            fetch_tree_error=RepoTreeFetchError(
                repo_full_name="acme/widgets", commit_sha="base-sha-1", detail="boom"
            )
        ),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert output.context_bundle.items == ()
    assert [r for r in caplog.records if "degraded" in r.message]


@pytest.mark.asyncio
async def test_repo_tree_truncated_error_degrades_gracefully(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher(
            fetch_tree_error=RepoTreeTruncatedError(
                repo_full_name="acme/widgets", commit_sha="base-sha-1"
            )
        ),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert output.context_bundle.items == ()
    assert [r for r in caplog.records if "degraded" in r.message]


@pytest.mark.asyncio
async def test_repo_tree_content_fetch_error_degrades_gracefully(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher(
            {_APP_PATH: _APP_PRE},
            fetch_content_error=RepoTreeContentFetchError(
                repo_full_name="acme/widgets", detail="boom"
            ),
        ),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert output.context_bundle.items == ()
    assert [r for r in caplog.records if "degraded" in r.message]


@pytest.mark.asyncio
async def test_context_query_error_from_graph_query_degrades_gracefully(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    error = ContextQueryError(operation="find_callers_edges", detail="boom")
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_RaisingGraphQuery(error),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert output.context_bundle.items == ()
    degraded = [r for r in caplog.records if "degraded" in r.message]
    assert len(degraded) == 1
    assert degraded[0].query_operation == "find_callers_edges"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_context_query_error_from_similarity_query_degrades_gracefully(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    error = ContextQueryError(operation="find_similar", detail="boom")
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_RaisingSimilarityQuery(error),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert output.context_bundle.items == ()
    assert [r for r in caplog.records if "degraded" in r.message]


@pytest.mark.asyncio
async def test_embedding_error_degrades_gracefully(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_RaisingEmbeddingClient(),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha="base-sha-1"), _context())

    assert output.context_bundle.items == ()
    assert [r for r in caplog.records if "degraded" in r.message]


@pytest.mark.asyncio
async def test_internal_timeout_degrades_gracefully_and_never_reaches_outer_timeout(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Proves the internal timeout fires (and is caught, degrading
    gracefully) well before the outer step timeout ever could — driven
    through a real `StepRegistry` so the outer per-step `asyncio.wait_for`
    is genuinely in play, not just this step's own call in isolation."""
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}, delay_seconds=2.0),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
        internal_timeout_seconds=0.1,
    )

    class _EchoStep:
        name = "upstream"
        depends_on: tuple[str, ...] = ()

        async def run(self, input: Any, context: StepContext) -> Any:
            return input

    step.depends_on = ("upstream",)
    registry = StepRegistry(
        steps=[_EchoStep(), step],
        enabled={"upstream": True, "context_builder": True},
        timeouts={"upstream": 5.0, "context_builder": 5.0},
        on_failure=FailurePolicy.FATAL,
    )

    with caplog.at_level(logging.WARNING, logger="review.context_builder"):
        outcomes = await registry.run(
            seed_input=upstream_output(diff, base_sha="base-sha-1"), context=_context()
        )

    assert outcomes["context_builder"].status == "success"
    output = outcomes["context_builder"].output
    assert output.context_bundle.items == ()
    assert [r for r in caplog.records if "degraded" in r.message]


# =============================================================================
# US9: budget truncation exercised end-to-end (not just Phase 4's unit test).
# =============================================================================


@pytest.mark.asyncio
async def test_budget_truncation_end_to_end(
    upstream_output: Any,
    repository: IndexingRepository,
    real_repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    large_content = "x" * 5_000
    for i in range(MAX_CALLER_EDGES_PER_SYMBOL):
        await _write_caller(
            repository,
            repo_id=real_repo_id,
            caller_file=f"src/caller{i}.ts",
            caller_symbol=f"caller{i}",
            caller_content=f"function caller{i}() {{ /* {large_content} */ add(1, 2); }}",
            target_symbol="add",
        )

    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=PostgresGraphQuery(session_factory),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
        max_total_bytes=2_000,
    )

    with caplog.at_level(logging.INFO, logger="review.context_builder"):
        output = await step.run(
            upstream_output(diff, base_sha="base-sha-1", repo_id=str(real_repo_id)), _context()
        )

    assert len(output.context_bundle.items) < MAX_CALLER_EDGES_PER_SYMBOL

    # Observability: the per-run "context_builder completed[...]" summary
    # log itself carries the dropped-by-ceiling count for Phase 4's
    # existing per-item ceilings (here, each caller's own content is large
    # enough to individually exceed `max_total_bytes`, so this exercises the
    # single-oversized-item-dropped layer specifically) — not just the
    # immediate `warning` log context_bundle.py already emits at drop time.
    completed = [r for r in caplog.records if "context_builder completed" in r.message]
    assert len(completed) == 1
    assert completed[0].oversized_items_dropped > 0  # type: ignore[attr-defined]
    # The logged byte metric must match the same serialized-size measure
    # the budget was actually enforced against, not just raw content bytes.
    assert completed[0].context_bundle_total_bytes == sum(  # type: ignore[attr-defined]
        serialized_item_size(item) for item in output.context_bundle.items
    )


# =============================================================================
# US8: mixed in-scope/out-of-scope-language PR.
# =============================================================================


@pytest.mark.asyncio
async def test_mixed_in_scope_out_of_scope_pr_still_gets_context_for_resolvable_symbols(
    upstream_output: Any,
    repository: IndexingRepository,
    real_repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    await _write_caller(
        repository,
        repo_id=real_repo_id,
        caller_file="src/caller.ts",
        caller_symbol="bar",
        caller_content="function bar() { add(1, 2); }",
        target_symbol="add",
    )

    diff = _diff(
        DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH),
        DiffFile(path=_README_PATH, status="modified", patch=_README_PATCH),
    )
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED, _README_PATH: b"new\n"}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE, _README_PATH: b"old\n"}),
        graph_query=PostgresGraphQuery(session_factory),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    output = await step.run(
        upstream_output(diff, base_sha="base-sha-1", repo_id=str(real_repo_id)), _context()
    )

    caller_items = [i for i in output.context_bundle.items if i.kind == ContextItemKind.CALLER]
    assert {i.file_path for i in caller_items} == {"src/caller.ts"}


# =============================================================================
# Two distinct base_sha=None cases.
# =============================================================================


@pytest.mark.asyncio
async def test_base_sha_none_with_zero_similarity_hits_is_degraded_empty(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.INFO, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha=None), _context())

    assert output.context_bundle.items == ()
    assert output.base_sha is None
    completed = [r for r in caplog.records if "context_builder completed" in r.message]
    assert len(completed) == 1
    assert completed[0].outcome == "degraded_empty"  # type: ignore[attr-defined]
    assert completed[0].base_sha_missing is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_base_sha_none_with_nonzero_similarity_hits_is_ok_and_base_sha_missing(
    upstream_output: Any, caplog: pytest.LogCaptureFixture
) -> None:
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))

    class _OneHitSimilarityQuery:
        async def find_similar(
            self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
        ) -> tuple[Chunk, ...]:
            return (
                Chunk(
                    file_path="src/other.ts",
                    language="typescript",
                    symbol_name="similarFn",
                    byte_range=(0, 10),
                    line_range=(1, 1),
                    content="function similarFn() {}",
                    chunk_index=0,
                ),
            )

    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_OneHitSimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )

    with caplog.at_level(logging.INFO, logger="review.context_builder"):
        output = await step.run(upstream_output(diff, base_sha=None), _context())

    assert len(output.context_bundle.items) == 1
    assert output.base_sha is None
    completed = [r for r in caplog.records if "context_builder completed" in r.message]
    assert len(completed) == 1
    assert completed[0].outcome == "ok"  # type: ignore[attr-defined]
    assert completed[0].base_sha_missing is True  # type: ignore[attr-defined]
