# Code-Smell Baseline & Spec-Compliance Review PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-11-24

Adds two review capabilities, adopted from the two-axis model of the
`code-review` skill in `mattpocock/skills`
(`skills/engineering/code-review/SKILL.md`): (1) a fixed baseline of Fowler
code smells the review checks every diff against, and (2) a spec-compliance
check that runs **only when a spec for the PR can be found**, and reports where
the diff misses, exceeds, or misimplements it. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification tag
`RQPTVE`); requirements are stated as user stories with `Given/When/Then`
acceptance criteria per `testing.md`'s mandatory rule.

**Extends, not replaces, shipped modules.** `review_generation`
(`review/review_generation.py`) gains a prompt section; `verification`
(`review/verification.py`) is reused unchanged in mechanism and now also
judges spec findings; `findings/models.py`'s `Finding` gains one optional
field; one new macro step is added to the existing `StepRegistry`
(`spec-pipeline-step-framework.md`, zero framework changes).

## Problem & Goal

**End user: the PR author.** Today a review judges a diff against general LLM
knowledge plus static analysis. It has two blind spots that cost the author
real rework *after* merge-readiness looks green:

1. **Structural quality.** Duplicated logic, feature envy, speculative
   abstraction and similar smells are not looked for deliberately. A reviewer
   (human or LLM) notices them only by chance.
2. **Intent.** Code can follow every convention and still build the wrong
   thing (or build more than asked). Nothing in the pipeline compares the diff
   to the document that says what was supposed to be built, even when that
   document sits in the repo. `intent_summary` summarises what the diff
   *appears* to do, which cannot reveal a missing requirement.

**Secondary end user: the platform operator**, whose BYO-key LLM spend grows
by at most one completion call per review, and zero when no spec is found
(see NFRs), and who can switch each capability off independently.

**Goal (capability-based, matching every prior phase; no fabricated precision
number):** (a) a review's prompt carries the 12-smell baseline and any smell
the model reports is posted only as a labelled judgement call that has passed
verification; (b) when a spec is found for the PR, every posted spec finding
quotes the spec line it relies on and has passed verification; (c) when no spec
is found, the review is byte-for-byte what it is today and says so in the logs.

## Scope

### In scope

- **Smell baseline in `review_generation` (decided).** The 12 smells (Mysterious
  Name, Duplicated Code, Feature Envy, Data Clumps, Primitive Obsession,
  Repeated Switches, Shotgun Surgery, Divergent Change, Speculative
  Generality, Message Chains, Middle Man, Refused Bequest), each as
  "what it is → how to fix", added as one labelled prompt section. Fixed in
  code; not customer-editable.
- **`Finding.category` (new optional field, decided).**
  `Literal["smell", "spec"] | None = None`, default `None`, set only by the
  producers in this spec. Needed so smell and spec findings can be labelled in
  the posted comment and counted in telemetry; `Finding` has no such field
  today.
- **Smell findings are judgement calls (decided).** A smell finding's posted
  message is prefixed with a fixed label (e.g. "Possible Feature Envy:") and
  has `Severity` of `low` or `info` only (**assumption pending validation**:
  `low` is the ceiling), so a smell never outranks a real defect in
  `findings/merge.py`'s severity ordering. Never presented as a hard violation.
- **New macro step `spec_review` (decided), placed after `review_generation`
  and before `verification`.** One step, not a separate resolution step plus a
  compliance step: the pipeline's single-dependency-input design
  (`review/step.py`) means a standalone resolution step would force every
  intermediate step's Output model to carry spec text to a later consumer. The
  step internally (1) resolves the spec, (2) if found, makes one LLM Gateway
  completion call, structurally separate from the review-generation call,
  (3) appends its findings (`source="llm"`, `category="spec"`) to
  `review_generation`'s findings and forwards everything else unchanged.
  Resolution *logic* is pure and separately unit-tested; the I/O reuses the
  existing `DiffFetcher` and `indexing.tree.RepoTreeFetcher` Protocols (see
  Units; no new Protocol).
