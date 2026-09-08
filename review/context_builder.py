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
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from findings.models import Finding
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingClient, EmbeddingError, EmbeddingVector
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)
from ingestion.diff import Diff, DiffContentMalformedError, DiffContentUnavailableError, DiffFetcher
from review.context_bundle import (
    MAX_CALLER_EDGES_PER_SYMBOL,
    MAX_SIMILARITY_MATCHES_PER_CHUNK,
    ContextBundle,
    assemble_context_bundle,
    serialized_item_size,
)
from review.context_symbols import fetch_diff_content, resolve_changed_symbols
from review.diff_prompt import render_diff
from review.step import JobIdentity, Step, StepContext

logger = logging.getLogger(__name__)


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

    ``base_sha`` is explicitly forwarded at construction (Assumption 2's
    fourth site) — it is *not* automatically carried forward just because
    ``_ContextBuilderInput`` also has it (see ``review.step.JobIdentity``'s
    own docstring for why an optional field is never auto-forwarded).
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
        """
        self._diff_fetcher = diff_fetcher
        self._tree_fetcher = tree_fetcher
        self._graph_query = graph_query
        self._similarity_query = similarity_query
        self._embedding_client = embedding_client
        self._internal_timeout_seconds = internal_timeout_seconds
        self._max_total_bytes = max_total_bytes
        self.depends_on: tuple[str, ...] = (depends_on,)

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

        duration_ms = int((time.monotonic() - start) * 1000)
        log_extra: dict[str, Any] = {
            "job_id": job_id,
            "org_id": parsed.org_id,
            "repo_id": parsed.repo_id,
            "repo_full_name": parsed.repo_full_name,
            "pr_number": parsed.pr_number,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "base_sha_missing": parsed.base_sha is None,
            "context_bundle_item_count": len(bundle.items),
            "context_bundle_total_bytes": sum(serialized_item_size(item) for item in bundle.items),
            **stats,
            **error_extra,
        }

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
        )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_context_builder_step: type[Step[Any, ContextBuilderOutput]] = ContextBuilderStep
