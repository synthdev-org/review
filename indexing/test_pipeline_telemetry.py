"""Unit tests for US9's indexing job telemetry (spans + metrics),
recorded by ``indexing.pipeline.run_indexing_job`` itself — per
``docs/rules/testing.md``'s Unit tier: OTel's own in-memory SDK test
components (``InMemorySpanExporter``/``InMemoryMetricReader``), no real
I/O. ``indexing/test_pipeline.py`` (the co-located Integration-tier file,
``@pytest.mark.integration``, real Postgres via ``testcontainers-python``)
already covers this module's actual indexing behavior end to end; these
tests exist solely to prove the telemetry contract — span/metric names,
attribute allowlist (Privacy/Compliance NFR), and job-completion metric
values — using in-memory fakes for every dependency instead.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Sequence

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from indexing import pipeline as pipeline_module
from indexing.chunk import chunk_file
from indexing.clone import CloneIndexError, build_clone_index
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.graph import GraphExtractionError, extract_graph
from indexing.pipeline import IndexingJobDependencies, IndexingJobFailedError, run_indexing_job
from indexing.repository import IndexingRepositoryError
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetchError,
)

pytestmark = pytest.mark.asyncio

_TS_CONTENT = b"function helper(): void {}\n"


class _FakeIndexRepository:
    """A no-I/O ``indexing.repository.IndexRepository`` fake — records
    every call it receives (per ``testing.md``'s "fakes over mocks"
    convention) without touching Postgres. Optionally raises
    ``IndexingRepositoryError`` from ``delete_file``, for the one test
    exercising that failure path.
    """

    def __init__(self, *, fail_delete: bool = False) -> None:
        self.fail_delete = fail_delete
        self.upsert_calls: list[str] = []
        self.delete_calls: list[str] = []

    async def upsert_file(
        self,
        *,
        repo_id: uuid.UUID,
        pr_number: int,
        file_path: str,
        chunks: Sequence[object],
        embeddings: Sequence[object],
        nodes: Sequence[object],
        edges: Sequence[object],
        clone_index: object = None,
    ) -> None:
        self.upsert_calls.append(file_path)

    async def delete_file(self, *, repo_id: uuid.UUID, pr_number: int, file_path: str) -> int:
        self.delete_calls.append(file_path)
        if self.fail_delete:
            raise IndexingRepositoryError(operation="delete_file", detail="forced failure")
        return 0


class _FakeIndexCommitTracker:
    """A no-I/O ``indexing.repository.IndexCommitTracker`` fake. Optionally
    raises ``IndexingRepositoryError`` from ``set_last_indexed_commit``,
    for the whole-job-fatal-failure test.
    """

    def __init__(self, *, fail_set: bool = False) -> None:
        self.fail_set = fail_set
        self.set_calls: list[str] = []

    async def get_last_indexed_commit(self, *, repo_id: uuid.UUID) -> str | None:
        return None

    async def set_last_indexed_commit(self, *, repo_id: uuid.UUID, commit_sha: str) -> None:
        self.set_calls.append(commit_sha)
        if self.fail_set:
            raise IndexingRepositoryError(operation="set_last_indexed_commit", detail="forced")


def _payload(
    *,
    org_id: uuid.UUID,
    repo_id: uuid.UUID,
    pr_number: int = 0,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "org_id": str(org_id),
        "repo_id": str(repo_id),
        "installation_id": 1,
        "repo_full_name": "acme/widgets",
        "pr_number": pr_number,
        "commit_sha": "a" * 40,
        "added": list(added),
        "removed": list(removed),
        "modified": [],
        "force_full_tree_fallback": False,
    }


def _entry(path: str, content: bytes) -> RepoTreeEntry:
    return RepoTreeEntry(path=path, mode="100644", blob_sha=f"sha-{path}", size=len(content))


def _chunk_texts(path: str, content: bytes) -> frozenset[str]:
    """The exact chunk text(s) ``chunk_file`` would produce for ``content``
    — mirrors ``indexing/test_pipeline.py``'s own identically-named helper
    (Tree-sitter's own node span excludes the file's trailing newline, so
    the raw ``content`` bytes themselves never equal any real chunk's own
    text)."""
    return frozenset(chunk.content for chunk in chunk_file("typescript", path, content).chunks)


@pytest.fixture
def in_memory_span_exporter(
    reset_otel_global_providers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[InMemorySpanExporter]:
    """Registers a real ``TracerProvider`` backed by ``InMemorySpanExporter``
    as the global one — mirrors ``review/test_output_telemetry.py``'s
    ``in_memory_metric_reader`` fixture, required for this module's
    module-level ``tracer`` (created once, at import time) to pick this
    test's provider up via OTel's proxy-instrument migration mechanism.
    ``SimpleSpanProcessor`` (not the batched processor) exports each span
    synchronously on end, so no explicit flush is needed before asserting.

    The installed ``opentelemetry-api``'s own ``ProxyTracer`` caches its
    resolved real ``Tracer`` permanently on first use
    (``opentelemetry.trace.ProxyTracer._tracer``'s own ``if
    self._real_tracer: return self._real_tracer`` — no per-set-provider
    re-migration callback the way ``ProxyMeter`` has, verified against the
    installed ``opentelemetry-api`` source) — matches
    ``review/test_step_registry.py``'s identically-reasoned
    ``in_memory_span_exporter`` fixture for ``review.pipeline``'s own
    module-level ``tracer``. Left alone, only the first test in the whole
    process to use this fixture would ever see ``indexing.pipeline.tracer``
    pick up its own provider; every later test would keep exporting to the
    first test's already-``shutdown()`` exporter (silently dropping every
    span, not raising). This fixture resets that cache directly so every
    test using this fixture gets a fresh migration.
    """
    monkeypatch.setattr(pipeline_module.tracer, "_real_tracer", None, raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "review-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


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


def _span_by_name(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    matches = [span for span in spans if span.name == name]
    assert len(matches) == 1, f"expected exactly one span named {name!r}, found {len(matches)}"
    return matches[0]


def _counter_value(reader: InMemoryMetricReader, metric_name: str) -> float:
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Sum)
                points = list(metric.data.data_points)
                assert len(points) == 1, f"expected exactly one data point for {metric_name!r}"
                return points[0].value
    raise AssertionError(f"metric {metric_name!r} was never recorded")


def _histogram_sum(reader: InMemoryMetricReader, metric_name: str) -> float:
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                points = list(metric.data.data_points)
                assert len(points) == 1, f"expected exactly one data point for {metric_name!r}"
                return float(points[0].sum)
    raise AssertionError(f"metric {metric_name!r} was never recorded")


async def test_happy_path_emits_root_and_every_stage_span_with_id_only_attributes(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    path = "src/helper.ts"
    tree = (_entry(path, _TS_CONTENT),)
    deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(tree=tree, content={path: _TS_CONTENT}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=_FakeIndexRepository(),
        commit_tracker=_FakeIndexCommitTracker(),
    )
    payload = _payload(org_id=org_id, repo_id=repo_id, added=[path])

    await run_indexing_job(payload, deps, job_id="job-telemetry-1")

    spans = in_memory_span_exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert names == {
        "review.indexing.job.run",
        "review.indexing.stage.resolve_file_set",
        "review.indexing.stage.fetch_content",
        "review.indexing.stage.process_files",
        "review.indexing.stage.delete_removed_files",
        "review.indexing.stage.advance_cursor",
    }

    root_span = _span_by_name(list(spans), "review.indexing.job.run")
    # Privacy/Compliance NFR's fixed, reviewed allowlist: IDs only, never a
    # file path, diff content, or exception text.
    assert dict(root_span.attributes or {}) == {
        "review.indexing.job.id": "job-telemetry-1",
        "review.indexing.repo.id": str(repo_id),
        "review.indexing.org.id": str(org_id),
    }

    for stage_name in (
        "review.indexing.stage.resolve_file_set",
        "review.indexing.stage.fetch_content",
        "review.indexing.stage.process_files",
        "review.indexing.stage.delete_removed_files",
        "review.indexing.stage.advance_cursor",
    ):
        stage_span = _span_by_name(list(spans), stage_name)
        assert dict(stage_span.attributes or {}) == {}
        # Every stage span is a child of the root span, per US9's "child
        # span per real indexing stage boundary" requirement.
        assert stage_span.parent is not None
        assert stage_span.parent.span_id == root_span.context.span_id


async def test_fetch_content_span_absent_when_no_entries_to_fetch(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    """A removed-only job never calls ``fetch_content`` at all (no
    added/modified paths) — its span should never appear, matching the
    real code's own ``if entries:`` gate."""
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    path = "src/gone.ts"
    # The removed path must be absent from the current tree for
    # `_resolve_file_set` to treat it as genuinely removed.
    deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(tree=(), content={}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=_FakeIndexRepository(),
        commit_tracker=_FakeIndexCommitTracker(),
    )
    payload = _payload(org_id=org_id, repo_id=repo_id, removed=[path])

    await run_indexing_job(payload, deps, job_id="job-telemetry-2")

    names = {span.name for span in in_memory_span_exporter.get_finished_spans()}
    assert "review.indexing.stage.fetch_content" not in names
    assert "review.indexing.stage.delete_removed_files" in names


