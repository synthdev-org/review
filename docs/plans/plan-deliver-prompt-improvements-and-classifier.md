# Plan: Review-quality improvements and the opt-in Classifier call type

**Status: Proposed (revision 3, after two `plan-review` passes).** Sequences two
bodies of work into ten phases, in two releases:

- **Release 1 (Phases 1-4): review-quality improvements** derived from
  [`docs/research/pr-review-bot-comments-chaptered.md`](../research/pr-review-bot-comments-chaptered.md).
  Ships first, as its own PR(s), with an offline eval harness. Phase 4 is
  optional (D6).
- **Release 2 (Phases 5-10): the `Classifier` call type**, sequencing
  [`docs/specs/spec-classifier.md`](../specs/spec-classifier.md) units 1-6 as
  decided by
  [ADR-0014](../adr/0014-classifier-call-type-swappable-backend-typesafe-first.md).
  Phases 8-10 ship together (a settable `active` mode must not exist before the
  runbook and shadow procedure do).

Format follows `plan-deliver-smell-and-spec-review.md`
(format only). Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (tag `RQPTVE`).
Reuse claims were checked against the working tree on 2026-11-24.

**A plan sequences, it does not decide.** Release 2's decisions live in ADR-0014
and the classifier spec (both ACCEPTED 2026-11-24, D1 signed off; Phases 5-10 unblocked).
**Release 1 has no spec yet**: Phase 1 writes it and the owner signs it off
before any Release 1 code lands (D2).

## Open decisions (owner, before the named phase)

| # | Decision | Blocks | Recommended |
| --- | --- | --- | --- |
| D1 | Sign off ADR-0014 and `spec-classifier.md`, including (a) its departure from "no interface without a second implementation in view", (b) an amendment that **v1 implements `noul` questions only** (`choice`/`score` deferred; ADR text and spec header edited in Phase 6), and (c) an amendment to US-C4: a classifier "no" on a high or critical finding goes to the LLM judge instead of dropping it. | 5-10 | Accept |
| D2 | Confirm Release 1 content: mandatory `scenario`, `tier`, reachability severity rubric, style-advice suppression, nit tier opt-in per org, within-job root-cause dedupe, fence-safe agent-ready fix prompt. (`effort` is dropped.) | 2-3 | Write the spec in Phase 1, review before coding |
| D3 | Research items 9 (plan/spec claim review) and 11 (learning signals, largely covered by shipped `memory/`) stay out of scope. | none | Defer, track in `docs/roadmap.md` |
| D4 | TypeSafe retention terms for non-enterprise accounts are unverified (DPA not read). Disclosure text will say "secrets detectable by the vendored ruleset are redacted", never "no secrets leave". | 8 | Operator confirms retention before the disclosure says anything about it |
| D5 | One operator-provisioned credential map: a `credential_ref` is global, so any org admin can bind it and spend the shared quota, and one org's large PRs can crowd others. Choose (a) state "single operator key, shared quota" in disclosure and ops docs, or (b) per-org ref allow-list and per-org concurrency share. | 8 | (a) for v1 |
| D6 | Phase 4 analyzers were not in the request. Approve as a separate PR, or drop (and delete RQ8). | 4 | Approve, last in Release 1 |
| D7 | The eval corpus quotes diff excerpts from the owner's repo into this repo. Confirm they are publishable (Gitleaks catches secrets, not proprietary code or PII). | 1 | Confirm |
| D8 | Memory fingerprints use `file/rule_id/message`, so the prompt rewrite can re-surface previously dismissed findings once, on the first v2 run. Accept, or require a mitigation (fingerprint without message). | 2 | Accept, documented in release notes |
| D9 | Proposed active-mode alert: fallback-to-LLM rate above 20% over one hour; missing-scenario drop rate above 30% of medium+ findings over one hour. Confirm the numbers. | 9-10 | Accept as starting values |

## Scope

**In scope**

- Release 1: eval corpus and harness; `Finding` additions (`scenario`, `tier`);
  prompt rewrite and one rubric criterion; tiered, capped, deduped, fence-safe
  output; optionally two deterministic analyzers (D6).
- Release 2: spec-classifier units 1-6.

**Out of scope**

- Research items 9 and 11 (D3). `effort` field. Cross-review-round dedupe (needs
  prior-review state the pipeline does not hold).
- Classifier uses beyond verification; `choice`/`score`/`box`; a second backend;
  per-repo override; a threshold setting.
- Generation, intent summary, indexing (ADR-0007). Dashboards (documented
  queries and alert rules only).

## Assumptions this plan makes (flagged, not decided)

| # | Assumption | Why it is safe |
| --- | --- | --- |
| A1 | New `Finding` fields `scenario: str | None` and `tier: Literal["blocker","should_fix","nit"] | None` are scalar, default `None`, like `category`/`verification_status`. There is **no** separate `suggestion` field: a suggestion block is rendered from `message`. | Established pattern; findings move as `model_dump` JSON and the model has no `extra=forbid`. |
| A2 | `findings_show_nits` is a new `SETTING_KEY_REGISTRY` key (bool, default false, org scope). | Registry is the closed, additive mechanism (precedent `FindingsSeverityThresholdSetting`). |
| A3 | Rubric criteria are one shared tuple. The new `not_style_only` criterion is added to `_VerificationVerdictItem` as `bool = True` (default), and `_passes_rubric` and the failure counters iterate only the criteria active for the job's version. A v1-shaped response parsed by v2 code, and the reverse, both validate. `has_scenario` is not a criterion (the parse rule enforces it). | Fixes the mixed-version parse failure: `_parse_verdicts` drops invalid items one by one and correlation is fail-closed, so a required new field would silently reject every finding. |
| A4 | Eval cassettes are recorded through the real `adapter/llm/litellm_client.py` against a real endpoint and replayed with **respx** (already a dev dependency; no new replay client). Two committed cassette sets: v1 (baseline) and v2. CI replays both; a **cassette-integrity test** fails if a prompt change leaves a cassette's request body stale, forcing a deliberate re-record. Re-recording is a manual, owner-run step and is the real gate. | `testing.md` mandates cassettes by default and live mode only manually or nightly. Removes the circularity of replaying v1 cassettes against a v2 prompt. |
| A5 | `StaticAnalyzer.run(diff, content: dict[str, bytes], *, timeout)` receives changed-file bytes only (`review/static_analysis.py:84`, verified). New analyzers do not widen it. | Anchor checks cover same-file anchors and link targets present in the diff or the repo tree listing (`RepoTreeFetcher.fetch_tree`). |
| A6 | Classifier model defaults to pinned `jev-1.14.0`, a setting. | The alias moves. |
| A7 | 0.7/0.3 thresholds are constants, no setting, until shadow data shows a need. | Spec open item 1. |
| A8 | One switch, `review_quality_v2_enabled` (bool in `WorkerSettings`), gates the new prompt, the scenario parse rule, the extra verification criterion and the new posted layout together. `WorkerSettings` is built once at process start, so the switch is **per process**, not per job (earlier drafts said per job: wrong). A job snapshots the value once at pipeline start into its job context and every step uses that snapshot. Rollback = set false and restart workers. Phase 2's first task is to verify what the queue does with interrupted jobs (`adapter/storage/queue.py` claim/reclaim); if a restarted job can resume with v2-shaped state, that is raised as a decision, not worked around. Removal date: delete the v1 path one release after v2 is default. | Single kill switch; no v1 prompt ever meets the v2 parse rule. |
| A9 | Retry-after parsing already exists in `adapter/product_knowledge/transport.py` (~line 312) and `adapter/github/http.py`. Phase 5 carries the `transport.py` copy into `adapter/egress/`; Phase 7 uses it with a bounded loop. `adapter/github/http.py` is left alone (a documented duplicate; adapters cannot import siblings). | Avoids a third copy. |

---

# Release 1: review-quality improvements

## Phase 1: Spec, eval corpus, cassette harness, secret scanning

**Why first:** every prompt change must be measurable; `testing.md` requires
stories mapped to integration tests; Release 2's shadow evaluation reuses the
corpus.

- Write `docs/specs/spec-review-quality.md`: Given/When/Then stories RQ1-RQ8 (the
  table at the end names them) with the traceability table. Owner signs off (D2).
