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
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.pipeline import IndexingJobDependencies, IndexingJobFailedError, run_indexing_job
from indexing.repository import IndexingRepositoryError
from indexing.tree import RepoTreeEntry

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
    ) -> None:
        self.upsert_calls.append(file_path)

    async def delete_file(self, *, repo_id: uuid.UUID, pr_number: int, file_path: str) -> None:
        self.delete_calls.append(file_path)
        if self.fail_delete:
            raise IndexingRepositoryError(operation="delete_file", detail="forced failure")


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
