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
import json
import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import Any, NamedTuple, TypedDict, Unpack

import pytest
import pytest_asyncio
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from adapter.egress.credentials import parse_credential_map
from adapter.egress.transport import EgressPolicy
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.product_knowledge.client import ClientTimeouts
from adapter.product_knowledge.factory import ProviderClientFactory
from adapter.product_knowledge.provider_resolver import FailOpenProviderResolver
from adapter.product_knowledge.testing.fake_provider import (
    Era,
    FakeProvider,
    Fault,
)
from adapter.product_knowledge.testing.loopback import LoopbackTransport, fixed_resolver
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.engine import create_session_factory
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import Org, Repo
from adapter.storage.tenancy import PostgresSettingRepository
from findings.models import Finding
from findings.redact import NoOpTextRedactor, TextRedactor
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
from review.context_bundle import (
    MAX_CALLER_EDGES_PER_SYMBOL,
    ContextItemKind,
    render_context_bundle_section,
    render_product_knowledge_section,
    serialized_item_size,
)
from review.pipeline import FailurePolicy, StepRegistry
from review.product_knowledge import EffectiveProvider, ProviderOpener
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
            self.private = False

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


@pytest.mark.asyncio
async def test_product_knowledge_capture_fields_are_forwarded_to_output(
    upstream_output: Any,
) -> None:
    """
    Phase 2 of docs/plans/plan-deliver-product-knowledge.md: forward optional identity fields.

    The optional ``JobIdentity`` fields must be explicitly forwarded by this step.
    """
    diff = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))
    step = _step(
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        tree_fetcher=_FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        embedding_client=_ZeroVectorEmbeddingClient(),
    )
    upstream = upstream_output(diff)
    upstream.pr_title = "Fix PROJ-9"
    upstream.pr_head_ref = "bugfix/PROJ-9"
    upstream.pr_issue_keys = ("PROJ-9", "#41")
    upstream.pr_is_fork = False
    upstream.private = True

    output = await step.run(upstream, _context())

    assert output.pr_title == "Fix PROJ-9"
    assert output.pr_head_ref == "bugfix/PROJ-9"
    assert output.pr_issue_keys == ("PROJ-9", "#41")
    assert output.pr_is_fork is False
    assert output.private is True  # fail-closed field: a dropped forward kills the feature


# =============================================================================
# Product-knowledge provider branch (docs/plans/plan-deliver-product-knowledge.md
# Phase 7: US-PK1/2/4/5/7/13/14/20). Real Postgres (the provider setting is
# stored and read through `PostgresSettingRepository`) plus a real fake MCP
# provider server (adapter/product_knowledge/testing/fake_provider.py) reached
# through the production client/factory over a loopback transport.
# =============================================================================

_PK_HOST = "wiki.test"
_PK_TOKEN = "tok-PK-SECRET-0123456789"
_PK_SECRET = "ghp_PASTEDSECRET0123456789abcdef"
_PK_TITLE = f"Fix PROJ-9 billing retry {_PK_SECRET}"
_PK_FAST = ClientTimeouts(connect_seconds=1.0, call_seconds=1.5)
_PK_DIFF = _diff(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH))


class _MaskingRedactor:
    """A fake ``TextRedactor`` masking the one known pasted secret."""

    async def redact(self, text: str) -> str:
        return text.replace(_PK_SECRET, "[REDACTED]")


class _FailingRedactor:
    async def redact(self, text: str) -> str:
        raise RuntimeError("gitleaks subprocess exploded")


class _HangingResolver:
    """An ``EffectiveProviderResolver`` whose settings read never returns."""

    async def resolve(self, org_id: str, repo_id: str) -> EffectiveProvider | None:
        await asyncio.sleep(3600)
        return None


@pytest_asyncio.fixture
async def pk_scope(session_factory: async_sessionmaker[Any]) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return {"org_id": str(org.id), "repo_id": str(repo.id)}


async def _write_setting(
    session_factory: async_sessionmaker[Any],
    scope: dict[str, str],
    port: int,
    **overrides: Any,
) -> None:
    value: dict[str, Any] = {
        "enabled": True,
        "display_name": "Company Wiki",
        "endpoint_url": f"http://{_PK_HOST}:{port}/mcp",
        "credential_ref": "wiki",
        **overrides,
    }
    await PostgresSettingRepository(session_factory).upsert(
        org_id=scope["org_id"],
        repo_id=None,
        team_id=None,
        key="product_knowledge_provider",
        value=value,
    )