- Corpus under `review/testdata/eval/` (new): about 40 true-positive and 25 noise
  rows from the research doc as `{diff_excerpt|null, finding_text, label,
  category, source_pr}`. Rows with `diff: null` feed verification-judge replay
  only. Publication confirmed (D7).
- `review/eval/` (new): metric computation (precision of kept findings, recall on
  true positives, findings per PR, per-category keep rate), a small respx
  cassette helper (none exists for LLM calls today), and a record script
  `python -m review.eval.record`. No separate `--report` CLI and no replay
  `LLMClient`: a single pytest reads committed cassettes and asserts metrics.
- **Secret scanning:** a CI step runs Gitleaks over `review/testdata/eval/` and
  **every cassette and fixture directory** (including Release 2's
  `adapter/classifier/testdata/`); the record script scrubs `Authorization` and
  cookie headers before writing.

**Tests:** unit for metrics. Integration (real `LiteLLMClient` over respx
cassettes, through the real `ReviewGenerationStep` and `VerificationStep`): report
shape and the cassette-integrity test. Gitleaks test: a planted secret in a
scratch fixture fails the CI step. No live calls in CI. Note for reviewers: about
65 rows give little statistical power; the gate is "no labelled true positive
lost and precision not lower", not a tolerance on a noisy ratio.

**Observability & rollback:** offline tooling, no runtime effect. Rollback is
deleting the package, testdata and CI step.

**Acceptance:** `pytest review/eval` green; v1 `baseline.json` and cassettes
committed with a documented re-record command; Gitleaks step wired and proven;
spec signed off.

**Phase 1 delivery notes (2026-11-24, facts verified in the tree):**

- `respx` was already a dev dependency, but no LLM respx helper existed:
  `adapter/llm/conftest.py` and `review/test_review_generation_step.py` use a
  local stub HTTP server. LiteLLM's default aiohttp transport bypasses respx
  (the call fails with a connection error), so `review/eval/cassette.py` sets
  `litellm.disable_aiohttp_transport = True` for the duration of a mock.
- Corpus rows add `id`, `file`, `line` and `severity` to the planned fields
  (needed to build a `Finding`, to match a kept finding back to its row, and to
  measure severity calibration). 65 rows (40 true positive, 25 noise), 20 with
  `diff_excerpt: null`; excerpts are paraphrased synthetic minimal diffs (D7).
- Cassettes store request and response bodies only, never headers, so
  `Authorization` and cookies cannot be written (stronger than scrubbing).
  Layout: `review/testdata/eval/cassettes/<version>.json`,
  `review/testdata/eval/baseline.json` (v1 only until Phase 2 records v2).
- **The committed v1 cassette and baseline are synthetic** (no live endpoint was
  available): flagged by `synthetic: true` and a `note` in both files. Owner
  re-record: `EVAL_LLM_API_KEY=<key> python -m review.eval.record --version v1
  --endpoint <openai-compatible /v1 url> --model <model> --write-baseline`.
- Gitleaks: `scripts/scan-fixtures.sh` runs in the integration CI job (the job
  that installs the binary). It excludes `review/testdata` and
  `adapter/analyzers/testdata`, which plant secrets on purpose. The integration
  tests (`review/eval/test_eval_cassettes.py`, `test_scan_fixtures.py`) run with
  `pytest -m integration review/eval`; the unit tests with plain `pytest`.
- `review/eval/cassette.py` and `record.py` import `respx`/`httpx` inside the
  `review` domain package. This is dev/CI tooling at the location the plan names;
  nothing in the runtime pipeline imports `review.eval`.

## Phase 2: `Finding` fields, prompt rewrite, verification criterion

**Why second:** schema before output; baseline before change.

- **First task:** verify interrupted-job semantics (A8) and record the finding in
  this plan.
- `findings/models.py`: `scenario`, `tier` (A1).
- `review/review_generation.py`: new prompt pieces (defect-class checklist,
  reachability-based severity rubric, negative instructions including no praise
  and no style or complexity advice unless the smell baseline is on,
  previous-behaviour and PR-intent checks, `scenario` required at medium and
  above, `tier` in the JSON shape), selected by the job's A8 snapshot.
  `_build_system_prompt` keeps its byte-identical guarantee for disabled options.
  Under v2, a medium+ finding with no scenario is dropped at parse with a
  `warning`; an unknown `tier` becomes `None` with
  `dropped_total{reason="tier_invalid"}`. Under v1 neither rule applies.
- `review/verification.py`: `not_style_only` per A3, with the default so mixed
  versions parse.
- **Redaction:** `findings/redact.py::redact_findings` currently updates only
  `message`. Redact `message` and `scenario` (the two LLM-sourced free-text
  fields), counted against the existing finding ceiling per finding, not per
  field. `rule_id` and `citation_markdown` are platform-set and unchanged.
- Log `review_quality_version` on job logs (no metric label).

**Tests:** unit: v1 prompt byte-identical; flag combinations; parser at the exact
`medium` boundary; invalid `tier`; redaction of a secret-shaped `scenario`;
v1-shaped verdicts parsed by v2 code and v2-shaped by v1 code (A3); job snapshot
honoured when the setting changes mid-process, and a missing or invalid value
defaults to v1; `memory.fingerprint.build_finding_fingerprint` for a finding
under v1 and v2 wording, asserting the documented re-surfacing (D8). RQ3 (no style
advice without the baseline) in the same cassette file as RQ1/RQ2. Integration:
v2 cassette run against the committed baseline (A4). Existing step tests and
`tests/e2e/test_review_pipeline.py` stay green under v1.

**Observability & rollback:** `review.generation.dropped_total{reason}`; `tier`
is a closed-set label with `none` for `None`. All new instruments are added to the
`docs/specs/spec-observability.md` allow-list in the same PR. Operations runbook
line (Phase 3 PR): how to flip and verify `review_quality_v2_enabled`, and the D9
missing-scenario drop-rate alert (an empty review otherwise looks like a clean
PR). Rollback: the A8 switch.

**Acceptance:** v2 cassettes show no labelled true positive lost and precision not
below baseline, or the owner records an explicit tradeoff; v1 output byte-identical
to before; the interrupted-job question answered.

**Phase 2 delivery notes (2026-11-24, facts verified in the tree):**

