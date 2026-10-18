# Plan: Phase 11 — Auto-fix / committable suggested-fix generation

**Status: Proposed.** Sequences
[`docs/specs/spec-auto-fix.md`](../specs/spec-auto-fix.md) —
[`docs/roadmap.md`](../roadmap.md)'s Phase 11 — into seven buildable,
independently-testable phases, following the phase / tests / observability &
rollback / acceptance shape of [`plan-phase-10.md`](plan-phase-10.md). Built
against [`docs/rules/architecture.md`](../rules/architecture.md) (verification
tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

This plan does not re-litigate the spec's product and architecture calls — the
separate `fix_generation` macro step (D1), eligibility by source/verification/
severity (D2), single contiguous range in one hunk (D3), deterministic-first
validation (D4), delivery inside the finding's own comment (D5), never
auto-applied (D6), fail-open (D7), off by default (D8), Memory populate-only
(D9), the `fix_generation` routing stage (D10) and the changed-line rule (D11).
It sequences the work and records the implementation-shape choices the spec
leaves open as **Assumptions** below. Reading the code during drafting
produced four spec corrections or gaps, all resolved here or in the spec:
overlap can only be detected after generation (spec US-AF2 and US-AF14
amended, 2026-10-17); the `output` step's dedupe runs after this step and
keeps the first duplicate (Assumption 8); the existing stuck-job validator
would reject the default settings once a 90 s step exists (Assumption 11); and
`indexing.chunk.language_for_path` does not cover JavaScript (Assumption 2).

**Delivery unit.** Phases 3, 4 and 5 land in one release (one branch, as Phases
9 and 10 did): Phase 3 wires `output` to `fix_generation` only when
`fix_generation_enabled` is true, so a build with Phase 3 but not Phase 5 would
reference an unregistered step if an operator enabled the flag. The setting is not
documented for operators until Phase 7.

This is additive work on running code. Every phase names the module it
extends; each reference below was checked against the file during drafting:
`findings/models.py` (`Finding`, `Source`, `source_rank`), `findings/markdown.py`
(`escape_markdown`), `findings/merge.py` (`merge_findings`, `_dedupe`:
first occurrence wins), `findings/redact.py` (`TextRedactor`),
`review/changed_lines.py` (`changed_lines`), `review/file_matchers.py`
(`match_python`, `match_javascript`, `match_php`, `match_go`),
`review/static_analysis.py` (`AnalyzerRegistration`, `_AnalyzerSubStep`,
`StaticAnalysisStep._fetch_config_files`, `RepoConfigFetcher`,
`analyzer_slots`), `review/clone_detection.py` (the fail-open macro step to
copy: internal `asyncio.wait_for`, constructor-injected `depends_on`),
`review/verification.py` (LLM call shape, `model_resolver.resolve(self.name,
org_id)`, `record_llm_token_usage`, unmatched-verdict-fails-closed),
`review/llm_response.py` (`build_completion_request`,
`parse_llm_json_envelope`), `review/llm_telemetry.py`, `review/config.py`
(`StepEnablement`, `StepTimeouts`, `resolve_review_pipeline_depends_on`,
`check_step_configuration_viable`), `review/output.py` (`FindingsOutputStep`,
its `depends_on` constructor parameter), `review/step.py` (`Step`),
`review/test_step_boundaries.py` (the exclusion list), `indexing/chunk.py`
(`parse_file`, `ChunkLanguage`, `language_for_path`), `ingestion/diff.py`
(`Diff`, `DiffFile.patch`, `DiffFetcher.fetch_content`),
`adapter/github/posting.py` (`_finding_body`, `_review_comments`,
`_record_provenance`, the `(path, line, body)` correlation key),
`memory/models.py` (`PostedComment`, `MemoryEntry`), `memory/store.py`
(`MemorySignalRecorder.record_comment_provenance`),
`adapter/storage/models.py` (`PostedFindingCommentRow`),
`adapter/storage/memory_repository.py`, migration `0011_clone_fingerprints.py`
(latest revision), `llmgateway/routing.py` (`KNOWN_MODEL_ROUTING_STAGES`,
`FallbackModelRoutingResolver`), `apps/admin_api/model_routing_routes.py`,
`apps/admin_api/frontend/src/api/types.ts` (`MODEL_ROUTING_STAGES`),
`apps/worker/settings.py` (`WorkerSettings`, the stuck-job validator),
`apps/worker/main.py` (`_build_review_registry`, `_build_findings_publisher`),
`charts/review/` (`values.yaml`, `templates/configmap.yaml`,
`dashboards/review.json`), `docker-compose.yml`, `docs/reference/operations.md`,
`tests/e2e/test_review_pipeline.py` (`_StubLLMHandler`, `_GitHubScenario`,
`_CallLog.review_bodies`), `tests/e2e/test_static_analysis_expansion.py` and
`tests/e2e/test_observability.py`.

**Conventions found / fallbacks.** Plan format: mimics `plan-phase-10.md`
(there is no `docs/plans/README.md`). Architecture: `docs/rules/architecture.md`
and `c4-architecture.md`. Testing: `docs/rules/testing.md`. All Python
commands in this plan run in the repo's Docker dev image
(`Dockerfile --target dev`), per the operator's standing rule — never a host
interpreter.

## Scope

**In scope** — the spec's Units A–F in full: the pure fix model and
validation, suggestion delivery through the GitHub adapter, settings, routing
stage and chain wiring, the analyzer re-run helper, the `fix_generation` step,
the Memory provenance marker, Helm/Compose/runbook, E2E, dashboard panels and
the ADR the spec recommends.

**Out of scope** — the spec's out-of-scope list applies unchanged:
auto-applying or committing a fix; multi-file or non-contiguous fixes; fixes
for Gitleaks, Trivy and `clone_detection` findings, for unverified LLM
findings and for unsupported languages; Memory *consumption* of `accepted_fix`
entries; a confidence score on `Finding`; fixes on lines the PR did not change;
Admin UI controls beyond the one model-routing stage row; runtime/sandbox
verification of a fix. Also not done here: cross-push de-duplication or reuse of
fixes (spec A9 — this plan only verifies and records the existing behavior,
Assumption 14); carrying fetched file content through step outputs to avoid a
repeat blob fetch (the Phase 10 Assumption 8 precedent); and updating the
roadmap's Phase 11 **Status** line or its "Findings merger gains a sub-step"
wording (refreshed only by re-running the roadmap's verification basis, and
corrected when the phase ships, per spec D1).

## Assumptions this plan makes (flagged, not decided)

Sequencing and implementation-shape calls the spec does not fix. Challenge any
of these before the phase that depends on it starts.

1. **Module layout.** `findings/models.py` gains the `Suggestion` model and
   `Finding.suggestion`; `findings/markdown.py` gains the suggestion-block
   renderer (shared by the adapter and its tests). Two pure helper modules
   in `review/`: `fix_validation.py` (patch application, range and text checks,
   syntax gate) and `fix_candidates.py` (eligibility, ordering, overlap).
   `review/analyzer_rerun.py` holds the re-analysis helper (Assumption 4).
   `review/fix_generation.py` holds the one `Step`. The four helpers define no
   `Step`, so they join the exclusion list in `review/test_step_boundaries.py`
   with the same rationale as `clone_regions`/`context_symbols`.
2. **Syntax-gate language map is local to the gate.** `indexing.chunk.
   language_for_path` governs *indexing* scope (`.ts .tsx .php .go`) and must
   not change (JavaScript would start being indexed). The gate has its own
   map built from the same extension sets as `review/file_matchers.py`:
   `.ts` → `typescript`, `.tsx` → `tsx`, `.js .jsx .mjs .cjs` → `tsx` grammar
   (a JavaScript superset parse — a spec A-class caveat: it may accept some
   invalid JavaScript, which ESLint's re-analysis, which parses as JavaScript,
   catches), `.php` → `php`, `.go` → `go`, `.py` → stdlib `ast.parse`. Tree-sitter
   gate: count `ERROR` and `MISSING` nodes via `indexing.chunk.parse_file`;
   patched must not exceed original (spec US-AF5). Python has no error count:
   the original must parse and the patched must parse; an original that does
   not parse rejects the fix (reason `syntax_error`) — a conservative narrowing
   of the spec's "adds none" rule for the one language where it cannot be counted.
3. **Re-analysis compares rule counts over the whole file, not a line range.**
   A fix changes line numbers below it, so the spec's "the original finding
   (same rule, within the range) no longer appears and nothing new on the
   patched range" is implemented line-shift-safely: run the originating
   analyzer on the original file content and on the patched content, whole file,
   with `changed_lines_only` filtering disabled, and require (a) the count of
   findings with the target's `rule_id` drops by at least one, (b) no `rule_id`
   count rises, and (c) no finding of the target's rule sits on the patched
   range's post-patch line span. This is stricter than the spec in (b) (any rise
   anywhere rejects) — a deliberate conservative choice that costs some
   acceptances and never admits a regression. The "before" run is cached per
   `(path, analyzer)` per job so several fixes in one file pay it once. Each
   fix is validated independently against the original file; two non-overlapping
   accepted fixes are never validated *together* (GitHub applies suggestions one
   at a time) — recorded as a runbook limitation.
