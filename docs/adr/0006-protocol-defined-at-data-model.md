# ADR: A Protocol may be defined alongside the data model it produces or substantively operates on, not only at its first consumer

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

[`docs/rules/architecture.md`](../rules/architecture.md)'s Decoupling
section says interfaces are `typing.Protocol`s "defined at the consumer."
[`docs/plans/plan-phase-1-walking-skeleton.md`](../plans/plan-phase-1-walking-skeleton.md)'s
Phase 4 section defines `ingestion.diff.DiffFetcher` in `ingestion/diff.py`
— the package that owns the `Diff` pydantic model the Protocol produces —
rather than in `review/prefilter.py`, its first real consumer. This is a
deliberate deviation from "defined at the consumer," not an oversight;
`architecture.md`'s own header requires a new ADR for any deviation, not a
one-off exception. This ADR documents the general rule the deviation
follows, so it doesn't read as an unreconciled inconsistency the next time
a similar case arises.

## Context

`DiffFetcher` has three consumers across this plan: `review/prefilter.py`
(Phase 4, `fetch()` only), `review/static_analysis.py` (Phase 5,
`fetch_content()` only), and `review/llm_review.py` (Phase 6, `fetch()`'s
patch data only). Defining the Protocol at any one of those three —
"the first consumer" — would be an arbitrary choice: the other two would
then import it from a sibling step module that happens to have no other
relationship to them, and a future fourth consumer would face the same
arbitrary choice again. `ingestion/diff.py` is this plan's designated home
for the `Diff` model every one of those three consumers receives —
mirroring, at a smaller scale, how `findings/models.py` owns `Finding` for
every consumer that produces or reads one.

The same shape recurs for `findings.redact.TextRedactor` (Phase 5): it
produces no data model of its own (it mutates `findings/models.py`'s
`Finding` text fields in place and returns `str`), but it substantively
operates on `Finding` data, and is reused by two sibling step modules
(Phase 5's static-analysis step and Phase 6's llm-review step) — the same
"which sibling owns it" problem `DiffFetcher` has, just without a
Protocol-owned data model of its own to anchor it to. Both cases are one
underlying deviation from "defined at the consumer," not two unrelated
ones.

## Decision

A Protocol may be defined alongside the data model it operates over,
rather than strictly at its first consumer, when:

- The Protocol **produces** a pydantic data model owned by a specific
  package (e.g. `DiffFetcher.fetch() -> Diff`, `Diff` owned by
  `ingestion/diff.py`) — the Protocol is defined in that same package,
  alongside the model; or
- The Protocol produces no data model of its own, but substantively
  **operates on** one bounded context's data model (e.g. `TextRedactor`
  mutating `findings/models.py`'s `Finding`) — the Protocol is defined in
  that data model's own package instead.

Either way, only when **more than one consumer** would otherwise each need
to import the Protocol from an arbitrarily-chosen "first" consumer module.
A Protocol with exactly one real consumer still follows the ordinary
"defined at the consumer" rule unchanged — this ADR does not relax that
case.

This ADR is scoped to Protocol-placement deviations specifically. It does
not cover `findings/markdown.py`'s separate plain-function-vs-Protocol
acceptance (Phase 7) — that's a different kind of deviation (no Protocol
exists there at all, by design, not a Protocol placed somewhere other than
the first consumer) and stands on its own reasoning.

**Scope, extended per phase as new cases land (not re-litigated per
case):**

- Phase 4: `ingestion.diff.DiffFetcher`, defined in `ingestion/diff.py`
  alongside the `Diff`/`DiffFile` models it produces.
- Phase 5 (this ADR's scope extended by that phase's own PR, not a second
  ADR): `findings.redact.TextRedactor`, defined in `findings/redact.py` —
  the package `Finding` (the data it substantively operates on) belongs
  to.

## Consequences

**Easier / positive:**

- No consumer imports a Protocol from an arbitrarily-chosen sibling step
  module that happens to have been built first — every consumer imports
  from the package that actually owns the concept, the same way every
  consumer of `Finding` already imports it from `findings/models.py`.
- A future fourth (or fifth) consumer needs no re-litigation of "which
  existing consumer module should this Protocol live in now" — the
  placement was never tied to consumer order in the first place.

**Harder / trade-offs:**

- A future implementer has to correctly judge "does this Protocol
  genuinely have more than one consumer, and does it produce/operate on a
  specific package's data model" before reaching for this exemption — a
  boundary that's usually obvious but not always. Mitigated the same way
  ADR-0005 mitigates its own equivalent risk: this ADR's own scope list
  above names each case explicitly; a new case has to be added here, not
  silently assumed to qualify.
- `ingestion/diff.py` and `findings/redact.py` each now define a Protocol
  neither package itself consumes — a reader unfamiliar with this ADR
  could reasonably ask "why does the model-owning package define an
  interface it never calls." This docstring-plus-ADR pairing is the
  mitigation, not a structural fix; both modules' own docstrings point
  here.

## Related

- [`docs/rules/architecture.md`](../rules/architecture.md) — the rule this
  ADR formally deviates from (verification tag `WKPYNQ`).
- [`docs/adr/0005-pure-no-io-logic-unit-tested.md`](0005-pure-no-io-logic-unit-tested.md) —
  the sibling ADR this plan's own review modeled this one's shape on (a
  narrow, explicitly-scoped exemption from a stated architecture rule,
  extended per phase rather than re-derived per case).
- [`docs/plans/plan-phase-1-walking-skeleton.md`](../plans/plan-phase-1-walking-skeleton.md) —
  Phase 4's own ADR bullet (creation) and Phase 5's (scope extension).