- **A8 interrupted-job finding (the first task).** `JobQueueRepository.claim_next`
  (`adapter/storage/queue.py`) selects only `status == PENDING` rows; nothing ever
  moves a `CLAIMED` row back to `PENDING`, and `apps/worker/dispatch.py`'s own module
  docstring states the gap ("a job whose Worker process is terminated mid-processing
  stays `CLAIMED` forever ... no reclaim-timeout logic exists"). A job interrupted by
  a restart is therefore **never resumed**: it is not re-run under the new setting
  value, so no job can start under v1 and resume under v2 (or the reverse). Pipeline
  state is in memory per run, not persisted between steps. The "resume with v2-shaped
  state" risk does not exist today; nothing to raise as a decision. **Revisit if a
  reclaim or retry mechanism is ever added**: a reclaimed job would re-run from the
  start under the then-current setting, which is still safe because the snapshot is
  taken at dispatch, but a mid-job resume would not be.
- **Snapshot mechanism.** `WorkerSettings.review_quality_v2_enabled` (default false,
  env `REVIEW_WORKER_REVIEW_QUALITY_V2_ENABLED`, also in `docker-compose.yml` and
  `.env.example`) is passed once to `run_dispatch_loop`, which stamps it on each
  REVIEW job's `StepContext.review_quality_v2` (`apps/worker/dispatch.py`). Steps read
  only the context, never the settings. The job log line `review job context` carries
  `review_quality_version` (`v1`/`v2`).
- **Fields and prompt.** `Finding.scenario` and `Finding.tier` (`findings/models.py`).
  `review/review_generation.py` appends `_V2_REVIEW_SECTION` and the `scenario`/`tier`
  JSON-shape fields only under v2; the style-advice prohibition is added only when the
  smell baseline is off. The v1 prompt is the same string as before (asserted by
  `review/test_review_quality_v2.py` and by the unchanged v1 cassette replay).
- **Parse rules.** Under v2, a finding whose final severity is `medium` or above with
  a blank `scenario` is dropped (`review.generation.dropped_total{reason=
  "missing_scenario"}`, `warning` log); a smell clamped to `low` needs none. An unknown
  `tier` becomes `None` and is counted as `tier_invalid` (the finding is kept). Under
  v1 both fields are ignored.
- **Verification.** `_VerificationVerdictItem.not_style_only: bool = True`;
  `_criteria_for(v2)` selects the four or five criteria for the pass decision and the
  `*_failures` log fields; `_SYSTEM_PROMPT_V2` is derived from the v1 prompt. The
  candidate JSON shown to the judge now includes `scenario` when set.
- **Redaction.** `redact_findings` redacts `message` and, when set, `scenario`, inside
  one semaphore slot per finding (the subprocess bound is unchanged).
- **Observability.** `review.generation.dropped_total` and a `tier` label on
  `review.findings.produced_total` are in the `spec-observability.md` allow-list. The
  operations runbook line and the D9 alert rule stay with Phase 3/10 as planned.
- **Eval.** `python -m review.eval.record --version v2` records the v2 set; the
  committed `review/testdata/eval/cassettes/v2.json` is **synthetic** (labelled in its
  `synthetic` and `note` fields; no live endpoint). Owner re-record: `EVAL_LLM_API_KEY=<key>
  python -m review.eval.record --version v2 --endpoint <openai-compatible /v1 url>
  --model <model>` (no `--write-baseline`: `baseline.json` stays v1). The synthetic v2
  replay keeps every labelled true positive at precision above the synthetic baseline;
  that statement is only as good as the synthetic judge until the owner re-records.
- **Story tests.** RQ1-RQ3 integration: `review/test_review_quality_prompt.py` (real
  `LiteLLMClient` over respx; canned completions are synthetic and labelled so). The D8
  fingerprint test is in `memory/test_fingerprint.py`.
- **Deviation from the test list.** "A missing or invalid value defaults to v1": a
  missing value defaults to v1 (tested). An invalid `REVIEW_WORKER_REVIEW_QUALITY_V2_ENABLED`
  is rejected by pydantic-settings at worker startup, like every other bool setting,
  rather than silently defaulting; failing fast was judged the safer behaviour.

## Phase 3: Tiered, capped, deduped, fence-safe output

**Why third:** needs `tier` from Phase 2.

- `findings/merge.py`: within-job root-cause dedupe added to `_dedupe`. Key: same
  `category`/`rule_id` (or both `None`) and message similarity above a **separate,
  stricter cross-location threshold** (the existing
  `_MESSAGE_SIMILARITY_THRESHOLD` was tuned for same-location dedupe), ignoring
  file and line. The merged finding takes its **message, scenario, severity and tier from the highest-severity member** (ties: first occurrence) and
  records the other locations. Tier-aware ordering so `nit` is cut first by
  `POSTED_FINDINGS_CEILING`. Existing first-occurrence-wins and `context_ref_ids`
  carry-forward preserved. A `None` tier is treated as `should_fix`.
- `adapter/github/posting.py`, `findings/markdown.py`: blocker and should_fix
  inline up to the cap; `nit` (only when allowed) and overflow in one collapsed
  summary comment, prose escaped with `escape_markdown`; layout gated by the A8
  snapshot.
- **Fence-safe agent prompt and suggestion blocks:** a separate renderer, not
  `escape_markdown`. Dynamic fence length longer than any backtick run in the
  content, no raw PR-author text beyond already-redacted `message` and
  `scenario`, **file paths rendered inside the fence only after validation against
  a strict charset** (git allows backticks and newlines in paths; failing paths
  are omitted from the block), no verbatim diff lines by default, and an
  untrusted-data statement.
- `tenancy/settings.py`: `findings_show_nits` (A2). Resolution reaches
  `review/output.py` through a consumer-defined narrow reader Protocol injected at
  the composition root, following `adapter/product_knowledge/provider_resolver.py`.

**Tests:** unit: dedupe true and false merges (distinct defects with shared
boilerplate must not merge), highest-severity retention, all-nits ordering,
mixed-tier overflow, `None` tier, fence-safe rendering against hostile `message`,
`scenario` and file paths (backtick runs, newlines, markdown, mentions),
`findings_show_nits` validation. Integration (real adapters over cassettes or
Postgres): the GitHub posting adapter over a cassette asserting the posted body
for RQ4 (nits collapsed, then shown), RQ5 (cap) and RQ7 (hostile `scenario`
rendered safely); per-org `findings_show_nits` through the injected reader with a
real tenancy store (`testcontainers` Postgres) and two orgs; an all-nit PR with
`findings_show_nits=false` posts no inline comments and a correct check-run
status; RQ6 dedupe through the output step into the posting adapter cassette.
E2E: `tests/e2e/test_review_pipeline.py` extended.

**Observability & rollback:** `posted_by_tier`, `collapsed_total`,
`deduped_total` (every merge also logged), `dropped_by_ceiling_by_tier`
(closed-set; added to the allow-list). `MergeResult.dropped_by_ceiling` is already
logged. Rollback: the A8 switch reverts layout and prompt together;
`findings_show_nits` covers nits only.

**Acceptance:** the integration tests above pass; an E2E review shows at most the
cap of inline comments, collapsed nits, no duplicate root cause, and a hostile
finding cannot close a fence.

**Phase 3 delivery notes (2026-11-24, facts verified in the tree):**

- **Gating.** All new behaviour runs only when `StepContext.review_quality_v2` is true
  (the A8 snapshot). `merge_findings(findings, tiered=False)` is the old behaviour; the
  v1 `post_review` call has no `collapsed_findings` argument and the v1 review body and
  comment bodies are byte-identical (existing posting tests pass unchanged).
- **Merge (`findings/merge.py`).** Root-cause dedupe runs after the same-location dedupe,
  with a separate threshold `_ROOT_CAUSE_SIMILARITY_THRESHOLD = 0.9` (the same-location one
  stays 0.8). A pair merges only when both are `source == "llm"`, with equal `category` and
  `rule_id`; the `llm`-only rule is an addition to the plan's key (a static-analysis rule
  firing in two files is two occurrences). The first finding is kept; its `severity` and
  `tier` become the highest of the pair and the others' `file:line` go in the new
  `Finding.other_locations` (a tuple; a new optional field, not in A1, needed because no
  existing field can hold them without changing `message` and so the memory fingerprint).
  `None` tier counts as `should_fix`. Under v2 the list is ordered tier, severity, source
  trust, so the ceiling cuts `nit` first.
- **Layout.** `review/output.py` drops nit-tier findings before merge unless the org opted in
  (`nit_hidden`), then splits the merged list: non-nit inline (at most
  `POSTED_FINDINGS_CEILING`, the cap), nits plus findings cut by the ceiling in
  `collapsed_findings`. `adapter/github/posting.py` puts the collapsed list in one
  `<details>` block in the review body (prose escaped with `escape_markdown`, at most 50
  items of 200 characters, then "and N more not listed"). The check-run counts inline plus
  collapsed. An all-nit PR with nits hidden posts no review and a "No findings" check-run.
- **Fence-safe renderer.** `findings/agent_prompt.py` (new, pure): dynamic fence longer than
  any backtick run, path included only if it matches `[A-Za-z0-9_][A-Za-z0-9_./-]*` with no
  `..` segment, control characters stripped, only the redacted `message` and `scenario`,
  no diff lines, untrusted-data notice first. Under v2 each inline comment also shows the
  `scenario` (escaped), the "Also at" locations and this block.
- **Setting.** `findings_show_nits` is `{"enabled": bool}`, registered in
  `SETTING_KEY_REGISTRY`, read at platform and org scope (the reader never passes a repo id).
  `review.output.ShowNitsResolver` is the consumer-defined Protocol;
  `tenancy.show_nits.FailClosedShowNitsResolver` (timeout, fail closed) is built in
  `apps/worker/main.py` over the existing `PostgresSettingRepository`.
- **Observability.** `review.findings.posted_by_tier_total{tier}`, `collapsed_total{reason}`,
  `deduped_total{kind}`, `dropped_by_ceiling_by_tier_total{tier}` are in the
  `spec-observability.md` allow-list; every merge is logged. The runbook section is in
  `docs/reference/operations.md` (flip and verify the switch, the D9 alert).
