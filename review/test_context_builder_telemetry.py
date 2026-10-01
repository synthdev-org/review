"""Unit tests for US6's Context Builder outcome telemetry
(docs/specs/spec-observability.md's METRIC & SPAN CATALOG), recorded
directly inside ``review/context_builder.py``'s ``ContextBuilderStep.run``
at the same point as its existing ``context_builder completed``/
``degraded`` log line.

A separate file rather than an extension of the co-located
``review/test_context_builder_step.py``, mirroring
``review/test_output_telemetry.py``'s identical precedent for US4: that
file is entirely ``pytestmark = pytest.mark.integration`` (real Postgres/
pgvector via testcontainers, a real local embedding model) at module
level, so a genuinely I/O-free Unit-tier test (OTel's own in-memory metric
reader, per testing.md's Unit tier) cannot live inside it without also
inheriting that mark. ``GraphQuery``/``SimilarityQuery``/``EmbeddingClient``
are Protocols this module already fully controls as fakes, so the "ok"
outcome (real items reaching the bundle) is reachable without any real
adapter — only ``DiffFetcher``/``RepoTreeFetcher`` supply real (in-memory)
byte content, reusing the same TypeScript fixture
``review/test_context_builder_step.py`` already proved resolves a real
changed-function symbol via the real (pure, no-I/O) tree-sitter chunker.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from indexing.chunk import Chunk
from indexing.embedding import EmbeddingVector
from indexing.tree import RepoTreeEntry
from ingestion.diff import Diff, DiffContentUnavailableError, DiffFetcher, DiffFile
from review.context_builder import CallerHit, ContextBuilderStep, GraphQuery, SimilarityQuery
from review.context_bundle import serialized_item_size
from review.step import StepContext

pytestmark = pytest.mark.asyncio

_METRIC_NAMES = (
    "review.context_builder.outcome_total",
    "review.context_builder.duration_ms",
    "review.context_builder.item_count",
    "review.context_builder.bytes",
)

# --- Fixture content (a TS function whose body changes, reused unchanged
# from review/test_context_builder_step.py's own proven fixture — see that
# file's `_APP_PATCH` docstring comment for why the padding lines exist:
# `assemble_context_bundle`'s proportional-to-diff byte budget would
# otherwise truncate every real hit away for a diff this trivially small).

_APP_PATH = "src/app.ts"
_APP_PRE = b"""\
import { Logger } from "./logger";

export function add(a: number, b: number): number {
  return a + b;
}
"""
_APP_POST_MODIFIED = b"""\
import { Logger } from "./logger";

export function add(a: number, b: number): number {
  return a + b + 1;
}
"""
_APP_PATCH = "@@ -4,1 +4,1 @@\n-  return a + b;\n+  return a + b + 1;\n" + (
    " // padding line to keep this fixture's diff comfortably above the byte budget\n" * 40
)


def _diff() -> Diff:
    return Diff(files=(DiffFile(path=_APP_PATH, status="modified", patch=_APP_PATCH),))


def _context() -> StepContext:
    return StepContext(job_id="job-1")


class _UpstreamOutput:
    """Structurally mirrors ``review.static_analysis.StaticAnalysisOutput``
    closely enough for ``ContextBuilderStep``'s own
    ``_ContextBuilderInput.model_validate(..., from_attributes=True)`` to
    read it — see ``review/test_context_builder_step.py``'s identical
    fake for the same pattern."""

    def __init__(self, diff: Diff) -> None:
        self.diff = diff
        self.findings: list[object] = []
        self.org_id = "org-1"
        self.repo_id = "repo-1"
        self.installation_id = 1
        self.repo_full_name = "acme/widgets"
        self.pr_number = 42
        self.head_sha = "head-sha"
        self.base_sha = "base-sha"


# --- Fakes at the DiffFetcher/RepoTreeFetcher/GraphQuery/SimilarityQuery/
# EmbeddingClient Protocol boundaries — no real I/O anywhere. ---------------


class _FakeDiffFetcher:
    def __init__(
        self, post_content: dict[str, bytes] | None = None, *, error: Exception | None = None
    ) -> None:
        self._post_content = post_content or {}
        self._error = error

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
        if self._error is not None:
            raise self._error
        return {path: content for path, content in self._post_content.items() if path in paths}


class _FakeTreeFetcher:
    def __init__(self, pre_content: dict[str, bytes] | None = None) -> None:
        self._pre_content = pre_content or {}

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
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
        wanted = {entry.path for entry in paths_with_blob_sha}
        return {path: content for path, content in self._pre_content.items() if path in wanted}


class _NonEmptyGraphQuery:
    """Returns one real ``CallerHit`` per resolved symbol name — proves the
    "ok" outcome without needing real Postgres, since ``GraphQuery`` is a
    Protocol this test fully controls."""

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        return {
            name: (CallerHit(file_path="src/caller.ts", symbol_name="bar", content="add(1, 2);"),)
            for name in symbol_names
        }


class _EmptyGraphQuery:
    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        return dict.fromkeys(symbol_names, ())


class _NonEmptySimilarityQuery:
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
                content="similar chunk",
                chunk_index=0,
            ),
        )


class _EmptySimilarityQuery:
    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        return ()


class _ZeroVectorEmbeddingClient:
    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        return tuple(EmbeddingVector(values=(0.0,) * 8, dimension=8) for _ in texts)


def _step(
    *, graph_query: GraphQuery, similarity_query: SimilarityQuery, diff_fetcher: DiffFetcher
) -> ContextBuilderStep:
    return ContextBuilderStep(
        diff_fetcher,
        _FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        graph_query,
        similarity_query,
        _ZeroVectorEmbeddingClient(),
        internal_timeout_seconds=25.0,
        max_total_bytes=200_000,
    )


# --- Shared in-memory metric reader fixture, per
# adapter/telemetry/test_heartbeat.py's established pattern (module-level
# counters/histograms are created once at import time, against whatever
# `MeterProvider` was global then — this fixture registers a fresh one as
# the global provider, which OTel's proxy-instrument mechanism then routes
# through to). -----------------------------------------------------------


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


def _recorded(reader: InMemoryMetricReader) -> dict[str, tuple[float, dict[str, object]]]:
    """Maps each of this step's own four metric names to its
    ``(value, attributes)`` pair — the counter's total, or the histogram's
    single recorded data point's sum (each test records exactly once)."""
    found: dict[str, tuple[float, dict[str, object]]] = {}
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name not in _METRIC_NAMES:
                    continue
                if isinstance(metric.data, Sum):
                    (sum_point,) = metric.data.data_points
                    found[metric.name] = (sum_point.value, dict(sum_point.attributes or {}))
                else:
                    assert isinstance(metric.data, Histogram)
                    (hist_point,) = metric.data.data_points
                    found[metric.name] = (hist_point.sum, dict(hist_point.attributes or {}))
    return found


