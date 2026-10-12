"""review.context_builder — the Context Builder's domain-defined query
boundaries, per docs/plans/plan-deliver-context-builder.md's Phase 2 section
(Unit B: the blast-radius graph query), Phase 3 section (Unit C: the
semantic similarity query), and docs/specs/spec-context-builder.md
(US2/US3/US4).

**Placement, per Assumption 3:** ``GraphQuery`` and ``SimilarityQuery`` are
defined here, at their one real consumer, rather than in a new
``indexing/query.py`` module — an earlier draft of the delivery plan cited
[ADR-0006](../docs/adr/0006-protocol-defined-at-data-model.md)'s
"alongside the data model" exception, which that ADR's own text says does
*not* apply to a Protocol with exactly one real consumer. Both Protocols
follow the ordinary "defined at the consumer" rule instead, matching this
codebase's existing single-implementation-Protocol precedent
(``ingestion.diff.DiffFetcher``, ``indexing.embedding.EmbeddingClient``,
``review.output.FindingsPublisher``).

**Cross-bounded-context import, per Assumption 4:** ``GraphQuery`` itself
imports no ``indexing`` types — it returns this module's own ``CallerHit``
model, not an ``indexing.chunk.Chunk``. ``SimilarityQuery`` (Phase 3) is
different, deliberately: the plan's own signature for ``find_similar``
returns ``tuple[indexing.chunk.Chunk, ...]`` and takes an
``indexing.embedding.EmbeddingVector`` as its query vector, so this module
does import both directly — the same ordinary cross-bounded-context shape
``review/context_symbols.py`` already has on ``indexing.chunk``/
``indexing.tree``, and ``review/review_generation.py`` has on ``llmgateway.models``,
per Assumption 4's own reasoning. Not a violation of architecture.md: both
``indexing`` and ``review`` are domain packages, and one importing another's
pure, zero-infra-import types is the ordinary shape, not a boundary
violation.

**One shared exception, not two, per Assumption 8:** ``ContextQueryError``
mirrors ``indexing.repository.IndexingRepositoryError``'s already-established
shape — one exception, discriminated by an ``operation`` field, shared
across both of this Phase's Postgres-backed query implementations
(``PostgresGraphQuery`` and ``PostgresSimilarityQuery``), which both live in
one adapter module (``adapter/storage/context_query.py``) and both wrap the
identical failure mode (a real Postgres-level failure at the adapter
boundary). ``SimilarityQuery``'s one implementation uses
``operation="find_similar"``.

Zero infra imports, per architecture.md's domain-package rule.

**Phase 5 (Unit E) extends this module with ``ContextBuilderStep``**, the
macro pipeline step (``name = "context_builder"``) that orchestrates Units
A-D (``review/context_symbols.py``'s diff-to-symbol resolution and
``fetch_diff_content``, this module's own ``GraphQuery``/``SimilarityQuery``,
and ``review/context_bundle.py``'s budget-enforced assembly) into one
``ContextBundle`` per review job, per
docs/plans/plan-deliver-context-builder.md's Phase 5 section and
docs/specs/spec-context-builder.md's US1/US5/US7/US8/US10.

``ContextBuilderStep`` imports ``review.context_symbols``/
``review.context_bundle`` directly — not a US-F1 step-to-step boundary
violation (``review/test_step_boundaries.py``'s own scan is updated this
phase to treat both as this Unit's own internal helper modules, alongside
``review.step``/``review.pipeline``/``review.config``, since neither
implements ``review.step.Step`` in its own right — they are Units A/D of
the *same* ``context_builder`` macro step this module's own ``Step``
implementation lives in, not sibling macro steps ``ContextBuilderStep``
would otherwise need to treat structurally, the way it treats
``static_analysis``'s/``prefilter``'s output).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar
from urllib.parse import urlsplit

from opentelemetry import metrics, trace
from pydantic import BaseModel, ConfigDict, Field

from findings.models import Finding
from findings.redact import NoOpTextRedactor, TextRedactor
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingClient, EmbeddingError, EmbeddingVector
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)
from ingestion.diff import Diff, DiffContentMalformedError, DiffContentUnavailableError, DiffFetcher
from ingestion.pr_metadata import filter_issue_keys
from review._telemetry import suppress_telemetry_errors
from review.context_bundle import (
    MAX_CALLER_EDGES_PER_SYMBOL,
    MAX_SIMILARITY_MATCHES_PER_CHUNK,
    ContextBundle,
    assemble_context_bundle,
)
from review.context_symbols import fetch_diff_content, resolve_changed_symbols
from review.diff_prompt import render_diff
from review.product_knowledge import (
    FETCH_TOP_N,
    SUPPORTED_MAJORS,
    EffectiveProvider,
    EffectiveProviderResolver,
    ProductKnowledgeError,
    ProductKnowledgeProvider,
    ProductKnowledgeSection,
    ProviderItem,
    ProviderOpener,
    ProviderOutcome,
    assemble_product_knowledge,
    formulate_query,
    normalize_fetch,
    normalize_results,
    should_query,
)
from review.step import JobIdentity, Step, StepContext

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# US6 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) — the OTel
# *API* import above, mirroring how `logging.getLogger(__name__)` is already
# imported directly in this module (see spec's "Resolved decisions" for why
# no `typing.Protocol` telemetry indirection exists). `metrics.get_meter`
# returns a proxy that safely no-ops until a composition root
# (`apps/webhook_api/main.py`/`apps/worker/main.py`, Unit A) registers a
# real `MeterProvider` — this module never constructs SDK/exporter infra
# itself. Meter/metric names are the catalog's literal strings, not this
# module's `__name__`.
meter = metrics.get_meter("review.context_builder")

_outcome_counter = meter.create_counter(
    "review.context_builder.outcome_total",
    description="Context Builder step outcomes, tagged by outcome.",
)
_duration_histogram = meter.create_histogram(
    "review.context_builder.duration_ms",
    unit="ms",
    description="Context Builder step wall-clock duration.",
)
_item_count_histogram = meter.create_histogram(
    "review.context_builder.item_count",
    description="Number of items in the assembled context bundle.",
)
_bytes_histogram = meter.create_histogram(
    "review.context_builder.bytes",
    unit="By",
    description="Total serialized byte size of the assembled context bundle.",
)

# Product-knowledge provider branch (docs/plans/plan-deliver-product-knowledge.md
# Phase 7, US-PK14). Literal instrument names so scripts/check-dashboard-metrics.py
# finds them. Label allow-list (closed sets): `operation` ∈ {contract, search,
# fetch, job}; `outcome` ∈ ProviderOutcome; `contract_version` ∈ {"1", "none",
# "unsupported"}; `origin` ∈ {"worker"} (test-connection records "test_connection"
# from the Admin API). Never a query, title, snippet, URL path, token or error body.
_pk_tracer = trace.get_tracer("review.product_knowledge")
_pk_meter = metrics.get_meter("review.product_knowledge")
_pk_call_counter = _pk_meter.create_counter(
    "review.product_knowledge.call_total",
    description="Product-knowledge provider calls and job-level outcomes.",
)
_pk_duration_histogram = _pk_meter.create_histogram(
    "review.product_knowledge.call_duration_ms",
    unit="ms",
    description="Product-knowledge provider call / branch wall-clock duration.",
)
_pk_item_count_histogram = _pk_meter.create_histogram(
    "review.product_knowledge.item_count",
    description="Number of provider items in the assembled section.",
)
_pk_bytes_histogram = _pk_meter.create_histogram(
    "review.product_knowledge.bytes",
    unit="By",
    description="Serialized byte size of the assembled provider section.",
)


def record_product_knowledge_call(
    operation: str,
    outcome: ProviderOutcome,
    contract_version: str,
    duration_ms: int,
    *,
    origin: str,
) -> None:
    """Record one provider call on the shared instruments. ``origin`` is the
    closed set {"worker", "test_connection"}; the Admin API's test-connection
    path uses this too so both appear on one dashboard row."""
    attributes = {
        "operation": operation,
        "outcome": outcome.value,
        "contract_version": contract_version,
        "origin": origin,
    }
    with suppress_telemetry_errors(
        logger, "product_knowledge telemetry recording failed (review unaffected)"
    ):
        _pk_call_counter.add(1, attributes)
        _pk_duration_histogram.record(duration_ms, attributes)


# Headroom the fetch stage leaves before the branch deadline.
_FETCH_DEADLINE_MARGIN_SECONDS: Final = 0.25

# Outcomes an operator must act on: logged at warning with the credential ref
# and endpoint host (log fields only), not just counted.
_PK_CONFIG_CLASS: Final = frozenset(
    {
        ProviderOutcome.CREDENTIAL_UNAVAILABLE,
        ProviderOutcome.BLOCKED_EGRESS,
        ProviderOutcome.UNSUPPORTED_VERSION,
        ProviderOutcome.AUTH_FAILED,
        ProviderOutcome.REDACTION_DISABLED,
    }
)


class ContextQueryError(Exception):
    """Raised by ``GraphQuery``/``SimilarityQuery`` implementations on a
    real Postgres-level failure — never a raw ``sqlalchemy``/``asyncpg``
    exception, per architecture.md's exception-type rule. ``operation``
    discriminates which query stage failed (``"find_callers_edges"`` /
    ``"find_callers_content"`` for ``GraphQuery``; ``"find_similar"`` for
    ``SimilarityQuery``), the same shared-exception,
    operation-discriminated shape ``indexing.repository.IndexingRepositoryError``
    already established for an identical situation (two Protocols, one
    adapter module, one failure mode).
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"context query operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class CallerHit(BaseModel):
    """One resolved blast-radius caller/importer (US2) — a graph edge's
    source ``file_path``/``symbol_name`` plus that caller's actual chunk
    content (US3), so the PR author sees the calling code itself, not just
    a pointer to it.

    ``line_range`` is ``None`` only for a chunk indexed before
    ``index_chunk.start_line``/``end_line``'s migration and never
    re-indexed since (per Assumption 5) — Phase 6's citation rendering
    renders without the line-range portion in that case, never an error.
    """

    model_config = ConfigDict(frozen=True)

    file_path: str
    symbol_name: str
    content: str
    line_range: tuple[int, int] | None = None