- **Spec resolution order (decided: "diff first, then repo").**
  1. A spec-like file **changed in this PR's diff**.
  2. Otherwise, a spec-like file in the repo tree **at `head_sha`**.
  3. Otherwise: no spec. The step is a no-op.
  "Spec-like" = a `.md` file under `docs/`, `specs/` or `.scratch/` whose path
  contains `spec` or `prd`. The single matching candidate is chosen by
  comparing tokens of `JobIdentity.pr_head_ref` to the candidate's path tokens
  (**assumption pending validation**: exact tokenisation and threshold are
  left to implementation; the contract is *deterministic, pure, one unambiguous best match
  or none*). Zero candidates, a tie for best, or `pr_head_ref is None` all
  resolve to **no spec**. The step never guesses between candidates.
- **Spec findings (decided).** The compliance call reports: (a) requirements
  the spec asks for that are missing or partial; (b) behaviour in the diff the
  spec did not ask for (scope creep); (c) requirements that look implemented
  but where the implementation looks wrong. Every finding quotes the spec line
  it relies on.
- **Spec findings go through the existing `verification` step (decided).** They
  are `source="llm"`, so the existing batched four-criterion rubric already
  applies; a rejected spec finding is dropped like any other. The verifier must
  be given the spec text for `category="spec"` findings (it cannot judge
  "missing requirement" without it); how that text reaches `verification` is a
  mechanism left to implementation, not decided here (see risk in Units).
