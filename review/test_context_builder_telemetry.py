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

import asyncio
import logging
import time
from collections.abc import Iterator, Sequence

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import review.context_builder as context_builder_module
from findings.redact import NoOpTextRedactor, TextRedactor
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingVector
from indexing.tree import RepoTreeEntry
from ingestion.diff import Diff, DiffContentUnavailableError, DiffFetcher, DiffFile
from review.context_builder import CallerHit, ContextBuilderStep, GraphQuery, SimilarityQuery
from review.context_bundle import (
    render_context_bundle_section,
    render_product_knowledge_section,
    serialized_item_size,
)
from review.pipeline import FailurePolicy, StepRegistry
from review.product_knowledge import (
    EffectiveProvider,
    NegotiationResult,
    ProductKnowledgeError,
    ProviderOutcome,
)
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


# ---------------------------------------------------------------------------
# Product-knowledge provider branch (plan Phase 7, US-PK14): span + metric
# isolation, at Unit tier against fakes at the three provider Protocols.
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(
        self,
        *,
        fail_with: ProviderOutcome | None = None,
        status_code: int | None = None,
        hang_fetch: bool = False,
        chosen_version: str | None = "1",
        deprecated: bool = False,
        negotiate_delay: float = 0.0,
    ) -> None:
        self._chosen_version = chosen_version
        self._deprecated = deprecated
        self._negotiate_delay = negotiate_delay
        self._fail_with = fail_with
        self._status_code = status_code
        self._hang_fetch = hang_fetch
        self.queries: list[str] = []

    async def negotiate(self) -> NegotiationResult:
        if self._negotiate_delay:
            await asyncio.sleep(self._negotiate_delay)
        return NegotiationResult(
            chosen_version=self._chosen_version,
            supported_versions=[self._chosen_version] if self._chosen_version else [],
            tools_present=[],
            deprecated=self._deprecated,
        )

    async def search(self, query: str) -> object:
        self.queries.append(query)
        if self._fail_with is not None:
            raise ProductKnowledgeError(self._fail_with, status_code=self._status_code)
        return {
            "results": [
                {
                    "id": "DOC-1",
                    "title": "Billing retry design",
                    "snippet": "How retries work.",
                    "source_type": "wiki",
                    "url": "https://wiki.example.com/secret/path",
                }
            ]
        }

    async def fetch(self, item_id: str) -> object:
        if self._hang_fetch:
            await asyncio.sleep(3600)
        return {"content": "Full text of the billing retry design."}


class _FakeResolver:
    def __init__(self, prefixes: tuple[str, ...] = (), *, configured: bool = True) -> None:
        self._prefixes = prefixes
        self._configured = configured

    async def resolve(self, org_id: str, repo_id: str) -> EffectiveProvider | None:
        if not self._configured:
            return None
        return EffectiveProvider(
            display_name="Company Wiki",
            endpoint_url="https://wiki.example.com/mcp",
            issue_key_prefixes=self._prefixes,
        )


class _IdentityRedactor:
    async def redact(self, text: str) -> str:
        return text


def _pk_step(
    provider: _FakeProvider,
    *,
    prefixes: tuple[str, ...] = (),
    deadline: float = 10.0,
    configured: bool = True,
    redactor: TextRedactor | None = None,
    wire_opener: bool = True,
) -> ContextBuilderStep:
    redactor = redactor if redactor is not None else _IdentityRedactor()
    return ContextBuilderStep(
        _FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        _FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        _NonEmptyGraphQuery(),
        _NonEmptySimilarityQuery(),
        _ZeroVectorEmbeddingClient(),
        internal_timeout_seconds=25.0,
        max_total_bytes=200_000,
        provider_resolver=_FakeResolver(prefixes, configured=configured),
        provider_deadline_seconds=deadline,
        provider_opener=(lambda endpoint, ref: provider) if wire_opener else None,
        provider_redactor=redactor,
    )