class GraphQuery(Protocol):
    """The blast-radius read path (US2/US3) — a one-hop reverse lookup
    against the default-branch (``pr_number = 0``) call/import graph,
    scoped to a single repo. Implemented by
    ``adapter/storage/context_query.py``'s ``PostgresGraphQuery``.
    """

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        """Resolve every one-hop caller/importer of each name in
        ``symbol_names`` (each name is looked up under its **pre-change**
        identity, per US10 — the caller is responsible for resolving a
        renamed/removed symbol to that name before calling this method).

        One call across every resolved pre-change symbol in the job — never
        one query per symbol (per Assumption 6's query-batching
        requirement). Returns a dict with exactly one entry per name in
        ``symbol_names`` (including duplicates collapsed to one key);
        a symbol with zero matching edges maps to an empty tuple, never
        omitted from the dict and never an error (US2).

        Each symbol's own hits are truncated to at most ``limit_per_symbol``
        entries, dropped lowest-ranked-first by a deterministic
        ``(file_path, symbol_name)`` ordering (US9) — never an unbounded
        result.

        Scoped to the default-branch graph and chunk table only
        (``pr_number = 0``) at every stage — never the PR's own indexed
        delta rows, per the spec's permanent default-branch-only decision.

        Raises ``ContextQueryError`` (``operation="find_callers_edges"`` or
        ``operation="find_callers_content"``, naming which of the two
        query stages failed) on a real Postgres-level failure.
        """
        ...