- **Tests.** Unit: `findings/test_merge.py`, `findings/test_agent_prompt.py`,
  `tenancy/test_settings.py`, `tenancy/test_show_nits.py`, `review/test_output_v2.py`,
  `review/test_output_telemetry.py`. Integration (real step and publisher over respx; the
  shared fixture is `run_output_step` in `adapter/github/conftest.py`):
  `adapter/github/test_posting_tiers_integration.py` (RQ4, RQ5),
  `test_posting_rootcause_integration.py` (RQ6), `test_posting_fence_integration.py` (RQ7);
  the two-org real-Postgres test is `adapter/storage/test_show_nits_integration.py`.
  E2E: `test_v2_review_posts_one_root_cause_hides_nits_and_keeps_the_fence_closed` in
  `tests/e2e/test_review_pipeline.py`, with a `running_stack_v2` fixture that recreates the
  Worker with the switch on.
- **Deviations from the plan text.** (1) No GitHub `suggestion` block is rendered: the plan
  says it comes from `message`, which is prose, and a "Commit suggestion" click would
  replace code with prose. Only the agent prompt block is rendered. (2) The E2E cannot show
  the 100-comment cap: the generation step caps LLM findings at 20 and static analysis at
  50, so the cap is proven in the RQ5 integration test (105 findings). (3) The E2E leaves
  `findings_show_nits` unset (nits hidden); the opt-in path is covered by the integration
  tests. (4) The admin GUI has no toggle for `findings_show_nits`; the config API accepts it.

## Phase 4 (optional, D6): deterministic doc and SQL analyzers

**Why last:** independent of the LLM path; not part of the original request.

- `adapter/analyzers/markdown_links.py` (new; no equivalent exists). For SQL lint,
  **first check whether Semgrep rules in the existing pinned ruleset mechanism**
  (`semgrep_ruleset.yaml` + `.sha256`; every rule there is `languages: [python]`
  today) can cover non-concurrent index build, dropped RLS policy and missing
  `IF EXISTS`; write `sql_migrations.py` only for what they cannot.
- Pure-Python analyzers run via `asyncio.to_thread`, with a hard **per-file and
  per-job total** input bound applied before any regex and linear-time parsing
  only (a thread cannot be cancelled, so the bound is the control). Link targets
  are resolved only against the diff or the tree listing (A5): no filesystem
  reads, no symlink following, `../` and absolute targets rejected.
- `review/config.py`: per-analyzer `StepEnablement` flags (**default False**) and
  matching `StepTimeouts` entries and `apps/worker/settings.py` fields, all with
  defaults. `findings.models.Source` gains the literals: **land the literals in
  one release and flip the flags in the next**, so no old worker ever sees an
  unknown literal. New-analyzer findings bypass verification
  (`source != "llm"`), so the default flips on only after fixture precision is
  checked.
- `_log_analyzer_outcomes` gains `skipped_oversize` and `timeout` outcomes.

**Tests:** unit: each rule (true positive and clean), pathological and oversize
inputs, path-traversal targets, an event-loop responsiveness assertion under
`to_thread`. Integration: through `StaticAnalysisStep` with real fixture files,
mirroring `adapter/analyzers/test_semgrep.py`, including the `skipped_oversize`
and `timeout` log outcomes. E2E: one PR touching a doc and a migration.

**Observability & rollback:** per-analyzer outcome logging; dismissal rate per
`source` from existing memory signals informs disabling. Rollback: per-analyzer
flags.

**Acceptance:** the corpus rows for broken anchors and unsafe migrations are
caught with zero LLM calls; false-positive fixtures stay clean.

**Phase 4 delivery notes (2026-11-24, facts verified in the tree):**

- **SQL checks are Semgrep rules, no `sql_migrations.py`.** Existing rules are
  `languages: [python]`, but Semgrep's `generic` language with `pattern-regex` and
  `paths.include: ["*.sql"]` runs the three checks (non-concurrent `CREATE INDEX`,
  `DROP POLICY`, `DROP ... ` without `IF EXISTS`) against the real binary. They live in a
  second pinned file, `adapter/analyzers/semgrep_sql_migrations_ruleset.yaml` (+ `.sha256`,
  checked by `test_sql_migrations.py`), so the analyzer has its own flag: adding them to
  the shared ruleset would have turned them on with `static_analysis_semgrep_enabled`.
  `SemgrepAnalyzer` takes an optional `ruleset_path`; SQL findings carry `source="semgrep"`,
  so only one new `Source` literal (`markdown_links`) was needed. Known limit: regex over
  raw text, so a match inside a SQL comment is a false positive.
- **Markdown link check is narrower than the plan text.** `StaticAnalyzer.run` has no tree
  listing (A5), so a link to a path absent from the diff cannot be told from a link to an
  unchanged file. The analyzer flags (a) a `#fragment` missing from the target markdown file
  when that file is in the changed content, and (b) a relative link to a path the diff
  `removed` and does not re-add. Absolute, `//`, `..`-escaping and scheme targets are never
  resolved. Widening this needs the tree listing passed into `run` (a decision, not done).
- **Bounds:** per file 512 KiB, per job 2 MiB of markdown, lines over 1024 characters skipped
  before any regex. Over the bound the analyzer raises `AnalyzerOversize` (defined in
  `review/static_analysis.py`), which the step logs at warning as `skipped_oversize`; the
  whole analyzer is skipped for that job (simplest safe choice). `timeout` was already
  logged at error by `_log_analyzer_outcomes`; Phase 4 adds a test pinning it.
- **Flags (default False):** `static_analysis_markdown_links_enabled`,
  `static_analysis_sql_migrations_enabled`, timeouts 10 s and 60 s, in `StepEnablement`,
  `StepTimeouts`, `WorkerSettings` and `_build_step_*`. `StaticAnalysisStep` takes
  `extra_analyzers`. `static_analysis_can_produce_findings` is unchanged: only the Gitleaks
  and Semgrep flags count, so a deployment with just the new analyzers on still needs one of them.
- **Not done:** the E2E (one PR touching a doc and a migration) needs the compose stack and
  the flags set; the stack could not run in this environment. Verification bypass is
  unchanged (`source != "llm"`), so findings from these analyzers are unverified;
  they are `low` (markdown) or `medium`/`high` (SQL) severity. The flag flip belongs to the next release.

**Release 1 gate:** run the eval report, update `docs/roadmap.md`, schedule v1
removal (A8).

---

# Release 2: the Classifier call type

*D1 signed off 2026-11-24.* Units map to spec-classifier's Phases/Units.

## Phase 5: Extract egress and credentials (unit 1)

**Why first in Release 2:** adapters may not import sibling adapters.

- Move to a neutral `adapter/egress/`: `egress.py`, `credentials.py`, the whole of
  `transport.py` (`EgressBlocked`, `ResponseTooLarge`, `EgressPolicy`,
  `ProviderCredential`, `GuardedTransport`, `build_http_client`), and the
  `retry-after` parser (A9).
- **This is not a pure move.** `transport.py:53` imports `MAX_RESPONSE_BYTES` and
  `ProviderOutcome` from `review.product_knowledge`. A neutral package must not
  depend on that module, so this phase includes a separately reviewable decoupling
  commit: `MAX_RESPONSE_BYTES` becomes a constructor parameter with the
  product-knowledge value passed by its caller, and code that needs
  `ProviderOutcome` stays in `adapter/product_knowledge`. The exact split is
  confirmed by reading the file at the start of the phase and recorded here.
- Confirm `test_adr_scope_sync.py` and the credential-map schema do not assume
  product-knowledge-only providers; if they do, add a key-namespace note rather
  than a schema change.

**Tests:** the moved `test_egress*.py`, `test_credentials.py`, `test_transport_*.py`
pass unchanged; `test_packaging_guards.py` and `test_adr_scope_sync.py` updated for
new paths; `tests/e2e/test_product_knowledge.py` unchanged and green.

**Observability & rollback:** none new; revert the PR.

**Acceptance:** `git diff --stat` shows moves, import rewrites and the named
decoupling commit only; full product-knowledge unit, integration and E2E suites
green.

