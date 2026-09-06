"""review.context_bundle — context assembly + budget enforcement (Unit D of
the Context Builder), per
docs/plans/plan-deliver-context-builder.md's Phase 4 section and
docs/specs/spec-context-builder.md's US9.

Combines Phase 2's blast-radius caller hits and Phase 3's semantic-similarity
matches into one ordered, ID-indexed ``ContextBundle`` — the shape Phase 5's
``ContextBuilderStep`` forwards to ``llm_review`` and Phase 6's citation
rendering references by integer ID — under two independent budget layers,
mirroring ``indexing/pipeline.py``'s own ``_apply_cost_ceilings`` shape:

1. **Per-item soft caps** (``MAX_CALLER_EDGES_PER_SYMBOL`` = 10,
   ``MAX_SIMILARITY_MATCHES_PER_CHUNK`` = 5): each changed symbol's caller
   hits and each changed chunk's similarity matches are truncated
   independently, first, so one heavily-called function or one chunk with
   many near-duplicates can't consume the whole proportional budget alone —
   a second, independent layer on top of Assumption 6's upstream per-job
   ceilings (``review/context_symbols.py``'s ``MAX_CHANGED_SYMBOLS_PER_JOB``/
   ``MAX_CHANGED_CHUNKS_PER_JOB``), not the only layer.
2. **Proportional total-byte truncation**: the combined, per-item-capped set
   is truncated lowest-ranked-first by a genuine cross-group rank — each
   item's own position within its own originating group (each input group
   is already ordered best-first by its own query — a caller-hit group
   deterministically by ``(file_path, symbol_name)``, a similarity-hit
   group by ascending cosine distance) — never group-append order, so one
   group's own low-ranked items don't get dropped ahead of another group's
   own low-ranked items just because that group was assembled first. Until
   the total serialized byte size is at or under
   ``min(diff_byte_size, max_total_bytes)``. A single item whose own
   serialized size alone exceeds ``max_total_bytes`` is dropped entirely
   *before* this truncation pass runs, regardless of the (possibly larger)
   proportional budget — a pathologically large single item never
   crowds out every other item just because the budget happened to have
   room for it. ``diff_byte_size = 0`` collapses the combined budget to
   zero via the plain ``min()`` above — no division anywhere in this
   module, so there is nothing to divide by zero on.

Every drop (either layer) is logged at ``warning`` with the dropped count —
never a failure, mirroring ``spec-indexing.md``'s own non-fatal
cost-ceiling drop pattern (per US9).

**Pure, no-I/O, per ADR-0005** (this phase's own deliverable registers this
module in that ADR's "Scope, extended per phase" list) — ``assemble_context_bundle``
performs no I/O of its own; it only aggregates and truncates already-fetched
hits.

**Caller-hit type is structural, not imported, per this module's own
circular-import avoidance:** ``review.context_builder.CallerHit`` is not
imported here. Phase 5 extends that same module (``review/context_builder.py``)
with ``ContextBuilderStep``, which needs to import *this* module's
``assemble_context_bundle``/``ContextBundle`` — importing ``CallerHit`` back
from there would create an import cycle between the two modules. Instead,
``_CallerHitLike`` below is a structural ``typing.Protocol`` naming exactly
the attributes this module's budget logic needs; ``CallerHit`` (and any
other object shaped the same way) satisfies it per PEP 544 with zero import
required. ``indexing.chunk.Chunk`` (similarity hits) has no such risk —
``indexing`` never depends on ``review`` — so it's imported directly here,
the same ordinary cross-bounded-context shape ``review/context_symbols.py``
and ``review/context_builder.py`` already have on ``indexing`` types
(Assumption 4).

Zero infra imports, per architecture.md's domain-package rule.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from indexing.chunk import Chunk

logger = logging.getLogger(__name__)

# Per-item soft caps (Phase 4's own second, independent budget layer) — see
# this module's own docstring for how these relate to Assumption 6's
# upstream, per-job ceilings in `review/context_symbols.py`. Hardcoded
# module-level constants, not `WorkerSettings` fields, mirroring
# `review/context_symbols.py`'s identical choice for the same documented
# reason (`spec-context-builder.md`'s own Priority section already decided a
# fixed default is sufficient).
MAX_CALLER_EDGES_PER_SYMBOL = 10
MAX_SIMILARITY_MATCHES_PER_CHUNK = 5


class _CallerHitLike(Protocol):
    """Structural shape of a blast-radius caller hit
    (``review.context_builder.CallerHit``) that this module's budget
    enforcement actually needs — declared here, not imported, per this
    module's own "Caller-hit type is structural" docstring note.

    Declared as read-only ``@property`` members, not plain mutable
    attributes: ``CallerHit`` is a frozen (``ConfigDict(frozen=True)``)
    pydantic model, and pydantic's own mypy plugin types a frozen model's
    fields as read-only attributes — a plain mutable-attribute Protocol
    member would (correctly) reject it as a structural mismatch under
    ``mypy --strict``.

    ``symbol_name`` is typed ``str | None``, not ``str`` — ``CallerHit``'s
    own field is never ``None``, but ``_build_candidates`` below (which
    accepts this Protocol) is also called with ``indexing.chunk.Chunk``
    values (Phase 3's similarity hits), whose own ``symbol_name`` genuinely
    can be ``None`` (a whole-file-packed chunk covering more than one
    declaration, per that model's own docstring) — narrowing this to
    ``str`` would reject ``Chunk`` as a structural match under
    ``mypy --strict``.
    """

    @property
    def file_path(self) -> str: ...

    @property
    def symbol_name(self) -> str | None: ...

    @property
    def content(self) -> str: ...

    @property
    def line_range(self) -> tuple[int, int] | None: ...


class ContextItemKind(StrEnum):
    """Which of Phase 2/Phase 3's two retrieval techniques produced a given
    ``ContextBundleItem`` — carried through for observability and for
    Phase 6's citation rendering, never used to change budget behavior
    (both kinds are truncated by the same combined total-byte pass)."""

    CALLER = "caller"
    SIMILARITY = "similarity"


class ContextBundleItem(BaseModel):
    """One item of an assembled ``ContextBundle`` — either a blast-radius
    caller hit (US2/US3) or a semantic-similarity match (US4), normalized to
    one shape so both can share a single ordered, ID-indexed sequence and a
    single combined budget pass.

    ``id`` is this item's position in its owning ``ContextBundle.items``
    (0-indexed) — the integer Phase 6's citation rendering and the LLM's
    structured output reference; stable only within one assembled bundle,
    never a cross-job identifier.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    kind: ContextItemKind
    file_path: str
    symbol_name: str | None
    content: str
    line_range: tuple[int, int] | None = None


class ContextBundle(BaseModel):
    """``assemble_context_bundle``'s return value — an ordered, ID-indexed
    sequence of ``ContextBundleItem``s, already budget-enforced. Empty
    (``items=()``) is a normal, expected value (US7's graceful-degradation
    empty bundle; US9's zero-hits case), never a distinguished error state.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[ContextBundleItem, ...] = ()

    def item(self, item_id: int) -> ContextBundleItem | None:
        """Looks up one item by its ``id`` — the lookup Phase 6's citation
        rendering needs (``render_citation(ref_ids: list[int], bundle: ...)``).
        Returns ``None`` for an id this bundle doesn't contain (e.g. one
        this pass dropped, or one the LLM hallucinated) rather than raising —
        citation rendering degrades to "no citation" for that reference, not
        an error, consistent with US6's own non-fatal posture.
        """
        # `id` is assigned as this item's own index in `self.items` at
        # construction (see this field's docstring) -- an O(1) index
        # lookup, not a scan, with a bounds check standing in for the "no
        # matching entry" case a plain `self.items[item_id]` would instead
        # raise `IndexError` for.
        if 0 <= item_id < len(self.items):
            return self.items[item_id]
        return None


@dataclass(frozen=True)
class _Candidate:
    """One not-yet-finalized bundle item, carried through this module's own
    two truncation passes with its serialized byte size precomputed once
    (rather than recomputed on every budget comparison)."""

    kind: ContextItemKind
    file_path: str
    symbol_name: str | None
    content: str
    line_range: tuple[int, int] | None
    size: int
    # Position within this candidate's own originating group (0 = best,
    # already ordered best-first by the query that produced it) -- what
    # layer 2b's truncation pass actually ranks on, so a similarity match
    # ranked 0 in its own group survives ahead of a caller hit ranked 9 in
    # its own group, regardless of which group was appended to `candidates`
    # first.
    rank: int


def _serialized_size(
    *,
    kind: ContextItemKind,
    file_path: str,
    symbol_name: str | None,
    content: str,
    line_range: tuple[int, int] | None,
) -> int:
    """The UTF-8 byte size of one item's JSON-serialized form — the same
    ``json.dumps(...).encode("utf-8")`` convention
    ``review/llm_review.py``'s own ``_render_static_findings_context``
    already uses for rendering structured context into the prompt, applied
    here per-item so this module's budget enforcement measures the same
    unit of "bytes actually added to the prompt" that ``diff_byte_size`` and
    ``max_total_bytes`` are themselves stated in. ``id`` is deliberately
    excluded (not yet assigned at this point in assembly, and its own
    digit-width contribution is negligible for budgeting purposes).
    """

    payload = {
        "kind": kind.value,
        "file_path": file_path,
        "symbol_name": symbol_name,
        "content": content,
        "line_range": list(line_range) if line_range is not None else None,
    }
    return len(json.dumps(payload).encode("utf-8"))


def serialized_item_size(item: ContextBundleItem) -> int:
    """Public wrapper around ``_serialized_size`` for external callers
    (e.g. ``ContextBuilderStep``'s own per-run observability log) that need
    to report the same byte count this module's budget enforcement actually
    measures a bundle item against — not just the item's raw ``content``
    size, which understates the true serialized/enforced size."""

    return _serialized_size(
        kind=item.kind,
        file_path=item.file_path,
        symbol_name=item.symbol_name,
        content=item.content,
        line_range=item.line_range,
    )


def _apply_per_item_cap[T](items: Sequence[T], ceiling: int) -> tuple[Sequence[T], int]:
    """Keeps the first ``ceiling`` items (each input group is already
    ordered best-first by its own query), returning ``(kept, dropped_count)``
    — the same "leading survivors, trailing excess dropped" shape
    ``indexing/pipeline.py``'s own ``_apply_cost_ceilings`` uses."""

    if len(items) <= ceiling:
        return items, 0
    return items[:ceiling], len(items) - ceiling


def _build_candidates(
    hits_by_key: Mapping[str, Sequence[_CallerHitLike]],
    *,
    kind: ContextItemKind,
    ceiling: int,
    ceiling_name: str,
    job_id: str,
) -> tuple[list[_Candidate], int]:
    """Applies Layer 1's per-item soft cap to every group in ``hits_by_key``
    and builds one ``_Candidate`` per surviving hit — shared by both the
    caller-hit and similarity-hit groups in ``assemble_context_bundle``,
    which previously duplicated this exact per-item-cap →
    ``_Candidate``-construction → drop-warning-log sequence once per group,
    differing only in ``kind`` and the hit objects' own field names (both
    ``review.context_builder.CallerHit`` and ``indexing.chunk.Chunk`` are
    structurally compatible with ``_CallerHitLike`` — see this module's own
    "Caller-hit type is structural" docstring note).
    """

    candidates: list[_Candidate] = []
    dropped_total = 0
    for hits in hits_by_key.values():
        kept, dropped = _apply_per_item_cap(list(hits), ceiling)
        dropped_total += dropped
        for rank, hit in enumerate(kept):
            candidates.append(
                _Candidate(
                    kind=kind,
                    file_path=hit.file_path,
                    symbol_name=hit.symbol_name,
                    content=hit.content,
                    line_range=hit.line_range,
                    size=_serialized_size(
                        kind=kind,
                        file_path=hit.file_path,
                        symbol_name=hit.symbol_name,
                        content=hit.content,
                        line_range=hit.line_range,
                    ),
                    rank=rank,
                )
            )
    if dropped_total:
        logger.warning(
            "context bundle: %s ceiling exceeded; dropping excess",
            ceiling_name,
            extra={
                "job_id": job_id,
                "ceiling": ceiling,
                "dropped_count": dropped_total,
            },
        )
    return candidates, dropped_total


def assemble_context_bundle(
    caller_hits: Mapping[str, Sequence[_CallerHitLike]],
    similarity_hits: Mapping[str, Sequence[Chunk]],
    diff_byte_size: int,
    max_total_bytes: int,
    *,
    job_id: str,
) -> tuple[ContextBundle, dict[str, int]]:
    """Assembles Phase 2's blast-radius caller hits and Phase 3's semantic
    similarity matches into one budget-enforced ``ContextBundle``, per US9
    and this module's own docstring.

    ``caller_hits`` is keyed by changed (pre-change) symbol name — each
    value already ordered best-first by ``GraphQuery.find_callers``.
    ``similarity_hits`` is keyed by an opaque per-changed-chunk identifier
    chosen by the caller (e.g. the changed chunk's own file path) — each
    value already ordered nearest-first by ``SimilarityQuery.find_similar``.
    Both may be empty (US9's zero-hits case), producing an empty bundle.

    ``diff_byte_size`` is the current job's own bare-diff byte size (the
    numerator of the spec's proportional budget); ``max_total_bytes`` is the
    absolute ceiling (``WorkerSettings.llm_max_diff_bytes``) both the
    combined budget and the single-oversized-item check are capped against.
    Neither is read from settings here — this module stays pure and
    no-I/O; the caller (Phase 5's ``ContextBuilderStep``) is responsible for
    supplying both.

    ``job_id`` is threaded through purely so this function's own
    ceiling/truncation ``warning`` logs can be correlated to the
    originating job, mirroring ``indexing/pipeline.py``'s own
    ``_apply_cost_ceilings(..., *, job_id: str)`` signature.

    Returns ``(bundle, dropped_counts)`` — the second element carries this
    function's own four drop tallies (``caller_edges_dropped``,
    ``similarity_matches_dropped``, ``oversized_items_dropped``,
    ``budget_truncated_dropped``), each already ``0`` when that layer
    dropped nothing. Phase 5's own Observability requirement is that
    ``ContextBuilderStep``'s per-run log surfaces "dropped-by-ceiling"
    counts for both this module's own per-item ceilings and
    ``review.context_symbols``' upstream per-job ceilings — the immediate
    ``warning`` logs below remain the point-in-time signal, this return
    value is what lets the caller fold the same counts into its own
    per-run summary.
    """

    # --- Layer 1: per-item soft caps, applied independently per group. ---
    caller_candidates, caller_dropped = _build_candidates(
        caller_hits,
        kind=ContextItemKind.CALLER,
        ceiling=MAX_CALLER_EDGES_PER_SYMBOL,
        ceiling_name="max_caller_edges_per_symbol",
        job_id=job_id,
    )
    similarity_candidates, similarity_dropped = _build_candidates(
        similarity_hits,
        kind=ContextItemKind.SIMILARITY,
        ceiling=MAX_SIMILARITY_MATCHES_PER_CHUNK,
        ceiling_name="max_similarity_matches_per_chunk",
        job_id=job_id,
    )
    candidates: list[_Candidate] = [*caller_candidates, *similarity_candidates]

    # --- Layer 2a: drop any single item whose own size alone exceeds the
    # absolute ceiling, regardless of the (possibly smaller) proportional
    # budget computed below. ---
    surviving: list[_Candidate] = []
    oversized_dropped = 0
    for candidate in candidates:
        if candidate.size > max_total_bytes:
            oversized_dropped += 1
            logger.warning(
                "context bundle: single item exceeds max_total_bytes; dropping item entirely",
                extra={
                    "job_id": job_id,
                    "file_path": candidate.file_path,
                    "symbol_name": candidate.symbol_name,
                    "item_bytes": candidate.size,
                    "max_total_bytes": max_total_bytes,
                },
            )
            continue
        surviving.append(candidate)

    # --- Layer 2b: truncate the combined, per-item-capped, non-oversized
    # set lowest-ranked-first until the total is at or under the
    # proportional budget. "Lowest-ranked-first" is a genuine cross-group
    # rank — each candidate's own `rank` (its position within its own
    # originating group) — not group-append order: a similarity match
    # ranked 0 in its own group is dropped only after every caller hit
    # ranked 9 (and every other candidate ranked 9) has already gone,
    # regardless of which group was appended to `candidates` first.
    # `diff_byte_size` is clamped at 0 (never negative in practice, but
    # defensively) before the plain `min()` below — no division anywhere in
    # this function, so `diff_byte_size == 0` simply collapses the budget
    # to zero without any special-casing. ---
    budget = min(max(diff_byte_size, 0), max_total_bytes)
    total_bytes = sum(candidate.size for candidate in surviving)
    truncated_dropped = 0
    if total_bytes > budget:
        # Worst rank first; stable sort keeps original append order as the
        # tie-break among candidates sharing the same rank.
        drop_order = sorted(range(len(surviving)), key=lambda i: surviving[i].rank, reverse=True)
        to_drop: set[int] = set()
        for index in drop_order:
            if total_bytes <= budget:
                break
            to_drop.add(index)
            total_bytes -= surviving[index].size
            truncated_dropped += 1
        surviving = [candidate for i, candidate in enumerate(surviving) if i not in to_drop]
    if truncated_dropped:
        logger.warning(
            "context bundle: total byte budget exceeded; truncating lowest-ranked items",
            extra={
                "job_id": job_id,
                "budget_bytes": budget,
                "dropped_count": truncated_dropped,
            },
        )

    items = tuple(
        ContextBundleItem(
            id=index,
            kind=candidate.kind,
            file_path=candidate.file_path,
            symbol_name=candidate.symbol_name,
            content=candidate.content,
            line_range=candidate.line_range,
        )
        for index, candidate in enumerate(surviving)
    )
    dropped_counts = {
        "caller_edges_dropped": caller_dropped,
        "similarity_matches_dropped": similarity_dropped,
        "oversized_items_dropped": oversized_dropped,
        "budget_truncated_dropped": truncated_dropped,
    }
    return ContextBundle(items=items), dropped_counts