class SimilarityQuery(Protocol):
    """The semantic-similarity read path (US4) — a vector-similarity (KNN)
    search against the default-branch (``pr_number = 0``) chunk table,
    scoped to a single repo, per Phase 3 (Unit C). Implemented by
    ``adapter/storage/context_query.py``'s ``PostgresSimilarityQuery``.
    """

    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        """Returns up to ``limit`` chunks from ``repo_id``'s default-branch
        index (``pr_number = 0``), ranked nearest-first by cosine distance
        to ``embedding``, excluding any chunk whose ``file_path`` equals
        ``exclude_file_path`` (US4's "excluding the chunk's own file"
        requirement).

        ``embedding`` is always the querying chunk's own post-change
        content, embedded via ``indexing.embedding.EmbeddingClient.embed()``
        — never a synthesized description, per the spec's decided
        query-embedding-source reasoning; the caller (a later phase's
        ``ContextBuilderStep``) is responsible for producing it, one batched
        ``embed()`` call across every changed chunk in the job (Assumption
        6), not this method's own concern.

        Fewer than ``limit`` matches in the index returns however many
        exist, never an error; zero matches returns an empty tuple.

        No ANN index backs this query for v1 (Assumption 5) — a sequential
        scan plus sort, accepted since no latency SLA is set and no
        real-repo-scale data exists yet to size an ANN index against.

        Raises ``ContextQueryError`` (``operation="find_similar"``) on a
        real Postgres-level failure.
        """
        ...


# ---------------------------------------------------------------------------
# Phase 5 (Unit E) — ContextBuilderStep, per this module's own docstring.
# ---------------------------------------------------------------------------