**Phase 5 delivery notes (2026-11-24, facts verified in the tree):**

- Moved with `git mv` to `adapter/egress/`: `egress.py`, `credentials.py`,
  `transport.py`, `test_egress_pure.py`. All importers (adapter, apps, review tests,
  ADR-0005/0014 path references, `test_adr_scope_sync.py` MODULES) rewritten.
  `test_packaging_guards.py` needed no change (it names only
  `adapter/product_knowledge/testing`, which did not move).
- Decoupling: `transport.py` used `ProviderOutcome` only to record four refusal
  reasons and `MAX_RESPONSE_BYTES` only as a default. Now a neutral
  `EgressOutcome` StrEnum (same values: blocked_egress, credential_unavailable,
  malformed, oversized) and `DEFAULT_MAX_RESPONSE_BYTES`; `client.py` passes
  `MAX_RESPONSE_BYTES` explicitly and maps `ProviderOutcome(transport.outcome)`.
  `test_egress_decoupling.py` guards: no `review`/product-knowledge import in the
  package, caps equal, enum values subset of `ProviderOutcome`.
- Retry-after: the `transport.py` parser (`_record_error_status`/`_is_small_int`) moved
  unchanged; `adapter/github/http.py` untouched (A9).
- Credential map: `credentials.py` imports only `egress`/`transport`; schema has
  no product-knowledge-only assumption in code. No key-namespace note was needed
  in code; Phase 7 should state the `ref` namespace is shared.
- Deviations: `test_egress.py`, `test_credentials.py`, `test_transport_policy.py` and
  `test_transport_tls.py` stay in `adapter/product_knowledge/` because they use its
  fake provider, loopback or factory (an `adapter/egress` test may not import a
  sibling adapter). Assertions `transport.outcome is ProviderOutcome.X` became
  `== EgressOutcome.X` (mypy --strict rejects the cross-enum comparison).

## Phase 6: Protocol, models, conformance, fake, telemetry, doc amendments (unit 2)

- `llmgateway/classifier.py` (new): `Classifier` Protocol; request and response
  models with `NoulQuestion`/`NoulAnswer` only (D1b), `answers: dict[str,
  Answer]` where `Answer` is a type alias that later grows into a union (no
  one-member union class to rewrite). Failures reuse `LLMUnreachableError` /
  `LLMMalformedResponseError`; every call takes `timeout`. Pure threshold helper
  (A7).
- `llmgateway/classifier_conformance.py` and `llmgateway/testing/fake_classifier.py`
  (new), mirroring `adapter/product_knowledge/conformance.py` and
  `adapter/product_knowledge/testing/`; testing packages are removed from the
  runtime image and excluded from mutmut with a guard test like
  `test_packaging_guards.py`.