def _pk_opener(provider: FakeProvider, timeouts: ClientTimeouts = _PK_FAST) -> ProviderOpener:
    credentials = parse_credential_map(
        SecretStr(json.dumps({"wiki": {"token": _PK_TOKEN, "allowed_hosts": [_PK_HOST]}}))
    )
    return ProviderClientFactory(
        credentials=credentials,
        policy=EgressPolicy(),
        timeouts=timeouts,
        resolver=fixed_resolver("10.9.8.7"),
        inner_transport=LoopbackTransport(provider.port),
    )


class _PkStepOptions(TypedDict, total=False):
    """Keyword overrides ``_pk_step`` accepts (all optional)."""

    redactor: TextRedactor | None
    opener: ProviderOpener | str | None
    resolver: Any
    graph_query: GraphQuery | None
    similarity_query: SimilarityQuery | None
    deadline: float
    internal_timeout_seconds: float
    timeouts: ClientTimeouts


def _pk_step(
    session_factory: async_sessionmaker[Any],
    provider: FakeProvider,
    **options: Unpack[_PkStepOptions],
) -> ContextBuilderStep:
    opener: Any = options.get("opener", "default")
    resolver: Any = options.get("resolver", "default")
    redactor = options.get("redactor")
    return ContextBuilderStep(
        _FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        _FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        options.get("graph_query") or _EmptyGraphQuery(),
        options.get("similarity_query") or _DistinctPerFileSimilarityQuery(),
        _ZeroVectorEmbeddingClient(),
        internal_timeout_seconds=options.get("internal_timeout_seconds", 25.0),
        max_total_bytes=200_000,
        provider_resolver=(
            FailOpenProviderResolver(PostgresSettingRepository(session_factory))
            if resolver == "default"
            else resolver
        ),
        provider_opener=(
            _pk_opener(provider, options.get("timeouts", _PK_FAST))
            if opener == "default"
            else opener
        ),
        provider_redactor=redactor if redactor is not None else _MaskingRedactor(),
        provider_deadline_seconds=options.get("deadline", 5.0),
    )


def _pk_upstream(
    upstream_output: Any,
    scope: dict[str, str],
    *,
    private: bool = True,
    is_fork: bool | None = False,
    title: str | None = _PK_TITLE,
    keys: tuple[str, ...] = ("PROJ-9",),
) -> Any:
    upstream = upstream_output(_PK_DIFF, repo_id=scope["repo_id"])
    upstream.org_id = scope["org_id"]
    upstream.pr_title = title
    upstream.pr_issue_keys = keys
    upstream.pr_is_fork = is_fork
    upstream.private = private
    return upstream


def _pk_queries(provider: FakeProvider) -> list[str]:
    return [
        str((r.arguments or {}).get("query"))
        for r in provider.requests
        if r.tool == "product_knowledge_search"
    ]


def _pk_outcome(caplog: pytest.LogCaptureFixture) -> str:
    summary = {
        "product_knowledge completed",
        "product_knowledge degraded",
        "product_knowledge not configured",
    }
    records = [r for r in caplog.records if r.message in summary]
    assert len(records) == 1, [r.message for r in caplog.records]
    return str(records[0].outcome)  # type: ignore[attr-defined]