class _ContextBuilderInput(JobIdentity):
    """Mirrors ``review.static_analysis.StaticAnalysisOutput``'s own field
    shape without importing it — the same "Import boundary" pattern
    ``review.review_generation._ReviewGenerationInput`` already established for the
    identical situation. ``from_attributes=True`` lets ``model_validate``
    read this shape off *any* object with matching attributes, including
    the real ``StaticAnalysisOutput``/``PrefilterOutput`` instance the
    outer registry hands this step as its own ``input`` — see
    ``ContextBuilderStep.__init__``'s own docstring for when each applies.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    # Defaults to empty, not required, so this step can also validate off
    # `review.prefilter.PrefilterOutput` (which carries no `findings` of its
    # own) when the composition root wires `depends_on="prefilter"` because
    # `static_analysis` can't produce findings — mirroring
    # `review.review_generation._ReviewGenerationInput`'s identical fallback.
    findings: list[Finding] = Field(default_factory=list)


class ContextBuilderOutput(JobIdentity):
    """This step's own output: the diff and findings, forwarded unchanged
    (this step adds no findings of its own — it only retrieves context for
    ``review_generation``, this step's own downstream dependency, to consume),
    plus the newly-assembled ``context_bundle``.

    ``base_sha`` and the product-knowledge ``pr_*`` fields are explicitly
    forwarded at construction (one of three hand-constructing sites, with
    ``prefilter`` and ``static_analysis``) — they are *not* automatically
    carried forward just because ``_ContextBuilderInput`` also has them (see
    ``review.step.JobIdentity``'s own docstring for why an optional field is never auto-forwarded).
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    findings: list[Finding]
    context_bundle: ContextBundle


def _diff_byte_size(diff: Diff) -> int:
    """The bare-diff byte size Non-Functional Requirements' proportional
    context budget is stated against — computed via
    ``review.diff_prompt.render_diff``, the same rendering every LLM-calling
    step uses to build its own diff prompt section. ``diff_prompt`` is a
    shared framework helper, not a sibling step module (see
    ``review/test_step_boundaries.py``'s ``_FRAMEWORK_MODULES``), so calling
    it here is not a US-F1 step-to-step import; it also keeps this budget
    figure from silently drifting out of sync with a future change to
    ``render_diff``'s per-file header format.
    """
    return len(render_diff(diff).encode())


@dataclass
class _ProviderTrace:
    """Mutable record the provider branch fills in as it goes, so the
    wrapper can report accurately even when the deadline cancels the flow
    mid-way. Holds only closed-set values and counts — never provider text."""

    outcome: ProviderOutcome = ProviderOutcome.PROVIDER_ERROR
    contract_version: str = "none"
    credential_ref: str | None = None
    endpoint_host: str | None = None
    display_name: str | None = None
    searched: bool = False
    duration_ms: int = 0
    started_ns: int = 0
    status_code: int | None = None
    items: tuple[ProviderItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _CodeRun:
    bundle: ContextBundle
    stats: dict[str, int]
    outcome: str
    error_extra: dict[str, Any]


class ContextBuilderStep:
    """``review.step.Step`` implementation for US1/US5/US7/US8/US9/US10 —
    see this module's own docstring for the full design, and
    ``docs/plans/plan-deliver-context-builder.md``'s Phase 5 section for
    the complete reasoning behind every choice below.

    **Timeout design — the single most important correctness property of
    this phase (Assumption 7):** the *entire* internal orchestration
    (Units A-D, ``_orchestrate`` below) is wrapped in exactly **one**
    ``asyncio.wait_for(..., timeout=internal_timeout_seconds)`` — never one
    ``wait_for`` per internal call. ``internal_timeout_seconds`` must be
    strictly less than the outer ``StepRegistry``'s own
    ``context_builder_step_timeout_seconds`` for this step, an invariant
    ``apps/worker/settings.py``'s ``WorkerSettings`` validates at
    construction, not here.

    **Unconditional graceful degradation (US7):** every typed failure that
    can actually reach this step's own top level —
    ``ingestion.diff.DiffContentUnavailableError``/``DiffContentMalformedError``,
    ``indexing.tree.RepoTreeFetchError``/``RepoTreeTruncatedError``/
    ``RepoTreeContentFetchError``, this module's own ``ContextQueryError``,
    ``indexing.embedding.EmbeddingError``, and the internal ``TimeoutError``
    the single ``wait_for`` above raises on expiry — is caught in ``run()``.
    On any of these, ``run()`` logs at ``warning`` with
    ``outcome="degraded_error"`` and returns ``ContextBuilderOutput`` with
    an empty ``context_bundle``. It never re-raises.
    ``indexing.chunk.ChunkParseError`` is deliberately **not** in this
    list — it is fully absorbed, per file, inside
    ``review.context_symbols.resolve_changed_symbols`` itself and can never
    propagate out to this step's own catch (per that module's own
    docstring).

    A repo with no index yet, or zero hits found with no error, logs at
    ``info`` with ``outcome="degraded_empty"``. A fully successful run logs
    ``outcome="ok"``. No cross-job escalation/alerting logic is added, per
    the plan's own reasoning (per-job structured logging is this phase's
    whole observability story).
    """

    name = "context_builder"

    def __init__(
        self,
        diff_fetcher: DiffFetcher,
        tree_fetcher: RepoTreeFetcher,
        graph_query: GraphQuery,
        similarity_query: SimilarityQuery,
        embedding_client: EmbeddingClient,
        *,
        internal_timeout_seconds: float,
        max_total_bytes: int,
        depends_on: str = "static_analysis",
        provider_resolver: EffectiveProviderResolver | None = None,
        provider_opener: ProviderOpener | None = None,
        provider_redactor: TextRedactor | None = None,
        provider_deadline_seconds: float = 10.0,
    ) -> None:
        """``depends_on`` names this step's single upstream dependency —
        ``"static_analysis"`` by default (the ordinary, both-steps-enabled
        case), or ``"prefilter"`` when the composition root
        (``apps/worker/main.py``) determines ``static_analysis`` can never
        produce findings — mirroring ``review.review_generation.ReviewGenerationStep``'s
        own identical constructor-parameter fix for the identical problem
        (see that class's own docstring for the full "why this is a
        constructor parameter, not a fixed class attribute" reasoning).

        ``max_total_bytes`` is the absolute ceiling
        (``WorkerSettings.llm_max_diff_bytes``, reused unchanged per the
        spec's Non-Functional Requirements — "no second cost knob that can
        drift out of sync with the first") both the proportional-to-diff
        budget and the single-oversized-item check in
        ``review.context_bundle.assemble_context_bundle`` are capped
        against.

        **Optional product-knowledge provider branch** (plan Phase 7; every
        parameter is keyword-only and defaults to the pre-feature behavior):
        ``provider_resolver`` ``None`` ⇒ no provider code path at all,
        byte-for-byte the prior step. ``provider_opener`` ``None`` with a
        resolver set is the kill-switch-off state: a configured provider
        yields ``outcome=disabled`` and no client is ever built.
        ``provider_redactor`` is the same instance the Worker already builds;
        a ``NoOpTextRedactor`` (or ``None``) means redaction is off, so the
        branch is skipped (``redaction_disabled``, A5) rather than send an
        unredacted title. ``provider_deadline_seconds`` is further capped to
        ``internal_timeout_seconds`` so the branch never outlives the window.
        """
        self._diff_fetcher = diff_fetcher
        self._tree_fetcher = tree_fetcher
        self._graph_query = graph_query
        self._similarity_query = similarity_query
        self._embedding_client = embedding_client
        self._internal_timeout_seconds = internal_timeout_seconds
        self._max_total_bytes = max_total_bytes
        self.depends_on: tuple[str, ...] = (depends_on,)
        self._provider_resolver = provider_resolver
        self._provider_opener = provider_opener
        self._provider_redactor = provider_redactor
        self._provider_deadline_seconds = min(provider_deadline_seconds, internal_timeout_seconds)

    async def _orchestrate(
        self, parsed: _ContextBuilderInput, job_id: str
    ) -> tuple[ContextBundle, dict[str, int]]:
        """Units A-D, in order — the single coroutine the outer
        ``asyncio.wait_for`` in ``run()`` wraps as one whole (Assumption 7).
        Raises whichever typed exception the first failing internal call
        raises; catches nothing itself — ``run()`` is the one place that
        decides graceful degradation (US7).
        """
        changed_paths = [file.path for file in parsed.diff.files]

        pre_content, post_content = await fetch_diff_content(
            diff_fetcher=self._diff_fetcher,
            tree_fetcher=self._tree_fetcher,
            diff=parsed.diff,
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            head_sha=parsed.head_sha,
            base_sha=parsed.base_sha,
            changed_paths=changed_paths,
            job_id=job_id,
        )

        changed = await resolve_changed_symbols(
            parsed.diff, pre_content, post_content, job_id=job_id
        )

        async def _fetch_caller_hits() -> dict[str, tuple[CallerHit, ...]]:
            # A resolved pre-change chunk can still have `symbol_name is
            # None` (e.g. a whole-file-packed chunk covering more than one
            # declaration, per `indexing.chunk.Chunk`'s own docstring) — no
            # single symbol identity to look blast radius up under, so it's
            # excluded here rather than passed through as a literal
            # `"None"` lookup. `dict.fromkeys(...)` dedupes while
            # preserving order — two changed files both touching a
            # same-named symbol (e.g. two files each changing a function
            # named `run`) would otherwise pass the same name twice into
            # `find_callers`, whose `unnest(:symbol_names)`-based query
            # issues one independent LATERAL subquery per *occurrence*, not
            # per distinct name — doubling both the query work and the
            # resulting `CallerHit` entries appended under that one key,
            # which could then crowd out distinct callers at Phase 4's own
            # per-symbol truncation.
            symbol_names = list(
                dict.fromkeys(
                    chunk.symbol_name
                    for chunk in changed.pre_change_hits
                    if chunk.symbol_name is not None
                )
            )
            if not symbol_names:
                return {}
            return await self._graph_query.find_callers(
                repo_id=parsed.repo_id,
                symbol_names=symbol_names,
                limit_per_symbol=MAX_CALLER_EDGES_PER_SYMBOL,
            )

        async def _fetch_similarity_hits() -> dict[str, tuple[Chunk, ...]]:
            if not changed.post_change_hits:
                return {}
            texts = [chunk.content for chunk in changed.post_change_hits]
            # Batched across every changed chunk in the job, one call — per
            # Assumption 6 — dispatched via `asyncio.to_thread` since local
            # ONNX inference is CPU-bound, mirroring
            # `indexing/pipeline.py`'s own precedent for this exact call.
            vectors = await asyncio.to_thread(self._embedding_client.embed, texts)
            # One `find_similar` DB round trip per changed chunk is
            # unavoidable (each queries a distinct embedding vector), but
            # the round trips are independent of each other — dispatched
            # concurrently via `asyncio.gather` rather than sequentially
            # awaited one at a time, so this branch's latency against the
            # `internal_timeout_seconds` budget is roughly the slowest
            # single call rather than their sum.
            keys = [f"{chunk.file_path}:{chunk.chunk_index}" for chunk in changed.post_change_hits]
            results = await asyncio.gather(
                *(
                    self._similarity_query.find_similar(
                        repo_id=parsed.repo_id,
                        embedding=vector,
                        exclude_file_path=chunk.file_path,
                        limit=MAX_SIMILARITY_MATCHES_PER_CHUNK,
                    )
                    for chunk, vector in zip(changed.post_change_hits, vectors, strict=True)
                )
            )
            return dict(zip(keys, results, strict=True))

        # The blast-radius lookup (over `pre_change_hits`) and the
        # embedding+similarity lookup (over `post_change_hits`) read
        # disjoint inputs and write disjoint local results — genuinely
        # independent work, run concurrently via `asyncio.gather` rather
        # than as two sequential `await`s, so this segment's latency (against
        # the tight `internal_timeout_seconds` budget) is roughly the
        # slower of the two branches instead of their sum.
        caller_hits, similarity_hits = await asyncio.gather(
            _fetch_caller_hits(), _fetch_similarity_hits()
        )

        bundle, dropped_counts = assemble_context_bundle(
            caller_hits,
            similarity_hits,
            _diff_byte_size(parsed.diff),
            self._max_total_bytes,
            job_id=job_id,
        )
        stats = {
            "resolved_symbol_count": len(changed.pre_change_hits),
            "resolved_chunk_count": len(changed.post_change_hits),
            "caller_edges_found": sum(len(hits) for hits in caller_hits.values()),
            "similarity_matches_found": sum(len(hits) for hits in similarity_hits.values()),
            # Assumption 6's upstream, per-job ceilings (review.context_symbols) —
            # surfaced here, not just at their own immediate `warning` log site,
            # per this phase's own Observability requirement that the per-run
            # log itself carry "dropped-by-ceiling" counts for both the
            # upstream and the existing per-item ceilings.
            "changed_symbols_dropped": changed.pre_dropped_count,
            "changed_chunks_dropped": changed.post_dropped_count,
            # Phase 4's existing per-item ceilings (review.context_bundle) —
            # same reasoning, folded in from assemble_context_bundle's own
            # returned drop tallies.
            **dropped_counts,
        }
        return bundle, stats

    async def _run_code_branch(self, parsed: _ContextBuilderInput, job_id: str) -> _CodeRun:
        """The code-retrieval branch (Units A-D) with its own single
        ``wait_for`` and its own typed-failure degradation (US7). The
        outcome/degraded labels and every pre-existing metric/log value are
        computed from this branch only."""
        bundle = ContextBundle()
        stats: dict[str, int] = {}
        outcome = "ok"
        error_extra: dict[str, Any] = {}

        try:
            bundle, stats = await asyncio.wait_for(
                self._orchestrate(parsed, job_id), timeout=self._internal_timeout_seconds
            )
        except (
            DiffContentUnavailableError,
            DiffContentMalformedError,
            RepoTreeFetchError,
            RepoTreeTruncatedError,
            RepoTreeContentFetchError,
            ContextQueryError,
            EmbeddingError,
            TimeoutError,
        ) as exc:
            outcome = "degraded_error"
            bundle = ContextBundle()
            error_extra = {"error_type": type(exc).__name__, "error": str(exc)}
            if isinstance(exc, ContextQueryError):
                error_extra["query_operation"] = exc.operation
                error_extra["query_detail"] = exc.detail
        else:
            if not bundle.items:
                outcome = "degraded_empty"
        return _CodeRun(bundle, stats, outcome, error_extra)

    async def run(self, input: Any, context: StepContext) -> ContextBuilderOutput:
        # Any (not _ContextBuilderInput) because the registry passes the
        # raw upstream step output here for validation — it may be
        # StaticAnalysisOutput or, when static analysis can't produce
        # findings, PrefilterOutput (see depends_on fallback above).
        # `model_validate(..., from_attributes=True)` below enforces the
        # actual shape at runtime.
        parsed = _ContextBuilderInput.model_validate(input, from_attributes=True)
        job_id = context.job_id
        start = time.monotonic()

        # Two independent branches (F9): the provider branch never raises and
        # has its own deadline, the code branch degrades on its own typed
        # failures; neither can replace or fail the other's result.
        provider_task: asyncio.Task[_ProviderTrace] | None = None
        if self._provider_resolver is not None:
            provider_task = asyncio.ensure_future(self._run_provider_branch(parsed, job_id))
        try:
            code = await self._run_code_branch(parsed, job_id)
        except BaseException:
            if provider_task is not None:
                provider_task.cancel()
            raise
        # Measured before awaiting the provider so the existing duration
        # histogram / log keep their code-branch-only meaning (plan Phase 7).
        duration_ms = int((time.monotonic() - start) * 1000)
        bundle, stats, outcome, error_extra = (
            code.bundle,
            code.stats,
            code.outcome,
            code.error_extra,
        )
        if provider_task is not None:
            provider = await provider_task
            # Assembled only now so ids continue after the final code id
            # (``item()`` for a code id is unchanged).
            section = assemble_product_knowledge(
                provider.items,
                label=provider.display_name or "Product knowledge",
                first_id=bundle.next_item_id(),
            )
            if section is not None:
                bundle = bundle.model_copy(update={"product_knowledge": section})
            self._finish_provider(parsed, job_id, provider, section)

        item_count = len(bundle.items)
        # Sourced from `stats` (assemble_context_bundle's own already-computed
        # truncation total) rather than re-serializing every surviving item a
        # second time — 0 on the error/empty paths above, where `bundle` is a
        # fresh, empty `ContextBundle` and `stats` was never populated.
        total_bytes = stats.get("context_bundle_total_bytes", 0)
        log_extra: dict[str, Any] = {
            "job_id": job_id,
            "org_id": parsed.org_id,
            "repo_id": parsed.repo_id,
            "repo_full_name": parsed.repo_full_name,
            "pr_number": parsed.pr_number,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "base_sha_missing": parsed.base_sha is None,
            "context_bundle_item_count": item_count,
            "context_bundle_total_bytes": total_bytes,
            **stats,
            **error_extra,
        }

        # US6 — recorded at the same point as the log line above, using the
        # real `outcome` literal ("ok" / "degraded_empty" / "degraded_error")
        # this method itself assigns, never a free-text value (Privacy NFR).
        metric_attributes = {"outcome": outcome}
        with suppress_telemetry_errors(
            logger,
            "context builder telemetry recording failed (context bundle unaffected)",
            job_id=job_id,
        ):
            _outcome_counter.add(1, metric_attributes)
            _duration_histogram.record(duration_ms, metric_attributes)
            _item_count_histogram.record(item_count, metric_attributes)
            _bytes_histogram.record(total_bytes, metric_attributes)

        if outcome == "degraded_error":
            logger.warning("context_builder degraded", extra=log_extra)
        elif outcome == "degraded_empty":
            logger.info("context_builder completed with no context", extra=log_extra)
        else:
            log_extra["items"] = [
                {"file_path": item.file_path, "symbol_name": item.symbol_name}
                for item in bundle.items
            ]
            logger.info("context_builder completed", extra=log_extra)

        return ContextBuilderOutput(
            diff=parsed.diff,
            findings=parsed.findings,
            context_bundle=bundle,
            org_id=parsed.org_id,
            repo_id=parsed.repo_id,
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
            base_sha=parsed.base_sha,
            pr_title=parsed.pr_title,
            pr_head_ref=parsed.pr_head_ref,
            pr_issue_keys=parsed.pr_issue_keys,
            pr_is_fork=parsed.pr_is_fork,
            private=parsed.private,
        )

    # ------------------------------------------------------------------
    # Product-knowledge provider branch (plan Phase 7)
    # ------------------------------------------------------------------

    async def _run_provider_branch(
        self, parsed: _ContextBuilderInput, job_id: str
    ) -> _ProviderTrace:
        """Never raises: every failure maps to a ``ProviderOutcome``. Bounded
        by its own deadline (≤ the internal window), so a hung provider or a
        hung settings read can neither delay nor fail the code branch."""
        trace_ = _ProviderTrace(started_ns=time.time_ns())
        start = time.monotonic()
        try:
            await asyncio.wait_for(
                self._provider_flow(
                    parsed, job_id, trace_, deadline_at=start + self._provider_deadline_seconds
                ),
                timeout=self._provider_deadline_seconds,
            )
        except TimeoutError:
            trace_.outcome = ProviderOutcome.TIMEOUT
        except ProductKnowledgeError as exc:
            trace_.outcome = exc.outcome
            trace_.status_code = exc.status_code
        except Exception as exc:
            # Deliberately broad (plan Phase 7: "the branch itself never
            # raises"; A5): an optional feature must not fail a review. Only
            # the exception type is logged, never its text.
            trace_.outcome = ProviderOutcome.PROVIDER_ERROR
            logger.warning(
                "product_knowledge branch failed unexpectedly",
                extra={"job_id": job_id, "error_type": type(exc).__name__},
            )
        trace_.duration_ms = int((time.monotonic() - start) * 1000)
        return trace_

    async def _provider_flow(
        self,
        parsed: _ContextBuilderInput,
        job_id: str,
        trace_: _ProviderTrace,
        *,
        deadline_at: float,
    ) -> None:
        resolver = self._provider_resolver
        assert resolver is not None  # only scheduled when a resolver is wired
        # The settings read is bounded by min(its own 2 s cap, this branch's
        # remaining deadline): the caller's ``wait_for`` on this whole flow is
        # the deadline, so a hung read cannot outlive the branch.
        setting = await resolver.resolve(parsed.org_id, parsed.repo_id)
        if setting is None:
            trace_.outcome = ProviderOutcome.NOT_CONFIGURED
            return
        trace_.credential_ref = setting.credential_ref
        trace_.display_name = setting.display_name
        trace_.endpoint_host = urlsplit(setting.endpoint_url).hostname
        opener = self._provider_opener
        if opener is None:
            trace_.outcome = ProviderOutcome.DISABLED
            return
        skip = should_query(parsed.private, parsed.pr_is_fork, setting)
        if skip is not None:
            trace_.outcome = skip
            return
        keys = filter_issue_keys(parsed.pr_issue_keys, setting.issue_key_prefixes)
        # Raises credential_unavailable on a missing/unbound credential,
        # before any DNS or network.
        provider = opener(setting.endpoint_url, setting.credential_ref)
        redactor = self._provider_redactor
        if redactor is None or isinstance(redactor, NoOpTextRedactor):
            trace_.outcome = ProviderOutcome.REDACTION_DISABLED
            return
        # A1(c): a non-empty allow-list is a content restriction, so free-text
        # title never reaches the provider; only allow-listed keys do.
        title = None if setting.issue_key_prefixes else parsed.pr_title
        if title:
            try:
                title = await redactor.redact(title)
            except Exception as exc:
                # Redaction raises on subprocess failure (findings.redact);
                # inside this branch that is a typed outcome, never fatal.
                logger.warning(
                    "product_knowledge title redaction failed",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )
                trace_.outcome = ProviderOutcome.PROVIDER_ERROR
                return
        query = formulate_query(title, keys)
        if query is None:
            trace_.outcome = ProviderOutcome.NO_QUERY
            return
        await self._provider_query(provider, query, setting, trace_, deadline_at=deadline_at)

    async def _provider_query(
        self,
        provider: ProductKnowledgeProvider,
        query: str,
        setting: EffectiveProvider,
        trace_: _ProviderTrace,
        *,
        deadline_at: float,
    ) -> None:
        contract_started = time.monotonic()
        negotiated = await self._timed_call("contract", trace_, provider.negotiate())
        contract_ms = int((time.monotonic() - contract_started) * 1000)
        chosen = negotiated.chosen_version
        trace_.contract_version = (
            chosen if chosen is not None and chosen in SUPPORTED_MAJORS else "unsupported"
        )
        if chosen is None or chosen not in SUPPORTED_MAJORS:
            raise ProductKnowledgeError(ProviderOutcome.UNSUPPORTED_VERSION)
        if negotiated.deprecated:
            self._record_call("contract", ProviderOutcome.DEPRECATED_VERSION, trace_, contract_ms)

        trace_.searched = True
        raw = await self._timed_call("search", trace_, provider.search(query))
        normalized = normalize_results(raw)
        if not normalized.items:
            trace_.outcome = normalized.outcome  # empty | malformed
            return
        items = list(normalized.items)

        top = items[:FETCH_TOP_N]
        # Own, earlier bound: the fetch stage must expire (keeping the search
        # snippets) before the branch's ``wait_for`` does, or a hung fetch
        # would lose the whole branch to the same-instant outer timeout.
        remaining = max(0.0, deadline_at - time.monotonic() - _FETCH_DEADLINE_MARGIN_SECONDS)
        fetched = await self._fetch_top(provider, top, trace_, remaining)
        items[: len(top)] = fetched
        trace_.items = tuple(items)
        trace_.outcome = normalized.outcome  # ok | partial

    async def _fetch_top(
        self,
        provider: ProductKnowledgeProvider,
        top: list[ProviderItem],
        trace_: _ProviderTrace,
        remaining_seconds: float,
    ) -> list[ProviderItem]:
        """Fetch the top ``FETCH_TOP_N`` in parallel; a failed, slow or
        unusable fetch keeps that item's snippet (it never fails the job)."""

        async def one(item: ProviderItem) -> ProviderItem:
            try:
                raw = await self._timed_call("fetch", trace_, provider.fetch(item.provider_item_id))
            except ProductKnowledgeError:
                return item
            return normalize_fetch(raw, item)

        try:
            return list(
                await asyncio.wait_for(
                    asyncio.gather(*(one(item) for item in top)), timeout=remaining_seconds
                )
            )
        except TimeoutError:
            return top

    async def _timed_call(
        self, operation: str, trace_: _ProviderTrace, awaitable: Awaitable[_T]
    ) -> _T:
        """Awaits one provider call, recording ``call_total``/
        ``call_duration_ms`` for it (the closed ``operation`` label) with the
        call's own outcome."""
        start = time.monotonic()
        try:
            result = await awaitable
        except ProductKnowledgeError as exc:
            trace_.status_code = exc.status_code
            self._record_call(
                operation, exc.outcome, trace_, int((time.monotonic() - start) * 1000)
            )
            raise
        if operation == "contract":
            # Label the negotiation's own call with the version it chose, not
            # the "none" the trace still holds until `_provider_query` sets it.
            chosen = getattr(result, "chosen_version", None)
            if isinstance(chosen, str) and chosen in SUPPORTED_MAJORS:
                trace_.contract_version = chosen
        self._record_call(
            operation, ProviderOutcome.OK, trace_, int((time.monotonic() - start) * 1000)
        )
        return result

    def _record_call(
        self, operation: str, outcome: ProviderOutcome, trace_: _ProviderTrace, duration_ms: int
    ) -> None:
        record_product_knowledge_call(
            operation, outcome, trace_.contract_version, duration_ms, origin="worker"
        )

    def _finish_provider(
        self,
        parsed: _ContextBuilderInput,
        job_id: str,
        trace_: _ProviderTrace,
        section: ProductKnowledgeSection | None,
    ) -> None:
        """Job-level telemetry: one ``operation=job`` counter/duration pair,
        one ``review.product_knowledge`` span (child of the step span) and one
        log line. Closed-set labels and counts only — never content."""
        outcome = trace_.outcome
        item_count = len(section.items) if section is not None else 0
        total_bytes = (
            sum(len(item.model_dump_json().encode()) for item in section.items)
            if section is not None
            else 0
        )
        attributes = {
            "operation": "job",
            "outcome": outcome.value,
            "contract_version": trace_.contract_version,
            "origin": "worker",
        }
        with suppress_telemetry_errors(
            logger,
            "product_knowledge telemetry recording failed (review unaffected)",
            job_id=job_id,
        ):
            # The branch ran concurrently with code retrieval and is only
            # reported here (item ids need the final code bundle), so the span
            # is created with the branch's real start/end times rather than
            # wrapping this call. It is a child of the step span (the current
            # context here), per spec-observability's span-per-macro-step.
            span = _pk_tracer.start_span("review.product_knowledge", start_time=trace_.started_ns)
            span.set_attribute("outcome", outcome.value)
            span.set_attribute("item_count", item_count)
            span.set_attribute("contract_version", trace_.contract_version)
            span.set_attribute("duration_ms", trace_.duration_ms)
            span.end(end_time=trace_.started_ns + trace_.duration_ms * 1_000_000)
            _pk_call_counter.add(1, attributes)
            _pk_duration_histogram.record(trace_.duration_ms, attributes)
            if trace_.searched:
                _pk_item_count_histogram.record(item_count, {"outcome": outcome.value})
                _pk_bytes_histogram.record(total_bytes, {"outcome": outcome.value})

        log_extra: dict[str, Any] = {
            "job_id": job_id,
            "org_id": parsed.org_id,
            "repo_id": parsed.repo_id,
            "provider_ref": trace_.credential_ref,
            "endpoint_host": trace_.endpoint_host,
            "operation": "job",
            "outcome": outcome.value,
            "status_code": trace_.status_code,
            "duration_ms": trace_.duration_ms,
            "item_count": item_count,
            "bytes": total_bytes,
        }
        if outcome in _PK_CONFIG_CLASS:
            logger.warning("product_knowledge degraded", extra=log_extra)
        elif outcome is ProviderOutcome.NOT_CONFIGURED:
            logger.debug("product_knowledge not configured", extra=log_extra)
        else:
            logger.info("product_knowledge completed", extra=log_extra)


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_context_builder_step: type[Step[Any, ContextBuilderOutput]] = ContextBuilderStep