- **Graceful degradation (decided).** Any failure inside `spec_review`
  (spec fetch error, LLM timeout, unparseable response) is caught inside the
  step: the step returns `review_generation`'s findings unchanged, logs at
  `warning`, and the review proceeds. An optional check must never take down
  the review (cf. ADR-0003's "optional augmentation, never a dependency" and
  ADR-0012's log-and-continue stance).
- **Independent kill switches (decided):** `spec_review_enabled` and
  `smell_baseline_enabled` on `StepEnablement` (bootstrap env config, the
  existing mechanism), plus a `spec_review` timeout in `StepTimeouts`.
  Disabled = behaviour identical to today.
- **Untrusted-content handling (decided).** Spec text is repository content, so
  it is wrapped in a delimited, labelled prompt section with the same
  prompt-injection framing `intent_summary`'s section uses, is size-capped, and
  every produced finding passes through the existing `TextRedactor`.

### Out of scope (deferred; recorded so nothing is "forgotten")

- **Tracker/ticket lookup** (Jira, GitHub issues) as a spec source.
- **PR-metadata issue keys** (`ingestion/pr_metadata.py`) and the PR body as
  spec sources. (`pr_head_ref`, an existing `JobIdentity` field, is read, but
  `pr_metadata.py` itself is untouched.)
- **knowhow / product-knowledge provider** as a spec source.
- **Discovering repo-documented coding standards** (Pocock's "Standards
  sources" step) and letting them override the smell baseline. The baseline is
  fixed and unconditional in this spec.
- **Per-org/per-repo/Admin-GUI configuration** of either capability (bootstrap
  env flags only; Admin GUI is a later extension).
- **A customer-editable smell list or rubric.**
- **A separate "Standards / Spec" section in the posted review**; findings are
  posted through the existing output path, distinguished by `category` label.
- **Multiple specs per PR**; exactly zero or one.
- **Auto-fix of smells** (Phase 11) and **PR-level walkthrough display** of the
  spec verdict (Phase 12).

### Priority

- **Must:** US-S1, US-S3, US-S4, US-S5, US-S6, US-S7, US-S8, US-S10, US-S12 (the spec
  check is only valuable if it finds the spec, stays quiet without one, and
  survives verification).
- **Should:** US-S2, US-S9 (kill switches), US-S11 (untrusted-content
  handling; Must if the spec step ships enabled by default).
- **Could / first to cut:** smell severity cap fine-tuning, extra telemetry
  attributes beyond the outcome counter. Cut what the PR author cannot see
  first.

## Requirements

User stories (acceptance criteria are `Given/When/Then`). Each maps to at least
one integration test per `testing.md`; pure logic additionally gets unit
tests.

**US-S1: Smell baseline reviewed on every diff.**
As a PR author, I want the review to look for the 12 baseline smells, so that
structural problems are raised before merge.
- *Given* `smell_baseline_enabled=True` and a diff introducing the same logic
  shape in two files, *when* `review_generation` runs, *then* its prompt
  contains the smell-baseline section and a recorded response that reports
  "Duplicated Code" yields a `Finding` with `category="smell"`.

**US-S2: Smell baseline can be switched off.**
As a platform operator, I want to disable smell checking, so that a noisy
deployment can opt out without a release.
- *Given* `smell_baseline_enabled=False`, *when* `review_generation` runs
  against a recorded cassette, *then* the prompt sent to the LLM Gateway is
  byte-identical to today's prompt and the posted output is unchanged.

**US-S3: Smell findings are labelled judgement calls and verified.**
As a PR author, I want smell findings clearly marked as suggestions and
filtered for quality, so that I am not told a subjective smell is a defect.
- *Given* a `category="smell"` finding, *when* it is posted, *then* its message
  carries the fixed "Possible <Smell>:" label and its severity is no higher
  than the capped level; *and given* `verification` rejects it, *then* it is
  not posted.

**US-S4: Spec found in the PR's diff.**
As a PR author, I want a spec I added or changed in this PR used as the spec,
so that the check reflects the spec I am actually shipping.
- *Given* a PR on branch `feat/spec-smell-review` whose diff adds
  `docs/specs/spec-smell-and-spec-review.md`, *when* `spec_review` runs, *then*
  that file is chosen as the spec and the compliance call runs once.

**US-S5: Spec found in the repo at `head_sha` when not in the diff.**
As a PR author, I want a spec merged in an earlier PR found automatically, so
that I do not have to re-add it to every implementation PR.
- *Given* a branch name matching exactly one spec-like file in the repo tree at
  `head_sha` and no spec-like file in the diff, *when* `spec_review` runs,
  *then* that file is fetched and used.
- *Given* a spec-like file in the diff **and** a different matching file in the
  repo, *then* the diff file wins.

**US-S6: No spec, or an ambiguous one, means no spec check.**
As a PR author, I want the review unchanged when no spec is found, so that a
missing spec never produces invented findings or a failed review.
- *Given* any of: no candidate files; two candidates tied for best match;
  `pr_head_ref is None`, *when* `spec_review` runs, *then* no LLM call is made,
  findings pass through unchanged, and one log line records outcome
  `none` / `ambiguous`.

**US-S7: Spec compliance findings quote the spec.**
As a PR author, I want missing, extra and wrongly-implemented requirements
reported against the spec's own words, so that I can check each claim myself.
- *Given* a resolved spec and a recorded response reporting one missing
  requirement, one scope-creep item and one wrong implementation, *when*
  `spec_review` runs, *then* three `Finding`s with `category="spec"` are
  appended, each message containing the quoted spec line; a response item
  without a quoted line is dropped and counted.

**US-S8: Spec findings are verified; rejected ones are not posted.**
As a PR author, I want spec findings held to the same quality gate as other LLM
findings, so that a vague or stale spec does not flood me with false "missing
requirement" claims.
- *Given* two `category="spec"` findings and a `verification` response that
  fails one on the rubric, *when* the pipeline completes, *then* only the
  passing one is posted and the verifier's prompt included the resolved spec
  text.

**US-S9: Spec check can be switched off.**
As a platform operator, I want to disable the spec check independently of
smells, so that I can control cost and noise per capability.
- *Given* `spec_review_enabled=False`, *when* a review runs, *then*
  `spec_review` does not execute, no spec lookup or LLM call occurs, and output
  is identical to today's.

**US-S10: Spec-check failure never fails the review.**
As a PR author, I want a broken optional check to cost me only the check, so
that my review still arrives.
- *Given* a spec fetch error, an LLM timeout, or an unparseable response inside
  `spec_review`, *when* the step runs, *then* it returns `review_generation`'s
  findings unchanged, logs at `warning` (error type only, never spec text), and
  the job completes successfully.

**US-S11: Spec text is treated as untrusted.**
As a platform operator, I want repository-supplied spec text handled like any
other untrusted input, so that a malicious spec cannot steer the model or leak
secrets.
- *Given* a spec containing an instruction to "ignore previous instructions"
  and a secret-shaped string, *when* `spec_review` runs, *then* the spec appears
  only inside its labelled, delimited prompt section, text beyond the size cap
  is truncated with the truncation logged, and any secret-shaped string quoted
  into a finding message is redacted before posting.

**US-S12: Spec findings with no changed line still reach the author.**
As a PR author, I want a "missing requirement" finding to appear even though no
line of my diff caused it, so that the most important spec gap is not silently
lost.
- *Given* a verified `category="spec"` finding with `line=None` and `file` set
  to the spec path, *when* findings are posted, *then* no inline comment is
  attempted for it and it appears in the check-run summary under "Findings not
  attributable to a specific line".

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Cost (decided):** at most **one** extra LLM completion call per review, made
  only when a spec is resolved; zero when none is. Smell checking adds prompt
  tokens to the existing `review_generation` call, no extra call.
- **Spec size cap:** a fixed module-level constant (matching
  `MAX_INTENT_SUMMARY_CHARS`'s precedent), not new config: **20,000
  characters** (assumption pending validation); text beyond it is truncated.
  A spec blob over the adapter's own blob-size ceiling resolves as
  `fetch_failed` (US-S10), not a crash.
- **Timeouts:** every I/O call (tree listing, blob fetch, LLM) takes a timeout
  (`architecture.md`); the step has its own entry in `StepTimeouts`, and the LLM
  call reuses `llm_timeout_seconds`. `WorkerSettings` already refuses to start
  if `stuck_job_warning_threshold_seconds` is below the summed worst-case
  chain, so adding the step's timeout to that chain, and raising the threshold
  default if needed, is a hard requirement (a misconfigured deployment must
  fail fast, as today). No new latency target is set: the no-spec path costs at
  most one tree listing, bounded by its timeout.
- **Reliability:** see US-S10; no new fatal failure mode is introduced.
- **Security:** no new egress destination. Spec text goes only to the
  already-configured BYO LLM endpoint, exactly as the diff does. No credentials
  handled by the new step; the existing `RepoTreeFetcher` adapter reuses the
  GitHub installation auth.

### Privacy / Compliance

- Spec text is customer repository content: never logged in full, never
  persisted by this step. Logged only as presence, byte length, and the chosen
  path.

### Observability

- Follows `spec-observability.md`'s catalog conventions (OTel API imported
  directly per ADR-0011; recording failures log-and-continue per ADR-0012).
- Required signals: a span for `spec_review`; a counter of resolution outcome
  (`found_in_diff`, `found_in_repo`, `none`, `ambiguous`, `fetch_failed`,
  `degraded`); counts of smell and spec findings produced, and posted after
  verification. Exact names are fixed at implementation against the catalog.
- Operator-facing: the `warning` log on degradation (US-S10) is how an
  operator learns the check is failing.

### Rollout & Release Safety

- Both flags are env bootstrap config, so a rollback is a config change and
  restart, no migration. **Assumption pending validation:** both default to
  `True`, matching every existing step flag. If early use shows smell noise,
  flip the default to `False` rather than weakening the requirements.
- Additive `Finding.category` field defaults to `None`; existing producers,
  tests and stored data are unaffected.
- When disabled, behaviour is identical to the shipped pipeline (proven by
  US-S2 and US-S9).

## UX & Design

N/A — no UI in this increment. The PR author sees the same GitHub PR comments
and check-run output as today; the only visible difference is the "Possible
<Smell>:" label and spec findings' quoted spec line. Admin GUI controls are out
of scope. No design system applies.

- **Non-line-anchored spec findings (decided).** A "missing requirement"
  finding has no changed line. It is created with `line=None` and `file` set to
  the resolved spec's path. `adapter/github/posting.py` already routes
  `line is None` findings into the check-run summary under "Findings not
  attributable to a specific line" instead of an inline comment, so no
  posting change is needed; US-S12 pins this. Scope-creep and wrong-
  implementation findings use the real changed file and line where the model
  supplies one.

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak (sole engineer/author; no separate design/QA/legal
  stakeholders exist at this stage).
- **Acceptance:** every `US-S*` `Given/When/Then` is proven by an automated
  test per the traceability table; `mypy --strict` and `ruff` clean;
  `review/test_step_boundaries.py` passes unmodified for the new step module
  (US-F1 still holds); with both flags off, the existing review integration
  tests pass unmodified.

## Phases / Units

- **Unit A: Smell baseline + `Finding.category`.** Depends on: nothing new.
  Risk: low (additive prompt section; additive optional field). Subject to a
  quality-noise risk (assumption: verification gates it adequately).
  **Acceptance:** US-S1, US-S2, US-S3 integration tests against a recorded
  cassette; `findings/test_models.py` unit test for the field's default/shape.
- **Unit B: Spec resolution (pure matching + I/O over existing Protocols).**
  Depends on: nothing new. Risk: **medium**: the matching heuristic is unproven
  (branch names are a weak signal). The repo-tree path reuses
  `indexing.tree.RepoTreeFetcher` (`fetch_tree` plus arbitrary-path
  `fetch_content`, already implemented by `GitHubTreeFetcher` and injected in
  `apps/worker/main.py`); no new Protocol or adapter. **Acceptance:** US-S4, US-S5, US-S6
  integration tests (GitHub adapter against a `respx` cassette) plus unit tests
  of the pure matcher across tie/none/diff-first cases.
- **Unit C: Spec-compliance call + `spec_review` step + verifier spec-context.**
  Depends on: Units A (`category` field) and B. Risk: **medium-high**: first
  step that conditionally appends findings; the verifier must receive spec text
  through a single-dependency pipeline; non-line-attributable findings must
  post somewhere sensible (resolved by US-S12 against existing posting
  behaviour). **Acceptance:** US-S7, US-S8, US-S10,
  US-S11 integration tests against a recorded cassette.
- **Unit D: Composition-root wiring.** Depends on: A-C. Risk: low (extends the
  existing `resolve_review_pipeline_depends_on` fallback logic; the
  disabled-step combinations multiply). **Acceptance:** US-S9, plus a unit test
  of every `depends_on` combination, plus one E2E run through the Docker
  Compose stack (happy path with a spec present and with none).

## Testing / Key Scenarios

Levels follow `testing.md`: integration tests use recorded LLM cassettes by
default; unit tests use fakes at Protocol boundaries.

| Story | Scenario (G/W/T summary) | Level | Test path (proposed) |
| --- | --- | --- | --- |
| US-S1 | Duplicated-shape diff → prompt has baseline; smell finding tagged | Integration | `review/test_review_generation_step.py` (`@integration`) |
| US-S2 | Flag off → prompt byte-identical to today, output unchanged | Integration (+ unit on prompt builder) | `review/test_review_generation_step.py` |
| US-S3 | Smell label + severity cap on post; rejected smell dropped | Integration | `review/test_smell_findings.py` |
| US-S4 | Spec in diff chosen; one LLM call | Integration | `review/test_spec_review_step.py` |
| US-S5 | Repo spec at `head_sha` chosen; diff beats repo | Integration | `review/test_spec_resolution.py` (diff-wins case), `review/test_spec_review_step.py`, `adapter/github/test_tree.py` (repo blob fetch, `respx`) |
| US-S6 | None / tied / null head ref → no call, unchanged | Integration | `review/test_spec_review_step.py` |
| US-S6 | Matcher: pure tie/none/threshold cases | Unit | `review/test_spec_resolution.py` |
| US-S7 | Three finding kinds appended; quote-less item dropped | Integration | `review/test_spec_review_step.py` |
| US-S8 | Verifier sees spec; failing spec finding dropped | Integration | `review/test_verification_step.py` |
| US-S9 | Flag off → step not run, output identical | Integration | `review/test_review_job.py` |
| US-S10 | Fetch error / timeout / bad JSON → unchanged findings, job succeeds | Integration | `review/test_spec_review_step.py` |
| US-S11 | Injection text confined, cap enforced, secret redacted | Integration | `review/test_spec_review_step.py` |
| US-S12 | `line=None` spec finding → summary section, no inline comment | Integration | `adapter/github/test_posting.py` |
| US-S10 | Oversize spec blob → `fetch_failed`, findings unchanged | Integration | `adapter/github/test_tree.py` (adapter raises the domain error), `review/test_spec_review_step.py` (step degrades) |
| All | Compose stack: PR with spec → posted spec finding; PR without → unchanged | E2E | `tests/e2e/test_spec_review.py` |

Failure-mode E2E additions (per `testing.md`): LLM timeout during `spec_review`
still posts the ordinary review.

## Open items (assumptions pending validation)

1. Branch-name matching heuristic: tokenisation, generic-token stoplist
   (`feat`, `fix`, …), and threshold (left to implementation; contract fixed above).
2. Both flags default to `True`.
3. Smell severity ceiling (`low`) and spec size cap (20,000 chars).
4. ~~Mechanism for carrying spec text to `verification` through the
   single-dependency pipeline.~~ Resolved: the established
   optional-field pattern, an optional `spec: ResolvedSpec | None = None` on
   `_VerificationInput`, forwarded by `spec_review`'s output.
5. "Spec-like" path convention (`docs/`, `specs/`, `.scratch/`, name contains
   `spec`/`prd`) matches how customers actually store specs; this repo's own
   `docs/specs/spec-*.md` fits, others unknown.