def _pk_upstream() -> _UpstreamOutput:
    upstream = _UpstreamOutput(_diff())
    upstream.pr_title = "Fix SECRET-TITLE-TEXT"  # type: ignore[attr-defined]
    upstream.pr_issue_keys = ("PROJ-9",)  # type: ignore[attr-defined]
    upstream.pr_is_fork = False  # type: ignore[attr-defined]
    upstream.private = True  # type: ignore[attr-defined]
    return upstream


@pytest.fixture
def in_memory_span_exporter(
    reset_otel_global_providers: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[InMemorySpanExporter]:
    # `ProxyTracer` caches its resolved tracer permanently; reset it so this
    # test's provider is picked up (same approach as review/test_step_registry.py).
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace

    trace.set_tracer_provider(provider)
    monkeypatch.setattr(context_builder_module._pk_tracer, "_real_tracer", None, raising=False)
    yield exporter
    provider.shutdown()


async def test_provider_span_carries_only_the_allow_listed_attributes(
    in_memory_span_exporter: InMemorySpanExporter,
) -> None:
    provider = _FakeProvider()
    output = await _pk_step(provider).run(_pk_upstream(), _context())

    assert output.context_bundle.product_knowledge is not None
    spans = [
        s
        for s in in_memory_span_exporter.get_finished_spans()
        if s.name == "review.product_knowledge"
    ]
    assert len(spans) == 1
    attributes = dict(spans[0].attributes or {})
    assert set(attributes) == {"outcome", "item_count", "contract_version", "duration_ms"}
    assert attributes["outcome"] == "ok"
    assert attributes["item_count"] == 1
    assert attributes["contract_version"] == "1"
    blob = repr(attributes)
    for forbidden in ("SECRET-TITLE-TEXT", "wiki.example.com", "Billing retry", "PROJ-9"):
        assert forbidden not in blob
    assert provider.queries == ["Fix SECRET-TITLE-TEXT PROJ-9"]


async def test_provider_failure_leaves_the_four_code_metrics_unchanged(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    step = _pk_step(_FakeProvider(fail_with=ProviderOutcome.TIMEOUT))

    output = await step.run(_pk_upstream(), _context())

    assert output.context_bundle.product_knowledge is None
    assert output.context_bundle.items != ()
    found = _recorded(in_memory_metric_reader)
    assert set(found) == set(_METRIC_NAMES)
    for _value, attributes in found.values():
        assert attributes == {"outcome": "ok"}
    assert found["review.context_builder.outcome_total"][0] == 1
    assert found["review.context_builder.item_count"][0] == len(output.context_bundle.items)


async def test_issue_key_allow_list_sends_only_matching_keys_and_no_title_text() -> None:
    provider = _FakeProvider()
    upstream = _pk_upstream()
    upstream.pr_title = "Fix HR-77 login #12 for PROJ-9"  # type: ignore[attr-defined]
    upstream.pr_issue_keys = ("HR-77", "#12", "PROJ-9")  # type: ignore[attr-defined]

    await _pk_step(provider, prefixes=("PROJ",)).run(upstream, _context())

    assert provider.queries == ["PROJ-9"]


async def test_issue_key_allow_list_without_a_matching_key_makes_no_request() -> None:
    provider = _FakeProvider()
    upstream = _pk_upstream()
    upstream.pr_title = "Executive salary bands 2026"  # type: ignore[attr-defined]
    upstream.pr_issue_keys = ()  # type: ignore[attr-defined]

    output = await _pk_step(provider, prefixes=("PROJ",)).run(upstream, _context())

    assert provider.queries == []
    assert output.context_bundle.product_knowledge is None


async def test_hanging_fetch_keeps_the_search_snippet_instead_of_losing_the_branch() -> None:
    provider = _FakeProvider(hang_fetch=True)

    start = time.monotonic()
    output = await _pk_step(provider, deadline=1.0).run(_pk_upstream(), _context())

    assert time.monotonic() - start < 3.0
    section = output.context_bundle.product_knowledge
    assert section is not None
    assert "How retries work." in section.items[0].model_dump_json()


@pytest.mark.parametrize(("status", "expected"), [(None, None), (503, 503)])
async def test_job_log_line_carries_status_code(
    caplog: pytest.LogCaptureFixture, status: int | None, expected: int | None
) -> None:
    # Other tests' logging config can leave this logger disabled.
    logging.getLogger("review.context_builder").disabled = False
    caplog.set_level(logging.DEBUG, logger="review.context_builder")
    provider = _FakeProvider(
        fail_with=ProviderOutcome.PROVIDER_ERROR if status else None, status_code=status
    )

    await _pk_step(provider).run(_pk_upstream(), _context())

    records = [r for r in caplog.records if getattr(r, "operation", None) == "job"]
    assert len(records) == 1
    assert hasattr(records[0], "status_code")
    assert records[0].status_code == expected


# --- Phase 7 audit gaps: log levels, contract_version label, span parentage,
# prompt equality, deprecated-record duration. -------------------------------

_SUMMARY_MESSAGES = {
    "product_knowledge completed",
    "product_knowledge degraded",
    "product_knowledge not configured",
}


def _job_summary(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = [r for r in caplog.records if r.getMessage() in _SUMMARY_MESSAGES]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    return records[0]


def _pk_points(reader: InMemoryMetricReader, name: str) -> list[tuple[dict[str, object], float]]:
    """
    Return every data point of one ``review.product_knowledge.*`` instrument.

    Each is ``(attributes, value-or-sum)``.
    """
    data = reader.get_metrics_data()
    assert data is not None
    points: list[tuple[dict[str, object], float]] = []
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value = point.value if isinstance(metric.data, Sum) else point.sum
                    points.append((dict(point.attributes or {}), value))
    return points


_LEVEL_CASES: list[tuple[str, dict[str, object], int]] = [
    # config-class outcomes: an operator must act, so warning.
    (
        "credential_unavailable",
        {"fail_with": ProviderOutcome.CREDENTIAL_UNAVAILABLE},
        logging.WARNING,
    ),
    ("blocked_egress", {"fail_with": ProviderOutcome.BLOCKED_EGRESS}, logging.WARNING),
    ("auth_failed", {"fail_with": ProviderOutcome.AUTH_FAILED}, logging.WARNING),
    ("unsupported_version", {"chosen_version": "9"}, logging.WARNING),
    ("redaction_disabled", {"noop_redactor": True}, logging.WARNING),
    # transient / expected outcomes: info.
    ("timeout", {"fail_with": ProviderOutcome.TIMEOUT}, logging.INFO),
    ("unreachable", {"fail_with": ProviderOutcome.UNREACHABLE}, logging.INFO),
    ("provider_error", {"fail_with": ProviderOutcome.PROVIDER_ERROR}, logging.INFO),
    ("malformed", {"fail_with": ProviderOutcome.MALFORMED}, logging.INFO),
    ("oversized", {"fail_with": ProviderOutcome.OVERSIZED}, logging.INFO),
    ("disabled", {"no_opener": True}, logging.INFO),
    ("ok", {}, logging.INFO),
    # M8: not_configured is the steady state for most repos; debug only
    # (documented in docs/reference/operations.md).
    ("not_configured", {"unconfigured": True}, logging.DEBUG),
]


@pytest.mark.parametrize(
    ("expected", "options", "level"), _LEVEL_CASES, ids=[c[0] for c in _LEVEL_CASES]
)
async def test_job_log_level_per_outcome_class(
    caplog: pytest.LogCaptureFixture, expected: str, options: dict[str, object], level: int
) -> None:
    logging.getLogger("review.context_builder").disabled = False
    caplog.set_level(logging.DEBUG, logger="review.context_builder")
    provider = _FakeProvider(
        fail_with=options.get("fail_with"),  # type: ignore[arg-type]
        chosen_version=str(options.get("chosen_version", "1")),
    )
    step = _pk_step(
        provider,
        configured=not options.get("unconfigured", False),
        redactor=NoOpTextRedactor() if options.get("noop_redactor") else None,
        wire_opener=not options.get("no_opener", False),
    )

    await step.run(_pk_upstream(), _context())

    record = _job_summary(caplog)
    assert record.outcome == expected  # type: ignore[attr-defined]
    assert record.levelno == level


async def test_bad_version_provider_is_labelled_unsupported_on_the_metrics(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    await _pk_step(_FakeProvider(chosen_version="9")).run(_pk_upstream(), _context())

    points = _pk_points(in_memory_metric_reader, "review.product_knowledge.call_total")
    # The contract call itself is recorded before a version is chosen ("none");
    # the job-level point carries the negotiated verdict.
    job = [a for a, _ in points if a["operation"] == "job"]
    assert [(a["outcome"], a["contract_version"]) for a in job] == [
        ("unsupported_version", "unsupported")
    ]


async def test_deprecated_record_carries_the_real_contract_call_duration(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    provider = _FakeProvider(deprecated=True, negotiate_delay=0.05)

    await _pk_step(provider).run(_pk_upstream(), _context())

    points = _pk_points(in_memory_metric_reader, "review.product_knowledge.call_duration_ms")
    (deprecated_ms,) = [
        v
        for a, v in points
        if a["operation"] == "contract" and a["outcome"] == "deprecated_version"
    ]
    assert deprecated_ms >= 40


async def test_provider_span_is_a_child_of_the_context_builder_step_span(
    in_memory_span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    import review.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module.tracer, "_real_tracer", None, raising=False)
    step = _pk_step(_FakeProvider())

    class _Seed:
        name = "static_analysis"
        depends_on: tuple[str, ...] = ()

        async def run(self, step_input: object, context: StepContext) -> _UpstreamOutput:
            return _pk_upstream()

    registry = StepRegistry(
        [_Seed(), step],
        enabled={"static_analysis": True, "context_builder": True},
        timeouts={"static_analysis": 5, "context_builder": 30},
        on_failure=FailurePolicy.FATAL,
    )

    await registry.run(seed_input="seed", context=_context())

    spans = {s.name: s for s in in_memory_span_exporter.get_finished_spans()}
    parent = spans["review.step.context_builder"]
    child = spans["review.product_knowledge"]
    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id


async def test_prompt_sections_are_identical_with_and_without_provider_config() -> None:
    """
    US-PK4: an unconfigured or disabled provider changes nothing the LLM sees.

    A configured one adds exactly the provider section.
    """
    bare = ContextBuilderStep(
        _FakeDiffFetcher({_APP_PATH: _APP_POST_MODIFIED}),
        _FakeTreeFetcher({_APP_PATH: _APP_PRE}),
        _NonEmptyGraphQuery(),
        _NonEmptySimilarityQuery(),
        _ZeroVectorEmbeddingClient(),
        internal_timeout_seconds=25.0,
        max_total_bytes=200_000,
    )
    baseline = (await bare.run(_pk_upstream(), _context())).context_bundle
    unconfigured = (
        await _pk_step(_FakeProvider(), configured=False).run(_pk_upstream(), _context())
    ).context_bundle
    configured = (await _pk_step(_FakeProvider()).run(_pk_upstream(), _context())).context_bundle

    def rendered(bundle: object) -> str:
        return render_context_bundle_section(bundle) + render_product_knowledge_section(bundle)  # type: ignore[arg-type]

    assert rendered(unconfigured) == rendered(baseline)
    assert render_product_knowledge_section(baseline) == ""
    assert render_context_bundle_section(configured) == render_context_bundle_section(baseline)
    assert rendered(configured) == rendered(baseline) + render_product_knowledge_section(configured)
    assert render_product_knowledge_section(configured) != ""