4. **Re-analysis reuses the registered analyzers, not a second copy.**
   `apps/worker/main.py` builds the `AnalyzerRegistration` list once into a
   local and passes it to `StaticAnalysisStep` (unchanged) and to a new
   `AnalyzerRerunner` (`review/analyzer_rerun.py`) together with the **same
   `analyzer_slots` semaphore**, so re-runs count against the Worker-wide
   analyzer bound (spec US-AF6). `fix_generation.py` defines a consumer-side
   `PatchReanalyzer` Protocol (one method) and never imports
   `static_analysis`; `analyzer_rerun.py` is a helper (excluded from the
   boundary scan) that imports `AnalyzerRegistration`, `RepoConfigFetcher` and
   the config-size constants from `review/static_analysis.py`. Repo-native
   configs are fetched once per job through the existing `RepoConfigFetcher`,
   only for the engines actually re-run, and degrade to default profiles on
   failure exactly as in Phase 9.
5. **D11 is applied to the finding's line.** The finding's `line` must be in
   `changed_lines(patch)`; the fix's range must lie within one hunk's new-file
   lines (changed *or* context). A new pure `hunk_line_ranges(patch)` in
   `review/changed_lines.py` returns each hunk's contiguous new-side span
   (the existing `changed_lines` is left unchanged).
6. **The two LLM calls label usage differently from how they route.**
   Generation routes under stage `fix_generation` and records usage with that
   label. The patch rubric routes under `verification` (spec D10) but records
   usage with the label `fix_verification`, so the shared `review.llm` counter
   and the Admin spend view separate fix spend from the verification step's.
   Phase 3 first checks whether the Admin stats view hard-codes stage values
   (it reads label values off the counter today; no allow-list was found).
7. **Fail-open timeout is derived.** As `CloneDetectionStep` does, the step
   wraps its own work in an internal `asyncio.wait_for` at
   `fix_generation_step_timeout_seconds − 5`; a validator requires the step
   timeout to exceed 5 (no second setting). Within it, attempts run
   concurrently with `asyncio.gather` bounded by the per-job cap; the Worker
   has no other LLM concurrency limit to share.
