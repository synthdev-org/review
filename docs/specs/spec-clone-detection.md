# Phase 10 — Whole-Codebase Clone Detection PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-10-17

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 10 ("Whole-codebase
duplication/clone detection") into a concrete spec. Adds one deterministic,
LLM-free review-pipeline step that tells a PR author when code their PR adds
duplicates code that already exists elsewhere in the repo — including in
files the PR never touched — built on Phase 2's Tree-sitter parse and
incremental indexing job
([`spec-indexing.md`](spec-indexing.md)). Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`); requirements are user stories with `Given/When/Then`
acceptance criteria per `testing.md`'s mandatory rule.

**Conventions found / fallbacks.** Spec format: `docs/specs/spec-*.md`
(structure mimicked from `spec-static-analysis-expansion.md` and
`spec-indexing.md`; `docs/documentation_index.md` lists them). Testing
pyramid: `docs/rules/testing.md`. Design system: N/A (no UI surface — see
"UX & Design"). All questions the interview would have asked were answered
from the documents, the code, and competitor documentation; every decision
is tagged **decided** or **assumption pending validation** under "Resolved
decisions".

## Problem & Goal

Phase 2's embedding-similarity search (`spec-context-builder.md` US4) only
fires for chunks that changed in the PR, and it feeds an LLM as *context* —
it never produces a finding. Nothing today tells an author "the 40 lines you
just added are a copy of `billing/invoice.ts`". The research this product
derives from ([`research-current-art.md`](../research/research-current-art.md),
Codacy profile) records Codacy's duplication engine flagging a clone spread
across files a diff never touched; Sonar, PMD CPD, jscpd and Code Climate all
ship token- or AST-based clone detection as a standard deterministic engine
(see "Competitor basis"). It is a gap in commercial parity and, more
importantly, a gap in what the author learns before merge.

**End user: the PR author** (and their reviewers). They want to know, while
the change is still cheap to fix, when they are copy-pasting something that
should be shared — and they want to be told only about duplication *they are
introducing*, with the other location named so they can act on it. Copy-paste
drift (a bug fixed in one copy and not the other) is the cost this prevents.

**Secondary end user: the platform operator**, who needs the feature
switchable, bounded in cost, free of new infrastructure or LLM spend, and
quiet by default — the research's most consistent finding is that
false-positive volume is why developers mute review tools.

**Goal / success metric (capability-based, matching Phases 1/2/9):** for a
repo in the three indexed languages (TypeScript, PHP, Go), a PR that adds or
modifies code which duplicates at least the configured minimum size of
normalized code elsewhere in the repo's default-branch index, or in another
file of the same PR, gets exactly one deterministic `duplication` finding per
duplicated region on its changed lines, naming both locations; a PR that
merely touches pre-existing duplication gets none; on each language's clean-code fixture the step reports nothing (the
noise bound, US-C8); no clone-detection
problem ever fails or delays a review beyond its step timeout; and with the
step disabled, posted output is identical to today's.

## Competitor basis

What the field does, and what this spec takes from it (sources: vendor docs
retrieved 2026-10-17; the repo's research doc for Codacy's observed behavior):

| Tool | Approach | Default minimum | Takeaway for this spec |
| --- | --- | --- | --- |
| SonarQube | Token-based; reports duplicated lines and density; gates on **new-code** duplication | 100 tokens across ≥ 10 lines (Java: 10 statements) | Adopt 100 tokens / 10 lines as the default; judge duplication on the *new* code only (US-C5). |
| PMD CPD | Token-based, normalizable identifiers/literals | 100 tokens | Same default; confirms token granularity. |
| jscpd | Token-based, many languages | 50 tokens, 5 lines | Shows the low end; 50 is the noise end of the range — rejected as default. |
| Code Climate (duplication engine, from `flay`) | Structural/AST similarity above a configurable "mass" | Per-language mass threshold | Structure-aware, but needs a per-language tuned number; we normalize tokens from the AST instead. |
| Codacy | CPD-style; counts clones *introduced by the PR*; caps analysis at 5,000 files per language | Configurable | Confirms PR-introduced scoping; confirms a repo-size cap is normal. Codacy also surfaces clones in files the diff did not touch — see decision D2 for where we deliberately differ. |

Every vendor above runs this as a **deterministic, non-LLM** engine; none
asks a model whether code is duplicated. This spec does the same.

## Scope

### In scope

- A new macro review-pipeline step, `clone_detection`, between
  `static_analysis` and `context_builder` (see Units), producing `Finding`s
  with `source="clone_detection"`, `rule_id="duplication"` — a distinct,
  filterable category with no change to `findings/merge.py` or the posting
  path beyond the ordering rule in US-C13.
- Clone fingerprints computed during indexing from the **same Tree-sitter
  parse** Phase 2 already produces, stored in a new table, kept incremental
  by the existing delta/prune logic (US-C1, US-C2).
- A fingerprint-schema version on the per-repo index tracker so existing
  repos are backfilled by the existing full-tree-diff machinery (US-C3).
- Detection of **Type-1/Type-2 clones**: identical code modulo whitespace,
  comments, identifier names and literal values (US-C7).
- PR-side detection against (a) the repo's default-branch index and (b) the
  PR's other changed files (US-C4, US-C6).
- Settings for enable/disable, minimum size, per-job finding cap and step
  timeout; Helm/Compose mirroring; operator runbook entry; metrics and
  dashboard panels; E2E.
- Memory suppression of dismissed duplication findings (US-C12), including a
  small amendment to ADR-0009 (see D6).

### Out of scope

- **Near-miss (Type-3) and semantic (Type-4) clones.** Embedding similarity
  already supplies semantically similar code to the LLM as context
  (`spec-context-builder.md` US4); promoting it to a deterministic finding
  would be non-reproducible and noisy. Revisit only with evidence.
- **Pairs where neither side is in the PR.** Not surfaced at PR time at all
  (D2). A scheduled "codebase health" report is a separate future job, not
  this phase and not designed here.
- **Same-file clones** (two regions inside one file). Excluded from v1 to
  keep the first release quiet; the data model does not preclude adding them.
- **Languages beyond TypeScript, PHP, Go** — the indexed set. A fourth
  language arrives with its chunker grammar (Phase 2's rule), then is
  clone-detected for free.
- **Test files, generated files, vendored paths** as either side of a pair
  (US-C8). No setting to include tests in this phase.
- **Per-language or per-repo threshold overrides.** One global threshold;
  there is no repo-data to justify per-language numbers (D4).
- **Adopting an external clone engine** (jscpd, PMD CPD) — decided against
  (D1).
- **Auto-fix / extract-function suggestions** — Phase 11.
- **Admin UI controls** — bootstrap/Helm settings only, as every existing
  `*_enabled` flag.
- **Any generative LLM call.** Hard requirement, consistent with ADR-0007.
- **Cross-repo clones.** The index is per-repo (`spec-indexing.md`).

### Priority

| Priority | Unit | Why |
| --- | --- | --- |
| Must | A — Fingerprint algorithm | The engine; nothing works without it. |
| Must | B — Storage, indexing integration, backfill | Without it the whole-codebase side is empty. |
| Must | C — Review step | The only end-user-visible unit. |
| Must | E — Settings, ops, observability | Nothing is releasable without a kill switch and visibility. |
| Must | F — E2E | Proves the goal end to end and exercises the new dashboard panels. |
| Should | D — Memory suppression | Stops a dismissed finding recurring on every push; the cap and new-code rule already bound the damage without it. |

If time forces a cut, cut D: a duplicate comment recurring is annoying but
bounded (cap, US-C10); a missing kill switch or a noisy default is not.

## Requirements

**US-C1 — The index carries clone fingerprints for every indexed file**
As a platform operator, I want clone fingerprints produced as part of
indexing, so that a PR can be compared against the whole repo without
re-reading it.
- Given an `INDEX` job processes a TypeScript, PHP or Go file, When the file
  is upserted, Then fingerprints derived from that file's normalized token
  stream are stored keyed by `(repo, pr_number, file, language)` in the same
  transaction as the file's chunks and graph nodes (all-or-nothing per file,
  matching `indexing/pipeline.py`'s per-file isolation).
- Given a file whose parse fails, When the job runs, Then no partial
  fingerprints are stored for it and the other files proceed (US10 of
  `spec-indexing.md`, unchanged).
- Given an out-of-scope-language or ignored file, Then no fingerprints are
  produced (US9 of `spec-indexing.md`, unchanged).
- Fingerprinting makes zero LLM calls and zero network calls.

**US-C2 — Stale fingerprints never survive a change**
As a PR author, I want a deleted or rewritten file's old code to stop being
matched, so that I'm never told I duplicated code that no longer exists.
- Given a file is modified and re-indexed, Then its previous fingerprints are
  replaced, not accumulated.
- Given a file is removed, Then its fingerprints are deleted with its chunks
  and graph nodes.
- Given a file is renamed (removed path + added path), Then the old path's
  fingerprints are gone and the new path's are present.

**US-C3 — Existing repos are backfilled without a new admin action**
As a platform operator, I want a repo indexed before this feature shipped to
become clone-searchable on its own, so that the feature isn't blind to every
file nobody pushes to.
- Given a repo whose index tracker records an older (or no) fingerprint-schema
  version, When its next default-branch `INDEX` job runs, Then the job treats
  every in-scope file as changed (the degenerate "full index" case
  `spec-indexing.md` already defines), subject to the full-run ceiling
  (20,000 files / 2 GB) with excess files dropped and logged at `warning`,
  and records the current version only after the job completes.
- Given the current version is already recorded, Then no backfill occurs.
- Given a repo that has never been indexed or has not yet been backfilled,
  Then clone detection for its PRs returns no default-branch matches without
  error (the same "no index yet is a normal state" rule as
  `spec-context-builder.md` US7).
- The version also lets a future change to the normalization rules or
  window parameters re-fingerprint every repo by bumping one constant.

**US-C4 — A PR that copies existing code is told, with both locations**
As a PR author, I want to be told when code I added duplicates code elsewhere
in the repo, so that I can reuse it or extract it before merging.
- Given a PR adds a function whose normalized body of at least the minimum
  size (default 100 tokens spanning at least 10 lines) matches code in a file
  the PR does not touch, When the review runs, Then exactly one finding is
  posted on the PR file at the first changed line of the duplicated region,
  with `source="clone_detection"`, `rule_id="duplication"`, `severity="low"`,
  and a message naming the other file and its line range
  (e.g. ``Duplicates 38 lines of `src/billing/invoice.ts` (lines 120–157);
  consider extracting shared code.``).
- Given a duplicated region matches several existing locations, Then one
  finding is posted naming the best match (largest, then lexicographically
  first path) plus `and N more` — never one comment per location.
- The message contains paths and line numbers only: no source text from
  either location is quoted (so redaction cannot be bypassed and no private
  code beyond a path is exposed).

**US-C5 — Only duplication the PR introduces is flagged**
As a PR author, I want to be blamed only for code I wrote, so that touching a
file with old duplication doesn't produce comments.
- Given a duplicated region on the PR side in which half or fewer of the
  region's lines are added or modified by the PR's `patch` (a region is kept
  only when **more than half** are), When results are
  filtered, Then it is dropped and counted in `filtered_count`.
- Given a changed file the diff reports without a `patch`, Then no clone
  findings are produced for it.
- Given a pre-existing clone pair, neither side touched, Then nothing is
  posted (D2).

**US-C6 — Duplicates inside the PR are found; stale copies of PR-touched files are ignored**
As a PR author, I want copy-paste between two files in my own PR caught, and I
don't want to be compared against the *old* version of a file I'm rewriting.
- Given a PR whose changed files A and B contain a duplicated region of at
  least the minimum size, When the review runs, Then one finding is posted
  naming the pair (anchored in the later-sorted path so the pair is reported
  once).
- Given a file F is modified, renamed or deleted by the PR, Then
  default-branch fingerprints for F's path are excluded from matching (they
  describe code the PR changes); F's PR-head content participates only in the
  PR-internal comparison.

**US-C7 — Renamed variables and changed literals still match; different structure does not**
As a PR author, I want a copy with renamed identifiers or edited constants
caught, since that is how copy-paste really looks.
- Given two regions identical except for identifier names and literal values,
  and for whitespace and comments, When compared, Then they match.
- Given two regions with the same identifiers but different operators,
  keywords, control flow or call structure, Then they do not match.
- Given a region where only a comment or blank line differs, Then it still
  matches.
- Detection finds **every** duplicated run of at least the minimum token size
  (no false negatives at or above the threshold, among the matches the
  fan-out caps below allow — at most 20 stored matches per fingerprint and 200
  candidate files per job; excess is counted and logged, so boilerplate that
  matches everywhere cannot exhaust a job).

**US-C8 — Small, boilerplate and non-production code is not flagged**
As a PR author, I want trivial similarity ignored, so that the tool doesn't
cry wolf.
- Given a duplicated run below the minimum token size **or** below the
  minimum line span, Then no finding is produced (both must be met).
- Given either side is a test file (`*_test.go`, `*.test.*`, `*.spec.*`, paths
  under `test/`, `tests/`, `__tests__/`), a generated or vendored file
  (`review/prefilter.py`'s existing ignore-pattern categories), Then the pair
  is not reported.
- Given a run consisting solely of `import`/`use`/`require` statements or
  license headers, Then it is not reported.
- Given overlapping or adjacent matches between the same pair of files,
  Then they are merged into one maximal region and reported once.
- **Noise bound:** given each language's committed clean-code fixture
  (idiomatic code with no seeded copy) indexed as the default branch, and a
  PR adding different idiomatic code, Then zero clone findings are produced.

**US-C9 — A hash collision cannot produce a false finding**
As a PR author, I want every posted duplication to be real.
- Given two regions whose fingerprints collide but whose normalized token
  sequences differ, When matched, Then no finding is produced.
- Reported regions are extended to the full extent of the matching token
  sequence (not just the fingerprinted window) and trimmed to equality.

**US-C10 — A PR can't be flooded with duplication comments**
As a PR author, I want a bounded number of duplication comments, so that a
large refactor or a vendored-in directory doesn't bury the review.
- Given more than `clone_detection_max_findings` (default 5) duplicated
  regions, Then the largest regions are kept, the rest are dropped, and the
  dropped count is logged at `warning` and exposed as a metric.
- Given two regions on the same file that overlap or are adjacent, Then they
  are reported as one finding.

**US-C11 — Clone findings behave like every other finding**
As a PR author, I want duplication findings to dedupe, order, redact and post
like the rest.
- Given a clone finding, When the findings merger and output steps run, Then
  no per-source branch is needed in `findings/merge.py`; the posting path is
  unchanged and the finding renders as an ordinary inline comment.
- Given a clone finding and an LLM finding on the same line with similar
  message text, Then the existing dedupe rule applies unchanged.

**US-C12 — A dismissed duplication finding stops recurring**
As a PR author, I want to dismiss a duplication comment once and not see it
again.
- Given an approved `dismissed_finding` Memory entry (same repo) whose
  fingerprint (`file` + `rule_id` + `message`) is above the similarity
  threshold of a new clone finding, When the clone step runs, Then the
  finding is suppressed and counted, via the same suppression logic and
  threshold ADR-0009 specifies for LLM findings (no LLM call).
- Given only `pending` entries, Then nothing is suppressed (design goal 9).
- Requires amending ADR-0009 decision 3 (D6).

**US-C13 — Duplication never crowds out security or correctness findings**
As a PR author, I want a secret or SAST finding to survive the finding
ceilings even when duplication is reported.
- Given more findings than a ceiling (`REDACTION_FINDING_CEILING` or
  `POSTED_FINDINGS_CEILING`), When the ceiling is applied, Then at equal
  severity `clone_detection` findings sort after every other source,
  including LLM findings; dropped counts keep being logged.

**US-C14 — Clone detection never blocks or fails a review**
As a PR author, I want my review to complete even if clone detection can't.
- Given no fingerprint rows exist for the repo, a database error, a
  fingerprint parse error on a PR file, or the step timeout is reached, When
  the step runs, Then it returns the incoming findings unchanged with zero
  clone findings, logs the cause at `warning` or `error`, and the job
  completes (the Context Builder US7 precedent). It is never raised as a
  fatal pipeline failure.
- Given a PR file in an unsupported language, Then it is skipped without
  affecting other files.

**US-C15 — Operators can switch it off and bound it**
As a platform operator, I want a kill switch and tunable bounds.
- Given `clone_detection_enabled=false`, Then the step is disabled (registered, as every step is, and gated by the
  enablement mapping so it never runs), no
  clone query is made, and posted output equals pre-Phase-10 output byte for
  byte. Fingerprint writing during indexing is **not** gated by this flag
  (it is cheap and keeps re-enabling instant); it is gated only by the
  existing `INDEXING_ENABLED`.
- Given `clone_detection_min_tokens` (default 100), `clone_detection_min_lines`
  (default 10), `clone_detection_max_findings` (default 5) and
  `clone_detection_step_timeout_seconds` (default 30), Then each is honored;
  invalid values (≤ 0) fail Worker startup with a clear message. Because
  stored fingerprints guarantee detection only at 100 or more tokens,
  `clone_detection_min_tokens` may be raised but a value below 100 also fails
  startup; lowering the floor means new window parameters and a
  fingerprint-version bump (owner decision 2026-10-17).
- Given `clone_detection_enabled=true` with `static_analysis` and
  `context_builder` in either enablement state, Then
  `resolve_review_pipeline_depends_on` wires the chain correctly and the
  existing fail-fast configuration check still holds (extended with this
  step).
- Settings mirror one-for-one into the Helm `worker` block.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Review-time latency:** PR-side fingerprinting is local CPU; matching is
  one indexed lookup of the PR's fingerprints (`hash = ANY(...)`, batched)
  per job. Budget: the step completes inside
  `clone_detection_step_timeout_seconds` (default 30 s) on the E2E fixture
  and adds no more than 2 s to a review of a 50-file PR against a
  100,000-fingerprint fixture index (budgets to be confirmed by measurement
  in the plan; a measured miss changes the budget by a recorded decision).
- **Index-time cost:** fingerprinting adds only a token walk over the parse
  Phase 2 already holds (no second parse); added indexing wall time ≤ 10 % on
  the Phase 2 fixtures (to be measured).
- **Storage:** fingerprints are *sampled* (see D1), not one row per token
  position. Stored rows per file ≈ 2.5 % of its token count at defaults;
  recorded per fixture in the plan so growth is visible. To verify a match
  without refetching code (US-C9), each indexed file also stores its
  normalized per-token hashes and a line map: ≈ 4 bytes per token plus 4 per
  line (≈ 4 MB per million tokens; owner decision 2026-10-17, replacing the
  refetch-from-GitHub alternative). All I/O takes an explicit timeout.
- **Security:** pure parsing and DB reads; no subprocess, no repo-supplied
  code execution, no network. File paths in findings originate from the
  repo's own default-branch index (the `pr_number = 0` rows are never
  writable from a PR — Assumption 9 of `spec-indexing.md`), so a fork PR
  cannot poison what it is compared against. Messages are built from paths
  and integers only; they are passed through the existing `escape_markdown`
  and redaction like every message.
- **Reliability:** US-C14's fail-open; a corrupt or missing fingerprint row
  can only cause a missed finding, never a wrong one (US-C9).
- **Scale:** per-job work is O(changed tokens) fingerprinting plus one
  batched index lookup; independent of repo size apart from the index scan,
  covered by a composite index `(repo_id, pr_number, hash)`.

### Privacy / Compliance

No data leaves the deployment: fingerprints are hashes plus coordinates
stored in the customer's Postgres; no LLM, no embedding call, no egress. A
posted finding discloses a default-branch file path and line range to anyone
who can read the PR — the same exposure `spec-context-builder.md`'s
citations already accept.

### Observability

Extends `spec-observability.md`'s catalog rather than inventing parallel
names:
- Logs: `clone_detection` step start/outcome with `job_id`, files
  considered/skipped, `candidate_count`, `filtered_count` (US-C5/C8),
  `suppressed_by_memory_count`, `dropped_by_cap_count`, `finding_count`,
  duration; indexing logs gain `fingerprints_written`.
- Metrics: step runs by outcome, step duration, candidates, findings emitted,
  memory-suppressed, cap-dropped; index fingerprint rows written/deleted.
- Dashboard: Grafana panels for the above. **Lesson from Phase 7:** the
  `test_observability` E2E check fails when a new panel receives no data from
  the test's traffic — every panel added here must be exercised by
  `tests/e2e/test_clone_detection.py` or the check updated in the same
  change.

### Rollout & Release Safety

- **Migration:** one additive Alembic migration (new fingerprint table;
  tracker version column). No existing table is altered destructively.
- **Default:** `clone_detection_enabled=true` (assumption A2); operators
  wanting a quiet upgrade set it false first. Severity is `low` and the cap
  is 5, bounding the blast radius of a bad default.
- **Backfill load:** the first default-branch push after upgrade triggers a
  full-run-sized index job per repo (US-C3). The runbook documents this, the
  ceiling that bounds it, and that `INDEXING_ENABLED=false` defers it.
- **Rollback:** disable the step with one flag. The tables are **not** inert
  once the step reads them: old Worker code never maintains clone rows, so a
  revert or rolling upgrade leaves stale fingerprints. The plan's rollback
  resets `repo.clone_fingerprint_version` to 0 after old Workers drain, and
  ships the index writer and the review reader in separate releases. Dropping
  them is a separate, optional
  migration, not required for rollback.
- **Additive:** with the step disabled, output equals today's.

## UX & Design

N/A — no new UI surface. A duplication finding renders through the existing
inline-comment/check-run output as an ordinary finding. The one authored
artifact is the comment wording (US-C4): one sentence, names the other
location, suggests extraction, quotes no code. Per-step settings are
bootstrap configuration (Helm values/env), not Admin UI screens.

## Stakeholders & Sign-off / Acceptance Criteria

- Owner/sign-off: Andy Beak (sole author/approver per the ADRs' RACI).
- Phase acceptance: through the full Compose stack with a stubbed GitHub, a
  PR that adds a copy of an existing function (with renamed identifiers) in
  each of TypeScript, PHP and Go posts exactly one `duplication` finding per
  copy naming the right location; a PR that only edits a line inside a
  pre-existing clone posts none; with the step disabled, posted output equals
  today's.
- Not verifiable in this repo (carried as in Phases 1 and 9): behavior on a
  real repository with real-world code. The plan lists a manual verification
  against a real repo, including a measured false-positive rate review of the
  first findings.

## Phases / Units

- **Unit A — Fingerprint algorithm.** Pure, no-I/O logic in the `indexing`
  package (`indexing/clone.py`): normalized token stream from a Tree-sitter
  tree (comments/whitespace dropped, identifiers → one class, literals → one
  class, structure/operators kept), winnowed fingerprints, and a
  verification step that extends and trims candidate matches to token
  equality. Depends on: Phase 2's `indexing/tree.py` / `chunk.py` parse
  (reused, not re-parsed). *Risk:* **medium** — normalization rules differ
  per grammar (three grammars, first token-level use of Tree-sitter here);
  getting identifier/literal node classes wrong silently causes misses or
  false matches. *Decision:* algorithm family decided (D1); exact window
  parameters set from fixtures in the plan to satisfy "no false negatives at
  or above the threshold" by construction. *Acceptance:* US-C7, US-C8 (size,
  boilerplate-run rules), US-C9 unit tests per language.
- **Unit B — Storage, indexing integration, backfill.** Two new tables
  (plan 2026-10-17): `index_clone_fingerprint` (`repo_id`, `pr_number`,
  `file_path`, `hash`, `token_offset`) with composite index, and
  `index_clone_file` (per-file `language`, per-token hashes and a line map,
  from which start/end lines are derived); upsert/replace
  and prune inside `IndexRepository.upsert_file` / `delete_file`; tracker
  fingerprint-schema version and its backfill trigger. Depends on: A.
  *Risk:* **medium** — touches the Phase 2 transaction boundary; the
  backfill piggybacks on the full-tree-diff fallback (US8 of
  `spec-indexing.md`) and must be confirmed feasible in the plan; if the
  tracker cannot carry a version without a breaking change, the fallback is a
  one-time version-gated reindex job type — a plan-time decision, not
  silent. *Acceptance:* US-C1, US-C2, US-C3 integration tests (real
  Postgres/pgvector).
- **Unit C — Review step.** `review/clone_detection.py` (macro step):
  fingerprint PR-side changed content from prefilter output, batched lookup
  against `pr_number = 0` rows excluding PR-touched paths, PR-internal
  comparison, new-code filter, grouping/cap, `Finding` construction. Wired in
  `resolve_review_pipeline_depends_on` and `apps/worker/main.py`. Depends on:
  A, B. *Risk:* **medium** — the step-boundary test
  (`review/test_step_boundaries.py`) forbids step-to-step imports and the
  chain-resolver is hard-coded to the current five-step chain; both must be
  extended deliberately. *Acceptance:* US-C4, US-C5, US-C6, US-C10, US-C11,
  US-C14.
- **Unit D — Memory suppression.** Apply ADR-0009's similarity suppression to
  `source="clone_detection"` findings; amend ADR-0009 decision 3 (D6).
  Depends on: C. *Risk:* low-medium — suppression currently lives inside
  `VerificationStep` and is wired for LLM findings only; the plan must
  extract or share it, not duplicate it. *Acceptance:* US-C12.
- **Unit E — Settings, ordering, ops, observability.** Worker settings and
  startup validation, Helm/Compose mirror, finding-ceiling ordering (US-C13),
  runbook entry in `docs/reference/operations.md`, metrics, dashboard panels,
  `Source` Literal widened with `"clone_detection"`. Delivered incrementally
  with C. *Acceptance:* US-C13, US-C15.
- **Unit F — E2E.** `tests/e2e/test_clone_detection.py` over the Compose
  stack. Depends on: A–E. *Risk:* low. *Acceptance:* the phase-acceptance
  scenario under "Stakeholders & Sign-off" passes (a renamed-identifier copy
  per language posts one finding; a line edited inside a pre-existing clone
  posts none; disabled reproduces today's output) and every new dashboard
  panel receives data.

## Testing / Key Scenarios

Per `testing.md`: every story maps to at least one **integration** test; pure
no-I/O logic (token normalization, winnowing, match verification, region
grouping, boilerplate-run detection, ordering) is also unit-tested per
[ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md). Stories whose
behavior is entirely pure still get an integration test through the review
step on real parsed fixtures.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-C1 | An `INDEX` job on TS/PHP/Go fixtures stores fingerprints in the file's transaction; a parse-failing file stores none | `adapter/storage/test_clone_fingerprint_repository.py` (new), `indexing/test_pipeline.py` (extended) | Integration (real Postgres) |
| US-C2 | Modify replaces, delete prunes, rename moves fingerprints | `adapter/storage/test_clone_fingerprint_repository.py` | Integration |
| US-C3 | Tracker at old version → next default-branch job reprocesses all in-scope files, then records version; current version → no backfill; empty index → no error | `indexing/test_pipeline.py` (extended), `adapter/storage/test_indexing_repository.py` (extended) | Integration |
| US-C4 | Added function copying an untouched file's body (renamed identifiers) yields one finding with both locations, no quoted code; multi-match → best + "and N more" | `review/test_clone_detection_step.py` (new) | Integration (real Postgres, real parsing) |
| US-C5 | Pre-existing clone + touched line → none; mostly-added region → one; no-`patch` file → none | `review/test_clone_detection_step.py` | Integration |
| US-C6 | Two PR files duplicate each other → one finding; PR-touched file's stale default-branch rows ignored | `review/test_clone_detection_step.py` | Integration |
| US-C7 | Rename/literal/whitespace/comment differences match; operator/control-flow differences do not; per-language fixtures | `indexing/test_clone.py` (new); `review/test_clone_detection_step.py` (a renamed-identifier copy in each language is found end to end) | Unit + Integration |
| US-C8 | Below size or span → none; test/generated/vendored → none; import-only run → none; clean-code fixtures per language → zero findings (noise bound) | `indexing/test_clone.py`, `review/test_clone_detection_step.py` | Unit + Integration |
| US-C9 | Forced hash collision with unequal token sequences → no finding; region extended and trimmed to equality | `indexing/test_clone.py`; `review/test_clone_detection_step.py` (a stored colliding fingerprint row yields no finding) | Unit + Integration |
| US-C10 | 8 regions, cap 5 → largest 5 kept, dropped count logged; adjacent regions merged | `review/test_clone_detection_step.py` | Integration |
| US-C11 | Merger/output need no per-source branch; dedupe against an LLM finding; a clone finding is posted as an ordinary inline comment | `findings/test_merge.py`, `findings/test_models.py` (extended); `review/test_findings_output_step.py` (extended, recorded GitHub cassette) | Unit + Integration |
| US-C12 | Approved dismissal suppresses a similar clone finding; pending does not | `review/test_clone_detection_step.py` | Integration (real Postgres/pgvector, real local embedding model) |
| US-C13 | 120 mixed findings: at equal severity clone findings drop first | `findings/test_merge.py`, `review/test_static_analysis_step.py` (ceiling tests); `review/test_findings_output_step.py` (extended: over-ceiling job still posts security findings) | Unit + Integration |
| US-C14 | Empty index, forced DB error, forced timeout, unparseable PR file → zero clone findings and a completed job | `review/test_clone_detection_step.py`, `review/test_review_job.py` (extended) | Integration |
| US-C15 | Disabled → step absent, output equals baseline; invalid settings fail startup; chain wiring in all enablement combinations | `apps/worker/test_settings.py`, `review/test_config.py` (extended) | Unit + Integration (Worker boot) |
| All (happy path) | Compose stack, stubbed GitHub, one copy per language posts expected findings only; disabled reproduces today's output; dashboard panels receive data | `tests/e2e/test_clone_detection.py` (new), `tests/e2e/test_observability.py` (extended) | E2E |

## Resolved decisions

**Decided** (by the documents, the code, or product-owner reasoning from end
user value and the competitor basis above):

- **D1 — Build a custom fingerprinting engine; do not adopt jscpd/PMD CPD.**
  Rationale: (1) incrementality — the whole point of Phase 10 is comparing
  only changed code against a persisted index; CPD and jscpd are batch tools
  that re-tokenize every input on every run, so wrapping them means
  re-reading the repo per review or maintaining a second cache; (2) Phase 2
  already holds the Tree-sitter parse for exactly the three languages in
  scope, so the token stream is nearly free; (3) a JVM (CPD) or Node (jscpd)
  runtime would add image size and attack surface that Phase 9 already
  agonized over (ESLint's Node, PHPStan's PHP), for no capability we need.
  Approach: AST-leaf normalized tokens plus **winnowing** (Schleimer, Wilkerson
  & Aiken, "Winnowing: Local Algorithms for Document Fingerprinting") — a
  published method that stores a bounded sample of window hashes while
  guaranteeing any duplicated run of at least the threshold length shares at
  least one fingerprint. This gives deterministic, no-false-negative
  detection at ≈ 2–3 % storage overhead. This decision should be captured as
  an ADR (engine choice reverses no existing ADR but fixes an architecture
  direction `architecture.md` says needs one when it deviates; here it is a
  new bounded capability, so the ADR is recommended, not blocking).
- **D2 — PR-time pipeline step, scoped to duplication the PR introduces.**
  Pairs where neither side is in the PR are **not** posted. Reason: the author
  cannot fix code they didn't write, Sonar's quality gate and Codacy's
  per-PR count both measure *new* duplication, and the research identifies
  noise as why tools get muted. Where we differ from Codacy's observed
  behavior, we differ on purpose; the "codebase health" view belongs to a
  future scheduled job and is out of scope.
- **D3 — Not a Context Builder feature, not a static-analyzer adapter.** It is
  a macro step because it reads the index (a database, not a subprocess) and
  must fail open (macro steps are `FATAL`; the step catches internally,
  following the Context Builder precedent). It sits after `static_analysis`
  so deterministic findings precede it in finding order.
- **D4 — One global minimum size, default 100 normalized tokens and 10
  lines**, matching Sonar and PMD CPD defaults (jscpd's 50/5 is the noisy
  end). Per-language overrides are deferred: this repo has no customer data
  to justify per-language values, and the stored `language` column makes
  adding them a settings change, not a redesign.
- **D5 — Finding shape.** New `Source` value `"clone_detection"` with fixed
  `rule_id="duplication"` and `severity="low"` provides the "distinct
  category" without a new `Finding` field, so no other module changes. Both
  locations live in the message; no code is quoted.
- **D6 — Memory reuse needs one amendment, not zero.** The roadmap says Memory
  is reused "unmodified". Verified in the code: `VerificationStep` applies
  ADR-0009's similarity suppression only to `source == "llm"` findings
  (clone findings, being deterministic, bypass verification). Dismissal
  *capture* is generic (every posted finding), but the *consumption* path is
  LLM-only. This spec therefore extends the suppression to
  `clone_detection`, requiring an amendment to ADR-0009 decision 3 and the
  roadmap wording to be corrected when this phase ships.
- **D7 — Fingerprint writing is ungated by `clone_detection_enabled`.**
  Cheap, keeps re-enable instant; gated only by `INDEXING_ENABLED`.
- **D8 — Backfill by schema-version bump through the existing full-index
  degenerate case**, avoiding a new job type or admin trigger (consistent
  with `spec-indexing.md`'s Rollout decision).

**Assumptions pending validation** (easy to challenge):

- **A1 — Defaults of 100 tokens / 10 lines suit TS/PHP/Go.** Go's repetitive
  error-handling idioms may false-positive at 100; validated by the plan's
  false-positive review on real repos before the default ships enabled.
- **A2 — Default enabled.** The alternative (default off) is safer for noise
  but makes the feature invisible. Mitigated by `low` severity, cap of 5 and
  the new-code rule.
- **A3 — "More than half the region's lines are added/modified" is the right
  new-code test.** Chosen to blame authors for what they introduced without
  exempting a function they copied then lightly edited. Tune if reviewers
  report misses.
- **A4 — Excluding same-file clones and test files in v1** loses some true
  positives to keep the first release quiet.
- **A5 — Backfill load is acceptable** (one full-run-sized job per repo on the
  first post-upgrade push, bounded by the 20,000-file / 2 GB ceiling).
- **A6 — The tracker can carry a schema version** without a breaking change
  (Unit B risk; fallback stated there).
- **A7 — Disclosing a default-branch path and line range in a PR comment** is
  acceptable, on the stated trust assumption that the PR author has read
  access to the repo. This is stronger than context citations: the step is an
  oracle (an author can submit code and learn whether near-identical code
  exists, with its location), so an author with narrower access than the repo
  would learn paths they could not read. Omitting the path for fork PRs is the
  fallback if that changes.