def _code_outcome(caplog: pytest.LogCaptureFixture) -> tuple[str, int]:
    records = [r for r in caplog.records if r.message.startswith("context_builder ")]
    assert len(records) == 1, [r.message for r in caplog.records]
    return str(records[0].outcome), int(records[0].context_bundle_item_count)  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_provider_section_joins_the_bundle_with_ids_after_code_ids(
    era: Era,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-PK1 (context-builder half) + US-PK7 + US-PK2 (query redaction)."""
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(era=era, token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        code_only = await _pk_step(session_factory, provider, resolver=None, opener=None).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )
        caplog.clear()  # the single-summary-line assertions below are about `output` only
        step = _pk_step(session_factory, provider)

        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

        queries = _pk_queries(provider)

    bundle = output.context_bundle
    assert len(bundle.items) >= 1  # code context is intact
    # The provider section never joins ``items``: length is exactly the
    # code-only length, and the first provider id is one past the last code id.
    assert len(bundle.items) == len(code_only.context_bundle.items)
    assert bundle.item(len(bundle.items)) is None
    section = bundle.product_knowledge
    assert section is not None
    assert section.label == "Company Wiki"
    assert [i.id for i in section.items] == [
        len(bundle.items) + n for n in range(len(section.items))
    ]
    assert bundle.item(0) is bundle.items[0]
    assert bundle.item(section.items[0].id) is None  # provider ids never index code items
    assert bundle.product_knowledge_item(section.items[0].id) is section.items[0]
    assert section.items[0].content.startswith("Full content of")  # top item was fetched
    # US-PK2: the provider saw the redacted title and the key, nothing else.
    # PROJ-9 is already in the title, so it is not repeated as a key.
    assert queries == ["Fix PROJ-9 billing retry [REDACTED]"]
    assert _PK_SECRET not in json.dumps([r.arguments for r in provider.requests])
    assert _pk_outcome(caplog) == "ok"
    code_outcome, code_count = _code_outcome(caplog)
    assert code_outcome == "ok"
    assert code_count == len(bundle.items)  # provider items never counted as code items


class _GateCase(NamedTuple):
    private: bool
    is_fork: bool | None
    allow_public: bool
    expected: str
    queried: bool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _GateCase(True, False, False, "ok", True),
        _GateCase(False, False, False, "skipped_public_repo", False),
        _GateCase(True, True, False, "skipped_fork", False),
        _GateCase(True, None, False, "skipped_fork", False),  # old payload: fail closed
        _GateCase(False, True, True, "ok", True),  # explicit per-scope opt-in
    ],
)
async def test_exposure_gate_matrix_sends_zero_requests_on_skip(
    case: _GateCase,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-PK20 (A1)."""
    private, is_fork, allow_public, expected, queried = case
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(
            session_factory, pk_scope, provider.port, allow_public_repos=allow_public
        )
        step = _pk_step(session_factory, provider)

        output = await step.run(
            _pk_upstream(upstream_output, pk_scope, private=private, is_fork=is_fork), _context()
        )

        assert bool(provider.requests) is queried
    assert _pk_outcome(caplog) == expected
    assert (output.context_bundle.product_knowledge is not None) is queried


class _ChainDiffFetcher:
    """
    Serve both the prefilter's ``fetch`` and the later steps' ``fetch_content``.

    Used by the chain-level ``private`` test.
    """

    async def fetch(self, **_: Any) -> Diff:
        return _PK_DIFF

    async def fetch_content(self, **_: Any) -> dict[str, bytes]:
        return {_APP_PATH: _APP_POST_MODIFIED}


class _NoFindingsAnalyzer:
    async def run(self, diff: Diff, content: Any, *, timeout: float) -> list[Finding]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(("private", "expected"), [(True, "ok"), (False, "skipped_public_repo")])
async def test_private_flag_survives_prefilter_static_analysis_to_the_exposure_gate(
    private: bool,
    expected: str,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A1: the raw job payload's ``private`` key reaches the gate through every hand-off.

    It must travel through the real prefilter -> static_analysis -> context_builder hand-offs;
    a dropped forward would silently turn every private repo into ``skipped_public_repo``.
    """
    from review.prefilter import PrefilterStep
    from review.static_analysis import StaticAnalysisStep

    caplog.set_level(logging.INFO, logger="review.context_builder")
    fetcher = _ChainDiffFetcher()
    prefilter = PrefilterStep(fetcher)
    static = StaticAnalysisStep(
        fetcher,
        gitleaks_analyzer=_NoFindingsAnalyzer(),
        semgrep_analyzer=_NoFindingsAnalyzer(),
        redactor=NoOpTextRedactor(),
        enabled={"gitleaks": True, "semgrep": True},
        timeouts={"gitleaks": 5.0, "semgrep": 5.0},
    )
    payload = {
        "org_id": pk_scope["org_id"],
        "repo_id": pk_scope["repo_id"],
        "installation_id": 1,
        "repo_full_name": "acme/widgets",
        "pr_number": 42,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "pr_title": _PK_TITLE,
        "pr_issue_keys": ["PROJ-9"],
        "pr_is_fork": False,
        "private": private,
    }
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(session_factory, provider)

        pre = await prefilter.run(payload, _context())
        sa = await static.run(pre, _context())
        await step.run(sa, _context())

        assert bool(provider.requests) is private
    assert _pk_outcome(caplog) == expected


@pytest.mark.asyncio
async def test_issue_key_prefix_allow_list_sends_only_matching_keys_never_title_text(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
) -> None:
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port, issue_key_prefixes=["PROJ"])
        step = _pk_step(session_factory, provider)

        await step.run(
            _pk_upstream(
                upstream_output, pk_scope, title="Fix salary bands", keys=("HR-77", "PROJ-9")
            ),
            _context(),
        )

        assert _pk_queries(provider) == ["PROJ-9"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    [
        "Executive salary bands 2026",
        "hr-77",
        "HR_77",
        "HR\u201177",
        "HR-1234567",
        "x_HR-77",
        "HR 77",
        "HR-77 #41",
    ],
)
async def test_issue_key_prefix_allow_list_blocks_title_evasions_with_zero_requests(
    title: str,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ingestion.pr_metadata import extract_issue_keys, filter_issue_keys

    caplog.set_level(logging.INFO, logger="review.context_builder")
    # Mirror the prefilter: keys are extracted from the title, then the
    # step filters them by the allow-list.
    keys = extract_issue_keys(title, None, None)
    assert filter_issue_keys(keys, ["PROJ"]) == ()
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port, issue_key_prefixes=["PROJ"])
        step = _pk_step(session_factory, provider)

        await step.run(_pk_upstream(upstream_output, pk_scope, title=title, keys=keys), _context())

        assert provider.requests == []
    assert _pk_outcome(caplog) == "no_query"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write_setting",
    [False, True],
    ids=["no_provider_setting", "repo_level_opt_out"],
)
async def test_no_provider_or_opt_out_sends_zero_requests_and_leaves_bundle_unchanged(
    write_setting: bool,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-PK4: output equals what a step with no provider wiring produces."""
    caplog.set_level(logging.DEBUG, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        if write_setting:
            await _write_setting(session_factory, pk_scope, provider.port, enabled=False)
        with_provider = await _pk_step(session_factory, provider).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )
        baseline = await _pk_step(session_factory, provider, resolver=None, opener=None).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

        assert provider.requests == []
    assert with_provider.context_bundle == baseline.context_bundle
    assert with_provider.context_bundle.product_knowledge is None
    # The strings the LLM would see are equal with and without provider config.
    for rendered in (render_context_bundle_section, render_product_knowledge_section):
        assert rendered(with_provider.context_bundle) == rendered(baseline.context_bundle)
    assert render_product_knowledge_section(with_provider.context_bundle) == ""


@pytest.mark.asyncio
async def test_kill_switch_with_a_configured_provider_reports_disabled_and_calls_nothing(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(session_factory, provider, opener=None)

        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

        assert provider.requests == []
    assert _pk_outcome(caplog) == "disabled"
    assert output.context_bundle.product_knowledge is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redactor", "expected"),
    [(NoOpTextRedactor(), "redaction_disabled"), (_FailingRedactor(), "provider_error")],
    ids=["noop_redactor", "redactor_failure"],
)
async def test_redaction_precondition_a5_blocks_the_call_and_the_job_completes(
    redactor: TextRedactor,
    expected: str,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(session_factory, provider, redactor=redactor)

        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

        assert provider.requests == []
    assert _pk_outcome(caplog) == expected
    assert output.context_bundle.product_knowledge is None
    assert len(output.context_bundle.items) >= 1
    assert "exploded" not in " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unbound_credential_fails_closed_before_any_request(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port, credential_ref="gone")
        step = _pk_step(session_factory, provider)

        await step.run(_pk_upstream(upstream_output, pk_scope), _context())

        assert provider.requests == []
    assert _pk_outcome(caplog) == "credential_unavailable"
    warned = [r for r in caplog.records if r.message == "product_knowledge degraded"]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING
    assert warned[0].provider_ref == "gone"  # type: ignore[attr-defined]
    assert warned[0].endpoint_host == _PK_HOST  # type: ignore[attr-defined]


class _FlowCase(NamedTuple):
    private: bool
    credential_ref: str
    redactor: TextRedactor | None
    opener: Any
    expected: str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        # A1 gate precedes the credential lookup.
        _FlowCase(False, "gone", None, "default", "skipped_public_repo"),
        # credential lookup precedes the A5 redaction precondition.
        _FlowCase(True, "gone", NoOpTextRedactor(), "default", "credential_unavailable"),
        # kill switch (no opener) precedes the A1 gate.
        _FlowCase(False, "wiki", None, None, "disabled"),
    ],
    ids=["a1_before_credential", "credential_before_a5", "disabled_before_a1"],
)
async def test_provider_flow_order_with_overlapping_skip_conditions(
    case: _FlowCase,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plan Phase 7 branch flow: disabled -> A1 -> credential -> A5."""
    private, credential_ref, redactor, opener, expected = case
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(
            session_factory, pk_scope, provider.port, credential_ref=credential_ref
        )
        step = _pk_step(session_factory, provider, redactor=redactor, opener=opener)

        await step.run(_pk_upstream(upstream_output, pk_scope, private=private), _context())

        assert provider.requests == []
    assert _pk_outcome(caplog) == expected


@pytest.mark.asyncio
async def test_code_duration_metric_and_log_exclude_provider_wait(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """
    The existing duration histogram/log stay code-branch-only.

    A provider that hangs until its 1.5 s deadline must not show up in them.
    """
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(
        token=_PK_TOKEN, fault=Fault("hang", "product_knowledge_search")
    ) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(
            session_factory,
            provider,
            deadline=1.5,
            timeouts=ClientTimeouts(connect_seconds=1.0, call_seconds=10.0),
        )
        start = time.monotonic()
        await step.run(_pk_upstream(upstream_output, pk_scope), _context())
        total_ms = (time.monotonic() - start) * 1000

    assert _pk_outcome(caplog) == "timeout"
    assert total_ms >= 1400  # the provider wait really happened
    (record,) = [r for r in caplog.records if r.message == "context_builder completed"]
    assert int(record.duration_ms) < 1000  # type: ignore[attr-defined]
    points = _histogram_points(in_memory_metric_reader, "review.context_builder.duration_ms")
    assert len(points) == 1 and points[0].max < 1000


@pytest.mark.asyncio
async def test_no_title_and_no_keys_is_no_query(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(session_factory, provider)

        await step.run(_pk_upstream(upstream_output, pk_scope, title=None, keys=()), _context())

        assert provider.requests == []
    assert _pk_outcome(caplog) == "no_query"


_PK_FAILURES: list[tuple[str, Fault, str]] = [
    ("auth", Fault("status401", "product_knowledge_search"), "auth_failed"),
    ("server_error", Fault("status500", "product_knowledge_search"), "provider_error"),
    ("error_result", Fault("error_result", "product_knowledge_search"), "provider_error"),
    ("malformed", Fault("bad_structure", "product_knowledge_search"), "malformed"),
    ("oversized", Fault("huge"), "oversized"),
    ("bad_version", Fault("badversion"), "unsupported_version"),
    ("slow", Fault("hang", "product_knowledge_search"), "timeout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected"), [(f, e) for _, f, e in _PK_FAILURES], ids=[n for n, _, _ in _PK_FAILURES]
)
async def test_every_provider_failure_class_leaves_code_context_and_completes(
    fault: Fault,
    expected: str,
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-PK5 at job level: right outcome, empty section, code unchanged."""
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN, fault=fault) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(session_factory, provider, deadline=5.0)

        baseline = await _pk_step(session_factory, provider, resolver=None, opener=None).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )
        caplog.clear()
        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

    assert _pk_outcome(caplog) == expected
    assert output.context_bundle.product_knowledge is None
    assert output.context_bundle.items == baseline.context_bundle.items
    assert _code_outcome(caplog)[0] == "ok"
    log_text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert _PK_TOKEN not in log_text


