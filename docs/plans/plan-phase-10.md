# Plan: Phase 10 — Whole-codebase clone detection

**Status: Proposed.** Sequences
[`docs/specs/spec-clone-detection.md`](../specs/spec-clone-detection.md) —
[`docs/roadmap.md`](../roadmap.md)'s Phase 10 — into seven buildable,
independently-testable phases, following the phase / tests / observability &
rollback / acceptance shape of [`plan-phase-9.md`](plan-phase-9.md). Built
against [`docs/rules/architecture.md`](../rules/architecture.md) (verification
tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

The spec was drafted and self-reviewed against `prd-review` (verdict: ready).
This plan does not re-litigate its product calls — the custom winnowing
engine (D1), PR-time scope limited to duplication the PR introduces (D2), the
macro-step placement (D3), the 100-token / 10-line default (D4), the finding
shape (D5), the Memory amendment (D6) and the schema-version backfill (D8) are
decided there. It sequences the work and records the implementation-shape
choices the spec leaves open as **Assumptions** below.

This is additive work on running code. Every phase names the module it
extends; each reference below was checked against the file during drafting:
`indexing/chunk.py` (`chunk_file`, `ChunkResult.tree`, `language_for_path`,
`_line_range`), `indexing/pipeline.py` (`_resolve_file_set`,
`_process_one_added_or_modified_file`, `IndexingJobDependencies`),
`indexing/repository.py` (`IndexRepository`, `IndexCommitTracker`),
`indexing/prefilter.py` (`is_indexable_path`), `adapter/storage/indexing_repository.py`
(`IndexingRepository.upsert_file` / `delete_file` / `get_last_indexed_commit`),
`adapter/storage/models.py` (`IndexChunkRow`, `Repo.last_indexed_commit_sha`),
`adapter/storage/context_query.py` (`PostgresSimilarityQuery`, the read-adapter
shape to copy), migration `0010_usage_counter.py` (latest revision),
`ingestion/diff.py` (`Diff`, `DiffFile`, `DiffFetcher.fetch_content`),
`review/context_builder.py` (the fail-open macro step to copy: internal
`asyncio.wait_for`, constructor-injected `depends_on`),
`review/changed_lines.py` (`changed_lines`), `review/config.py`
(`StepEnablement`, `StepTimeouts`, `resolve_review_pipeline_depends_on`,
`check_step_configuration_viable`), `review/verification.py`
(`VerificationStep._suppress_via_memory`, `MemorySuppressionConfig`),
`memory/fingerprint.py` (`build_finding_fingerprint`), `findings/models.py`
(`Source`, `Finding`), `findings/merge.py` (ceiling ordering),
`findings/redact.py` (`apply_redaction_ceiling`), `apps/worker/settings.py`,
`apps/worker/main.py` (`_build_review_registry`, `_build_indexing_deps`),
`review/test_step_boundaries.py`, `charts/review/` (`values.yaml`,
`templates/configmap.yaml`, `dashboards/review.json`), `docker-compose.yml`,
`docs/reference/operations.md`, `tests/e2e/test_static_analysis_expansion.py`
and `tests/e2e/test_observability.py`.

**Conventions found / fallbacks.** Plan format: mimics `plan-phase-9.md`
(there is no `docs/plans/README.md`). Architecture: `docs/rules/architecture.md`
and `c4-architecture.md`. Testing: `docs/rules/testing.md`. All Python
commands in this plan run in the repo's Docker dev image
(`Dockerfile --target dev`), per the operator's standing rule — never a host
interpreter.

## Scope

**In scope** — the spec's Units A–F in full: the fingerprint engine, storage
and indexing integration with the schema-version backfill, the fingerprint
query adapter, the `clone_detection` review step with its settings and
wiring, Memory suppression, finding ordering, Helm/Compose/runbook, E2E,
dashboard panels and the two ADR documents the spec calls for.

**Out of scope** — the spec's out-of-scope list applies unchanged:
near-miss (Type-3) and semantic (Type-4) clones; pairs where neither side is
in the PR and any scheduled "codebase health" report; same-file clones;
languages beyond TypeScript, PHP and Go; test, generated and vendored files
as either side of a pair (no include-tests setting); per-language or per-repo
threshold overrides; adopting an external engine (jscpd, PMD CPD); auto-fix
(Phase 11); Admin UI controls; cross-repo clones; any generative LLM call.
Also not done here: carrying fetched file content through the step outputs to
avoid a repeat blob fetch (Assumption 8) and updating the roadmap's Phase 10
**Status** line (refreshed only by re-running the roadmap's verification
basis, never from a plan).

## Assumptions this plan makes (flagged, not decided)

Sequencing and implementation-shape calls the spec does not fix. Challenge any
of these before the phase that depends on it starts.

1. **Winnowing parameters and the tuning floor.** Gram size `K = 25`, window
   `W = 76`, so any duplicated run of at least `W + K − 1 = 100` normalized
   tokens shares a fingerprint (the winnowing guarantee), sampled density
   ≈ `2 / (W + 1)` ≈ 2.6 % (the spec's storage figure). The guarantee is
   baked into the stored fingerprints, so `clone_detection_min_tokens` can be
   **raised** freely (a post-filter) but not lowered below 100 without
   re-fingerprinting. The spec lists the setting as tunable with default 100;
   this plan enforces `clone_detection_min_tokens >= 100` at Worker startup
   (the spec's "invalid values fail startup") and documents that lowering
   needs a `CLONE_FINGERPRINT_VERSION` bump with new `K`/`W`.
2. **Verification data lives in the index.** The spec requires that a
   colliding fingerprint can never produce a finding (US-C9) and that regions
   are extended and trimmed to token equality, but does not say where the
   stored side's tokens come from. This plan stores, per indexed file, the
   normalized per-token hashes (`integer[]`, 32-bit) plus a `line_first_token`
   array (token index at which each line begins). Cost ≈ 4 bytes per token
   plus 4 per line — about 4 MB per million tokens; this extends the spec's
   storage NFR (which counted only sampled fingerprints) and Phase 2 records
   the measured figure per fixture. **Rejected alternative:** re-fetching the
   other file's blob from GitHub at match time — adds API calls and a failure
   mode to every match, for a saving of a few MB.
3. **Fingerprint rows carry `(hash, token_offset)` only.** Start/end lines
   are derived from the file row's `line_first_token` array, so the spec's
   "start/end line" columns on the fingerprint table are not stored (one
   source of truth for the token→line map).
4. **`upsert_file(clone_index=None)` means "no clone data": it deletes the
   file's existing clone rows.** This is how a tokenization failure
   (`CloneIndexError`, stage `clone`) is isolated without costing the file
   its chunks and graph, while never leaving stale fingerprints (US-C2). The
   spec's "all-or-nothing per file" is honored for the *write* (one
   transaction), not by failing the file's other stages over a clone-stage
   error.
5. **Backfill is implemented inside `_resolve_file_set`.** For a
   `pr_number == 0` job, when `Repo.clone_fingerprint_version <
   CLONE_FINGERPRINT_VERSION`, the added/modified set becomes **every
   in-scope path in the current tree**, and `removed` still comes from the
   existing previous-tree diff when a last-indexed commit exists (so deleted
   files are still pruned). The job is treated as a full run (full-run
   ceilings), ordered deterministically (modified-path priority does not
   apply; sorted by path so a ceiling drop is reproducible). **Stamping rule
   (review-driven):** the version is written after the cursor advance only
   when the job had no *transient* failure — no whole-batch
   `RepoTreeContentFetchError` and no per-file `fetch_content`/`upsert`
   stage failure — so a flaky backfill is retried by the next push. Two
   outcomes are *deterministic* and therefore do **not** block the stamp
   (blocking it would re-run a full job on every push, forever): files
   dropped by the full-run ceilings, and `clone`-stage (`CloneIndexError`)
   failures. Both are counted and logged (Phase 2 metrics), never silent.
   A repo that can never complete (larger than the ceilings) is therefore
   stamped and visible as a nonzero dropped counter, the same accepted limit
   the initial index already has (spec A5).
6. **A public `parse_file` in `indexing/chunk.py`.** The review step must
   parse PR content without chunking it; `chunk_file` is refactored to call
   a new `parse_file(language, path, content) -> Tree` with unchanged
   behavior. The indexing pipeline tokenizes `chunk_result.tree` directly
   (no second parse, spec Unit A). `review` already imports
   `indexing.chunk` (`review/context_symbols.py`), so no new cross-package
   edge.
7. **Pure logic gets its own modules, and helper modules join the boundary
   exclusion list.** `indexing/clone.py` (tokenize / normalize / winnow /
   match), `review/clone_regions.py` (pure: new-code filter, grouping, cap,
   message — an internal helper of the one `clone_detection` step) and
   `review/memory_suppression.py` (shared suppression helper) are added to
   the exclusion list in `review/test_step_boundaries.py` with the same
   rationale as `context_symbols`/`citation`: helpers of a step, defining no
   `Step`.
8. **The step re-fetches PR file content.** `StaticAnalysisStep` and
   `context_symbols` already each call `DiffFetcher.fetch_content`; the clone
   step does the same for its (language- and path-filtered, patch-bearing)
   subset rather than changing the shared step-output models. Cost: one more
   blob fetch of a subset per job. Carrying content forward is a separate
   change to `JobIdentity`/step outputs and is deferred.
9. **Fail-open timeout is derived.** A macro step's registry timeout is
   fatal (`FailurePolicy.FATAL`), but US-C14 requires a timeout to fail
   open. As `ContextBuilderStep` does, the step wraps its own work in an
   internal `asyncio.wait_for`; here the internal value is derived at the
   composition root as `clone_detection_step_timeout_seconds − 5` (a
   validator requires the step timeout to exceed 5) rather than adding a
   second setting.
10. **Fan-out caps are module constants, not settings** (like
    `indexing/pipeline.py`'s ceilings): at most 20 stored matches considered
    per PR fingerprint hash and at most 200 candidate files loaded per job;
    excess is dropped, counted and logged at `warning`. Boilerplate that
    matches in hundreds of files therefore cannot blow up a job.
11. **`DiffFile` gains an optional `previous_path`.** The spec requires
    renamed files to be excluded from default-branch matching (US-C6), but
    `DiffFile` has no rename source: a renamed file would match its own old
    path's default-branch fingerprints. GitHub's Files endpoint returns
    `previous_filename`; the adapter will populate a new optional
    `previous_path` (default `None`, additive). Phase 4 first verifies the
    adapter reads that endpoint field.
12. **Telemetry names** follow the catalog's `review.<component>.<name>`
    form: meter `review.clone_detection` with `outcome_total{outcome}` (the
    six named outcomes in Phase 4), `duration_ms`, `candidates_total`,
    `filtered_total`, `findings_total`, `memory_suppressed_total`,
    `cap_dropped_total` (finding cap) and `candidate_cap_dropped_total`
    (per-hash / candidate-file caps); the indexing meter gains
    `clone_fingerprints_written_total`, `clone_fingerprints_deleted_total`,
    `clone_stage_failed_total`, `clone_backfill_total{outcome}` (`complete`,
    `complete_with_drops`, `incomplete`, `superseded`) and
    `clone_backfill_dropped_total`. Dashboard panels arrive in Phase 7, not
    earlier.
13. **PRs into non-default base branches** (release gathering branches)
    compare against the default-branch index only, exactly as the Context
    Builder does today. Duplication of code that exists only on the base
    branch is not found; code removed on the base branch can still match.
    Accepted and documented in the runbook.
14. **Memory fingerprint is line-number-stable for clone findings
    (decided, 2026-10-17).** ADR-0009's fingerprint is `file + rule_id +
    message`, and a clone message embeds the other location's line range, so
    any push that shifts lines would defeat a dismissal. Industry practice
    (SonarQube issue tracking, CodeRabbit learnings) is to identify a finding
    by rule plus a location-insensitive signature, never raw line numbers. The
    one function both the capture side (the admin `approve` route) and the
    check side (suppression) already share, `memory.fingerprint.
    build_finding_fingerprint`, is the single place to fix it, so capture and
    check cannot diverge. It gains **one named, pure function** that strips
    only the anchored line-range suffix from a `duplication` finding's message
    (`path:N-M` → `path`), selected by a `DUPLICATION_RULE_ID` constant kept in
    `memory` (that package must not import `findings` or `indexing` rules). A
    one-rule branch, not a registry: a table with a single entry is
    speculative until a second rule needs one (`architecture.md`'s YAGNI).
    The strip must be narrow — the other file's path stays in the fingerprint,
    so a dismissal for one clone pair can never suppress a different pair; Phase
    5 adds negative tests (same file, different other path; paths containing
    digits, `:1-5` or `v1-2`). **Evidence gate:** Phase 5 first measures the
    embedding similarity of unnormalized messages whose ranges differ by a few
    lines against the threshold; if they already match above it, the
    normalizer is not added. No admin-route code changes; only the output of
    the shared function changes, and only for `duplication` (existing
    fingerprints stay byte-identical).
    **Evidence gate result (2026-10-17, real `all-MiniLM-L6-v2`, threshold
    0.90):** a clone message with the other location's range shifted by 1, 3
    or 10 lines scores 0.9988-0.9989 against the original, a shift to a
    3-digit range 0.9979, and a resized range (24 to 27 lines) 0.9961. All are
    far above the threshold, so **the normalizer is not added** and
    `build_finding_fingerprint` is unchanged. Recorded caveat: a *different*
    other path in the same file scores 0.935 (and a different directory
    0.870), so a dismissal can also suppress a near-identical pair in the same
    directory; accepted for now (same behavior as LLM findings) and the
    reason the negative-case unit tests pin the fingerprint text, not the
    similarity. The remainder of this assumption (named normalizer, negative
    tests) applies only if a future message format changes the measurement.
15. **Stuck-job threshold.** `WorkerSettings`'s validator sums the macro-step
    timeouts (515 s today against the 600 s default). Phase 4 adds the clone
    step (30 s → 545 s) to that sum and its docstring; the 600 s default
    still holds.
16. **Per-grammar normalization tables** (which Tree-sitter leaf types are
    identifiers, literals, comments or structural) are enumerated in
    Phase 1 from each grammar's `node-types.json` and pinned by a
    completeness test over the fixture corpus, not asserted here from
    memory.

## Phase 1 — Fingerprint engine (spec Unit A)

**Why first:** everything else stores, queries or reports what this produces;
it is pure, so it can be built and exhaustively tested with no database,
network or LLM, and it carries the spec's highest technical risk (three
grammars, first token-level Tree-sitter use here) — front-loaded.

- `indexing/chunk.py`: extract public `parse_file(language, path, content)`
  from `chunk_file` (Assumption 6); `chunk_file` behavior and its tests
  unchanged.
- `indexing/clone.py` (pure, zero infra imports):
  - `tokenize(language, path, tree) -> NormalizedTokens` (as built; `build_clone_index(language, path, tree)` wraps tokenize + fingerprints + line map): walk leaves;
    drop comments and whitespace; map identifier-class leaves to one class
    and literal-class leaves to one class; keep keywords, operators and
    punctuation as themselves. Each token carries a deterministic 32-bit
    hash (`zlib.crc32` over the class-or-text bytes; never Python's salted
    `hash()`) and its 1-based line taken straight from the Tree-sitter node's
    `start_point` row (`indexing.chunk._line_range` is a byte-range newline
    counter and is the wrong tool here); also emits the
    `line_first_token` array (Assumption 2). Per-grammar node-type tables per
    Assumption 16; an unclassified leaf type raises `CloneIndexError` rather
    than silently normalizing wrongly.
  - `fingerprints(token_hashes) -> tuple[Fingerprint, ...]` (takes the per-token hash tuple, as built): winnowing with
    `K = 25`, `W = 76` over a deterministic polynomial rolling hash folded to
    a signed 63-bit integer (fits Postgres `BIGINT`); module constants
    `GRAM_TOKENS`, `WINDOW`, `GUARANTEE_TOKENS = 100`, and
    `CLONE_FINGERPRINT_VERSION = 1`.
  - `CloneFileIndex` (pydantic, frozen): `file_path`, `language`, token-hash
    array, `line_first_token`, fingerprints.
  - `match_regions(query: CloneFileIndex, stored: CloneFileIndex, *,
    min_tokens=GUARANTEE_TOKENS, min_lines=10) -> tuple[CloneRegion, ...]` (as
    built; it derives the hits itself from the two indexes' shared fingerprint
    hashes, and applies the size/span minimums): for each shared-fingerprint
    hit, extend left and right while per-token hashes are equal, trim to
    equality (US-C9), merge regions of the same file pair that overlap or touch
    on both the query and the stored side (US-C8), keep only runs reaching
    `min_tokens` and `min_lines`, and convert token spans to inclusive line
    ranges. A hit whose tokens differ yields
    nothing.
  - Pure rule helpers: `is_excluded_path(path)` (test paths: `*_test.go`,
    `*.test.*`, `*.spec.*`, and `test/`, `tests/`, `__tests__/` segments;
    plus `indexing.prefilter.is_indexable_path` for generated/vendored/
    lockfile/ignored), and `region_is_import_only(tokens, start, end)` (as built)
    (a run made only of import/use/require statements or a license header).
- Fixture corpus under `indexing/testdata/clone/` per language: a clean-code
  file, an original and a renamed-identifier copy, a literal-changed copy
  (`literals.*`), a structure-changed non-copy (`restructured.*`), a
  comment/whitespace-only variant, a boilerplate (imports) file, and
  `coverage.*` for the completeness test. As built: TypeScript, TSX, PHP and
  Go; the TSX copies are the TS text (JSX syntax is exercised by
  `coverage.tsx`); the hand-built colliding pair is a synthetic in-test
  `CloneFileIndex`, not a file.
- Record, per fixture, token counts and fingerprint density (should be near
  2.6 %), as the evidence for the storage NFR.

**Tests:** *Unit* — `indexing/test_clone.py`: US-C7 (rename/literal/
whitespace/comment variants match; operator, keyword and control-flow changes
do not) per language; the **no-false-negative** property (a property test
over generated runs of length ≥ 100 shared at random offsets must always share
a fingerprint); US-C8 size, span and boilerplate-run rules and path
exclusions; US-C9 forced window-hash collision with unequal tokens → no
region, plus extension/trim to exact equality; deterministic golden hashes
(the same value in two separate processes); the grammar-completeness test
(every leaf type in the corpus is classified). Pure no-I/O logic is Unit per
[ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md); the stories' story-level
*integration* proof arrives in Phases 2–4.
**Observability & rollback:** none (no runtime caller yet); rollback is a plain
revert.
**Acceptance:** `indexing/test_clone.py` green for TypeScript, TSX, PHP and Go;
`chunk_file`'s existing suite unchanged and green; `ruff`, `ruff format
--check` and `mypy --strict` clean.

## Phase 2 — Storage, indexing integration, backfill (spec Unit B)

**Why second:** the review step has nothing to query until fingerprints are
persisted, and the backfill must be in place before any existing repo can be
searched. Depends on Phase 1's `CloneFileIndex`.

- Alembic migration `0011` (revises `0010`), additive only:
  - `index_clone_file` — `repo_id` (FK `repo.id`, cascade), `pr_number`,
    `file_path`, `language`, `token_hashes integer[]`, `line_first_token
    integer[]`; unique on `(repo_id, pr_number, file_path)`, matching
    `IndexChunkRow`'s scoping.
  - `index_clone_fingerprint` — `repo_id`, `pr_number`, `file_path`, `hash
    bigint`, `token_offset integer`; index on `(repo_id, pr_number, hash)`
    for the lookup and a unique/ordering key that makes per-file replacement a
    single delete-then-insert.
  - `repo.clone_fingerprint_version integer not null server_default 0`.
  Models `IndexCloneFileRow`, `IndexCloneFingerprintRow` and the new `Repo`
  column in `adapter/storage/models.py`; the migration test
  (`adapter/storage/test_migrations.py`) extended.
- `IndexRepository.upsert_file` (Protocol in `indexing/repository.py` and
  `IndexingRepository`) gains `clone_index: CloneFileIndex | None`.
  Inside the **existing** `_write` transaction: replace the file's
  `index_clone_file` row and its fingerprint rows (delete-then-insert), or
  delete both when `clone_index is None` (Assumption 4). `delete_file` also
  deletes both. Grep and update every implementer and test fake of
  `IndexRepository` before changing the signature.
- `indexing/pipeline.py`: in `_process_one_added_or_modified_file`, after the
  graph stage, run `build_clone_index` (tokenize + fingerprints) over `chunk_result.tree` via
  `asyncio.to_thread` in its own `try` catching `CloneIndexError` (stage
  `"clone"`, `_log_stage_failure`, continue with `clone_index=None`). The
  stage helper returns a `_FileOutcome` carrying the failed stage (`chunk`,
  `graph`, `embed`, `upsert`; a clone failure sets `clone_failed` instead of
  failing the file). `IndexingJobDependencies` gains one **optional** field,
  `clone_backfill: CloneBackfillTracker | None = None` (`None` disables the
  backfill, so callers predating clone detection are unchanged; production
  wiring in `apps/worker/main.py` always passes the repository, tested in
  `apps/worker/test_main.py`). `IndexingResult` and the job log gain
  `fingerprints_written`, `clone_stage_failures` and `clone_backfill_outcome`.
  `IndexRepository.upsert_file` takes `clone_index: CloneFileIndex | None = None`
  and `delete_file` returns the removed fingerprint count (an `int`, feeding
  `clone_fingerprints_deleted_total`).
- Backfill (Assumption 5): a new `CloneBackfillTracker` Protocol (kept apart
  from `IndexCommitTracker`, which is unchanged, to stay within the 1-3
  method cap) and `IndexingRepository` gain `get_clone_fingerprint_version`
  and `advance_cursor_with_clone_version` (the conditional cursor-and-version
  write below; there is no separate `set_clone_fingerprint_version`).
  `_resolve_file_set` forces the all-in-scope-paths set for stale
  `pr_number == 0` jobs while still pruning removed files, and the version is
  stamped by that one conditional write, only on a job with no transient
  failure. An unknown repo makes the version read raise; the conditional
  write returns `False` (read as `superseded`) since no row matches.
- Metrics (so none of these is silent): `review.indexing.clone_fingerprints_written_total`;
  `review.indexing.clone_stage_failed_total` (index-time tokenizer failures)
  with an `error`-level log when clone-stage failures exceed
  `FAILURE_RATIO_WARNING_THRESHOLD` of a job's files (a systematic tokenizer
  bug otherwise empties the index quietly);
  `review.indexing.clone_fingerprints_deleted_total`;
  `review.indexing.clone_backfill_total{outcome}` where `outcome` is
  `complete` (stamped, nothing dropped or failed), `complete_with_drops`
  (stamped, ceiling drops or `clone`/`embed`-stage failures — a partial
  backfill never reads as healthy), `incomplete` (not stamped, transient
  failure) or `superseded` (not stamped, another job advanced the cursor); and
  `review.indexing.clone_backfill_dropped_total` (ceiling drops). A repo
  stuck below `CLONE_FINGERPRINT_VERSION` is surfaced as repeated
  `incomplete` outcomes and in the runbook's operator query (Phase 6).
- **Stamping needs a pipeline change (verified against the code):**
  `_process_one_added_or_modified_file` currently returns `None` for every
  stage failure alike, and `run_indexing_job` folds those with delete and
  `fetch_content` failures into one `files_failed` count, so the rule cannot
  be implemented as written. The helper therefore returns the **failed stage**
  and the job keeps a separate `transient_failed` flag, set by: the
  whole-batch `RepoTreeContentFetchError`; per-file `upsert`; and both
  `delete` sites (the symlink flip and the removed-file loop) — network and
  database failures. `chunk`, `graph`, `clone` **and `embed`** failures are
  treated as deterministic and do not set it: a file that reliably fails the
  local embedder would otherwise keep the repo unstamped and turn every later
  default-branch push into a full re-chunk and re-embed (an amplification any
  pusher could trigger); those failures are counted and logged instead. `clone_backfill_total` reads
  the same flag. `files_failed` and every existing log and metric keep their
  current meaning. In `_resolve_file_set` the non-fallback branch never calls
  `get_last_indexed_commit` or fetches the previous tree, so a backfill adds
  that fetch there; if it fails the job already fails fatally and the version
  stays unstamped.
- **Concurrency (verified: `claim_next` is `skip_locked` on the oldest pending
  row with no per-repo lock or dedupe):** two `pr_number == 0` `INDEX` jobs
  for one repo can run at once, so a backfill that read file F at commit X
  can overwrite a push job's newer F at commit Y. That last-writer-wins race
  on file rows and the cursor **already exists** and affects chunks equally;
  fixing it is outside this phase (decided 2026-10-17 after review: a per-repo
  advisory lock was drafted and removed — no story requires it, a lock timeout
  would mark a job `FAILED` and an ordinary push's changed files would never
  be re-indexed, a pooled session lock can be returned to the pool still held
  after a failed unlock or cancellation, and it breaks behind PgBouncer
  transaction pooling. See "Follow-up" at the end of this plan for what a
  correct serialization change must handle). This phase's mitigation is
  industry-standard **optimistic concurrency**, scoped to the backfill: the
  job reads the cursor it started from (`previous_commit_sha`, already read by
  `_resolve_file_set`) and writes the cursor **and** the version in one
  conditional `UPDATE repo SET last_indexed_commit_sha = :sha,
  clone_fingerprint_version = :v WHERE id = :id AND last_indexed_commit_sha IS
  NOT DISTINCT FROM :expected_previous`, extending
  `IndexingRepository.set_last_indexed_commit`'s existing rowcount pattern. A
  rowcount of 0 means another job advanced the cursor first: this job neither
  moves the cursor nor stamps, logs outcome `superseded`, and the next
  default-branch push re-backfills, so stale rows heal instead of becoming
  permanent. Non-backfill jobs keep today's unconditional cursor write
  unchanged.
- Cost check: a backfill re-chunks and **re-embeds** every in-scope file, not
  just tokenizes it. Phase 2 measures backfill CPU and wall time per 1,000
  files; if it is unacceptable the clone-only shortcut (tokenize from a fresh
  parse, skip embed/graph) is an owner decision, not assumed here.
- Write-side poisoning guard: delete-then-insert on both clone tables is keyed
  on `(repo_id, pr_number, file_path)`; a PR job never touches `pr_number = 0`
  rows (test below).

**Tests:** *Integration* (real Postgres/pgvector via `testcontainers`, per
`testing.md`) — `adapter/storage/test_clone_fingerprint_repository.py` (new):
US-C1 rows written in the file's transaction and absent for a parse-failing
file; US-C2 modify replaces, delete prunes, rename moves, and `clone_index=None`
clears; a mid-write failure rolls back chunks, graph and clone rows together.
`indexing/test_pipeline.py` (extended): US-C1 end to end; US-C3 stale version
→ every in-scope file reprocessed, removed files pruned, version recorded only
after completion, current version → no backfill, never-indexed repo → no
error, and the full-run ceiling logs and drops. `adapter/storage/test_indexing_repository.py`
(extended) for the tracker methods. `adapter/storage/test_migrations.py`
extended (up/down on a populated schema). Also: a PR-scoped job writing or
deleting the same path as the default branch leaves the `pr_number = 0` clone
rows intact; the stamping rule (a transient fetch failure leaves the version
stale, a ceiling drop or `clone`-stage failure stamps it and counts); a
backfill still prunes files deleted since the cursor; the conditional
cursor-and-version write (`adapter/storage/test_indexing_repository.py`, real
Postgres): matching expected cursor → both written, a cursor advanced by
another job → rowcount 0, neither written, outcome `superseded`; the new
previous-tree fetch in the non-fallback backfill branch (a failure is fatal
and leaves the version unstamped); and the three backfill outcomes
(`complete`, `complete_with_drops`, `incomplete`) plus
`clone_backfill_dropped_total`, `clone_fingerprints_deleted_total` and the
error log at `FAILURE_RATIO_WARNING_THRESHOLD` are each asserted (an
in-memory OTel reader). A parse-failing file stores no clone rows in
`indexing/test_pipeline.py` (integration, real Postgres). *Unit* — the
stage-returning helper (`indexing/test_pipeline.py`, fakes at the Protocol
boundary): each stage failure returns its stage, and `transient_failed` is set
by exactly the fetch-batch, `upsert` and both `delete` failures and by none of
`chunk`, `graph`, `clone` or `embed`; pipeline stage-failure
isolation with fakes at the Protocol boundary (a `CloneIndexError` file still
gets chunks). Record fingerprint-row and token-array storage per fixture and
the added indexing wall time against the spec's ≤ 10 % target.

**Measured (Docker dev image, 2026-10-17, re-counted 2026-10-18 after the
corpus grew and the TS/TSX template-literal-type fix; fixtures
`indexing/testdata/clone/`):** 7,881 normalized tokens produced 170
fingerprints across 28 fixture files, a density of 2.16 % (the spec figure is
about 2.6 %); the tiny boilerplate fixtures run higher (`imports.go`: 24
fingerprints on 167 tokens), so per-file density varies widely and the
aggregate is the meaningful figure. The CPU, storage and wall-time figures
below were taken on the earlier 19-file, ~1.4 KB-per-file corpus (5,187 tokens,
100 fingerprints, 1.9 %) and were not re-run; treat them as order-of-magnitude. Token-hash plus line-map arrays cost
4 B per token, about 1.3-2.1 KB per fixture (roughly the source size); each
fingerprint row stores `hash` (8 B) and `token_offset` (4 B) plus the key and
the `(repo_id, pr_number, hash)` index entry. CPU per 1,000 ~1.4 KB TypeScript
files: chunk 0.27 s, graph 0.20 s, clone tokenize+fingerprint 0.45 s, so the
clone stage roughly doubles the Tree-sitter-side CPU (about 0.45 ms/file) and a
1,000-file backfill costs about 0.9 s of non-embedding CPU. **End-to-end wall time
(same date, same image):** a 300-file TypeScript index job through
`run_indexing_job` against real Postgres/pgvector with the real
`LocalONNXEmbeddingClient`, clone stage on versus off (three alternating runs
each after a warm-up run): on 26.6 / 26.2 / 24.9 s (mean 25.9 s), off 25.3 /
28.4 / 24.0 s (mean 25.9 s), about 86 ms per file. The difference is inside the
run-to-run noise (about +-2 s), so the added wall time is well under the
spec's ≤ 10 % target (the CPU figure above, about 0.14 s per 300 files plus
the row inserts, predicts roughly 1 %). Embedding dominates, as expected, so
a backfill costs what a full re-embed costs (about 86 s per 1,000 files on this
machine) and the clone-only shortcut stays an owner decision, not needed for the
target. Storage is reported as a range per fixture (1.3-2.1 KB of arrays plus
4-12 fingerprint rows each) because the fixtures are near-uniform in size.
Test split as built: the storage tests are in
`adapter/storage/test_clone_fingerprint_repository.py` and
`adapter/storage/test_indexing_repository.py`; the backfill, stamping-rule,
outcome-metric and stage-helper tests that use fakes live in
`indexing/test_pipeline_telemetry.py` (unit), with the real-Postgres versions
in `indexing/test_pipeline.py`.
**Observability & rollback:** the counters above and the log field;
production behavior is unchanged for review (nothing reads the tables yet).
The first post-upgrade default-branch push per repo runs a full-run-sized
index job (spec A5) — flagged for Phase 6's runbook. **Rollback is not "inert
tables":** once Phase 4 reads them, a revert or a rolling upgrade leaves old
Workers (which never touch the clone tables) writing chunks while stale
fingerprints for modified and deleted files stay behind, and a later
roll-forward sees the version already stamped and never repairs them. So:
Phases 2 and 4 ship in **separate releases** (the writer one release before the
reader, with the step still disabled), the runbook's rollback resets
`repo.clone_fingerprint_version` to 0 (re-backfill on the next push), and the
Phase 3 reader additionally requires an `index_chunk` row for the candidate
file so a file deleted under old code is never matched. `INDEXING_ENABLED=false`
defers the backfill.
**Acceptance:** all Phase 2 tests pass; the existing indexing unit and
integration suites and `tests/e2e/test_indexing_pipeline.py` unchanged and
green; measured storage and indexing-time figures recorded in the PR.

## Phase 3 — Fingerprint query adapter

**Why third:** the review step needs a read path with a stable Protocol, and
the first read against these tables should be proven against real Postgres
before step logic sits on top. Depends on Phase 2's tables.

- `adapter/storage/clone_query.py`: `PostgresCloneIndexQuery`, copying
  `PostgresSimilarityQuery`'s shape (`_run`, session factory, typed error),
  implementing two methods of a `CloneIndexQuery` Protocol defined at the
  consumer (`review/clone_detection.py`, Phase 4):
  - `find_candidates(repo_id, hashes, excluded_paths, per_hash_limit)` — one
    batched query on `(repo_id, pr_number = 0, hash = ANY(:hashes),
    file_path <> ALL(:excluded))`, capped per hash (Assumption 10),
    hashes sent in batches.
  - `load_files(repo_id, file_paths)` — the `index_clone_file` rows for the
    candidate files, `pr_number = 0` only.
- `CloneQueryError(operation, detail)` in the consumer module, mirroring
  `ContextQueryError`. The `_run` wrapper is copied per the two existing
  copies' convention (a shared helper is a separate cleanup, not this phase).
- Array parameters are bound with explicit types, as `context_query.py` does:
  `bindparam(..., type_=ARRAY(BigInteger))` for hashes and `ARRAY(String)` for
  excluded paths. Hashes are signed 63-bit by construction (Phase 1), proven by
  a test that a hash at the top of the range round-trips — an unsigned value
  would overflow `bigint`, fail every lookup and silently disable the feature.
  The excluded-path list is bounded by the pre-filter's existing file-count
  ceiling (500), so it needs no separate cap.
- Both reads require an `index_clone_file` row **and** an `index_chunk` row
  for the candidate file (rollback/rolling-upgrade guard, Phase 2). The
  200-file candidate cap bounds `load_files`; no second byte cap is added.
- Every call takes a timeout. `pr_number = 0` is hard-coded, so a PR-scoped
  row can never be a comparison target (the poisoning gap
  `spec-indexing.md`'s Assumption 9 closed).

**Tests:** *Integration* — `adapter/storage/test_clone_query.py` (new), real
Postgres: hits found by hash, `pr_number > 0` rows never returned, excluded
paths honored, per-hash cap applied and reported, empty hash list and empty
index return empty without error, and a forced database error surfaces as
`CloneQueryError`. A query-plan assertion (`EXPLAIN`, after an explicit `ANALYZE` so the planner choice is not flaky)
that the lookup uses the `(repo_id, pr_number, hash)` index at the
100,000-fingerprint fixture size the spec's latency budget names, and that the
per-hash limit is applied in SQL (its plan is checked too); a signed-63-bit
round-trip test; the chunk-row guard (a file whose chunk row is gone is never
returned).
**Observability & rollback:** no new instruments; adapter errors are logged by
the caller (Phase 4). Rollback is a plain revert (no caller yet).
**Acceptance:** `adapter/storage/test_clone_query.py` green; lookup latency on
the 100,000-fingerprint fixture recorded.
**As built:** `find_candidates` / `load_files` take a `timeout_seconds` keyword
(`asyncio.timeout`, surfaced as `CloneQueryError`); `find_candidates` returns
`CloneCandidates(hits, capped_hash_count)` and fetches `limit + 1` rows per hash
in a `LATERAL` join to detect truncation; the `index_chunk` guard is in both
reads, the `index_clone_file` row is required by `load_files` only (the writer
creates both in one transaction). The consumer module `review/clone_detection.py`
exists now with the Protocol, error, hit models and caps (Phase 4 extends it).
**Measured (Phase 3 acceptance):** 200 hashes against a 100,000-fingerprint
index (1,000 files x 100): median 1.9 ms, max 3.4 ms per lookup; plan uses
`ix_index_clone_fingerprint_repo_pr_hash` with a `Limit` node.

## Phase 4 — `clone_detection` review step, settings, wiring (spec Unit C, part of E)

**Why fourth:** the only end-user-visible phase; it needs Phase 1's engine,
Phase 2's data and Phase 3's reader. It also owns the settings and registry
wiring, because the step cannot run without them.

- `ingestion/diff.py` / `adapter/github/diff.py`: add optional
  `DiffFile.previous_path` (Assumption 11); first verify the adapter's Files
  endpoint parsing sees `previous_filename`, and extend its cassette test.
- `findings/models.py`: widen `Source` with `"clone_detection"` (confirm no
  persisted constraint: `PostedFindingCommentRow` stores only
  `finding_rule_id`; none found at drafting).
- `review/clone_regions.py` (pure): the new-code test using
  `review.changed_lines.changed_lines` (a region is kept only when **more than
  half** its lines are added or modified, US-C5); drop of no-`patch` files;
  grouping (best match = largest then lexicographically first path, plus
  `and N more`, US-C4); adjacent/overlapping same-file regions merged and the
  per-job cap by region size (US-C10); a PR-internal pair is reported once,
  anchored in the later-sorted path (US-C6, tested in `test_clone_regions.py`);
  the one-sentence message built from
  paths and integers only (file names can contain backticks, newlines and
  `]()`; the existing `escape_markdown` posting path handles them and a test
  pins it, and the plan confirms the redactor also runs over `message` for
  this source); `Finding` construction with
  `source="clone_detection"`, `rule_id="duplication"`, `severity="low"`, anchored
  at the first changed line of the region.
- `review/clone_detection.py`: `CloneDetectionStep` (`name =
  "clone_detection"`, constructor-injected `depends_on` like
  `ContextBuilderStep`). `run()`: select files (status not `removed`, has a
  patch, `language_for_path` not `None`, not `is_excluded_path`); fetch content
  via `DiffFetcher.fetch_content` (Assumption 8); `parse_file` + `tokenize` +
  `fingerprints` via `asyncio.to_thread`; PR-internal comparison among the
  changed files (US-C6); one `find_candidates` call excluding **every** path
  the PR changes, deletes or renames (`path` and `previous_path`); `load_files`
  for candidate files; `match_regions` against stored tokens; hand to
  `clone_regions`. The whole body is inside an internal `asyncio.wait_for`
  (Assumption 9) and a broad, **logged** catch of exactly the typed
  exceptions this step raises (`CloneQueryError`, `CloneIndexError`,
  `ChunkParseError`, `DiffContentUnavailableError`,
  `DiffContentMalformedError`, `TimeoutError`) — each returns the incoming
  findings unchanged (US-C14). **`CloneIndexError`/`ChunkParseError` are
  caught per file**, so one odd or unparseable PR file is skipped and counted
  while a valid copy elsewhere in the same PR is still flagged; only
  query/content/timeout errors fail the whole step open. A per-job cap on
  files considered (module constant, 100) bounds how many `to_thread` parses
  one job can leave running. Each run ends with exactly one **named outcome**
  on `outcome_total`: `completed`, `no_matches`, `no_eligible_files`,
  `failed_open_query`, `failed_open_content`, `failed_open_timeout` — so "no
  index yet" is distinguishable from "the step is failing", and the structured
  log carries the same value; a genuine bug is left uncaught per
  `architecture.md` (and is then fatal like any macro step, which the
  Context Builder precedent also accepts). Output model
  `CloneDetectionOutput(JobIdentity)` with `diff` and `findings`, forwarding
  `base_sha` and the product-knowledge fields explicitly (per `JobIdentity`'s
  docstring); `ContextBuilderStep`'s input model (which mirrors
  `StaticAnalysisOutput`'s shape) is verified to accept `CloneDetectionOutput`
  with a test. Protocols defined at the consumer; no `fastapi`/`sqlalchemy`/
  `httpx` imports. The constructor already takes an optional
  `memory_suppression: MemorySuppressionConfig | None = None` (and the
  embedder/matcher it needs) so Phase 5 changes behavior, not the composition
  root's shape.
- `review/config.py`: `StepEnablement.clone_detection_enabled` (default
  `True`), `StepTimeouts.clone_detection_step_timeout_seconds` (30), both
  `as_mapping()`s; `clone_detection_can_produce_value`;
  `resolve_review_pipeline_depends_on` gains `clone_detection` (upstream
  `static_analysis` or `prefilter`) and `context_builder` now depends on it
  when effectively enabled; `check_step_configuration_viable` rejects
  `clone_detection_enabled` with `prefilter` disabled (a fixed dependency, as
  for `static_analysis`). The two functions stay the one shared definition.
- `apps/worker/settings.py`: `clone_detection_enabled`,
  `clone_detection_step_timeout_seconds`, `clone_detection_min_tokens` (100),
  `clone_detection_min_lines` (10), `clone_detection_max_findings` (5);
  validators — all positive, `min_tokens >= GUARANTEE_TOKENS` (Assumption 1),
  step timeout > 5 s (Assumption 9); the stuck-job sum includes the new step
  (Assumption 15).
- `apps/worker/main.py`: construct `PostgresCloneIndexQuery` from the existing
  `session_factory`, build `CloneDetectionStep` unconditionally in
  `_build_review_registry` (gating only via the `enabled` mapping, like every
  step), pass the resolved `depends_on`. No second connection pool.
- `review/test_step_boundaries.py`: add `clone_regions` to the exclusion list
  (Assumption 7); `clone_detection.py` stays scanned.
- Telemetry via `review/_telemetry.py` (ADR-0011/0012): the Assumption 12
  instruments; step log with `candidate_count`, `filtered_count`,
  `dropped_by_cap_count`, `finding_count`, files considered/skipped.
  **No dashboard panels yet** (Phase 7).

**Tests:** *Unit* — `review/test_clone_regions.py` (new): half-line boundary
(exactly half dropped, more than half kept), grouping and `and N more`, cap
and adjacent-region merge, message contains paths and integers only (no source
text), the anchor line; `review/test_config.py` (extended): the chain resolves
correctly in every enablement combination and the viability rejection;
`apps/worker/test_settings.py` (extended): each validator and the stuck-job
sum; `review/test_step_boundaries.py`. *Integration* (real Postgres, real
parsing, fakes only for GitHub) — `review/test_clone_detection_step.py` (new):
US-C4 a renamed-identifier copy of an untouched file yields one finding with
both locations and no quoted code, multi-match → best plus `and N more`; US-C5
a line edited inside a pre-existing clone → none, a mostly-added region → one,
a no-`patch` file → none; US-C6 two PR files duplicating each other → one
finding, a modified/deleted/**renamed** file's stale default-branch rows
ignored; US-C7/C8 end to end per language including the per-language
clean-code fixture noise bound (zero findings); US-C9 a stored colliding
fingerprint row yields nothing; US-C10 cap and merge; US-C14 empty index,
forced database error, forced timeout and an unparseable PR file each return
zero clone findings and a completed job with the right named outcome; **per-file
isolation** — an unparseable file and an unsupported-language file in the same
PR as a valid copy still leave the valid copy flagged; the 200-file candidate
cap and the files-considered cap fire, are logged and counted; US-C8's
below-size, below-span, test/generated/vendored and import-only cases each
run through the step (not only the unit module). *Unit* with a fake
`CloneIndexQuery` at the consumer Protocol: the exclusion list (modified,
deleted, renamed and `previous_path` paths) and the cap branches.
`apps/worker/test_main.py` (extended): the derived internal timeout is
`step timeout − 5` and `depends_on` is wired through; the same file holds the
Worker-boot test: invalid settings (min tokens < 100, step timeout ≤ 5) abort
startup (`test_settings.py` keeps the pure validator unit tests). `review/test_review_job.py`
(extended): the step registered in the real chain, and with it disabled the
posted output equals the baseline (US-C15). `adapter/github/test_diff.py`
(extended, `respx` cassette): `previous_path` populated for a renamed file.
**Observability & rollback:** new metrics and log fields emit; the step ships
behind `clone_detection_enabled`, so rollback is the flag (a plain revert
needs the Phase 6 version reset too, run only after every old Worker has drained)
(no schema change in this phase). A broken step cannot fail a review except
via an uncaught bug, which the kill switch contains.
**Acceptance:** all Phase 4 tests pass; the existing review unit and
integration suites and `tests/e2e/test_review_pipeline.py` unchanged and green
with the step both enabled and disabled.
**As built:** `review/test_clone_detection.py` (unit, fake `CloneIndexQuery` and `DiffFetcher`,
real parsing) holds the exclusion-list, cap, outcome and per-file-isolation cases;
`review/test_clone_detection_step.py` is the real-Postgres story file. Differences from the text
above: (a) `no_matches` means no candidate row matched and no PR-internal duplicate was found
(the Protocol has no "is the index populated" call, so an empty index and a PR with no overlapping
code are indistinguishable); (b) the import-boilerplate rule is `>= 90 %` import tokens in the
region (`_is_import_boilerplate`), not `indexing.clone.region_is_import_only`'s strict "every
token", because a copied import block drags in the `<?php`/`package` header; (c) the files-considered
cap is counted in `files_skipped` and in `candidate_cap_dropped_total`; (d) `memory_suppression` is
accepted as `object | None` and ignored until Phase 5 types it; (e) `_build_review_registry` takes
a new positional `clone_query` after `similarity_query`; (f) `.env.example` documents the five
settings; (g) `clone_detection_can_produce_value` is just the step's own flag (its consumer,
`output`, is mandatory). Clone finding messages are not run through the redactor in this step
(paths and integers only, no source text); the posting path escapes Markdown as for any finding.

## Phase 5 — Memory suppression (spec Unit D)

**Why fifth:** a dismissal must not recur on every push, but the cap and the
new-code rule already bound the damage, so this is *Should* and can slip
without blocking release (spec priority). Depends on Phase 4.

- Extract `review/memory_suppression.py`: `MemorySuppressionConfig` and a
  function `suppress_via_memory(findings, *, config, repo_id) ->
  (kept, suppressed_count)` moved from
  `VerificationStep._suppress_via_memory`; `VerificationStep` delegates to it
  (behavior byte-for-byte unchanged, existing tests untouched); add the module
  to the boundary exclusion list (Assumption 7). `apps/worker/main.py`'s
  existing config construction is reused and passed to both steps; the one
  `memory_suppression_enabled` flag governs both.
- `CloneDetectionStep` applies the helper to its findings before returning,
  recording `memory_suppressed_total`.
- Amend [ADR-0009](../adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md)
  decision 3: similarity suppression covers `source == "clone_detection"`
  findings as well as `llm` findings; same threshold, repo scope, approved-only
  rule, no LLM call. Correct the roadmap's "Reuses Memory (Phase 4) unmodified"
  wording to say the *capture* path is unmodified and the *consumption* path is
  extended. The same amendment records Assumption 14: if the evidence gate
  shows it is needed, a narrow named normalizer inside `memory/fingerprint.py`'s
  `build_finding_fingerprint` that strips the line-range suffix from
  `duplication` messages. The function stays the single shared composition for
  the admin `approve` route and the suppression check, so capture and check
  cannot diverge.

**Tests:** *Unit* — `memory/test_fingerprint.py` (extended): the normalizer
(every existing input byte-identical; `duplication` messages differing only in
line numbers produce the same fingerprint); negative cases — the same file paired with a
different other path, and paths containing digits, `:1-5` or `v1-2`, must
**not** collapse to one fingerprint; and the admin `approve` route's capture
side (`memory` API/integration test, extended) yields the same fingerprint as
the suppression check for shifted line ranges. The evidence-gate measurement
(Assumption 14) is recorded in the phase PR. *Integration* —
`review/test_clone_detection_step.py` (extended),
real Postgres/pgvector and the real local embedding model: an approved
`dismissed_finding` suppresses a similar clone finding (US-C12); a `pending`
entry does not; an approved entry from another repo does not; the
Assumption 14 case — the other location's line range shifted by a few lines
still suppresses. `review/test_verification_step.py` and the Memory suites run
unchanged as the regression proof for the extraction. *Unit* —
`review/test_memory_suppression.py` for the helper with fake matcher/embedder.
**Observability & rollback:** `memory_suppressed_total` and the log field;
rollback is a revert, or `memory_suppression_enabled=false` (existing flag).
**Acceptance:** US-C12 tests pass, including the shifted-lines case;
verification and Memory suites unchanged and green; the ADR-0009 amendment is
merged with the phase.

**Phase 5 outcome (2026-10-17):** the evidence gate said the normalizer is not
needed (Assumption 14), so the normalizer, its negative cases and the admin
`approve` parity test were not built; `memory/test_fingerprint.py` instead
pins that clone messages with shifted ranges still fingerprint to the exact
`file\nrule_id\nmessage` text (function unchanged), and the shifted-lines case
is covered by the integration test above. The ADR-0009 amendment records the
measurement. The helper is `suppress_via_memory(findings, *, config, repo_id)`;
`CloneDetectionStep` fails open on `MemoryStoreError` (findings kept, warning
logged) and logs `suppressed_by_memory_count`.

## Phase 6 — Ordering, Helm/Compose, runbook (spec Unit E remainder)

**Why sixth:** the step is complete and usable after Phase 4; this phase makes
it correct under the finding ceilings and operable in a real deployment. It
can run in parallel with Phase 5. Depends on Phase 4.

- US-C13 ordering: trace every ceiling a clone finding passes through (grep
  `apply_redaction_ceiling`, `REDACTION_FINDING_CEILING` and
  `POSTED_FINDINGS_CEILING` first; `findings/merge.py` today ranks only
  `source != "llm"` ahead of `llm`) and make `clone_detection` sort last at
  equal severity in each. One shared rank function lives in `findings`
  (beside `SEVERITY_RANK`) and every ceiling site calls it — no per-source
  branch scattered across callers (US-C11); `apply_redaction_ceiling` itself
  stays unchanged because `review/review_generation.py` also uses it (Phase 9's
  Assumption 7 reasoning). Dropped counts keep being logged.
- `charts/review/values.yaml`, `templates/configmap.yaml`, `docker-compose.yml`:
  one-for-one mirror of the five settings (`REVIEW_WORKER_CLONE_DETECTION_*`);
  `charts/review/test-render.sh` and `apps/test_helm_render.py` updated.
- `docs/reference/operations.md`: a clone-detection section — the settings, the
  `min_tokens >= 100` floor and why, what the backfill is and the load of the
  first post-upgrade push per repo (`INDEXING_ENABLED=false` defers it), the
  Memory flag's shared scope, storage growth figures from Phase 2, the
  non-default-base-branch limit (Assumption 13), and a "noisy or wrong finding"
  triage (disable the step). Also: an explicit **migrate-first step** (the
  chart has no migration Job or hook — `alembic upgrade head` before `helm
  upgrade`, as the existing rule says, now with the Worker crashing index jobs
  if skipped); the **rollback procedure** (set the flag false, and reset
  `repo.clone_fingerprint_version` to 0 so the next push re-backfills —
  with the exact SQL, run only **after every old Worker has drained** —
  otherwise old Workers keep leaving stale rows behind the reset); concrete
  expressions, not descriptions: stuck backfill
  `increase(review_indexing_clone_backfill_total{outcome="incomplete"}[6h]) > 3`,
  ceiling-dropped files `increase(review_indexing_clone_backfill_dropped_total[1d]) > 0`,
  tokenizer failures `rate(review_indexing_clone_stage_failed_total[1h]) /
  (rate(review_indexing_files_processed_total[1h]) +
  rate(review_indexing_clone_stage_failed_total[1h])) > 0.2` (the denominator
  is every file that reached the clone stage, so the 0.2 threshold means a
  fifth of attempted files), and the step
  failing open `sum(rate(review_clone_detection_outcome_total{outcome=~"failed_open_.*"}[1h]))
  / sum(rate(review_clone_detection_outcome_total[1h])) > 0.2` (metric-name
  spelling confirmed against the exporter when written); an operator SQL query
  counting repos with `clone_fingerprint_version < <current>` (the only view of
  a repo that never gets another default-branch push, since `incomplete` only
  emits when a job runs); and a **release
  note** that the stuck-job threshold sum grows from 515 s to 545 s, so an
  operator-overridden `stuck_job_warning_threshold_seconds` between 515 and
  545 now fails Worker startup. The background-thread-after-timeout note lives
  here too.

**Tests:** *Unit* — `findings/test_merge.py` and the ceiling tests in
`review/test_static_analysis_step.py` (extended): 120 mixed findings, at equal
severity `clone_detection` drops first, security and high severity survive.
*Integration* — `review/test_findings_output_step.py` (extended, recorded
GitHub cassette): an over-ceiling job still posts the security findings and
posts a surviving clone finding as an ordinary inline comment (US-C11, US-C13);
a clone finding and an LLM finding on the same line with similar text dedupe
under the existing rule (US-C11).
`apps/test_helm_render.py` and `charts/review/test-render.sh` pass; the
Worker-boot settings test from Phase 4 is listed against US-C15 in the
traceability table with the Helm render tests.
**Observability & rollback:** no new telemetry; dropped counts already logged.
Rollback is **setting `clone_detection_enabled=false`** (the default is on in
code, so reverting the values file alone does nothing) plus the version reset
in the runbook.
**Acceptance:** ordering tests pass at every ceiling traced; Helm render and
Compose config checks pass.

## Phase 7 — E2E, dashboard, documents (spec Unit F)

**Why last:** it proves the goal end to end over the real Compose stack and
closes the observability loop — Phase 7 of the previous feature shipped with
dashboard panels that no E2E traffic exercised, and `test_observability`
failed for it. Depends on Phases 4–6.

- `tests/e2e/test_clone_detection.py` (new), reusing the stub GitHub/LLM
  fixtures and helpers exported by `tests/e2e/test_review_pipeline.py` the way
  `test_static_analysis_expansion.py` does: index a fixture default-branch
  repo through the real `push` path (so fingerprints are written by the real
  worker), then drive a PR that adds a renamed-identifier copy of an existing
  function in each of TypeScript, PHP and Go → one `duplication` finding per
  copy naming the right location; a line edited inside a pre-existing clone →
  none; a renamed file → none; the step disabled (`REVIEW_WORKER_CLONE_DETECTION_ENABLED=false`)
  → posted output equals the baseline; and a **failure case** (testing.md's
  failure-modes rule): a repo with no fingerprint data yet still gets a
  completed review with the `no_matches` outcome and no clone finding.
- Alert expressions stay documentation in the Phase 6 runbook (this repo has no
  alert-rule mechanism to extend, and no spec story asks for shipped rules);
  the dashboard panels show the same series.
- `charts/review/dashboards/review.json`: panels for step outcome and duration,
  candidates/filtered/findings, memory-suppressed and cap-dropped counts,
  fingerprints written and deleted, backfill outcome and dropped counts, and
  index-time clone-stage failures. `tests/e2e/test_observability.py`: extend its driven
  traffic so every new panel receives data, **or** list any panel that cannot
  in `_PANELS_WITHOUT_E2E_TRAFFIC` with the reason, in the same change. The
  fail-open outcome panels (`failed_open_*`) and `incomplete` backfill are the
  expected candidates for that list; the E2E failure case above supplies
  `no_matches` traffic.
- Documents: new ADR recording D1 (engine choice: custom winnowing over
  adopting jscpd/PMD CPD, with the competitor basis) as ADR-0014; update
  `docs/documentation_index.md` rows (ADR, plan, and any summary changed by
  Phase 5's ADR-0009 amendment); update `docs/specs/spec-observability.md`'s
  catalog with the Assumption 12 instruments. The spec's **Status** changes
  from Draft only after its own sign-off; the roadmap's Phase 10 **Status**
  is refreshed only by re-running its verification basis, never from this
  plan.
- Manual verification (not automatable here): run against a real repository
  of each language, review the first findings for false positives (spec A1/A2
  — Go's repeated error-handling idioms are the named risk), and record the
  result honestly in the roadmap, as Phase 1's unexercised "real PR" clause
  was. A measured false-positive problem changes the default (disable by
  default, or raise the Go threshold) by a recorded owner decision, not
  silently.

**Tests:** *E2E* as above; the existing full E2E suite (run one module per
process, per the roadmap's note on the stub-port collision) and
`test_observability` green.
**Observability & rollback:** this phase *is* the observability closeout.
Rollback: disable the step and revert the dashboard JSON. Panels added also
cover `clone_stage_failed_total`, `clone_backfill_dropped_total` and
`candidate_cap_dropped_total`; each is exercised by E2E traffic or listed in
`_PANELS_WITHOUT_E2E_TRAFFIC` with a reason.
**Acceptance:** `test_clone_detection.py` and `test_observability.py` green;
`ruff`, `ruff format --check`, `mypy --strict`, unit, integration, frontend
and Helm render all green; the manual real-repo result recorded.

## Security review

One pass across everything this plan introduces. Worst case first.

- **Poisoning what a PR is compared against.** A fork PR must not be able to
  plant fingerprints that later PRs match. Mitigation: the lookup hard-codes
  `pr_number = 0`, and `pr_number = 0` rows are written only from
  default-branch pushes (the `spec-indexing.md` Assumption 9 contract,
  unchanged); Phase 3 has a test that `pr_number > 0` rows are never returned.
- **Source exposure through a comment.** A finding discloses a default-branch
  file path and line range to anyone who can read the PR. Messages are built
  from paths and integers only — never source text — and pass through the
  existing `escape_markdown` and redaction like every message. **Trust
  assumption, stated:** the PR author has read access to the repo. Unlike a
  citation, the step is an oracle — any author can submit code and learn
  whether near-identical code exists on the default branch, with its path and
  line range — so an author with narrower access than the repo (a fork author
  on a private repo, a restricted-path setup) would learn paths they could not
  read. This repo's deployment model (one customer's own repos, ADR-0004) makes
  that acceptable; the runbook states it, and the spec's A7 is amended to
  match. Omitting the path for fork PRs is the fallback if an owner wants it.
- **Resource exhaustion.** PR file count and size are bounded by the existing
  pre-filter and blob ceilings; the step adds a fail-open internal timeout,
  per-hash and per-job candidate caps (Assumption 10), a hash batch size and
  the finding cap. A CPU-bound parse inside `asyncio.to_thread` cannot be
  cancelled by `wait_for` and may finish in the background after a timeout —
  bounded by the blob-size ceiling; accepted and noted in the runbook.
- **Adversarial input.** Tree-sitter is a parsing library, not an
  interpreter; no subprocess, no repo-supplied code execution, no network in
  any new path. A malformed or adversarial file raises a typed error that is
  isolated per file at index time and fail-open at review time.
- **Fingerprint-collision abuse.** An attacker crafting window-hash
  collisions can at worst waste candidate lookups (capped); it cannot create a
  false finding because every region is verified to per-token equality
  (US-C9).
- **Authz / cross-tenant.** No new endpoint, credential or permission. All
  reads are scoped by the job's own `repo_id`; the index is per-repo, so no
  cross-repo or cross-org path exists. Memory suppression stays repo-scoped
  (ADR-0009 decision 2).
- **Information at rest.** Fingerprints and per-token hashes are hashes plus
  line coordinates, stored in the customer's own Postgres; no LLM, embedding
  or network egress. They are not "non-reversible" in practice: the per-token
  array reveals code structure and could be dictionary-attacked after a
  database leak, so it is treated as sensitive as the chunk text beside it. It
  is covered by the same `repo` deletion cascade as chunks and graph nodes.
- **Noise as a trust attack.** A flood of low-value comments buries real
  ones; mitigated by the 100-token / 10-line floor, the more-than-half
  new-code rule, severity `low`, the cap of 5, sorting last under every
  ceiling, and the per-language clean-code zero-finding fixtures.

## Compatibility & rollout

- **Schema:** one additive Alembic migration (`0011`): two new tables and one
  `NOT NULL DEFAULT 0` column on `repo`. No existing column is altered; old
  code ignores the new objects, so rollback needs no down-migration to be
  safe (the migration's `downgrade` is still written and tested).
- **Contracts changed:** `IndexRepository.upsert_file` gains a parameter and
  `CloneBackfillTracker` (new) two methods (internal Protocols; every implementer and
  fake updated in the same change, found by grep first); `DiffFile` gains an
  optional field (additive, default `None`, safe across serialization between
  steps); `Source` gains a literal; `resolve_review_pipeline_depends_on`
  returns one more key. Nothing external changes.
- **Rollout order inside a deploy:** run `alembic upgrade head` before
  `helm upgrade` (existing operations rule — the Worker's new code reads the
  new column). The first default-branch push per repo then triggers the
  backfill; the review step returns no default-branch matches for a repo until
  its backfill completes (a normal state, US-C3).
- **Default-on caveat:** the step defaults enabled (spec A2), so an upgrade
  changes review output once a repo is backfilled. The release note and
  runbook tell operators to set `clone_detection_enabled=false` before
  upgrading for a quiet rollout; severity `low` and the cap of 5 bound a bad
  default.
- **Backfill load:** one full-run-sized index job per repo on the first
  post-upgrade push, bounded by the 20,000-file / 2 GB ceiling; deferrable
  with `INDEXING_ENABLED=false`.
- **Dependency failure containment:** an empty index, a database error, a
  timeout or an unparseable file yields zero clone findings and a completed
  job; a fingerprint-stage error at index time costs a file its clone data
  only, never its chunks.
- **Rolling upgrades and reverts:** old Worker code never touches the clone
  tables, so a rolling upgrade or a revert leaves stale fingerprints for files
  changed or deleted under old code, and a roll-forward would not repair them
  (the version is already stamped). Mitigations: the writer (Phase 2) and the
  reader (Phase 4) ship in separate releases; the reader requires a matching
  `index_chunk` row; rollback resets `clone_fingerprint_version` to 0.
- **Backfill stamping:** transient failures leave the version stale (retried by
  the next push); deterministic outcomes (ceiling drops, `clone`-stage errors)
  stamp and are counted — see Assumption 5.
- **Phase merge order:** 1 → 2 → 3 → 4 each mergeable alone and inert until
  Phase 4 (the step is the only reader); 5 and 6 parallel after 4; 7 last.
  Phase 5 is the one that may slip without blocking release.

## Traceability (story → phase → integration test)

| Story | Phase | Integration test |
| --- | --- | --- |
| US-C1 fingerprints stored with the file | 2 | `adapter/storage/test_clone_fingerprint_repository.py`; `indexing/test_pipeline.py` |
| US-C2 stale fingerprints never survive | 2 | `adapter/storage/test_clone_fingerprint_repository.py` |
| US-C3 backfill by version bump | 2 | `indexing/test_pipeline.py`; `adapter/storage/test_indexing_repository.py` |
| US-C4 copy flagged with both locations | 1 (engine), 3, 4 | `review/test_clone_detection_step.py`; `adapter/storage/test_clone_query.py` |
| US-C5 only duplication the PR introduces | 4 | `review/test_clone_detection_step.py` |
| US-C6 in-PR duplicates; stale/renamed copies ignored | 4 | `review/test_clone_detection_step.py`; `adapter/github/test_diff.py` |
| US-C7 renames/literals match; structure doesn't | 1, 4 | `review/test_clone_detection_step.py` (unit: `indexing/test_clone.py`) |
| US-C8 small/boilerplate/non-production not flagged; noise bound | 1, 4 | `review/test_clone_detection_step.py` (unit: `indexing/test_clone.py`) |
| US-C9 collision cannot produce a finding | 1, 4 | `review/test_clone_detection_step.py` (unit: `indexing/test_clone.py`) |
| US-C10 bounded comments | 4 | `review/test_clone_detection_step.py` |
| US-C11 behaves like every finding | 4, 6 | `review/test_findings_output_step.py` (unit: `findings/test_merge.py`; `findings/test_models.py` extended in Phase 4 for the widened `Source`) |
| US-C12 dismissal stops recurrence | 5 | `review/test_clone_detection_step.py` |
| US-C13 ordering under ceilings | 6 | `review/test_findings_output_step.py` (unit: `findings/test_merge.py`) |
| US-C14 never blocks a review | 4 | `review/test_clone_detection_step.py`; `review/test_review_job.py` (a fail-open step inside the real chain: the job completes and still posts the other steps' findings) |
| US-C15 switch and bounds | 4, 6 | `review/test_review_job.py`; `apps/worker/test_main.py` (Worker boot, startup validation); `apps/test_helm_render.py`; unit: `apps/worker/test_settings.py`, `review/test_config.py` |
| All, happy path + observability | 7 | `tests/e2e/test_clone_detection.py`; `tests/e2e/test_observability.py` |

A phase is not done until its rows' tests exist and pass.

## Open items for the owner

0. **Resolved (owner delegated, 2026-10-17):** `pr_number == 0` `INDEX` jobs
   are NOT serialized by this plan. A per-repo advisory lock was drafted and
   removed after the third review (see Assumption 5 and "Follow-up"); the
   backfill uses a conditional cursor-and-version write instead.
1. **Resolved (owner, 2026-10-17):** Assumption 1 — floor at 100; the spec's
   US-C15 now says so.
2. **Resolved (owner, 2026-10-17):** Assumption 2 — per-token arrays in the
   index (≈ 4 MB per million tokens); the spec's storage NFR now says so.
3. **Resolved (owner delegated, 2026-10-17):** Assumption 14 — one narrow,
   named normalizer function for `duplication` messages inside
   `build_finding_fingerprint`, gated on measured evidence. The gate ran in
   Phase 5 and said **no normalizer is needed** (Assumption 14); recorded in
   the ADR-0009 amendment.

No open owner decisions remain. Still unproven (measured in Phases 1–3, not
decided): the false-positive rate on real repos, backfill CPU/embedding cost,
and the storage and latency figures.

## Follow-up (not part of Phase 10): serializing default-branch index jobs

`claim_next` hands the oldest pending row to whichever Worker asks, with no
per-repo lock, so two `pr_number == 0` `INDEX` jobs for one repo can overlap
and an older job can overwrite a newer job's rows and move the cursor back.
That race predates this phase and affects chunks and graph nodes equally; this
phase only keeps it from making a backfill stamp permanent. If it is fixed
later, the third review's findings are the checklist, so it is built right:
- A session-level advisory lock (the repo's only existing advisory lock,
  `apps/admin_api/bootstrap.py`, is transaction-scoped) must be released on a
  dedicated connection that is **invalidated, not returned to the pool**, on
  unlock failure or cancellation, with the unlock shielded from a second
  cancel; it does not work behind PgBouncer transaction pooling.
- The key must be derived from 64 bits of the repo UUID under a fixed
  namespace constant (not `hashtext`, which is 32-bit), kept separate from the
  bootstrap key.
- A bounded wait needs `pg_try_advisory_lock` polling or `SET LOCAL
  lock_timeout`, and a timed-out job **must not be marked `FAILED` and
  forgotten**: an ordinary push's payload covers only its own commits, so its
  files would never be re-indexed. It needs a retry, or a forced tree diff
  when the cursor differs from the payload's parent commit.
- A lock serializes jobs but not their commit order; an older job running
  after a newer one still regresses the cursor unless it is skipped.
Prefer a queue-level dedupe or concurrency key (the Sidekiq-unique-jobs /
GitHub Actions concurrency-group model) over a long-held database lock.