- `llmgateway/classifier_telemetry.py` (new, OTel API only per ADR-0011, with a
  log-and-continue suppressor; ADR-0012's scope list extended). A separate domain
  module because adapters cannot import `review/llm_telemetry.py`.
- **Doc amendments:** edit ADR-0014 and `spec-classifier.md` per D1(b) and D1(c).

**Tests:** unit for models, thresholds, fake, suppressor; conformance suite against
the fake; the packaging guard.

**Observability & rollback:** instruments added to the allow-list. No runtime
caller yet; revert.

**Acceptance:** conformance green on the fake; `mypy --strict` and `ruff` clean;
guard passes; ADR and spec amendments merged.

**Phase 6 delivery notes (2026-11-24, facts verified in the tree):**

- **Reuse checked.** `adapter/product_knowledge/conformance.py` is a runnable CLI
  against a live MCP URL (httpx2, one `_Run` session); nothing in it applies to an
  in-process Protocol, so only its idea carried over: `llmgateway/classifier_conformance.py`
  is a `CHECKS` dict of async checks (`answers_keyed_by_question_ids`,
  `answers_are_probabilities`, `repeat_call_is_independent`) that raise
  `ConformanceFailure`, run against any `Classifier`. Each check is proven able to fail
  by a seeded-faulty classifier in `llmgateway/test_classifier_conformance.py`.
  `review/llm_telemetry.py` and `review/_telemetry.py` are review-package-local, and
  `llmgateway/routing.py` has its own module-level counter but no suppressor, so
  `llmgateway/classifier_telemetry.py` carries a local copy of the ADR-0012
  `suppress_telemetry_errors` (ADR-0012's scope list now names it).
- **Models.** `llmgateway/classifier.py`: `NoulQuestion(instructions)`, `NoulAnswer(p in
  [0,1])`, aliases `Question`/`Answer` (D1b), `ClassifierUsage(input_tokens,
  output_tokens)`, `ClassificationRequest` (at least one question),
  `ClassificationResponse`, the `Classifier` Protocol, and `decide(answer)` with
  `YES_THRESHOLD=0.7`/`NO_THRESHOLD=0.3` constants (A7; edges inclusive). The wire's
  `type`/`noul` field names are the Phase 7 adapter's mapping, not the domain's.
  No `ClassifierError` base was added (ADR-0014).
- **Fake and guard.** `llmgateway/testing/fake_classifier.py` (answers by question id
  or a default, optional error, records calls). The existing
  `adapter/product_knowledge/test_packaging_guards.py` is parametrised over both testing
  directories rather than copied; the `Dockerfile` runtime stage removes
  `./llmgateway/testing` after `COPY llmgateway`, and `pyproject.toml` `do_not_mutate`
  lists `llmgateway/testing/*`. `classifier_conformance.py` ships (no infra imports).
- **Telemetry.** Instruments and closed label sets are in the `spec-observability.md`
  allow-list: `calls_total`, `call_duration_ms`, `tokens_total`, `verdict_total`,
  `shadow_agreement_total`, `verification.llm_fallback_total`. The `reason` set
  (`uncertain`, `error`, `high_severity_no`, `redaction_failure`, `credential_unresolved`)
  and the `backend` set were chosen here; Phase 9 may extend them with the allow-list.
- **Doc amendments.** ADR-0014 (decision 1: noul only, aliases; decision 2: constants;
  decision 5: high/critical "no" goes to the LLM judge; decision 6) and
  `spec-classifier.md` (scope, US-C4 split by severity, US-C5) now state D1b and D1c.
- **Not done.** No integration test: Phase 6 has no runtime caller and no story is
  satisfied until Phase 7 runs the same `CHECKS` against the real adapter
  (`adapter/classifier/test_conformance.py`, US-C7).

## Phase 7: TypeSafe adapter (unit 3)

- `adapter/classifier/typesafe.py` (new): `httpx.AsyncClient`,
  `POST {endpoint}/v1/systemone`, bearer auth, model per A6, connection policy
  from `adapter/egress/`. 401/403 -> closed `unauthorized` (logged at `error`);
  422 -> malformed; a wrong answer type fails the whole response; 429 and 529
  retried at most twice with the shared `retry-after` parser, capped at the lesser
  of 10 s and the remaining call timeout; 5xx and timeouts not retried. A small
  **circuit breaker** (open after N consecutive 429/529 across the worker) stops
  retry amplification. Token never in errors or logs.
- Fixtures from `docs/research/jev-live-wire-examples.json` under
  `adapter/classifier/testdata/` (headers scrubbed; covered by the Phase 1
  Gitleaks step); live-mode test behind a flag, never default CI.

**Tests:** integration: conformance suite against the real adapter; respx wire
tests: 429 twice then success, 429 three times (closed failure), `retry-after`
above the cap and above the remaining timeout, 529, 5xx not retried, malformed
body, wrong answer type, breaker opens and closes; token-leak scan of logs and
exception text; metrics emitted from a real adapter call over a cassette (US-C8).
Live nightly: one 5-question call asserts shape and a latency bound.

**Observability & rollback:** `review.classifier.calls_total{backend,outcome}`,
a call-duration histogram, and token usage via the Phase 6 recorder (allow-list
updated). Nothing calls it yet; revert.

**Acceptance:** conformance green on the real adapter over cassettes; a manual
live run (model, latency, usage) attached to the PR.

**Phase 7 delivery notes (2026-11-24, facts verified in the tree):**

- **Reuse checked.** `GuardedTransport`/`build_http_client`/`ProviderCredential`
  (Phase 5) supply the connection policy, bearer attachment and the shared
  `retry-after` value (`transport.retry_after_seconds`); no parser was rewritten.
  Telemetry uses the Phase 6 `record_call`/`record_token_usage`. The
  `ref` namespace of the credential map is shared with product knowledge.
- **Deviation: httpx2, not httpx.** The egress guard is an `httpx2` transport, so
  the adapter uses `build_http_client`; respx patches `httpx` only, so wire tests
  use `httpx2.MockTransport` through the guard's `inner` seam (the guard still runs).
- **Failures.** No new base class: `UnauthorizedError` and `RateLimitedError`
  (adapter-local, subclass `LLMUnreachableError`) carry the closed `outcome`;
  `outcome_of(exc)` maps any failure to the label.
- **Breaker.** `CircuitBreaker` (5 consecutive 429/529, 30 s cooldown, one probe
  per cooldown); `DEFAULT_BREAKER` is process-wide, Phase 8's factory shares it.
  Retry waits use `retry-after`, else 0.5 s then 1 s; `retry-after` at or past the
  remaining timeout fails at once without sleeping.
- **Fixtures.** `adapter/classifier/testdata/jev-live-wire-examples.json` (copy of
  the research file; `scripts/scan-fixtures.sh` discovers `testdata` dirs). The
  noul-only success body is derived from it in the test.
- **Live test** `test_live_endpoint_conforms` runs only with
  `TYPESAFE_LIVE_KEY`; it runs the conformance checks, with no latency assertion yet
  (no live key was available). The manual live run attached to the PR is still open.

## Phase 8: Settings, Admin API, Admin GUI (unit 4)

*Ships with Phases 9 and 10.*

- `tenancy/settings.py`: `ClassifierProviderSetting` (`enabled`, `backend:
  Literal["typesafe"]`, `endpoint_url`, `credential_ref`, `model`, `mode:
  Literal["shadow","active"]`) under `classifier_provider`; org scope only, added
  to `TEAM_SCOPE_UNSUPPORTED_KEYS`. The model does **shape checks only** (https,
  no userinfo, query or fragment, strict `model` pattern).
- `apps/admin_api/config_routes.py`: generalise the single `ProviderWriteValidator`
  hook (`config_routes.py:65`) to a per-key map (its own step, own tests) and put
  the host deny-list, unknown-ref and ref/host-mismatch checks there, not in
  `tenancy`. Audit view logs host, credential reference, enabled and mode only.
- Test-connection route (`apps/admin_api/classifier_routes.py`, new), one fixed
  synthetic `noul` question, closed-set failures. The product-knowledge route's
  private `_SlidingWindow` is itself a local copy of
  `apps/webhook_api/rate_limiter.InMemoryRateLimiter`; extract **one** shared
  admin limiter (a distinct refactor step touching `product_knowledge_routes.py`,
  naming that lineage), keyed by org and admin and per pod.
- Admin GUI panel beside `ProductKnowledgePanel`; the egress disclosure must be
  acknowledged before `enabled` saves, **enforced by the API**, not only the UI;
  wording per D4 and D5.

**Tests:** unit: setting shape matrix; validator matrix with concrete deny-list
cases (private, loopback, link-local and metadata IPs, DNS rebinding to a private
IP, redirect refusal). Integration (real Postgres via `testcontainers`): write,
audit row has no token, team-scope write rejected, `enabled=true` refused without
acknowledgement at the API, ref/host mismatch, probe outcomes against a stubbed
host, shared limiter across both routes, two-org isolation. Frontend tests for the
disclosure gate. E2E: `tests/e2e/test_admin_ui.py` extended.

**Observability & rollback:** probe outcome counter and
`classifier_config_write_total` (closed-set, allow-list updated). Rollback: delete
the org's setting (absent = today's behaviour).

**Acceptance:** admin can save, test and disable; token never persisted, logged or
echoed; invalid writes give specific errors.

**Phase 8 delivery notes (2026-11-24, facts verified in the tree):**

- **Reuse checked.** The write hook was a single `ProviderWriteValidator` argument
  (`config_routes.py`); it is now `write_validators: Mapping[key, hook]` plus a per-key
  audit view, and the product-knowledge hook is registered under its key unchanged. The
  credential map, `binding_problem` (ref exists, host and port bound), `canonicalize_host`
  and `classify_address` come from `adapter/egress`; the probe runs the Phase 7
  `TypeSafeClassifier` over the same `GuardedTransport` (public networks only, so DNS
  rebinding and redirects are refused by the guard, not re-implemented). `_SlidingWindow`
  was confirmed to be a private copy of `apps/webhook_api/rate_limiter.InMemoryRateLimiter`'s
  core; it moved to `apps/admin_api/probe_rate_limit.py` (lineage named in its docstring)
  because the shared limiter needs separate has-room and record steps, which
  `InMemoryRateLimiter` does not offer. One `ProbeRateLimiter` instance is built in
  `create_app` and given to both routers.
- **Deviation: `disclosure_acknowledged` field.** The plan's field list has no place to
  record the acknowledgement the API must enforce, so the setting carries
  `disclosure_acknowledged: bool = False`; the validator refuses `enabled: true` without it
  (`disclosure_not_acknowledged`). Disabling never needs it.
- **Deviation: `credential_ref` is required** (a bearer token is always sent) and is not
  nullable as in product knowledge. The model field is `model`, shape-checked only.
- **Deviation: probe takes the target in the body** (`org_id`, `endpoint_url`,
  `credential_ref`, `model`), like the product-knowledge probe, instead of reading the
  saved row; the panel only offers it for saved values. It re-runs the write-time target
  check, so it cannot deliver the token to an unbound or denied host (422, zero bytes).
  The response `model` echoes the configured model, never provider text.
- **Limiter keys.** Session, user, org (from the probe body) and pod; the org window is
  only charged when an org id is given (the product-knowledge probe has none).
- **Metrics.** `review.classifier.probe_total{outcome}` is new; writes of the new key count
  in the existing `review.config.write_total` (its `key` label is registry-driven).
  `spec-observability.md` updated. `classifier_config_write_total` was not created.
- **Disclosure** (`classifierCopy.ts`) follows D4 and D5 (a); `operations.md` states the
  single shared key. The runbook proper is Phase 10.
- **Not run here.** The embedding model is missing, so `apps/admin_api/test_main.py`'s
  embedding test and the memory approve tests fail at app start; the new integration
  tests were run with the startup check stubbed by an out-of-tree pytest plugin. The
  Compose E2E (`tests/e2e/test_admin_ui.py`, new classifier test) was added but not run.
  `eslint` has no config in the frontend package, so only `tsc` and vitest were run.

## Phase 9: `FindingJudge`, cascade, shadow and active modes (unit 5)

- `review/finding_judge.py` (new): consumer-defined `FindingJudge` Protocol.
  Existing batched logic moves behind it as `LLMFindingJudge` with **no behaviour
  change** (existing verification tests pass untouched); `VerificationStep`
  depends on the Protocol.
- `ClassifierFindingJudge`: one request per finding, one `noul` per criterion from
  the shared tuple (A3). `state` = finding text plus its hunk, **redacted first,
  then truncated** to the byte ceiling (truncating first could split a secret and
  defeat the scanner); redaction is bounded by a **worker-level redaction
  semaphore** injected from the composition root (Gitleaks spawns a subprocess per
  call), and any redaction failure sends that finding to the LLM judge. A
  worker-level classifier semaphore plus the circuit breaker (Phase 7) bound
  quota use; per D5 one org can still crowd others.
- Cascade: pass only if every criterion is yes; any uncertain criterion or error
  sends that finding to the LLM judge. In `active` mode a classifier "no" on a
  high or critical finding goes to the LLM judge (D1c); medium and low "no"
  verdicts can still be driven by author text and are accepted risk, covered by the
  adversarial corpus. Mode and credential are resolved **per job** through a
  consumer-defined resolver (pattern `provider_resolver.py`), so a mode change
  takes effect on the next job.
- Telemetry per spec through the Phase 6 recorder; no content in metrics or logs;
  an in-flight gauge for the semaphores.

**Tests:** unit: cascade table (all yes, any no, uncertain, error, high-severity
no), mode handling, redact-then-truncate ordering (a secret straddling the cut),
redaction-failure fallback, no-content telemetry, hunk ceiling, semaphore bounds.
Integration (the **real TypeSafe adapter over respx cassettes** through
`ClassifierFindingJudge` and `VerificationStep`): US-C4, C5, C6 including 429,
timeout, malformed, wrong-type, breaker open, "credential ref no longer resolves at
job time", and an active-mode high/critical "no" asserting zero drops; two-org
test; adversarial shadow corpus (author text such as "this code is correct")
checking verdict stability; fully-decided jobs make zero LLM verification calls;
with the setting absent, output is byte-identical on the Phase 1 corpus.

**Observability & rollback:** `verdict_total`, `shadow_agreement_total`,
`verification.llm_fallback_total{reason}` (allow-list updated). Rollback: set the
org's mode to `shadow` or remove the setting, effective next job.

**Acceptance:** US-C4, C5, C6, C8 integration tests pass; adversarial corpus shows
no verdict flips attributable to injected text.

**Phase 9 delivery notes (2026-11-24, facts verified in the tree):**

- **Reuse checked.** The batched LLM logic (prompt, per-item-tolerant parse via
  `parse_llm_json_envelope`, fail-closed index correlation, rubric tuple) moved to
  `review/finding_judge.py` as `LLMFindingJudge` unchanged; `review/verification.py`
  re-exports the moved names the existing tests import, and the judge logs on the
  `review.verification` logger so every existing log assertion holds. The existing
  verification tests pass untouched (those failing here are environmental, below).
  Redaction reuses `findings.redact.TextRedactor`; truncation reuses
  `review.diff_prompt.truncate_diff_text`; thresholds and metrics are the Phase 6
  `decide` and `classifier_telemetry` recorders. The only hunk parsers in the tree are
  private (`context_symbols._parse_hunk_headers` returns ranges, not text;
  `spec_review._HUNK_RE` is private), so `_hunk_for` is a small new splitter.
- **Shape.** `FindingJudge.judge(findings, JudgeInput) -> list[Judgement]`;
  `Judgement.passed is None` means "not decided here". Implementations: `LLMFindingJudge`,
  `ClassifierFindingJudge`, `CascadeFindingJudge` (active), `ShadowFindingJudge`
  (shadow). `VerificationStep` keeps its constructor and gains an optional
  `classifier: ClassifierVerification` (resolver, redactor, the two worker-level
  semaphores, state byte ceiling, timeout); `None` means no classifier code runs.
- **Cascade rule** (`_apply_cascade_rule`): any "no" on high or critical defers
  (`high_severity_no`, D1c); any other "no" rejects; otherwise any uncertain defers;
  otherwise pass. A "no" beside an uncertain criterion on medium or lower rejects, per
  US-C4's first scenarios.
- **Per-job resolver.** `ClassifierSelectionResolver.resolve(org_id)` returns `None`
  (no setting), or `ClassifierSelection(mode, model, classifier)` where `classifier is
  None` means the credential ref no longer resolves (warning, per-finding
  `credential_unresolved` fallback). A resolver exception is treated as no setting.
  **The concrete resolver (setting reader, credential map, `TypeSafeClassifier`
  construction) and the semaphores are built in Phase 10 at the composition root**;
  Phase 9 ships only the consumer-defined Protocol and test fakes.
- **Telemetry.** New `review.classifier.in_flight{resource}` UpDownCounter (allow-list
  updated); `verdict_total`, `shadow_agreement_total` and `llm_fallback_total` are
  recorded. No state, finding text or error text is in any label or log line.
- **Deviation: respx.** The plan says "respx cassettes", but the adapter's wire is
  `httpx2` behind the egress guard (Phase 7), so the integration tests replay
  `httpx2.MockTransport` bodies shaped like `jev-live-wire-examples.json` through the
  real `TypeSafeClassifier`. The LLM side of the integration tests is a fake
  `LLMClient`, not a recorded cassette.
- **Adversarial corpus** (`review/testdata/classifier-adversarial-corpus.json`, 8
  synthetic strings). With no live key, it can only prove the structure: a worst-case
  classifier steered to "no" by the planted text still never drops a high or critical
  finding, the medium/low drop boundary is asserted as the accepted risk, and the
  questions are identical with and without the planted text. **Real-model verdict
  stability is not measured**; that needs a live run or shadow data.
- **Not run here.** The embedding model is missing, so `review/test_verification_step.py`
  was collected with an out-of-tree plugin stubbing the startup check; its three real
  admin-approve tests still fail (500, no model). Gitleaks is absent, so
  `review/eval/test_scan_fixtures.py`, `test_review_job.py` (two) and
  `test_static_analysis_step.py` fail for that reason. In a combined `pytest review
  llmgateway adapter/classifier` run 36 tests fail from a logger-disabling ordering
  effect (Alembic `env.py`) that is identical with the Phase 9 files excluded.
- **Not done.** "Byte-identical on the Phase 1 corpus with the setting absent" is
  proven on the step (`test_without_a_setting_nothing_is_sent_...`: same output and
  same LLM request as a step with no classifier wiring), not by re-running the Phase 1
  cassette corpus.

## Phase 10: Worker wiring, E2E, shadow rollout, runbook (unit 6)

- `apps/worker/main.py`: construct the backend, resolver and semaphores at the
  composition root; no module-level singletons. `apps/worker/settings.py` gains
  only pinned defaults.
- `tests/e2e/test_classifier.py` (new): Compose stack, stubbed classifier host,
  synthetic webhook; setting absent, shadow, active, provider down, 401.
- **Operations** (`docs/reference/operations.md`): runbook for credential
  rotation, a TypeSafe outage, a 401/403 storm and flipping
  `review_quality_v2_enabled`; documented queries for fallback rate and
  per-criterion agreement; alert rule at the D9 fallback threshold.
- Rollout: `shadow` for one org for at least one review cycle (volume chosen by
  the owner), review agreement on the corpus and real traffic, then the owner
  decides on `active` explicitly.

**Tests:** E2E as above; unit for wiring (setting absent yields no classifier).

**Observability & rollback:** counters from Phases 7 and 9; rollback by removing the
setting.

**Acceptance:** E2E scenarios pass, a recorded shadow report exists, runbook
merged, and the owner records the `active` decision.

**Phase 10 delivery notes (2026-11-24, facts verified in the tree):**

- **Reuse checked.** `ClassifierVerification`, the `ClassifierSelectionResolver` Protocol and
  `select_judge` (Phase 9) are used unchanged; `VerificationStep` already took
  `classifier=`. The setting reader is the existing `PostgresSettingRepository`
  (`list_rows_for_key`, the `SettingRowReader` Protocol of `provider_resolver.py`), the
  scope rule is `tenancy.settings.resolve_setting`, the credential check is
  `CredentialMap.binding_problem`, and the redactor is the one `_build_text_redactor` already
  builds for the other steps (not a second Gitleaks instance). The only new production module is
  `adapter/classifier/resolver.py` (`SettingClassifierResolver`, org row only; reader errors
  and the 2 s timeout propagate because `select_judge` already fails open).
- **Wiring.** `_build_classifier_verification` in `apps/worker/main.py` builds the resolver and
  the two semaphores per process and `_run` passes the result to `_build_review_registry`; no
  module-level state. It is always wired: with no setting the resolver returns `None` per job.
  `WorkerSettings` gains pinned defaults (timeout 10 s, 8 classifier slots, 4 redaction slots,
  8000-byte state ceiling), all `>= 1`.
- **Deviation: `classifier_allow_private_networks` (default false).** The plan says settings gain
  only pinned defaults, but the Compose E2E stub is a host process on the Docker gateway
  address, which the egress guard refuses without it. Production keeps public hosts only, like
  the Admin probe. It is passed through `docker-compose.yml` and listed in `.env.example`.
- **Deviation: TLS trust in the E2E.** The setting requires `https` and the guard trusts only the
  system store (`SSL_CERT_*` is ignored), so `tests/e2e/test_classifier.py` serves a throwaway
  self-signed cert (`self_signed_cert`, reused) and a Compose override bind-mounts it over the
  Worker's `/etc/ssl/certs/ca-certificates.crt`. This mount path is not verified in a real image
  (see "Not run here").
- **E2E reuse.** The stub GitHub and stub LLM handlers, ports and helpers are imported from
  `tests/e2e/test_product_knowledge.py` rather than copied. Scenarios: absent (3 LLM calls, zero
  classifier requests), shadow (classifier called with the bearer token, 3 LLM calls), active
  (all-yes answers, 2 LLM calls: no LLM verification), 401 and outage (finding still posted,
  LLM judge verifies).
- **Operations.** `docs/reference/operations.md` has the Worker runbook (rotation, outage, 401/403
  storm, v2 flip), the fallback-rate and per-criterion agreement queries, the D9 alert table
  (20% fallback over one hour; the 30% missing-scenario rule stays with the v2 section) and the
  shadow rollout procedure. The fallback-rate denominator is a derived proxy (`valid` verdicts plus
  the fallbacks that never got a verdict), because no per-finding counter exists.
- **Tests.** Unit: `adapter/classifier/test_resolver.py` (absent, enabled, disabled, invalid,
  platform and foreign-org rows, credential no longer resolving, reader error and timeout) and
  `apps/worker/test_classifier_wiring.py` (setting absent yields no classifier, semaphores
  per build and sized from the defaults, zero slots refused).
- **Not run here.** The Compose E2E (needs Docker image builds and the embedding model), and
  `apps/worker/test_main.py` (35 fail at the missing embedding model or an unrelated
  `_NoOpSemgrepAnalyzer` stub signature in the existing test file; none exercise this change).
- **Open (owner).** The shadow report does not exist yet (no org has run in shadow) and the
  owner has **not** made the `active` decision; this phase records neither. The Phase 10
  acceptance ("a recorded shadow report exists ... the owner records the `active` decision") is
  therefore not met, by design.

---

## Security review

| Surface | Trigger / input | Worst case | Control (phase) |
| --- | --- | --- | --- |
| Egress of diff-derived text to a third party | Org admin enables; every PR after | Customer code or secrets sent to an unintended host | Opt-in, off by default, API-enforced acknowledgement, host-bound credential, deny-list, no redirects, address pinning (5, 8); redact then truncate, bounded scanner, fallback on failure (9). Disclosure says detectable secrets are redacted, not that none leave (D4) |
| New free text posted to PRs (`scenario`; suggestion rendered from `message`) | LLM output echoing diff | Secret or PII posted publicly | `redact_findings` covers `message` and `scenario` (2); fence-safe rendering (3) |
| Agent-ready prompt block | PR-author-influenced text, including file paths, into a developer's coding agent | Indirect prompt injection into an agent with write access | Separate fence-safe renderer, dynamic fence, validated paths, no raw author text, no diff by default, untrusted-data statement, hostile-input test (3) |
| Credential | Operator secret | Token in DB, logs, errors, audit, cassettes | Never in Postgres; audit logs ref and host only; leak tests (7, 8); header scrubbing and Gitleaks over every cassette directory (1, 7). Shared-key stance per D5 |
| Admin API setting write and test route | Authenticated admin | SSRF through `endpoint_url`; probe abuse against a paid key | Validators in `admin_api`; strict shape checks; one shared probe limiter keyed by org and admin (8) |
| Prompt injection via diff text in `state` | PR author | Steering a verdict to suppress a true finding | Typed output; prose framing not relied on; adversarial corpus; high/critical "no" always re-judged by the LLM; medium/low accepted risk (9) |
| Resource exhaustion | Many or large findings, many concurrent jobs | Fan-out cost, latency, quota exhaustion, retry amplification | Byte ceiling, worker-level semaphores (classifier and redaction), circuit breaker, bounded retries, timeouts (7, 9) |
| New analyzers (D6) | PR author files and link targets | ReDoS; path traversal | Input bounds, `to_thread`, linear-time parsing, no filesystem reads or symlinks (4) |
| Eval corpus and cassettes | Committed excerpts | Secrets or private code in repo | Gitleaks over corpus and all cassette dirs, header scrubbing, D7 (1) |
| Cross-tenant | Any org | One org's setting or cache serving another | Per-`org_id` resolution, per-job caches only, two-org tests (8, 9); shared quota per D5 |

## Compatibility & rollout

- **Contracts changed:** `Finding` gains optional fields (additive).
  `_VerificationVerdictItem` gains a defaulted field so mixed-version verdicts
  parse (A3). Posted layout changes (Phase 3), gated by A8. `findings.models.Source`
  gains literals only if D6 is approved (literals land one release before flags
  flip). `adapter/product_knowledge` import paths change (internal only). Prompt
  wording changes memory fingerprints (D8).
- **Migrations:** none; settings use the existing table and registry.
- **Order:** Release 1 first, default-safe behind A8. Release 2 phases 5-7 add no
  behaviour; phases 8-10 ship together and change nothing for orgs without the
  setting.
- **Failure containment:** classifier outage, slowness, malformed output, redaction
  failure or an open breaker falls back per finding to the existing judge; a bad
  prompt or layout reverts with one worker setting and a restart; removing the
  classifier setting restores today's behaviour with no deploy.

## Traceability

Release 1 G/W/T is written in Phase 1's spec; one-line form here. Release 2 stories
are in `spec-classifier.md`. Every row names an integration test (real adapter over
a cassette or real Postgres), per `testing.md`.

| Story | Given / When / Then (summary) | Phase | Integration test (planned path) |
| --- | --- | --- | --- |
| RQ1 | A medium+ LLM finding has no scenario / review runs under v2 / it is dropped and counted | 2 | `review/test_review_quality_prompt.py` (real `LiteLLMClient`, respx cassette) |
| RQ2 | A lint-class issue / reviewed / never above `low`; a reachable defect is `high` | 2 | same file |
| RQ3 | Smell baseline off / style-only issue in diff / no style finding posted | 2 | same file |
| RQ4 | An org has `findings_show_nits` off / nits exist / one collapsed summary or none | 3 | `adapter/github/test_posting_tiers_integration.py` (cassette), plus E2E |
| RQ5 | More findings than the cap / posted / at most the cap inline, rest summarised | 3 | same file, plus `tests/e2e/test_review_pipeline.py` |
| RQ6 | Same defect in two files / merged / one finding, highest severity, locations noted | 3 | `adapter/github/test_posting_rootcause_integration.py` (output step into posting cassette) |
| RQ7 | Hostile `scenario`/path / rendered / fence not closable, secrets redacted | 2, 3 | `adapter/github/test_posting_fence_integration.py` |
| RQ8 (D6) | Broken anchor or unsafe migration / reviewed / finding with no LLM call | 4 | `adapter/analyzers/test_markdown_links.py`, `test_sql_migrations.py` via `StaticAnalysisStep` |
| US-C1 | credential map / startup, bad binding / parsed, rejected, no token in logs | 5, 8 | `adapter/egress/test_credentials_integration.py` |
| US-C2 | setting absent or invalid / review, write / no-op or specific error | 8 | `apps/admin_api/test_classifier_settings_routes.py`, `review/test_classifier_optin_e2e.py` |
| US-C3 | saved setting / test pressed / ok or closed failure, key never echoed | 8 | `apps/admin_api/test_classifier_probe_routes.py` |
| US-C4 | active mode / findings verified / confident verdicts decide, uncertain go to LLM | 9 | `review/test_verification_classifier_cascade.py` (real adapter, respx) |
| US-C5 | classifier fault / verification / per-finding fallback, job succeeds | 9 | `review/test_verification_classifier_faults.py` |
| US-C6 | shadow mode / verification / LLM decides, agreement recorded | 9 | `review/test_verification_classifier_shadow.py` |
| US-C7 | fake and TypeSafe / conformance suite / both pass | 6, 7 | `adapter/classifier/test_conformance.py` |
| US-C8 | any classifier call / telemetry / closed-set labels, no content | 7, 9 | `adapter/classifier/test_telemetry_integration.py` (real adapter, cassette) |
| E2E | Compose stack / synthetic webhook / absent, shadow, active, down, 401 | 10 | `tests/e2e/test_classifier.py` |