# ---------------------------------------------------------------------------
# US6's own Given/When/Then, per outcome.
# ---------------------------------------------------------------------------


async def test_ok_outcome_records_all_four_metrics_tagged_ok(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    step = _step(
        graph_query=_NonEmptyGraphQuery(),
        similarity_query=_NonEmptySimilarityQuery(),
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
    )

    output = await step.run(_UpstreamOutput(_diff()), _context())

    assert output.context_bundle.items != ()
    item_count = len(output.context_bundle.items)
    total_bytes = sum(serialized_item_size(item) for item in output.context_bundle.items)

    found = _recorded(in_memory_metric_reader)
    assert set(found) == set(_METRIC_NAMES)
    for _value, attributes in found.values():
        assert attributes == {"outcome": "ok"}
    assert found["review.context_builder.outcome_total"][0] == 1
    assert found["review.context_builder.item_count"][0] == item_count
    assert found["review.context_builder.bytes"][0] == total_bytes
    assert found["review.context_builder.duration_ms"][0] >= 0


async def test_degraded_empty_outcome_records_zero_item_count_and_bytes(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    step = _step(
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
    )

    output = await step.run(_UpstreamOutput(_diff()), _context())

    assert output.context_bundle.items == ()
    found = _recorded(in_memory_metric_reader)
    assert set(found) == set(_METRIC_NAMES)
    for _value, attributes in found.values():
        assert attributes == {"outcome": "degraded_empty"}
    assert found["review.context_builder.outcome_total"][0] == 1
    assert found["review.context_builder.item_count"][0] == 0
    assert found["review.context_builder.bytes"][0] == 0


async def test_degraded_error_outcome_records_zero_item_count_and_bytes(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    step = _step(
        graph_query=_EmptyGraphQuery(),
        similarity_query=_EmptySimilarityQuery(),
        diff_fetcher=_FakeDiffFetcher(
            error=DiffContentUnavailableError(repo_full_name="acme/widgets", detail="boom")
        ),
    )

    output = await step.run(_UpstreamOutput(_diff()), _context())

    assert output.context_bundle.items == ()
    found = _recorded(in_memory_metric_reader)
    assert set(found) == set(_METRIC_NAMES)
    for _value, attributes in found.values():
        assert attributes == {"outcome": "degraded_error"}
    assert found["review.context_builder.outcome_total"][0] == 1
    assert found["review.context_builder.item_count"][0] == 0
    assert found["review.context_builder.bytes"][0] == 0


async def test_no_metric_attribute_ever_carries_anything_beyond_the_outcome_literal(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Privacy NFR: every emitted attribute set across all three outcomes
    is exactly ``{"outcome": <one of the three bounded literals>}`` —
    never source code, diff content, or finding free text, and never an
    unbounded-cardinality value."""
    step = _step(
        graph_query=_NonEmptyGraphQuery(),
        similarity_query=_NonEmptySimilarityQuery(),
        diff_fetcher=_FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
    )

    await step.run(_UpstreamOutput(_diff()), _context())

    found = _recorded(in_memory_metric_reader)
    for _value, attributes in found.values():
        assert set(attributes) == {"outcome"}
        assert attributes["outcome"] in {"ok", "degraded_empty", "degraded_error"}