async def test_advance_cursor_span_absent_for_pull_request_triggered_job(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    """``set_last_indexed_commit`` (and its span) is only ever called for
    ``pr_number == 0`` jobs, per this module's own cursor-poisoning-fix
    rule."""
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(tree=(), content={}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=_FakeIndexRepository(),
        commit_tracker=_FakeIndexCommitTracker(),
    )
    payload = _payload(org_id=org_id, repo_id=repo_id, pr_number=7)

    await run_indexing_job(payload, deps, job_id="job-telemetry-3")

    names = {span.name for span in in_memory_span_exporter.get_finished_spans()}
    assert "review.indexing.stage.advance_cursor" not in names


async def test_job_fatal_failure_exports_spans_without_leaking_exception_text(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    """A whole-job-fatal failure (cursor advance, here) still ends and
    exports the root span and the failed stage's own span — but never
    records the underlying exception as span data (``record_exception=False``/
    ``set_status_on_exception=False``), per the Privacy/Compliance NFR."""
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(tree=(), content={}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=_FakeIndexRepository(),
        commit_tracker=_FakeIndexCommitTracker(fail_set=True),
    )
    payload = _payload(org_id=org_id, repo_id=repo_id, pr_number=0)

    with pytest.raises(IndexingJobFailedError):
        await run_indexing_job(payload, deps, job_id="job-telemetry-4")

    spans = in_memory_span_exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert "review.indexing.job.run" in names
    assert "review.indexing.stage.advance_cursor" in names

    for span in spans:
        assert span.events == ()
        assert dict(span.attributes or {}) in (
            {},
            {
                "review.indexing.job.id": "job-telemetry-4",
                "review.indexing.repo.id": str(repo_id),
                "review.indexing.org.id": str(org_id),
            },
        )


async def test_job_completion_metrics_match_returned_result(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    path = "src/helper.ts"
    tree = (_entry(path, _TS_CONTENT),)
    deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(tree=tree, content={path: _TS_CONTENT}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=_FakeIndexRepository(),
        commit_tracker=_FakeIndexCommitTracker(),
    )
    payload = _payload(org_id=org_id, repo_id=repo_id, added=[path])

    result = await run_indexing_job(payload, deps, job_id="job-telemetry-5")

    assert result.files_processed == 1
    assert result.files_failed == 0

    assert (
        _counter_value(in_memory_metric_reader, "review.indexing.files_processed_total")
        == result.files_processed
    )
    assert (
        _counter_value(in_memory_metric_reader, "review.indexing.files_skipped_total")
        == result.files_skipped
    )
    assert (
        _counter_value(in_memory_metric_reader, "review.indexing.files_failed_total")
        == result.files_failed
    )
    assert (
        _counter_value(in_memory_metric_reader, "review.indexing.chunks_created_total")
        == result.chunks_created
    )
    assert (
        _counter_value(in_memory_metric_reader, "review.indexing.edges_created_total")
        == result.edges_created
    )
    assert _histogram_sum(in_memory_metric_reader, "review.indexing.duration_ms") == pytest.approx(
        result.duration_seconds * 1000
    )


async def test_per_file_failure_is_isolated_and_counted_without_extra_spans(
    in_memory_span_exporter: InMemorySpanExporter,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """A per-file stage failure (a forced embedding failure here) is
    isolated per US10 — reflected only in the job-level
    ``files_failed_total`` counter, never as its own span (per this
    module's own "deliberately not one span per per-file sub-stage"
    design note)."""
    org_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    path = "src/helper.ts"
    tree = (_entry(path, _TS_CONTENT),)
    deps = IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(tree=tree, content={path: _TS_CONTENT}),
        embedding_client=FakeEmbeddingClient(fail_on=_chunk_texts(path, _TS_CONTENT)),
        index_repository=_FakeIndexRepository(),
        commit_tracker=_FakeIndexCommitTracker(),
    )
    payload = _payload(org_id=org_id, repo_id=repo_id, added=[path])

    result = await run_indexing_job(payload, deps, job_id="job-telemetry-6")

    assert result.files_failed == 1
    assert result.files_processed == 0

    names = {span.name for span in in_memory_span_exporter.get_finished_spans()}
    assert names == {
        "review.indexing.job.run",
        "review.indexing.stage.resolve_file_set",
        "review.indexing.stage.fetch_content",
        "review.indexing.stage.process_files",
        "review.indexing.stage.delete_removed_files",
        "review.indexing.stage.advance_cursor",
    }
    assert _counter_value(in_memory_metric_reader, "review.indexing.files_failed_total") == 1


# ---------------------------------------------------------------------------
# Clone-fingerprint backfill (docs/plans/plan-phase-10.md Phase 2): stage
# outcomes, the stamping rule, and the clone instruments. Fakes at the
# Protocol boundary, no I/O.
# ---------------------------------------------------------------------------

_C1 = "1" * 40
_C2 = "2" * 40


class _BackfillRepo:
    """One in-memory object playing ``IndexRepository``, ``IndexCommitTracker``
    and ``CloneBackfillTracker`` — the same single-instance role
    ``IndexingRepository`` plays in production. Failure injection is per path."""

    def __init__(
        self,
        *,
        version: int = 0,
        cursor: str | None = None,
        fail_upsert: frozenset[str] = frozenset(),
        fail_delete: frozenset[str] = frozenset(),
        supersede: bool = False,
    ) -> None:
        self.version = version
        self.cursor = cursor
        self.fail_upsert = fail_upsert
        self.fail_delete = fail_delete
        self.supersede = supersede
        self.clone_indexes: dict[str, object] = {}
        self.deleted: list[str] = []

    async def upsert_file(
        self,
        *,
        repo_id: uuid.UUID,
        pr_number: int,
        file_path: str,
        chunks: Sequence[object],
        embeddings: Sequence[object],
        nodes: Sequence[object],
        edges: Sequence[object],
        clone_index: object = None,
    ) -> None:
        if file_path in self.fail_upsert:
            raise IndexingRepositoryError(operation="upsert_file", detail="forced")
        self.clone_indexes[file_path] = clone_index

    async def delete_file(self, *, repo_id: uuid.UUID, pr_number: int, file_path: str) -> int:
        if file_path in self.fail_delete:
            raise IndexingRepositoryError(operation="delete_file", detail="forced")
        self.deleted.append(file_path)
        return 3

    async def get_last_indexed_commit(self, *, repo_id: uuid.UUID) -> str | None:
        return self.cursor

    async def set_last_indexed_commit(self, *, repo_id: uuid.UUID, commit_sha: str) -> None:
        self.cursor = commit_sha

    async def get_clone_fingerprint_version(self, *, repo_id: uuid.UUID) -> int:
        return self.version

    async def advance_cursor_with_clone_version(
        self,
        *,
        repo_id: uuid.UUID,
        commit_sha: str,
        expected_previous_commit: str | None,
        clone_version: int,
    ) -> bool:
        if self.supersede or self.cursor != expected_previous_commit:
            return False
        self.cursor = commit_sha
        self.version = clone_version
        return True


class _TwoCommitFetcher:
    """A ``RepoTreeFetcher`` fake with a different tree per commit."""

    def __init__(
        self,
        trees: dict[str, tuple[RepoTreeEntry, ...]],
        content: dict[str, bytes],
        *,
        fail_content: bool = False,
        unreachable: frozenset[str] = frozenset(),
    ) -> None:
        self._trees = trees
        self._content = content
        self._fail_content = fail_content
        self._unreachable = unreachable

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        if commit_sha in self._unreachable:
            raise RepoTreeFetchError(
                repo_full_name=repo_full_name, commit_sha=commit_sha, detail="forced"
            )
        return self._trees[commit_sha]

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        if self._fail_content:
            raise RepoTreeContentFetchError(repo_full_name=repo_full_name, detail="forced")
        return {
            e.path: self._content[e.path] for e in paths_with_blob_sha if e.path in self._content
        }


_BAD_UTF8 = b"\xff\xfe\x00\x01not valid utf-8"


def _ts(name: str) -> bytes:
    return f"function {name}(): void {{}}\n".encode()


def _backfill_world(
    *, extra: dict[str, bytes] | None = None, previous_extra: tuple[str, ...] = ()
) -> tuple[dict[str, bytes], _TwoCommitFetcherArgs]:
    content = {"a.ts": _ts("a"), "b.ts": _ts("b"), **(extra or {})}
    current = tuple(_entry(p, c) for p, c in content.items())
    previous = (*current, *(_entry(p, _ts("gone")) for p in previous_extra))
    return content, {_C1: previous, _C2: current}


_TwoCommitFetcherArgs = dict[str, tuple[RepoTreeEntry, ...]]


async def _run_backfill(
    repo: _BackfillRepo,
    fetcher: _TwoCommitFetcher,
    *,
    embedding: FakeEmbeddingClient | None = None,
    pr_number: int = 0,
    modified: Sequence[str] = (),
) -> pipeline_module.IndexingResult:
    deps = IndexingJobDependencies(
        tree_fetcher=fetcher,
        embedding_client=embedding or FakeEmbeddingClient(),
        index_repository=repo,
        commit_tracker=repo,
        clone_backfill=repo,
    )
    payload = _payload(org_id=uuid.uuid4(), repo_id=uuid.uuid4(), pr_number=pr_number)
    payload["commit_sha"] = _C2
    payload["modified"] = list(modified)
    return await run_indexing_job(payload, deps, job_id="job-backfill")


def _points(reader: InMemoryMetricReader, metric_name: str) -> dict[str | None, float]:
    """Every data point of a counter, keyed by its ``outcome`` attribute."""
    data = reader.get_metrics_data()
    assert data is not None
    out: dict[str | None, float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == metric_name:
                    assert isinstance(metric.data, Sum)
                    for point in metric.data.data_points:
                        outcome = (point.attributes or {}).get("outcome")
                        out[str(outcome) if outcome is not None else None] = point.value
    return out


async def test_stale_version_backfills_every_file_prunes_removed_and_stamps_after_completion(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    content, trees = _backfill_world(previous_extra=("gone.ts",))
    repo = _BackfillRepo(cursor=_C1)

    result = await _run_backfill(repo, _TwoCommitFetcher(trees, content))

    assert set(repo.clone_indexes) == {"a.ts", "b.ts"}
    assert all(index is not None for index in repo.clone_indexes.values())
    assert repo.deleted == ["gone.ts"]
    assert (repo.version, repo.cursor) == (1, _C2)
    assert result.clone_backfill_outcome == "complete"
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_total") == {
        "complete": 1
    }
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_dropped_total") == {
        None: 0
    }
    assert _points(in_memory_metric_reader, "review.indexing.clone_fingerprints_deleted_total") == {
        None: 3
    }
    written = _points(in_memory_metric_reader, "review.indexing.clone_fingerprints_written_total")
    assert written == {None: result.fingerprints_written}


async def test_backfilled_file_without_content_counts_as_a_drop_not_a_clean_complete(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    content, trees = _backfill_world()
    del content["b.ts"]  # in the tree, but the fetcher returns nothing for it

    result = await _run_backfill(_BackfillRepo(cursor=_C1), _TwoCommitFetcher(trees, content))

    assert result.clone_backfill_outcome == "complete_with_drops"
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_dropped_total") == {
        None: 1
    }


async def test_current_version_runs_no_backfill() -> None:
    content, trees = _backfill_world()
    repo = _BackfillRepo(version=1, cursor=_C1)

    result = await _run_backfill(repo, _TwoCommitFetcher(trees, content), modified=["a.ts"])

    assert result.clone_backfill_outcome is None
    assert result.used_full_tree_fallback is False
    assert set(repo.clone_indexes) == {"a.ts"}
    assert repo.version == 1


async def test_pull_request_job_never_backfills_even_when_version_is_stale() -> None:
    content, trees = _backfill_world()
    repo = _BackfillRepo(cursor=_C1)

    result = await _run_backfill(repo, _TwoCommitFetcher(trees, content), pr_number=9)

    assert result.clone_backfill_outcome is None
    assert (repo.version, repo.cursor) == (0, _C1)


async def test_never_indexed_repo_backfills_everything_without_error() -> None:
    content, trees = _backfill_world()
    repo = _BackfillRepo(cursor=None)

    result = await _run_backfill(repo, _TwoCommitFetcher(trees, content))

    assert set(repo.clone_indexes) == {"a.ts", "b.ts"}
    assert result.clone_backfill_outcome == "complete"
    assert (repo.version, repo.cursor) == (1, _C2)


async def test_full_run_ceiling_drops_are_logged_counted_and_still_stamp(
    in_memory_metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # alembic's fileConfig (an earlier migration test) disables existing loggers.
    logging.getLogger("indexing.pipeline").disabled = False
    monkeypatch.setattr(pipeline_module, "FULL_RUN_FILE_CEILING", 1)
    content, trees = _backfill_world()
    repo = _BackfillRepo(cursor=_C1)

    with caplog.at_level("WARNING", logger="indexing.pipeline"):
        result = await _run_backfill(repo, _TwoCommitFetcher(trees, content))

    assert set(repo.clone_indexes) == {"a.ts"}  # sorted, so the drop is reproducible
    assert result.files_skipped == 1
    assert result.clone_backfill_outcome == "complete_with_drops"
    assert repo.version == 1
    assert any("ceiling exceeded" in r.getMessage() for r in caplog.records)
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_total") == {
        "complete_with_drops": 1
    }
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_dropped_total") == {
        None: 1
    }


@pytest.mark.parametrize("failure", ["fetch", "upsert", "delete_removed", "delete_symlink"])
async def test_transient_failure_leaves_version_unstamped_but_advances_cursor(
    failure: str, in_memory_metric_reader: InMemoryMetricReader
) -> None:
    content, trees = _backfill_world(previous_extra=("gone.ts",))
    repo = _BackfillRepo(cursor=_C1)
    if failure == "upsert":
        repo.fail_upsert = frozenset({"b.ts"})
    elif failure == "delete_removed":
        repo.fail_delete = frozenset({"gone.ts"})
    elif failure == "delete_symlink":
        link = RepoTreeEntry(path="link.ts", mode="120000", blob_sha="l", size=1)
        trees = {c: (*t, link) for c, t in trees.items()}
        repo.fail_delete = frozenset({"link.ts"})

    result = await _run_backfill(
        repo, _TwoCommitFetcher(trees, content, fail_content=failure == "fetch")
    )

    assert result.clone_backfill_outcome == "incomplete"
    assert repo.version == 0
    assert repo.cursor == _C2
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_total") == {
        "incomplete": 1
    }


@pytest.mark.parametrize("failure", ["chunk", "graph", "clone", "embed"])
async def test_deterministic_failures_do_not_block_the_stamp(
    failure: str,
    in_memory_metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content, trees = _backfill_world(
        extra={"bad.ts": _BAD_UTF8 if failure == "chunk" else _ts("x")}
    )
    trees = {
        c: (*(t for t in ts if t.path != "bad.ts"), _entry("bad.ts", content["bad.ts"]))
        for c, ts in trees.items()
    }
    embedding = FakeEmbeddingClient()
    if failure == "graph":
        real_graph = extract_graph

        def _graph(language: object, path: str, tree: object) -> object:
            if path == "bad.ts":
                raise GraphExtractionError(file_path=path, language="typescript", detail="forced")
            return real_graph(language, path, tree)  # type: ignore[arg-type]

        monkeypatch.setattr(pipeline_module, "extract_graph", _graph)
    elif failure == "clone":
        real_clone = build_clone_index

        def _clone(language: object, path: str, tree: object) -> object:
            if path == "bad.ts":
                raise CloneIndexError(file_path=path, language="typescript", detail="forced")
            return real_clone(language, path, tree)  # type: ignore[arg-type]

        monkeypatch.setattr(pipeline_module, "build_clone_index", _clone)
    elif failure == "embed":
        embedding = FakeEmbeddingClient(fail_on=_chunk_texts("bad.ts", content["bad.ts"]))
    repo = _BackfillRepo(cursor=_C1)

    result = await _run_backfill(repo, _TwoCommitFetcher(trees, content), embedding=embedding)

    assert result.clone_backfill_outcome == "complete_with_drops"
    assert (repo.version, repo.cursor) == (1, _C2)
    assert {"a.ts", "b.ts"} <= set(repo.clone_indexes)
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_total") == {
        "complete_with_drops": 1
    }
    if failure == "clone":
        # The file keeps its chunks and graph; only its clone rows are cleared.
        assert repo.clone_indexes["bad.ts"] is None
        assert result.clone_stage_failures == 1
        assert _points(in_memory_metric_reader, "review.indexing.clone_stage_failed_total") == {
            None: 1
        }
    else:
        assert "bad.ts" not in repo.clone_indexes


async def test_cursor_advanced_by_another_job_is_superseded_and_not_stamped(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    content, trees = _backfill_world()
    repo = _BackfillRepo(cursor=_C1, supersede=True)

    result = await _run_backfill(repo, _TwoCommitFetcher(trees, content))

    assert result.clone_backfill_outcome == "superseded"
    assert (repo.version, repo.cursor) == (0, _C1)
    assert _points(in_memory_metric_reader, "review.indexing.clone_backfill_total") == {
        "superseded": 1
    }


async def test_previous_tree_fetch_failure_is_job_fatal_and_leaves_version_unstamped() -> None:
    content, trees = _backfill_world()
    repo = _BackfillRepo(cursor=_C1)

    with pytest.raises(IndexingJobFailedError):
        await _run_backfill(repo, _TwoCommitFetcher(trees, content, unreachable=frozenset({_C1})))

    assert (repo.version, repo.cursor) == (0, _C1)


@pytest.mark.parametrize(
    ("total", "failing", "expect_error_log"),
    [(5, 1, False), (5, 2, True), (20, 4, False), (20, 5, True)],
)
async def test_clone_stage_failure_ratio_above_threshold_logs_at_error(
    total: int,
    failing: int,
    expect_error_log: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    names = [f"f{i}.ts" for i in range(total)]
    content = {n: _ts(n.removesuffix(".ts")) for n in names}
    trees = {"a" * 40: tuple(_entry(n, c) for n, c in content.items())}
    bad = set(names[:failing])
    real_clone = build_clone_index

    def _clone(language: object, path: str, tree: object) -> object:
        if path in bad:
            raise CloneIndexError(file_path=path, language="typescript", detail="forced")
        return real_clone(language, path, tree)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_module, "build_clone_index", _clone)
    repo = _BackfillRepo(version=1)

    # alembic's fileConfig (an earlier migration test) disables existing loggers.
    logging.getLogger("indexing.pipeline").disabled = False
    with caplog.at_level("INFO", logger="indexing.pipeline"):
        deps = IndexingJobDependencies(
            tree_fetcher=_TwoCommitFetcher(trees, content),
            embedding_client=FakeEmbeddingClient(),
            index_repository=repo,
            commit_tracker=repo,
            clone_backfill=repo,
        )
        payload = _payload(org_id=uuid.uuid4(), repo_id=uuid.uuid4(), added=names)
        await run_indexing_job(payload, deps, job_id="job-ratio")

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert bool(errors) is expect_error_log


@pytest.mark.parametrize("stage", ["chunk", "graph", "embed", "upsert", "clone", "ok"])
async def test_file_helper_returns_the_failed_stage(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _BAD_UTF8 if stage == "chunk" else _ts("a")
    embedding = FakeEmbeddingClient()
    repo = _BackfillRepo(fail_upsert=frozenset({"a.ts"}) if stage == "upsert" else frozenset())
    if stage == "graph":

        def _graph(language: object, path: str, tree: object) -> object:
            raise GraphExtractionError(file_path=path, language="typescript", detail="forced")

        monkeypatch.setattr(pipeline_module, "extract_graph", _graph)
    elif stage == "clone":

        def _clone(language: object, path: str, tree: object) -> object:
            raise CloneIndexError(file_path=path, language="typescript", detail="forced")

        monkeypatch.setattr(pipeline_module, "build_clone_index", _clone)
    elif stage == "embed":
        embedding = FakeEmbeddingClient(fail_on=_chunk_texts("a.ts", content))
    deps = IndexingJobDependencies(
        tree_fetcher=_TwoCommitFetcher({}, {}),
        embedding_client=embedding,
        index_repository=repo,
        commit_tracker=repo,
    )
    parsed = pipeline_module.IndexingJobPayload.model_validate(
        _payload(org_id=uuid.uuid4(), repo_id=uuid.uuid4())
    )

    outcome = await pipeline_module._process_one_added_or_modified_file(
        "a.ts", "typescript", content, parsed=parsed, deps=deps, job_id="j"
    )

    if stage in ("chunk", "graph", "embed", "upsert"):
        assert outcome.failed_stage == stage
    else:
        assert outcome.failed_stage is None
        assert outcome.clone_failed is (stage == "clone")
        # A CloneIndexError file still gets its chunks written, with no clone data.
        assert "a.ts" in repo.clone_indexes
        assert (repo.clone_indexes["a.ts"] is None) is (stage == "clone")
