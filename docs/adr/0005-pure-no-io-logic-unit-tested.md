# ADR: Pure, no-I/O logic is Unit-tested regardless of story or package

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

[`docs/rules/testing.md`](../rules/testing.md)'s "every user story maps to
at least one integration test, not a unit test" rule is stated without a
fakes/no-I/O carve-out, and states any deviation "requires a new ADR, not a
one-off exception." Some logic this plan's Phase 1 introduces is pure,
no-I/O decision logic with no adapter or external system in the loop — no
test at that logic's own granularity can structurally satisfy testing.md's
middle-tier definition ("one real adapter ... against a real or
high-fidelity recorded external system"). This ADR formally exempts that
narrow case: pure, no-I/O logic gets Unit-tier coverage regardless of which
user story it serves or which package it happens to be physically
implemented in.

## Context

[`docs/plans/plan-phase-1-walking-skeleton.md`](../plans/plan-phase-1-walking-skeleton.md)'s
Phase 1 section introduces `adapter/github/triggers.py`'s declarative
`(event_type, action)` lookup table — a pure function with no I/O of its
own, even though it's physically implemented inside `adapter/github`, a
package testing.md otherwise assigns wholesale to the Integration tier. A
pure function with no I/O doesn't structurally fit an Integration-tier
definition just because the file (or package) it lives in also contains
I/O elsewhere.

This same structural gap recurs elsewhere in the same plan — the pipeline
step framework's own pure orchestration stories (US-F1/F2/F3/F4/F6/F7, no
adapter or external system in the loop), the pre-filter step's pure
filtering logic (US3), and `adapter/analyzers/subprocess_runner.py`'s
path-traversal/mode-guard logic — all genuinely pure, no-I/O logic that
would otherwise force either a contrived "integration" test with no real
boundary crossing (defeating the tier's purpose) or a silent,
un-reconciled deviation from testing.md in each of several separate
places. This ADR resolves all of them under one principle in one place,
scoped initially to the one case Phase 1 itself needs
(`adapter/github/triggers.py`) and extended by later phases as their own
cases land, rather than re-derived or re-justified independently each time.

## Decision

Pure, no-I/O logic — a function or decision table with no real I/O
(network, subprocess, filesystem, database) in its own call path — is
tested at **Unit tier**, using `docs/rules/testing.md`'s existing Unit-tier
conventions (fakes at Protocol boundaries, co-located `test_*.py`, no real
Postgres/network/subprocess), regardless of:

- which PRD user story it serves (even a story whose PRD-assigned test
  level implies Integration, e.g. via `pipeline-step-framework.md`'s own
  Testing table), or
- which package it is physically implemented in (even a package
  testing.md's tier language would otherwise assign wholesale to the
  Integration tier, e.g. an adapter package).

This is a narrow, mechanical exemption, not a general license to avoid
Integration tests: it applies only where the logic under test genuinely
performs no I/O in its own execution path. The moment a case needs a real
adapter/external system to prove its behavior, the ordinary Integration-tier
rule applies unchanged.

**Scope, extended per phase as new cases land (not re-litigated per case):**

- Phase 1: `adapter/github/triggers.py`'s `(event_type, action)` lookup
  table (`adapter/github/test_triggers.py`).
- Phase 2 (this ADR's scope extended by that phase's own PR, not a second
  ADR): the pipeline step framework's pure orchestration stories
  (US-F1/F2/F3/F4/F6/F7 — `review/test_step_boundaries.py`,
  `review/test_step_registry.py`, `review/test_static_analysis_step.py`)
  and `apps/worker/test_settings.py`'s startup-validation predicate (US-F7).
- Phase 4 (this ADR's scope extended by that phase's own PR, not a second
  ADR): the pre-filter step's pure filtering story, US3 —
  `review/test_prefilter_step.py`, against a fake `DiffFetcher` (the step
  itself accepts any `DiffFetcher` implementation; the *filtering logic*
  under test — ignore patterns, the materialization/aggregate-bytes
  ceilings — performs no I/O of its own, matching the PRD's own
  Unit-only assignment for this story) — and
  `adapter/analyzers/subprocess_runner.py`'s path-traversal guard logic
  (`validate_relative_path`, `adapter/analyzers/test_subprocess_runner.py`),
  a pure path-string check with no I/O of its own despite living inside an
  adapter package.
- Phase 1 of `docs/plans/plan-deliver-context-builder.md` (this ADR's scope
  extended by that phase's own PR, not a second ADR): `review/context_symbols.py`'s
  diff-to-chunk resolution logic (US1/US10) —
  `review/test_context_symbols.py`, against already-fetched
  `pre_content`/`post_content` dicts (the hunk-header parsing, byte-offset
  overlap resolution, and per-file `ChunkParseError` isolation genuinely
  perform no I/O of their own; the module's own `fetch_diff_content`
  orchestration function, which *does* do real I/O via `DiffFetcher`/
  `RepoTreeFetcher`, is deliberately excluded from this exemption and proven
  instead by Phase 5's end-to-end integration test).
- Phase 4 of `docs/plans/plan-deliver-context-builder.md` (this ADR's scope
  extended by that phase's own PR, not a second ADR): `review/context_bundle.py`'s
  context assembly and budget enforcement logic (US9) —
  `review/test_context_bundle.py`, against already-computed caller-hit and
  similarity-hit values (per-item cap enforcement, proportional-total
  truncation, the single-oversized-item-dropped-entirely case, and the
  `diff_byte_size = 0` case all perform no I/O of their own — `assemble_context_bundle`
  only aggregates and truncates already-fetched hits handed to it).
- Phase 5 of `docs/plans/plan-deliver-context-builder.md` (this ADR's scope
  extended by that phase's own PR, not a second ADR):
  `apps/worker/settings.py`'s `WorkerSettings._context_builder_timeout_margin_is_valid`
  validator (Assumption 7's margin invariant) — `apps/worker/test_settings.py`,
  a pure cross-field numeric comparison with no I/O, mirroring
  `apps/webhook_api/test_settings.py`'s existing
  `pytest.raises(ValidationError)` pattern for its own analogous validator.
- Phase 6 of `docs/plans/plan-deliver-context-builder.md` (this ADR's scope
  extended by that phase's own PR, not a second ADR): `review/citation.py`'s
  `render_citation` — `review/test_citation.py`, against an
  already-assembled `ContextBundle` (looking up already-retrieved metadata
  by ID, formatting it, and logging on an unknown ID or a missing
  `line_range` all perform no I/O of their own; `render_citation` never
  reads a `Chunk`'s content or talks to Postgres itself).
- Later phases extend this same list as their own pure, no-I/O cases land —
  each phase's own plan section names the specific addition; this ADR's own
  text is updated to list it, rather than a new ADR being created per case.

## Consequences

**Easier / positive:**

- One documented exemption, referenced by name everywhere it applies,
  instead of several silent, unreconciled deviations from testing.md's
  literal text scattered across the plan.
- Pure logic gets fast, deterministic Unit coverage (no testcontainers,
  no real subprocess) without contriving an artificial "integration" test
  that doesn't actually cross a real boundary.
- Keeps testing.md's actual guarantee intact: every story that *does*
  involve a real boundary crossing still gets a real Integration test —
  this ADR only narrows what counts as "the story's own logic" for the
  handful of genuinely pure cases.

**Harder / trade-offs:**

- A future implementer has to correctly judge "does this really have zero
  I/O in its own path" before reaching for this exemption — a boundary
  that's usually obvious (a lookup table, a pure filter) but not always;
  misapplying it would quietly erode testing.md's Integration-tier
  guarantee. Mitigated by this ADR's own explicit scope list above: a new
  case has to be named here, not silently assumed to qualify.
- Real, end-to-end proof that the pure logic is wired correctly into its
  real caller still depends on some later Integration test incidentally
  covering it (e.g. `review/test_review_job.py`'s real, fully-wired
  registry) — this ADR does not claim that incidental coverage substitutes
  for a dedicated Integration test per story; it only exempts the
  story-level requirement itself for these specific, named cases.

## Related

- [`docs/rules/testing.md`](../rules/testing.md) — the rule this ADR
  formally deviates from (verification tag `RQPTVE`).
- [`docs/plans/plan-phase-1-walking-skeleton.md`](../plans/plan-phase-1-walking-skeleton.md) —
  Phase 1's own ADR bullet (creation) and Phase 2's (scope extension).