8. **Candidate selection runs `merge_findings` first.** `findings/merge.py`'s
   dedupe (in the `output` step, after this one) keeps the **first** occurrence
   of near-duplicates and its ceiling drops findings. To avoid generating a
   fix for a finding that will be deduped or ceiling-dropped — or attaching a
   suggestion to a loser — `fix_generation` computes `merge_findings(
   incoming).findings` purely to choose candidates, then attaches each accepted
   `Suggestion` (via `model_copy`) to the matching finding in the **original**
   list, matched on `(file, line, message, source, rule_id)`. The step's output
   keeps every incoming finding, so `output`'s own merge, its dropped-count log
   and its metrics are unchanged; the surviving finding carries the suggestion
   (`_dedupe`'s `model_copy` keeps unrelated fields).
9. **Prompt shape.** One system message and one user message per call, through
   `review/llm_response.build_completion_request`; the user message numbers the
   window's lines with their real file line numbers so the model's
   `start_line`/`end_line` are in file coordinates. The response is one JSON
   object parsed with a local pydantic model (the `parse_llm_json_envelope`
   helper parses a *list* envelope, which does not fit; Phase 5 confirms and
   reuses it if it does). Window selection: `fix_generation_window_lines`
   centered on the finding line, clamped to the file, always containing the
   hunk-bounded candidate range limit.
10. **Provenance correlation uses the posted line.** `_record_provenance`
    keys submitted findings by `(file, finding.line, body)` and matches GitHub's
    echoed comment `(path, line, body)`. A multi-line suggestion comment is
    posted with `line = end_line`, so a module-level `_comment_line(finding)`
    (`suggestion.end_line` when a suggestion is present, else `finding.line`)
    is used by both `_review_comments` and the correlation key. The stored
    `PostedComment.line` stays `finding.line` (what Memory fingerprints on).
11. **Stuck-job threshold default rises from 600 s to 700 s.**
    `WorkerSettings._stuck_job_warning_threshold_covers_worst_case_chain`
    sums *every* macro step's timeout regardless of enablement. Today's sum is
    545 s; adding `fix_generation` at 90 s makes 635 s, above the 600 s
    default, so the Worker would refuse to start with default settings. Phase 3
    adds the new term to the validator and raises the default to 700 s (a
    warning threshold only; no behavior change below it). The runbook's
    arithmetic is updated (Phase 7).
12. **Migration `0012` adds a boolean, nullable-free, defaulted column.**
    `posted_finding_comment.carried_suggestion boolean not null server_default
    false` (spec Unit E; the spec left "migration or none" to the plan — there
    is no existing field to reuse). Additive; old Workers never write it.
13. **Telemetry names** follow the catalog's `review.<component>.<name>` form:
    meter `review.fix_generation` with `outcome_total{outcome}` (closed set:
    `accepted` plus the rejection reasons listed in spec US-AF14),
    `candidates_total`, `attempts_total`, `skipped_cap_total`, `duration_ms`,
    and `phase_duration_ms{phase}` (`fetch_content`, `generate`, `validate`,
    `reanalyze`, `rubric`), mirroring `review_clone_detection_phase_duration_ms`.
    Dashboard panels arrive in Phase 7, not earlier.
14. **Re-push behavior is inherited and verified, not changed (spec A9).**
    No cross-push de-duplication of review comments exists in
    `review/output.py`, `adapter/github/triggers.py` or the webhook routes (a
    grep during drafting found none; each `synchronize` enqueues a fresh
    review). Phase 5 re-confirms this, records it in the runbook, and adds no
    caching. If the confirmation finds existing de-duplication, the step
    inherits it and the plan records the difference instead.
15. **The stub LLM is extended by prompt marker, not call position.** The E2E
    `_StubLLMHandler` answers by call *order* today; the new calls are made
    after `verification` and in variable number, so the stub gains a branch that
    recognizes the fix prompts by a fixed marker in the system message and reads
    a new `fix_scenario`, leaving the existing order logic untouched.
16. **Recorded GitHub cassette is hand-built from GitHub's documented
    response schema.** No real repository was available while drafting; the
    multi-line suggestion comment cassette (`start_line`, `line`, `side`,
    `body` with the fence) is constructed from the REST docs. The spec's
    "not verifiable in this repo" caveat stands: the manual real-repo
    verification in Phase 7 is where the fence round-trip and the
    `(path, line, body)` match are confirmed against real GitHub.

## Phase 1 — Pure fix model and validation (spec Unit A)

**Why first:** everything else attaches, posts or gates what this produces. It
is pure, so it can be built and exhaustively tested with no database, network
or LLM, and it carries the spec's highest correctness risk (the trust
boundary: three grammars' error counting, hunk bounds, text rules) —
front-loaded.

- `findings/models.py` (extend): `Suggestion` (frozen pydantic: `start_line`,
  `end_line`, `replacement: str`) and `Finding.suggestion: Suggestion | None =
  None` — same default-`None`, frozen/hashable pattern as `context_ref_ids`;
  every existing construction site is unaffected. `Finding`'s docstring gains
  one paragraph.
- `findings/markdown.py` (extend): `suggestion_block(replacement: str) -> str`
  — a fence of at least three backticks and one longer than the longest backtick
  run in the replacement, info string `suggestion`; an empty replacement renders
  the empty block (deletion). Not passed through `escape_markdown` (US-AF10).
- `review/changed_lines.py` (extend): `hunk_line_ranges(patch) -> tuple[range,
  ...]` (Assumption 5), reusing the existing `_HUNK_HEADER` regex.
- `review/fix_validation.py` (new, pure, zero infra imports):
  - `check_range(suggestion, finding_line, hunks, max_range_lines)` — US-AF4's
    five conditions; returns a typed rejection reason or `None`.
  - `apply_suggestion(content: str, suggestion) -> str` — in-memory patch of the
    1-based inclusive range.
  - `check_replacement(original_range_text, replacement, max_lines)` — `noop`,
    `oversized`, `invalid_text` (`\r`, NUL).
  - `syntax_error_count(path, text) -> int | None` via the gate map
    (Assumption 2): Tree-sitter `ERROR`/`MISSING` node walk over
    `indexing.chunk.parse_file`, or `ast.parse`; `None` for an unsupported path.
  - `RejectionReason` — a `StrEnum` whose values are the closed metric label set
    (Assumption 13).
- `review/fix_candidates.py` (new, pure): `select_candidates(findings, diff_files,
  *, verification_enabled) -> list[Candidate]` — US-AF2's eligibility matrix
  (source allow-list, `llm` only when `verified`, ineligible sources and `info`
  severity, `line` in `changed_lines`, `patch` present, gate-supported path),
  ordered by `(SEVERITY_RANK, source_rank)`, one per `(file, line)`;
  `resolve_overlaps(accepted) -> (kept, dropped)` — same-file overlapping ranges,
  higher-ranked wins (reason `overlap`).
- Fixture corpus `review/testdata/fix/` per language: an original file, a valid
  patched file, a syntax-breaking patch, a file with a pre-existing syntax error
  outside the patched range, and (Go/PHP/TS/TSX/JS/Python) one deletion case.
  Gate completeness is pinned by a test over the corpus.

**Tests:** *Unit* (pure, ADR-0005) — `review/test_fix_validation.py`: US-AF4
range matrix (in one hunk accepted; cross-hunk, finding outside range, inverted,
over cap, off-file-side rejected; deletion accepted), `apply_suggestion` round
trips, US-AF5 per language (breaking patch rejected, clean patch accepted,
pre-existing error outside the range tolerated, JS parsed via the `tsx` grammar,
Python original-must-parse), US-AF8 `noop`/`oversized`/`invalid_text`, golden
rejection-reason strings. `review/test_fix_candidates.py`: the full US-AF2
eligibility matrix, ordering, same-line collapse, `resolve_overlaps`.
`findings/test_markdown.py` (extended): US-AF10 fence cases (replacement with
```` ``` ````, with a longer run, empty, trailing newline). `review/test_changed_lines.py`
(extended): `hunk_line_ranges` on multi-hunk, rename and no-newline patches.
`findings/test_models.py` (extended): `Finding` stays hashable with a suggestion.
**Observability & rollback:** none (no runtime caller yet); rollback is a plain
revert.
**Acceptance:** the files above green for TypeScript, TSX, JavaScript, PHP, Go
and Python; `findings`/`review` existing suites unchanged and green; `ruff`,
`ruff format --check` and `mypy --strict` clean in the Docker dev image.

## Phase 2 — Suggestion delivery through the GitHub adapter (spec Unit B)

**Why second:** the spec's only end-user-visible surface and its sharpest
external-contract risk (multi-line anchoring and the provenance correlation key).
Proving it against a recorded response now, before the step exists, surfaces a
bad assumption cheaply. Depends on Phase 1's `Suggestion` and
`suggestion_block`.

- `adapter/github/posting.py` (extend): `_finding_body` appends
  `suggestion_block(...)` **after** the citation and **unescaped** when
  `finding.suggestion` is set (the surrounding text keeps `escape_markdown`);
  `_review_comments` emits `{"path", "line": end_line, "start_line": start_line,
  "start_side": "RIGHT", "side": "RIGHT", "body"}` for a multi-line range and the
  existing single-`line` form otherwise; a module-level `_comment_line(finding)`
  (Assumption 10) is used by `_review_comments` and by `_record_provenance`'s
  `submitted` key. `post_review`'s event stays `COMMENT`, the check-run stays
  `neutral`, and no other GitHub call is added (US-AF9).
- Cassette: `adapter/github/testdata/review_comment_multiline_suggestion.json`
  (Assumption 16) holding a create-review response and the `GET .../comments`
  echo for a one-line, a multi-line and a deletion suggestion.

**Tests:** *Integration* (`respx` cassette against the real adapter, per
`testing.md`) — `adapter/github/test_posting.py` (extended): US-AF1 the posted
body is the unchanged finding text followed by exactly one suggestion block;
the request payload carries `start_line`/`line`/`start_side`/`side` for a range
and only `line` for one line; US-AF9 the *only* requests a full publish makes
are the review POST, the comments GET and the check-run POST (no commit, ref,
branch or contents write), event `COMMENT`, conclusion `neutral`; US-AF10 a
replacement containing a backtick fence renders with a longer fence and the
surrounding text is still escaped; US-AF13 the provenance correlation matches a
multi-line comment (posted `line == end_line`, stored `PostedComment.line ==
finding.line`) and an unmatched comment is logged and skipped, never raised.
*Unit* — `_comment_line` and the payload builder as pure helpers.
**Observability & rollback:** the existing `posting: provenance capture
correlation summary` log carries `matched_count`/`unmatched_count`; a sustained
non-zero `unmatched_count` after enabling the feature is the signal that the
multi-line correlation is broken (the runbook says so, Phase 7). Rollback is a
revert; no data written.
**Acceptance:** the extended posting suite green against the cassette; with no
finding carrying a suggestion, the request payload and body for every existing
posting test are byte-identical to before (the existing assertions pass
unmodified).

## Phase 3 — Settings, chain wiring and the routing stage (spec Unit D, config half)

**Why third:** the step in Phase 5 is registered through these and the operator
safety properties (kill switch, startup validation) must exist before anything
spends LLM budget. All pure or declarative; no LLM yet. Depends on nothing from
Phases 1–2 beyond names.

- `apps/worker/settings.py`: `fix_generation_enabled: bool = False`,
  `fix_generation_step_timeout_seconds: int = Field(default=90, gt=0)`,
  `fix_generation_max_fixes_per_job: int = Field(default=5, gt=0)`,
  `fix_generation_max_range_lines: int = Field(default=20, gt=0)`,
  `fix_generation_max_replacement_lines: int = Field(default=40, gt=0)`,
  `fix_generation_window_lines: int = Field(default=60, gt=0)`; a validator
  (the `_clone_detection_settings_are_valid` shape) requires the step timeout
  greater than 5 and `window_lines >= max_range_lines + 2`; the stuck-job
  validator sums the new timeout in and its default rises to 700 s
  (Assumption 11), docstring arithmetic updated (545 → 635 s).
- `review/config.py`: `StepEnablement.fix_generation_enabled` and
  `StepTimeouts.fix_generation_step_timeout_seconds` (both added to
  `as_mapping`); `fix_generation_can_produce_value(enablement)` — enabled and
  at least one eligible producer is possible (`static_analysis_can_produce_findings`
  or `review_generation_enabled and verification_enabled`);
  `resolve_review_pipeline_depends_on` gains `"fix_generation"` (depends on
  `verification` if enabled, else `verification_depends_on`) and `output` depends
  on `"fix_generation"` when `fix_generation_can_produce_value`, else exactly as
  today; `check_step_configuration_viable`'s generic loop covers the new step.
- `llmgateway/routing.py`: `KNOWN_MODEL_ROUTING_STAGES` gains `"fix_generation"`
  (the docstring "three LLM-calling stages" updated).
  `apps/admin_api/frontend/src/api/types.ts`: `MODEL_ROUTING_STAGES` gains
  `"fix_generation"`. `docs/specs/spec-admin-gui.md` US-A14 gets a one-line note
  that the stage list is four from Phase 11.
- Helm/Compose: the six settings mirrored in `charts/review/values.yaml` (`worker`
  block, `fixGeneration…` camel-cased like the existing clone keys),
  `charts/review/templates/configmap.yaml`, `charts/review/values-production.yaml`
  (comment only: leave default) and `docker-compose.yml`; `charts/review/test-render.sh`
  extended to assert the keys render.

**Tests:** *Unit* — `apps/worker/test_settings.py`: defaults (off, 90/5/20/40/60),
each `<= 0` fails startup with a clear message, `timeout <= 5` fails,
`window_lines < max_range_lines + 2` fails, the stuck-job sum now includes the new
timeout and the new default passes while 600 s with defaults is rejected.
`review/test_config.py`: chain wiring for every enablement combination of
`verification`, `static_analysis`, `clone_detection`, `review_generation` and
`fix_generation` (including `fix_generation` on with no producer → `output`
wired as today). *Integration* — `apps/admin_api/test_model_routing_routes.py`
(extended): `GET` lists four stages, `PUT /fix_generation` accepted, an unknown
stage still 422; the frontend `ModelRouting.test.tsx` (extended, `vitest`): the
fourth row renders and is editable like the others. `charts/review/test-render.sh`
(helm lint/template + compose config) passes.
**Observability & rollback:** none new at runtime; the startup validators are the
fail-fast signal. Rollback is a revert (default-off means no running behavior
depends on it).
**Acceptance:** the suites above green; `docker compose config` and
`helm template` render the new keys; the Worker starts with all defaults (no
stuck-job validator failure).

## Phase 4 — Analyzer re-run helper (spec Unit C, re-analysis half)

**Why fourth:** it is the riskiest reuse seam — it calls the Phase 9 analyzers
from outside `static_analysis` — and the step cannot be completed without it.
Isolating it with real binaries lets the step in Phase 5 treat it as a Protocol
fake in unit tests. Depends on Phase 1's gate vocabulary only.

- `review/static_analysis.py` (small extension, reuse before new code): promote
  `_AnalyzerSubStep` to a public `AnalyzerSubStep` (rename plus export; its
  behavior and the `StaticAnalysisStep` suite are unchanged). It already owns the
  per-engine file routing, the allow-listed config-file filter, the per-analyzer
  timeout and the duration histogram, so the re-run helper builds on it instead
  of duplicating that logic.
- `review/analyzer_rerun.py` (new helper): `AnalyzerRerunner(registrations,
  analyzer_slots, timeouts, config_fetcher)`; `rerun(analyzer_name, path,
  original: bytes, patched: bytes, *, installation_id, repo_full_name, head_sha)
  -> ReanalysisResult` implementing Assumption 3: acquire the shared slot, run
  `AnalyzerSubStep` over the registration copied with
  `dataclasses.replace(registration, changed_lines_only=False)` on a one-file
  `Diff` + `content` for the original (cached per job per `(path, analyzer)`) and
  the patched bytes, honoring `timeouts[analyzer]`. Any analyzer error, timeout or non-success result
  returns `ReanalysisResult.failed` (the step maps it to `reanalysis_failed`).
  It never raises past its boundary. The config fetch reuses
  `RepoConfigFetcher.fetch_config_files` once per job for the needed names.
- `apps/worker/main.py`: hoist the `AnalyzerRegistration` list into a local used
  by both `StaticAnalysisStep` (arguments unchanged) and a new
  `AnalyzerRerunner`, passing the same `asyncio.Semaphore`. `review/static_analysis.py`
  gains no change beyond (if needed) exporting the config-size constants.
- `review/fix_generation.py` (stub in this phase, completed in Phase 5):
  `PatchReanalyzer` Protocol, consumer-defined, one method.

**Tests:** *Integration* (real analyzer binaries on fixtures, per `testing.md` —
the same pinned tools Phase 9 uses) — `review/test_analyzer_rerun.py` (new): US-AF6
for Ruff, ESLint, golangci-lint and PHPStan fixtures — a fix that clears the
finding passes; a fix that leaves it fails; a fix that clears it but introduces
another rule violation fails (Assumption 3 (b)); a fix that deletes a line
shifting later findings but changing no counts passes (line-shift safety); a
binary crash and a timeout both return `failed`; the "before" run executes once
for two fixes in one file; the shared semaphore is acquired for each run (a
2-slot test with 4 concurrent re-runs never exceeds 2 running at once); an
engine config present in the repo is honored. *Unit* — `review/test_step_boundaries.py`
(extended exclusion list).
**Observability & rollback:** reuses the existing `review.static_analysis`
analyzer duration histogram labeled by `analyzer_name` for the re-runs (no new
instrument; the step's own `phase_duration_ms{phase="reanalyze"}` in Phase 5 is
the per-job view). Rollback is a revert.
**Acceptance:** `review/test_analyzer_rerun.py` green against the real binaries;
`StaticAnalysisStep`'s existing suite unchanged and green.

## Phase 5 — The `fix_generation` step (spec Unit C)

**Why fifth:** it integrates Phases 1–4. Highest-risk phase (money, analyzers,
quality not provable in-repo), so everything it relies on is already proven in
isolation. Depends on Phases 1–4.

- `review/fix_generation.py` (complete): `FixGenerationStep` (`name =
  "fix_generation"`), constructor-injected `diff_fetcher`, `llm_client`,
  `reanalyzer: PatchReanalyzer`, `redactor: TextRedactor`, `model_resolver`,
  `internal_timeout_seconds`, the four caps/windows, `verification_enabled`,
  `depends_on` — the `CloneDetectionStep`/`VerificationStep` shape. `run`:
  1. Parse input (`from_attributes=True`, mirrors `VerificationOutput`'s fields; no
     import of another step).
  2. Candidate selection from `merge_findings(incoming).findings` (Assumption 8),
     `select_candidates`, truncate to `fix_generation_max_fixes_per_job` with the
     skipped count logged at `warning` and counted.
  3. One `fetch_content` for the candidates' files only; a fetch failure rejects
     those candidates (`fetch_failed`), never raises.
  4. Per candidate, concurrently (`asyncio.gather`): build the numbered window;
     generation call routed `model_resolver.resolve("fix_generation", org_id)`;
     parse the three-field JSON (`parse_error` on any deviation); then the gates in
     order — `check_range` → `check_replacement` → redaction (`redactor.redact(
     replacement) != replacement` → `redaction`; a redactor failure rejects only
     this fix) → `syntax_error_count` → (analyzer source) `reanalyzer.rerun` or (verified
     `llm` source) the rubric call routed under `verification`, four booleans
     (`resolves`, `minimal`, `safe`, `compiles_in_context`), all true, an
     unparseable or missing verdict failing closed.
  5. `resolve_overlaps` across accepted fixes; attach survivors with `model_copy`
     (Assumption 8); return every incoming finding.
  The whole body sits inside the internal `asyncio.wait_for`; on timeout, fixes
  already accepted are kept and the rest are counted as `timeout`. Any other
  exception in an attempt is caught per attempt, logged with `raise ... from`
  semantics preserved, and counted by reason; nothing propagates (US-AF12).
- System prompts carry a fixed marker line (used by the E2E stub, Assumption 15)
  and delimit PR content as data (US-AF3); neither prompt receives the diff,
  other files or other findings.
- `apps/worker/main.py`: `_build_review_registry` constructs `FixGenerationStep`
  unconditionally (the enablement-mapping-gated pattern), inserts it before the
  `FindingsOutputStep`, passes `depends_on=resolved["fix_generation"]`, and builds
  `FindingsOutputStep(depends_on=resolved["output"])`. `llm_step_kwargs` /
  `model_resolver` reuse the existing wiring.
- `review/test_step_boundaries.py`: the step module itself stays in the scan (it
  imports no sibling step).

**Tests:** *Integration* (real Postgres is not needed by this step; real parsers,
real Gitleaks redactor, real analyzer binaries, recorded LLM cassettes — per
`testing.md`) — `review/test_fix_generation_step.py` (new), the story→test heart
of the phase: US-AF1 an eligible finding gets exactly one suggestion and every
gate failure yields today's finding; US-AF2 the source/severity/status/line/
patch/language matrix end to end; US-AF3 exactly one distinct generation call
under stage `fix_generation` whose prompt contains only the finding and a window
within `fix_generation_window_lines`; US-AF4/AF5/AF8 each rejection from the
cassette produces the right reason; US-AF6 an analyzer finding is accepted only
when the real re-run agrees and rejected on crash/timeout; US-AF7 a verified LLM
finding passes only on four true criteria, with an unparseable and an unmatched
verdict failing closed and the rubric routed under `verification`; US-AF11 eight
candidates with cap 5 attempt the five highest-ranked and log/count the three
skipped, and a step timeout keeps completed fixes; US-AF12 an LLM timeout, bad
JSON, fetch failure, analyzer crash, rubric failure and redactor failure each
leave the findings unchanged and the step returning normally; the `merge_findings`
interaction (a near-duplicate pair where the first occurrence wins carries the
suggestion through `output`'s merge). `review/test_review_job.py` (extended):
US-AF12 a job with the step failing every attempt still completes and posts.
*Unit* — attach/match-back logic, window selection, prompt construction, response
parsing, the closed reason vocabulary (fakes at the `PatchReanalyzer`, `LLMClient`
and `DiffFetcher` Protocols).
**Observability & rollback:** `review.fix_generation` instruments per Assumption 13
(each recorded inside `suppress_telemetry_errors`, ADR-0012), a start/outcome log
per job with `job_id`, candidates, attempts, accepted, rejected-by-reason counts,
skipped, fetch count and duration, and token usage through
`record_llm_token_usage` with labels `fix_generation` and `fix_verification`
(Assumption 6). Rollback: `fix_generation_enabled=false` skips the step, `output`
rewires to its prior upstream, no cleanup.
**Acceptance:** `review/test_fix_generation_step.py` and the extended
`test_review_job.py` green; with the step disabled the existing review
integration suite is unchanged and green.

## Phase 6 — Memory provenance marker (spec Unit E)

**Why sixth, and cuttable:** spec priority "Should". The fix works without it;
this only makes fix threads distinguishable at approval. Depends on Phase 2's
correlation. If time forces a cut, drop this phase and ship Phases 1–5 and 7.

- Migration `0012_posted_comment_suggestion.py` (revises `0011`):
  `posted_finding_comment.carried_suggestion boolean not null server_default false`;
  `PostedFindingCommentRow` gains the column (Assumption 12).
- `memory/models.py`: `PostedComment.carried_suggestion: bool = False`;
  `adapter/github/posting.py` sets it from `finding.suggestion is not None`;
  `adapter/storage/memory_repository.py` writes it in `record_comment_provenance`
  and reads it into the Memory list view model. Phase 6 first reads the
  admin Memory list route and screen to find the one place the entry's source
  comment is displayed, and adds a read-only "suggested change" indicator there
  reusing the existing badge component; no new screen or control, no change to
  approve/reject/delete.
- No capture change: Phase 4's generic capture already records a reaction, reply
  or thread resolution on any posted comment as `pending` with `entry_type` unset;
  classification stays a human act (ADR-0009 decision 1) and no pipeline stage reads
  `accepted_fix` (US-AF13).

**Tests:** *Integration* (real Postgres via `testcontainers`) —
`adapter/storage/test_memory_repository.py` (extended): US-AF13 provenance row
stores the marker for a suggestion comment and `false` for an ordinary one; a
captured signal on a suggestion comment is `pending` with `entry_type` unset; a
dismissed-finding approval on a finding that also carried a suggestion still
suppresses a similar later finding exactly as before and happens before fix
generation (`review/test_fix_generation_step.py` asserts no generation call for
a suppressed LLM finding — suppression runs in `verification`, upstream).
`adapter/storage/test_migrations.py` (extended): up/down on a populated schema.
`apps/admin_api/test_memory_routes.py` (extended) and the Memory screen
`vitest` test (extended): the indicator renders. *Unit* — `PostedComment` default.
**Observability & rollback:** none new (the `matched_count`/`unmatched_count` log
from Phase 2 already covers correlation). Rollback: the column is additive and
unused if the code is reverted; no data cleanup. Deploy order: run `alembic upgrade
head` before the Worker (existing runbook rule); a Worker running ahead of the
migration logs a swallowed `MemoryStoreError` per posted batch and loses
provenance for that batch — fail-safe, documented.
**Acceptance:** the suites above green; the migration applies up and down; the
Admin API and frontend builds pass.

## Phase 7 — E2E, observability, runbook, ADR and docs (spec Unit F)

**Why last:** the closing proof over the full Compose stack, and it exercises every
new dashboard panel (the Phase 7 `test_observability` lesson). Depends on Phases
1–6.

- `tests/e2e/test_auto_fix.py` (new), reusing `test_review_pipeline.py`'s stub
  servers and helpers and `test_static_analysis_expansion.py`'s analyzer fixtures:
  a PR with a Ruff/ESLint/golangci-lint/PHPStan finding per language posts one
  review comment with one suggestion whose range and replacement match the stub
  LLM's validated patch, and the stub GitHub receives only the review and the
  check-run writes; each bad stub patch (syntax, no-op, secret-shaped, wrong
  range, re-analysis failure, rubric false) posts the finding with no suggestion
  and the job completes; Gitleaks, Trivy, clone and unverified LLM findings get
  none; the step disabled reproduces today's output byte for byte (the Worker is
  recreated, so this case runs last, as in Phase 10). The stub GitHub's tree and
  `patch` hunks must match what `changed_lines`/`hunk_line_ranges` expect (the
  Phase 7 stale-stub lesson). `_StubLLMHandler` gains the marker-based
  `fix_scenario` branch (Assumption 15).
- Dashboard (`charts/review/dashboards/review.json`): panels for fix outcomes,
  rejection reasons, phase durations and fix LLM usage;
  `tests/e2e/test_observability.py` extended so each panel receives data from this
  test's traffic (or its check updated in the same change).
- `docs/reference/operations.md`: a "Fix generation" runbook section — what is
  sent to the LLM endpoint (a bounded file window plus the finding text, the same
  data class as the diff the review already sends, to the same endpoint), the
  default-off switch and its cost bound (≤ 10 extra calls per job at defaults), the
  per-reason outcome meanings, the `unmatched_count` provenance signal, the stuck-job
  arithmetic (635 s, default threshold 700 s), the migration-before-Worker rule, the
  single-suggestion-at-a-time limitation (Assumption 3), the re-push behavior
  (Assumption 14), the rate-limiter pairing, and how to verify rollout. It carries
  two recommended alert expressions in the style of the clone-detection runbook: the
  share of attempts ending in `parse_error`, `timeout` or `fetch_failed` over an hour
  (a systematically failing endpoint or prompt), and a sustained non-zero provenance
  `unmatched_count` after enabling (a broken multi-line correlation).
- `docs/adr/0015-fix-generation-step-and-validation-gate.md` (**PROPOSED**): the
  separate step vs a merger sub-step (D1), the validate-before-display gate and its
  deterministic-first ordering (D4), default-off (D8) and the never-auto-apply
  guarantee (D6); added to `docs/documentation_index.md`, as are the plan's own row
  and the runbook summary update.
- Manual real-repository verification (carried as in Phases 1, 9 and 10; not
  runnable in this repo): against a real GitHub repo and a real LLM endpoint,
  confirm the fence round-trips, the multi-line `(path, line, body)` correlation
  matches, a sample of validated fixes is read for subtle wrongness, and measure the
  rejection-reason distribution. Record the result in the roadmap's Phase 11 status
  note when run.

**Tests:** *E2E* — `tests/e2e/test_auto_fix.py` (new) and `tests/e2e/test_observability.py`
(extended), per `testing.md` (happy path plus the failure modes that matter: LLM
timeout, bad patch, analyzer crash). *Integration* — `charts/review/test-render.sh`
re-run.
**Observability & rollback:** this phase delivers the dashboard and runbook;
rollback of the feature as a whole is `fix_generation_enabled=false`, then `helm
rollback` / image revert, and the migration need not be reversed.
**Acceptance:** the spec's phase-acceptance scenario passes over the Compose stack
(a suggestion per supported language; each bad-patch gate posts none; ineligible
sources get none; disabled equals baseline) and every new dashboard panel receives
data; the full gates in the roadmap's "Verification basis" (`ruff`, format, `mypy
--strict`, unit, integration, frontend build + `vitest`, `test-render.sh`) pass in
the Docker dev image.

## Traceability

Per `testing.md`: every story maps to at least one integration test (pure logic is
also Unit per ADR-0005).

| Story | Phase | Test path | Level |
| --- | --- | --- | --- |
| US-AF1 | 2, 5 | `adapter/github/test_posting.py`, `review/test_fix_generation_step.py` | Integration |
| US-AF2 | 1, 5 | `review/test_fix_candidates.py`, `review/test_fix_generation_step.py` | Unit + Integration |
| US-AF3 | 5 | `review/test_fix_generation_step.py` | Integration |
| US-AF4 | 1, 5 | `review/test_fix_validation.py`, `review/test_fix_generation_step.py` | Unit + Integration |
| US-AF5 | 1, 5 | `review/test_fix_validation.py`, `review/test_fix_generation_step.py` | Unit + Integration |
| US-AF6 | 4, 5 | `review/test_analyzer_rerun.py`, `review/test_fix_generation_step.py` | Integration (real binaries) |
| US-AF7 | 5 | `review/test_fix_generation_step.py` | Integration |
| US-AF8 | 1, 5 | `review/test_fix_validation.py`, `review/test_fix_generation_step.py` | Unit + Integration |
| US-AF9 | 2, 7 | `adapter/github/test_posting.py`, `tests/e2e/test_auto_fix.py` | Integration + E2E |
| US-AF10 | 1, 2 | `findings/test_markdown.py`, `adapter/github/test_posting.py` | Unit + Integration |
| US-AF11 | 5 | `review/test_fix_generation_step.py` | Integration |
| US-AF12 | 5 | `review/test_fix_generation_step.py`, `review/test_review_job.py` | Integration |
| US-AF13 | 2, 6 | `adapter/github/test_posting.py`, `adapter/storage/test_memory_repository.py` | Integration (real Postgres) |
| US-AF14 | 5, 7 | `review/test_fix_generation_step.py`, `tests/e2e/test_observability.py` | Integration + E2E |
| US-AF15 | 3 | `apps/worker/test_settings.py`, `review/test_config.py`, `apps/admin_api/test_model_routing_routes.py`, `ModelRouting.test.tsx` | Unit + Integration |
| All (happy path) | 7 | `tests/e2e/test_auto_fix.py` | E2E |

## Security review

One pass across every new input, trust boundary and permission this plan
introduces.

- **Prompt injection through PR content.** A changed file (comment, string, docstring)
  can try to steer the model's replacement. Controls, in layers: the model's only
  output is three typed fields parsed by a local pydantic model (no tool use, no
  free text acted on); the range is confined to one hunk and must contain the
  finding's own changed line (`check_range`); the patched file must parse; analyzer
  findings must clear under the same analyzer, LLM findings under a separate rubric
  call; and the result is a suggestion the PR author must click. The prompts delimit
  PR content as data. **Residual risk** (spec A5): a subtle malicious edit confined to
  the author's own changed lines can pass the gates; the click is the last control.
  For a **fork PR** the contributor controls the content and a maintainer may commit
  the suggestion into the contributor's branch; the runbook states this and the
  feature is off by default.
- **Secrets.** Gitleaks findings are never candidates; a replacement that the
  `TextRedactor` changes is rejected, not posted redacted. The prompt window is a
  bounded slice of a file the review already sends in the diff to the same endpoint,
  so no new exposure class is introduced, but a window can include lines near a
  secret the diff hunk did not; the runbook documents it and the window size is
  bounded (`fix_generation_window_lines`).
- **Rendering injection.** The only unescaped text reaching GitHub's renderer is the
  suggestion block, inside a fence longer than any backtick run in the replacement;
  `\r`/NUL are rejected; every other part of the comment keeps `escape_markdown`.
  The finding message (analyzer- or LLM-derived) still passes through the existing
  redaction and escaping.
- **Resource exhaustion.** LLM spend is bounded by `fix_generation_max_fixes_per_job`
  (≤ 2 calls per fix), the step timeout and per-attempt isolation; the prompt is a
  window, not the diff; analyzer re-runs share the Worker-wide analyzer semaphore and
  per-analyzer timeouts so a PR cannot multiply subprocesses; replacement and range
  sizes are capped; a content fetch is limited to the attempted candidates' files.
  A PR that produces many low-value candidates only wastes up to the cap per
  review. **Repeat-push spend:** any PR author, including a fork contributor,
  triggers a fresh review per push, and no cross-push fix reuse exists
  (Assumption 14), so spend is bounded per job but not per PR. The existing
  per-repo review-enqueue rate limiter (`review_enqueue_rate_limit_per_minute`,
  disabled by default; `docs/reference/operations.md`) is the control that bounds
  repeat pushes; the runbook (Phase 7) states that enabling fix generation should be
  paired with enabling it.
- **AuthN/authZ and cross-tenant.** No new endpoint, credential, GitHub permission
  or write API (US-AF9). Model selection is per org through the existing resolver, and
  the data stays within the org's own configured endpoint. The Admin routing route's
  existing `admin`-only write guard covers the new stage unchanged (the stage is just
  one more member of the validated set).
- **Supply chain / subprocess.** No new binary, dependency or network egress; the
  re-analysis uses the pinned, checksum-verified Phase 9 tools, honoring repo configs
  as data only (executable configs are never run), unchanged.

## Compatibility & rollout

- **Contracts changed, all additive.** `Finding` gains one optional field defaulting
  to `None` (the `context_ref_ids` precedent; findings never leave the process between
  steps, so no serialized contract changes). `StepEnablement`/`StepTimeouts` gain
  fields with defaults. `KNOWN_MODEL_ROUTING_STAGES` and the frontend list gain a
  member (an Admin API client that hard-codes three stages sees a fourth row, which the
  `GET` already enumerates). Migration `0012` adds a defaulted column.
- **Sequencing.** Phases 1–5 ship code that is inert while `fix_generation_enabled`
  is false (the default). The suggested rollout is: deploy with the switch off →
  confirm the migration applied → enable on one staging deployment or org →
  watch `review_fix_generation_outcome_total` rejection reasons, the `unmatched_count`
  provenance log and fix LLM spend → widen. The migration is run before the Worker
  rolls (existing runbook rule); the opposite order is fail-safe (Phase 6).
- **One breaking-looking change, contained.** The stuck-job warning threshold default
  rises from 600 s to 700 s (Assumption 11). It is a warning threshold only; an
  operator who pinned the setting below 635 s will see the Worker refuse to start with
  a message naming the required minimum, which is the validator's existing behavior
  and is called out in the release note and runbook.
- **Dependency failure during rollout.** A slow or failing LLM endpoint, analyzer or
  GitHub content fetch is contained per attempt and by the step's internal timeout
  (US-AF12); it can delay a review by at most the step timeout and never fails it. A
  full LLM outage is already surfaced by the existing startup check and
  `review_generation` failures.
- **Rollback.** Disable the flag (instant, no cleanup); comments already posted remain
  valid GitHub comments. Reverting the code leaves migration `0012` in place unused.
