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
- Phase 5 of `docs/plans/plan-deliver-verification-pipeline.md` (this ADR's
  scope extended by that phase's own PR, not a second ADR):
  `review/config.py`'s `resolve_review_pipeline_depends_on` (Assumption 6's
  hard-coded-to-this-one-chain resolver) and `intent_summary_can_produce_value`
  — `review/test_config.py`, against hand-built `StepEnablement` fixtures
  (both are pure, no-I/O boolean/dict logic operating only on an already-
  constructed `StepEnablement` instance, the same shape
  `context_builder_can_produce_value`/`static_analysis_can_produce_findings`
  already established this exemption for).
- Phase 1 of `docs/plans/plan-deliver-verification-pipeline.md` (this ADR's
  scope extended by that phase's own PR, not a second ADR): `review/diff_prompt.py`'s
  `render_diff`/`truncate_diff_text` (extracted from `review/review_generation.py`'s
  former private `_render_diff`/`_truncate_diff_text` once `review/intent_summary.py`
  needed the identical logic) and `review/intent_summary.py`'s `_cap_intent_summary`
  — `review/test_diff_prompt.py` and `review/test_intent_summary_step.py`, against
  already-rendered diff text/already-received LLM response text (per-file
  header/patch concatenation and byte- or char-boundary truncation, neither
  of which perform any I/O of their own).
- Phase 3 of `docs/plans/plan-deliver-verification-pipeline.md` (this ADR's
  scope extended by that phase's own PR, not a second ADR): `review/verification.py`'s
  `_parse_verdicts`, `_correlate_verdicts`, and `_passes_rubric` — `review/test_verification.py`,
  against hand-built LLM response payloads/`_VerificationVerdictItem` fixtures
  (JSON parsing with per-item validation tolerance, index correlation with
  fail-closed duplicate/out-of-range handling, and the four-criterion rubric
  check all operate only on already-received data, with no I/O of their own).
- Phase 2 of `docs/plans/plan-deliver-verification-pipeline.md` (this ADR's
  scope extended by that phase's own PR, not a second ADR): `review/llm_response.py`'s
  `build_completion_request` and `parse_llm_json_envelope` (extracted once
  `review/review_generation.py`, `review/intent_summary.py`, and
  `review/verification.py` all needed the identical completion-request
  assembly and JSON-envelope-parsing logic) — `review/test_llm_response.py`,
  against hand-built request parameters/already-received LLM response text
  (assembling a request payload and parsing/validating an already-received
  JSON envelope perform no I/O of their own).
- Phase 1 of `docs/plans/plan-deliver-product-knowledge.md` (this ADR's scope
  extended by that phase's own PR, not a second ADR): `review/product_knowledge.py`'s
  wire models and limits constants (US-PK16) —
  `adapter/product_knowledge/test_contract_schemas.py`, a static check that
  the committed JSON Schemas under `docs/reference/product-knowledge-contract/v1/`
  equal those generated from the models and that every committed example
  validates or fails as named (pydantic model definitions and file
  comparison perform no network or database I/O; later phases add pure
  functions to this module and extend this entry). Mutation testing covers
  this module through those unit tests only.
- Phase 2 of `docs/plans/plan-deliver-product-knowledge.md` (same
  mechanism): `ingestion/pr_metadata.py`'s title/head-ref normalization
  (`normalize_title`, `normalize_head_ref`), issue-key extraction/prefix
  filtering (`extract_issue_keys`, `filter_issue_keys`) and fail-closed `is_fork_pr`
  derivation (US-PK2 capture, US-PK20) — `ingestion/test_pr_metadata.py`;
  pure string/boolean logic over already-parsed webhook fields.
- Phase 3 of `docs/plans/plan-deliver-product-knowledge.md` (same
  mechanism): `review/product_knowledge.py`'s `should_query` (A1 exposure
  gate), `formulate_query`, `normalize_results`/`normalize_fetch`, the single
  `sanitize_url`/`url_display` sanitizer, `assemble_product_knowledge`,
  `product_knowledge_prompt_payload`, plus
  `review/context_bundle.py`'s `render_product_knowledge_section` /
  `product_knowledge_item` and `review/citation.py`'s product-knowledge
  block (US-PK2/PK3/PK6/PK7/PK20) — `review/test_product_knowledge.py`,
  `review/test_context_bundle.py`, `review/test_citation.py`; string,
  URL-parsing and bounded-arithmetic logic over already-received provider
  payloads, with the prompt/post wiring proven by the integration tests in
  `review/test_review_generation_step.py`, `test_verification_step.py` and
  `test_findings_output_step.py`.
- Phase 4 of `docs/plans/plan-deliver-product-knowledge.md` (same
  mechanism): `adapter/product_knowledge/egress.py`'s `canonicalize_host`,
  `split_host_port`, `classify_address`, `host_matches_allowlist`,
  `parse_allow_entry` and `endpoint_origin` (US-PK12,
  Amendment A3) — `adapter/product_knowledge/test_egress_pure.py`; stdlib
  `ipaddress`/`socket.inet_aton` string-and-address arithmetic with no
  network, DNS or socket I/O (`inet_aton` only parses text). The enforcing
  transport (`transport.py`) is deliberately a separate module so this one
  stays purely unit-testable and mutation-testable; its behavior is proven by
  `test_egress.py`/`test_client.py` against real sockets. The fake provider
  under `adapter/product_knowledge/testing/` is test tooling and is excluded
  from mutation testing (`[tool.mutmut] do_not_mutate`).
- Phase 6 of `docs/plans/plan-deliver-product-knowledge.md` (same
  mechanism): `adapter/product_knowledge/credentials.py`'s
  `parse_credential_map`, `parse_endpoint_allowlist` and
  `CredentialMap.binding_problem` (US-PK9/PK13, Amendment A2) —
  `adapter/product_knowledge/test_credentials.py`; JSON parsing, pydantic
  validation and the shared host canonicaliser only, no I/O, and the
  error-message-never-contains-a-token property is unit-asserted there. Plus
  `tenancy/settings.py`'s `ProductKnowledgeProviderSetting`,
  `validate_endpoint_url` and `effective_settings` (`tenancy/test_settings.py`).
  The keyed Postgres read and the fail-open resolver wrapper are adapter code
  proven by `adapter/storage/test_tenancy.py` against real Postgres.
- Phase 7 of `docs/plans/plan-deliver-product-knowledge.md` (same
  mechanism): no new pure function — the provider branch in
  `review/context_builder.py` is orchestration (async, I/O through injected
  Protocols) and is proven by the integration tests in
  `review/test_context_builder_step.py`; the pure functions it composes
  (`should_query`, `formulate_query`, `filter_issue_keys`,
  `normalize_results`/`normalize_fetch`, `assemble_product_knowledge`) are
  the ones already listed above and stay under mutation testing via their
  own unit tests. `review/product_knowledge.py` is therefore registered here
  as the pure module those unit tests cover, not the branch itself.
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