@pytest.mark.asyncio
async def test_partial_results_keep_the_valid_items(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(
        token=_PK_TOKEN, fault=Fault("partial", "product_knowledge_search")
    ) as p:
        await _write_setting(session_factory, pk_scope, p.port)
        output = await _pk_step(session_factory, p).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

    section = output.context_bundle.product_knowledge
    assert section is not None
    assert [i.title for i in section.items] == ["Product doc 1", "Product doc 2", "Product doc 3"]
    assert _pk_outcome(caplog) == "partial"


@pytest.mark.asyncio
async def test_failed_fetch_keeps_the_search_snippet(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
) -> None:
    async with FakeProvider(
        token=_PK_TOKEN, fault=Fault("status500", "product_knowledge_fetch")
    ) as p:
        await _write_setting(session_factory, pk_scope, p.port)
        output = await _pk_step(session_factory, p).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

    section = output.context_bundle.product_knowledge
    assert section is not None
    assert section.items[0].content.startswith("Snippet for document 1")


@pytest.mark.asyncio
async def test_code_failure_keeps_the_provider_section(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two-way isolation, direction 1 (F9)."""
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(
            session_factory,
            provider,
            graph_query=_RaisingGraphQuery(
                ContextQueryError(operation="find_callers_edges", detail="x")
            ),
        )

        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

    assert output.context_bundle.items == ()
    section = output.context_bundle.product_knowledge
    assert section is not None
    assert section.items[0].id == 0  # ids continue after zero code items
    assert _code_outcome(caplog)[0] == "degraded_error"
    assert _pk_outcome(caplog) == "ok"


@pytest.mark.asyncio
async def test_hanging_provider_cannot_exceed_the_internal_window_or_hurt_code_context(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Two-way isolation, direction 2, with a provider deadline larger than the window.

    The deadline is *larger* than the internal window: the branch is capped to the window.
    """
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(
        token=_PK_TOKEN, fault=Fault("hang", "product_knowledge_search")
    ) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        # The adapter call timeout (10s) and the branch deadline (60s) are both
        # far above the 1.0s window, so only the min() cap in the step can end
        # the hung search in time; deleting the cap pushes elapsed to ~10s.
        step = _pk_step(
            session_factory,
            provider,
            deadline=60.0,
            internal_timeout_seconds=1.0,
            timeouts=ClientTimeouts(connect_seconds=1.0, call_seconds=10.0),
        )

        start = time.monotonic()
        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())
        elapsed = time.monotonic() - start

    assert elapsed < 2.5
    assert _pk_outcome(caplog) == "timeout"
    assert len(output.context_bundle.items) >= 1


@pytest.mark.asyncio
async def test_hung_settings_read_is_bounded_by_the_branch_deadline(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        step = _pk_step(session_factory, provider, resolver=_HangingResolver(), deadline=0.5)

        start = time.monotonic()
        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

    assert time.monotonic() - start < 3.0
    assert _pk_outcome(caplog) == "timeout"
    assert len(output.context_bundle.items) >= 1


@pytest.mark.asyncio
async def test_existing_outcome_metrics_and_log_are_emitted_once_when_the_provider_fails(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN, fault=Fault("status500")) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        await _pk_step(session_factory, provider).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

    code_logs = [r for r in caplog.records if r.message.startswith("context_builder ")]
    assert len(code_logs) == 1
    data = in_memory_metric_reader.get_metrics_data()
    assert data is not None
    outcome_points = [
        p
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.context_builder.outcome_total"
        for p in m.data.data_points
    ]
    assert len(outcome_points) == 1
    assert dict(outcome_points[0].attributes or {}) == {"outcome": "ok"}
    assert outcome_points[0].value == 1


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


_PK_ALLOWED_ATTRIBUTES = {
    "review.product_knowledge.call_total": {"operation", "outcome", "contract_version", "origin"},
    "review.product_knowledge.call_duration_ms": {
        "operation",
        "outcome",
        "contract_version",
        "origin",
    },
    "review.product_knowledge.item_count": {"outcome"},
    "review.product_knowledge.bytes": {"outcome"},
}


def _pk_metric_attributes(reader: InMemoryMetricReader) -> dict[str, list[dict[str, Any]]]:
    """Every ``review.product_knowledge.*`` data point's attributes, by metric."""
    data = reader.get_metrics_data()
    assert data is not None
    seen: dict[str, list[dict[str, Any]]] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if not metric.name.startswith("review.product_knowledge."):
                    continue
                assert isinstance(metric.data, Sum | Histogram)
                seen.setdefault(metric.name, []).extend(
                    dict(p.attributes or {}) for p in metric.data.data_points
                )
    return seen


def _assert_closed_attribute_sets(seen: dict[str, list[dict[str, Any]]]) -> None:
    for name, attribute_sets in seen.items():
        if name not in _PK_ALLOWED_ATTRIBUTES:
            continue  # config_resolution_total has its own closed {source} set
        for attributes in attribute_sets:
            assert set(attributes) <= _PK_ALLOWED_ATTRIBUTES[name], (name, attributes)


@pytest.mark.asyncio
async def test_telemetry_uses_only_closed_label_sets_and_no_sensitive_text(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """
    US-PK14: metric attributes are the closed allow-list.

    The pasted secret, query, titles, URLs and token appear nowhere in them.
    """
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        await _pk_step(session_factory, provider).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

    seen = _pk_metric_attributes(in_memory_metric_reader)
    assert set(_PK_ALLOWED_ATTRIBUTES) <= set(seen)
    _assert_closed_attribute_sets(seen)
    calls = seen["review.product_knowledge.call_total"]
    assert {a["operation"] for a in calls} == {"contract", "search", "fetch", "job"}
    assert {a["origin"] for a in calls} == {"worker"}
    assert {a["contract_version"] for a in calls} <= {"1", "none"}
    blob = json.dumps(seen)
    for forbidden in (_PK_SECRET, _PK_TOKEN, "PROJ-9", "Product doc", "wiki.example.com", _PK_HOST):
        assert forbidden not in blob


class _HangingGraphQuery:
    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        await asyncio.sleep(3600)
        return {}


def _histogram_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        p
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == name
        for p in m.data.data_points
    ]


@pytest.mark.asyncio
async def test_only_the_code_branch_timing_out_keeps_the_provider_section(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Two-way isolation, direction 1, via the code branch's own ``wait_for``.

    The failure is a TimeoutError from a hung graph query, not a typed query error.
    """
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        step = _pk_step(
            session_factory,
            provider,
            graph_query=_HangingGraphQuery(),
            internal_timeout_seconds=2.0,
        )

        output = await step.run(_pk_upstream(upstream_output, pk_scope), _context())

    assert output.context_bundle.items == ()
    section = output.context_bundle.product_knowledge
    assert section is not None and section.items
    degraded = [r for r in caplog.records if r.message == "context_builder degraded"]
    assert len(degraded) == 1
    assert degraded[0].error_type == "TimeoutError"  # type: ignore[attr-defined]
    assert _pk_outcome(caplog) == "ok"


@pytest.mark.asyncio
async def test_code_histograms_exclude_provider_items_on_a_successful_provider_run(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        code_only = await _pk_step(session_factory, provider, resolver=None, opener=None).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )
        with_provider = await _pk_step(session_factory, provider).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

    section = with_provider.context_bundle.product_knowledge
    assert section is not None and section.items  # the provider really contributed
    code_count = len(code_only.context_bundle.items)
    assert len(with_provider.context_bundle.items) == code_count
    logged_bytes = [
        int(r.context_bundle_total_bytes)  # type: ignore[attr-defined]
        for r in caplog.records
        if r.message == "context_builder completed"
    ]
    assert len(logged_bytes) == 2 and logged_bytes[0] == logged_bytes[1] > 0
    counts = _histogram_points(in_memory_metric_reader, "review.context_builder.item_count")
    sizes = _histogram_points(in_memory_metric_reader, "review.context_builder.bytes")
    assert [(p.count, p.min, p.max) for p in counts] == [(2, code_count, code_count)]
    assert [(p.count, p.min, p.max) for p in sizes] == [(2, logged_bytes[0], logged_bytes[0])]


_STD_LOG_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


@pytest.mark.asyncio
async def test_job_log_line_has_exactly_the_documented_field_set(
    upstream_output: Any,
    session_factory: async_sessionmaker[Any],
    pk_scope: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="review.context_builder")
    async with FakeProvider(token=_PK_TOKEN) as provider:
        await _write_setting(session_factory, pk_scope, provider.port)
        await _pk_step(session_factory, provider).run(
            _pk_upstream(upstream_output, pk_scope), _context()
        )

    (record,) = [r for r in caplog.records if r.message == "product_knowledge completed"]
    assert set(record.__dict__) - _STD_LOG_ATTRS == {
        "job_id",
        "org_id",
        "repo_id",
        "provider_ref",
        "endpoint_host",
        "operation",
        "outcome",
        "status_code",
        "duration_ms",
        "item_count",
        "bytes",
    }
    assert record.operation == "job"  # type: ignore[attr-defined]
