"""memory.decay — the pure, no-I/O ranking-time decay math behind US-M9.

Per [ADR-0005](../docs/adr/0005-pure-no-io-logic-unit-tested.md), pure
domain logic like this is Unit-tested directly, with no fake/adapter
needed, despite living in a domain package (``memory``) that otherwise has
no I/O tier of its own.

Exponential half-life decay, never a deletion/expiry job — an old,
unused approved entry naturally stops influencing new findings over time
(needing closer and closer raw similarity to still clear the comparison
threshold) rather than requiring an admin to notice and prune it, per
``docs/specs/spec-memory.md``'s US-M9 and
[ADR-0009](../docs/adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md)
decision 3's own "weighted by the entry's own ranking-time decay" text.
"""

from __future__ import annotations

from datetime import datetime


def effective_similarity(
    raw_similarity: float,
    *,
    approved_at: datetime,
    half_life_days: float,
    now: datetime,
) -> float:
    """``raw_similarity * 0.5 ** (days_since_approved_at / half_life_days)``
    — US-M9's exact formula, evaluated exactly (a fractional
    ``days_since_approved_at``, not truncated to whole days, so a
    comparison run partway through a day still decays smoothly rather than
    jumping once per midnight).

    ``approved_at`` is the reference point (an entry's trust clock starts
    when a human approved it, not when the raw signal was first captured,
    per US-M9's own text) — the caller (``adapter/storage/memory_repository.py``'s
    ``PostgresMemoryRepository.best_effective_similarity``) is
    responsible for only ever calling this against an ``approved`` entry's
    own ``approved_at`` timestamp; this function itself has no concept of
    entry status.

    A negative ``days_since_approved_at`` (``now`` before ``approved_at`` —
    should not happen given ``approved_at`` is server-assigned at approval
    time, but not assumed impossible against clock skew) decays *upward*
    from ``raw_similarity`` under the same formula rather than being
    clamped — mathematically consistent (``0.5 ** negative > 1``, though the
    caller's own ``raw_similarity <= 1.0`` cosine-similarity bound keeps the
    result bounded in practice), and simpler than inventing an undocumented
    clamping rule this function's own contract doesn't need.
    """
    days_since_approved_at = (now - approved_at).total_seconds() / 86400.0
    return float(raw_similarity * 0.5 ** (days_since_approved_at / half_life_days))
