# Phase 11 — Auto-Fix / Committable Suggested-Fix PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-10-17

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 11 ("Auto-fix /
committable suggested-fix generation") into a concrete spec. Adds one
review-pipeline step that, for a small set of findings the platform already
trusts, proposes a validated one-click-committable GitHub "suggested change"
inside the finding's own inline comment. Built on Phase 3's verification
([`spec-verification-pipeline.md`](spec-verification-pipeline.md)), Phase 9's
analyzers ([`spec-static-analysis-expansion.md`](spec-static-analysis-expansion.md))
and Phase 4's Memory ([`spec-memory.md`](spec-memory.md)). Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`); requirements are user stories with `Given/When/Then`
acceptance criteria per `testing.md`'s mandatory rule.

**Conventions found / fallbacks.** Spec format: `docs/specs/spec-*.md`
(structure mimicked from `spec-clone-detection.md`;
`docs/documentation_index.md` lists them). Testing pyramid:
`docs/rules/testing.md`. Design system: N/A (no UI surface of review's own —
the suggestion renders in GitHub's native UI; see "UX & Design"). Questions
the interview would have asked were answered from the documents and the code
where they could be; every decision is tagged **decided** or **assumption
pending validation** under "Resolved decisions". Two places where documents
and code disagree (D1, D9) are surfaced there, not silently resolved.

## Problem & Goal

Today every finding the platform posts stops at a diagnosis. The PR author
reads "unused import" or "possible nil dereference", then opens the file,
works out the edit, and types it. For the large class of findings whose fix is
a few lines in one place, that is pure friction; it is also why unfixed
findings linger. Commercial reviewers close this gap with a one-click fix
([`research-current-art.md`](../research/research-current-art.md) §3:
CodeRabbit, Qodo, Bito, DeepSource, Copilot Autofix, Amazon Q), and the same
research shows the risk: an unvalidated generated fix is worse than no fix —
Copilot Autofix's own numbers attribute a 3× success-rate gain to *validating*
candidate fixes before showing them, and GitHub documents that such models
can emit fabricated dependencies and syntactically valid but wrong edits.

**End user: the PR author.** They want, for a finding they agree with, to
apply the fix with one click and move on — and they want to never be offered a
"fix" that doesn't compile, that adds a new problem, or that changes more than
the finding warrants. A fix that fails any of those costs more trust than the
missing fix would have gained.

**Secondary end user: the platform operator**, who pays for the LLM calls
(bring-your-own endpoint) and so needs the feature off by default, capped, and
visible in spend, and who must be certain the platform never writes to a
customer's branch.

**Goal / success metric (capability-based, matching Phases 1/2/9/10):** with
the step enabled, an eligible finding on a changed line of a supported file
is posted with a single committable suggestion if and only if the proposed
patch passed every validation gate (parses; for analyzer findings, re-analysis
of the patched code no longer reports the finding and reports nothing new on
the changed lines; for LLM findings, a structurally separate rubric call
passes); every other finding posts exactly as today; no fix-generation
problem ever fails or delays a review beyond its step timeout; nothing is ever
applied without the PR author's click; and with the step disabled, posted
output is byte-identical to today's. **No numeric acceptance-rate target is
set:** there is no pilot data to ground one; the applied rate is observed
(US-AF14), and a target is set by recorded decision after a pilot.

## Scope

### In scope

- A new macro review-pipeline step, `fix_generation`, between `verification`
  and `output` (D1), that attaches a proposed `suggestion` to eligible
  findings and drops unvalidated ones silently (the finding still posts).
- An eligibility policy (D2): findings from the five language analyzers and
  Semgrep, and verified LLM findings; nothing else.
- A separate LLM call per eligible finding to generate the patch, scoped to
  the finding's file and lines (design goal 6: never the call that produced
  the finding).
- Validation before display (US-AF5–AF8): syntax check, analyzer re-run on
  the patched code for analyzer-sourced findings, a separate fixed-rubric
  call for LLM-sourced findings, no-op and secret-redaction rejection.
- Delivery as a GitHub suggested change inside the finding's existing inline
  comment, through the existing batched `post_review` path (D5).
- Settings (enable, caps, timeout, size bounds), startup validation, chain
  wiring, Helm/Compose mirroring, a new model-routing stage, runbook entry,
  metrics and dashboard panels, E2E.
- Memory: fix-bearing comments get ordinary provenance so a human can classify
  the thread `accepted_fix` at approval (US-AF13); no consumption (D9).

### Out of scope

- **Auto-applying or committing a fix.** Never. The platform requests no new
  GitHub permission and never writes to a customer's branch (US-AF9).
- **Multi-file or non-contiguous fixes.** GitHub's suggestion UI is
  single-range-per-comment (D3). A finding whose fix needs more is posted
  without one. A "patch bundle"/PR-against-PR mechanism is a separate future
  delivery design, not this phase.
- **Fixes for secrets (Gitleaks), IaC (Trivy) and duplication
  (`clone_detection`) findings.** A secret's real fix is rotation, and a model
  rewriting a line that holds a secret must not see or echo it; IaC and clone
  fixes are multi-location by nature (D2).
- **Fixes for unverified LLM findings**, and for LLM findings when
  `verification` is disabled (D2).
- **Languages without a syntax gate.** Only TypeScript/JavaScript, PHP, Go and
  Python files get fixes (D2).
- **Memory consumption of `accepted_fix` entries** (using accepted fixes to
  steer generation) — ADR-0009 decision 4's "stored but unread" stands (D9).
- **A new confidence score on `Finding`.** The roadmap's "above a confidence
  threshold" is realized as source/verification/severity eligibility, because
  no confidence attribute exists (D2).
- **Fixes for findings on lines the PR did not change.** The platform only
  suggests edits to code the author is already changing.
- **Admin UI controls for the feature** beyond the new model-routing stage
  row; settings are bootstrap/Helm, like every `*_enabled` flag.
- **Dynamic/runtime verification** of a fix (running tests in a sandbox) —
  deferred roadmap item, with its stated sandbox-escape rationale.

### Priority

| Priority | Unit | Why |
| --- | --- | --- |
| Must | A — Fix model & pure validation | Nothing may be shown unvalidated; this is the trust boundary. |
| Must | B — Finding model & suggestion delivery | The only end-user-visible unit. |
| Must | C — `fix_generation` step | Produces and gates the fixes. |
| Must | D — Settings, ops, routing, observability | No releasable LLM-spending feature without a kill switch, caps and spend visibility. |
| Must | F — E2E | Proves the goal end to end and exercises new dashboard panels. |
| Should | E — Memory provenance | Makes fix threads classifiable; the fix itself works without it. |

If time forces a cut, cut E: fix threads still post and are still captured as
ordinary comments by Phase 4; they just aren't distinguishable at approval.
Never cut a validation gate (US-AF5–AF8) to ship sooner.

## Requirements

**US-AF1 — An eligible finding arrives with a one-click fix**
As a PR author, I want a finding I agree with to come with a fix I can commit
in one click, so that I don't have to hand-type a mechanical edit.
- Given an eligible finding (US-AF2) on a changed line of a supported file,
  and a patch that passes every gate (US-AF5–AF8), When the review posts, Then
  the finding's single inline comment carries exactly one GitHub suggestion
  block replacing the fix's line range, with the finding's message, severity,
  rule and source text unchanged above it.
- Given the same finding when any gate fails or the step fails, Then the
  comment posts exactly as it does today (no suggestion, no mention of the
  failed attempt).
- Given a suggestion is posted, Then the comment says nothing about
  confidence; presence of the block is the only signal that a validated fix
  exists.

**US-AF2 — Only findings the platform already trusts are fix candidates**
As a PR author, I want fixes offered only where they are likely right, so that
a suggestion block is a signal worth trusting.
- Given a finding whose `source` is `ruff`, `eslint`, `golangci_lint`,
  `phpstan` or `semgrep`, Then it is a candidate.
- Given a finding with `source="llm"`, Then it is a candidate only if
  `verification_status == "verified"`; with `verification` disabled none are.
- Given a finding from `gitleaks`, `trivy` or `clone_detection`, or with
  severity `info`, Then it is never a candidate.
- Given a finding with no `line`, a line outside the PR's changed lines
  (`review/changed_lines.py`), a file the diff reports without a `patch`, or a
  file with an unsupported language, Then it is never a candidate.
- Given two candidates on the same file and line, Then only the highest-ranked
  (severity, then source rank) gets a fix attempt.
- Given two validated fixes in one file whose ranges overlap (the model, not
  the step, chooses a range, so this is only knowable after generation), Then
  only the higher-ranked is kept and the other is dropped with reason
  `overlap`, so two posted suggestions never overlap.

**US-AF3 — The fix is generated by a separate, narrowly scoped call**
As a PR author, I want the fixer to be a different pass from the reviewer, so
that a finding isn't "fixed" by the same reasoning that produced it.
- Given a candidate, When a fix is requested, Then exactly one LLM Gateway
  call is made, distinct from every call that produced or verified the
  finding, routed under its own model-routing stage `fix_generation`.
- The call's input is limited to the finding (message, rule, severity, line),
  a window of the file's head content of at most
  `fix_generation_window_lines` (default 60) lines centered on the finding's
  line and always containing the whole candidate range, and nothing else; it
  never receives the whole diff,
  other files, product-knowledge context or other findings.
- The call's required output is a fixed structured shape: `start_line`,
  `end_line`, `replacement` (text, possibly empty for a deletion). Anything
  else is a parse failure (US-AF12).
- Prompt-injection posture: PR content is delimited as data; the output is
  validated structurally and never executed, evaluated or followed as
  instructions beyond the three fields.

**US-AF4 — A fix is one contiguous range the author can commit**
As a PR author, I want the suggestion to be committable exactly as shown.
- Given a returned patch, Then it is accepted only if `start_line <= end_line`,
  both lie on lines present on the new-file side of one diff hunk of this PR
  with every line in between also in that hunk, the finding's `line` lies
  within the range, and the range spans at most
  `fix_generation_max_range_lines` (default 20) lines.
- Given a patch outside those bounds (a different hunk, a different file, a
  range that grows past the cap), Then it is rejected and the finding posts
  without a fix.
- Given an empty `replacement`, Then it is a deletion of the range and is
  accepted (an unused-import fix is a deletion).
- The posted comment is anchored to the range with GitHub's multi-line fields
  (`start_line`/`line`), and a one-line range uses the single-line form.

**US-AF5 — A fix that doesn't parse is never shown**
As a PR author, I want no suggestion that breaks the file.
- Given a patch, When it is applied to the file's head content in memory,
  Then the patched file must parse with zero syntax errors using the language's
  parser (Tree-sitter for TypeScript/JavaScript, PHP and Go, as `indexing`
  already uses; Python's own parser for `.py`).
- Given the original file already has syntax errors outside the patched range,
  Then the gate compares error counts and requires the patch to add none.
- Given a patch that fails the gate, Then it is rejected (reason
  `syntax_error`).

**US-AF6 — An analyzer finding is fixed only if the analyzer agrees**
As a PR author, I want a lint or SAST fix confirmed by the tool that raised
the finding.
- Given a finding from analyzer X and a syntax-valid patch, When the patched
  file is re-analyzed by the same analyzer X with the same configuration
  (including honored repo-native configs, per `spec-static-analysis-expansion.md`),
  Then the fix is accepted only if the original finding (same rule, within the
  range) no longer appears **and** X reports no new finding on the patched
  range that was not present before.
- Given the analyzer errors or times out on the re-run, Then the fix is
  rejected (reason `reanalysis_failed`), never accepted unchecked.
- Re-analysis uses the existing analyzer adapters and the Worker-wide analyzer
  concurrency bound; it adds no analyzer binary, no network call and executes
  no repo-supplied code (same constraint as Phase 9).

**US-AF7 — An LLM finding's fix passes a separate fixed rubric**
As a PR author, I want a model-generated fix for a model-generated finding
checked by a structurally separate pass, never by self-rating.
- Given a verified LLM finding and a syntax-valid patch, When the patch is
  judged, Then one LLM Gateway call, routed under the existing `verification`
  stage and distinct from the generation call, evaluates the patched range
  against four fixed boolean criteria — `resolves` (addresses the finding),
  `minimal` (changes nothing the finding doesn't require), `safe` (introduces
  no new defect), `compiles_in_context` (consistent with the surrounding
  code shown) — and the fix is accepted only if all four are true.
- Given the response is unparseable, lacks a verdict, or any criterion is
  false, Then the fix is rejected (reason `rubric_failed`); an unmatched
  verdict fails closed (the Phase 3 precedent).
- No criterion asks the model to rate its own confidence.

**US-AF8 — No-op, secret-bearing or oversized fixes are rejected**
As a PR author, I want only meaningful, safe text in a suggestion.
- Given a patch whose resulting text equals the original range, Then it is
  rejected (reason `noop`).
- Given a `replacement` that the configured `TextRedactor` changes (i.e. it
  contains secret-shaped text), Then the fix is rejected (reason `redaction`)
  — never posted redacted, since altered code is wrong code.
- Given a `replacement` longer than `fix_generation_max_replacement_lines`
  (default 40) lines, Then it is rejected (reason `oversized`).

**US-AF9 — Nothing is ever applied without the author's click**
As a PR author and as an operator, I want to be certain the platform only
suggests.
- The step makes no call to any GitHub write API other than the existing
  batched `post_review` already used for findings; no commit, branch, ref or
  contents write is added, and the GitHub App's permission set is unchanged.
- The check-run conclusion stays `neutral`; the review event stays `COMMENT`.
- Given the author does not click, Then the PR contains no change from the
  platform.

**US-AF10 — A suggestion can't break out of its block or the comment**
As a PR author, I want the fix to render as code and nothing else.
- Given a `replacement` containing backtick fences, Then the suggestion block
  uses a fence longer than any backtick run inside the replacement so the
  block cannot terminate early.
- The fix text is rendered verbatim inside the block (not passed through
  `escape_markdown`, which would corrupt committable code); every other part of
  the comment keeps the existing escaping, so no PR-derived or model-derived
  text outside the block reaches GitHub's renderer unescaped.
- Given a `replacement` containing `\r` or NUL bytes, Then it is rejected
  (reason `invalid_text`).

**US-AF11 — Fix spend and review noise are bounded**
As an operator, I want a ceiling on the extra LLM spend and on suggestion
volume per review.
- Given more candidates than `fix_generation_max_fixes_per_job` (default 5),
  Then attempts are made in order of severity then source rank, the remainder
  are skipped, and the skipped count is logged at `warning` and exposed as a
  metric. At most that many generation calls (and rubric calls) are made per
  job.
- Given the step's wall-clock exceeds
  `fix_generation_step_timeout_seconds` (default 90), Then in-flight attempts
  are abandoned, completed fixes are kept, and the job continues (US-AF12).
- File-content fetches for the step are limited to the files of the attempted
  candidates.

**US-AF12 — A fix problem never blocks or fails a review**
As a PR author, I want my review to complete even if fix generation can't.
- Given an LLM timeout, an unparseable or schema-violating response, a content
  fetch failure, an analyzer crash, a rubric call failure or the step timeout,
  When the step runs, Then every finding passes through unchanged (without a
  suggestion for the affected ones), the cause is logged at `warning` or
  `error` with the rejection reason, and the job completes. It is never raised
  as a fatal pipeline failure (the Context Builder and clone-detection
  precedent).
- A redaction-call failure rejects only that fix; unlike `review_generation`,
  it is not fatal here, because an omitted suggestion is a safe outcome.

**US-AF13 — Fix threads are classifiable in Memory**
As a team approving Memory entries, I want to tell an accepted fix from an
accepted finding.
- Given a comment posted with a suggestion, Then its provenance record is
  stored exactly as for any posted finding (the `(path, line, body)`
  correlation continues to work for the multi-line anchor) and is marked as
  having carried a suggestion.
- Given a reaction, reply or thread resolution on that comment, Then Phase 4's
  generic capture records it as `pending` with `entry_type` unset; a human may
  assign `accepted_fix` at approval (ADR-0009 decision 1 unchanged — no
  automatic classification, no keyword or "was it committed" inference).
- Given an approved `dismissed_finding` for a similar finding, Then suppression
  behaves exactly as today and is applied before `fix_generation`, so no fix
  is generated for an already-suppressed LLM finding.
- `accepted_fix` entries are not read by any pipeline stage in this phase.

**US-AF14 — Operators can see what fixes cost and how they fare**
As an operator, I want to know spend, volume and rejection reasons.
- Given any job, Then logs and metrics record: candidates, attempts, accepted,
  rejected by reason (`syntax_error`, `reanalysis_failed`, `rubric_failed`,
  `noop`, `redaction`, `oversized`, `out_of_range`, `invalid_text`, `overlap`,
  `parse_error`, `timeout`, `fetch_failed`), skipped by cap, and duration.
- Given LLM calls for `fix_generation` and the patch rubric, Then token usage
  is recorded under the existing `review.llm` usage metric with the stage
  label, so the Admin stats LLM-spend view includes them without a new metric.
- The applied rate (suggestions committed) is not computed by the platform in
  this phase; it is read from approved `accepted_fix` entries. This is a stated
  limitation (see Resolved decisions, A4).

**US-AF15 — Operators can switch it off, route it, and bound it**
As an operator, I want a kill switch, a model choice and tunable bounds.
- Given `fix_generation_enabled=false` (the default, D8), Then the step is
  registered but gated by the enablement mapping so it never runs, no fix LLM
  call or content fetch is made, and posted output equals pre-Phase-11 output
  byte for byte.
- Given `fix_generation_max_fixes_per_job` (5), `fix_generation_max_range_lines`
  (20), `fix_generation_max_replacement_lines` (40),
  `fix_generation_window_lines` (60) and
  `fix_generation_step_timeout_seconds` (90), Then each is honored; a value
  ≤ 0 fails Worker startup with a clear message.
- Given `fix_generation_enabled=true` with `verification`, `static_analysis`
  or `clone_detection` in any enablement state, Then
  `resolve_review_pipeline_depends_on` and the fail-fast configuration check
  wire `output` to the correct upstream (extended with this step), and with
  `verification` disabled only analyzer findings are candidates.
- Given the model-routing configuration, Then `fix_generation` is a known
  stage (`KNOWN_MODEL_ROUTING_STAGES`), resolvable per org with the existing
  org → platform → static-default fallback, and settable in the Admin UI's
  model-routing screen like the existing three.
- Settings mirror one-for-one into the Helm `worker` block.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Review-time latency:** the step adds at most one generation call per
  attempted candidate plus (LLM findings only) one rubric call, run
  concurrently up to the Worker's existing LLM concurrency, bounded by
  `fix_generation_max_fixes_per_job` and the step timeout. On the E2E fixture
  (stub LLM) the step adds under 2 s; a real-endpoint budget is a plan-time
  measurement, and a measured miss changes the budget by recorded decision.
- **Cost:** worst case per job is `max_fixes × 2` extra LLM calls (default
  10). Off by default. Prompts are bounded to a window of the file, not the
  diff or repo.
- **Security:**
  - *Trust of model output:* the model's only output is three typed fields,
    validated structurally (US-AF4, AF8, AF10) and never executed. The
    validation gates, not the prompt, are the control.
  - *Prompt injection via PR content:* a malicious comment or string in the
    window can try to steer the replacement. Containment: the replacement is
    confined to the finding's already-changed range (US-AF4), must parse and
    re-analyze clean (US-AF5/AF6) or pass a separate rubric (US-AF7), is shown
    to the PR author as a diff they must click to apply, and cannot reach
    other files or any write API (US-AF9). A residual risk remains that a
    subtle malicious edit to the author's *own* changed lines passes the
    gates; the human-click requirement is the final control (A5).
  - *Secrets:* Gitleaks findings are ineligible, so a secret line is never
    sent as a fix prompt on the platform's initiative; a replacement is
    redacted-or-rejected (US-AF8); the window is passed through the same
    redaction pipeline `review_generation`'s prompt input already is subject
    to (plan confirms equivalence).
  - *No new subprocess surface, binary, network egress or credential;* the
    step reuses the LLM Gateway and existing analyzer adapters.
- **Reliability:** US-AF12 fail-open; a missing suggestion is always a safe
  outcome and a wrong suggestion never ships by gating.
- **Scale:** per-job work is bounded by the cap and the timeout; independent
  of repo size.

### Privacy / Compliance

Fix generation sends a bounded window of a changed file, with the finding
text, to the org's configured LLM endpoint — the same destination and data
class `review_generation` already uses for the diff, and no more (no other
files, no index content, no product-knowledge content). No new data leaves the
deployment; nothing is stored beyond the existing comment provenance and
ephemeral logs. The runbook states this in the same terms as the existing LLM
egress statement.

### Observability

Extends `spec-observability.md`'s catalog rather than inventing parallel
names:
- Logs: step start/outcome with `job_id`, candidates, attempts, accepted,
  rejected-by-reason counts, skipped-by-cap, fetch count, duration.
- Metrics: a counter of fix outcomes labeled by closed-set `outcome` /
  `reason` (never free text), a step-duration histogram via the existing
  per-step histogram, and LLM token usage under the existing `review.llm`
  counter with stage `fix_generation`.
- Dashboard: Grafana panels for outcomes, rejection reasons and fix LLM
  usage. **Lesson from Phase 7 (carried from the Phase 10 spec):** the
  `test_observability` E2E check fails when a new panel receives no data from
  the test's traffic — every panel added here must be exercised by
  `tests/e2e/test_auto_fix.py` or the check updated in the same change.

### Rollout & Release Safety

- **Migration:** one small additive Alembic migration for the "carried a
  suggestion" provenance marker (US-AF13), if the plan finds that column
  cannot reuse existing fields; otherwise none. No destructive change.
- **Default:** `fix_generation_enabled=false` (D8). Operators opt in after
  reading the runbook; a recommended path is to enable for one org/staging
  deployment first.
- **Rollback:** disable the step with one flag; no data cleanup. Fix-bearing
  comments already posted remain valid GitHub comments.
- **Additive:** with the step disabled, output equals today's byte for byte.
- **Compatibility:** `Finding` gains one optional field defaulting to `None`;
  no existing producer or consumer changes (the `context_ref_ids` precedent).

## UX & Design

N/A for review's own UI: no new screen. The one authored artifact is the
rendered comment: the existing finding comment followed by a native GitHub
suggestion block, which GitHub renders with its own "Commit suggestion" and
"Add to batch" controls (keyboard-accessible, mobile-capable, localized by
GitHub). Edge states: a comment with no suggestion is today's comment; a
suggestion that becomes outdated after a force-push is marked outdated by
GitHub itself. Admin UI change is limited to the model-routing screen
listing the new `fix_generation` stage, reusing the existing stage-row
component, and the operator-facing disclosure that the stage sends file
windows to the configured endpoint (same pattern as the existing
product-knowledge egress disclosure).

## Stakeholders & Sign-off / Acceptance Criteria

- Owner/sign-off: Andy Beak (sole author/approver per the ADRs' RACI).
- Phase acceptance: through the full Compose stack with a stubbed GitHub and a
  stubbed LLM, (a) a PR with an analyzer finding on a changed line in each of
  TypeScript, PHP, Go and Python posts one review comment carrying one
  suggestion whose range and replacement match the validated patch, and the
  stub GitHub receives no write other than the one review and the check run;
  (b) each validation gate (syntax, re-analysis, rubric, no-op, redaction,
  range) driven by a bad stub patch posts the finding with no suggestion and
  the job completes; (c) Gitleaks, Trivy and `clone_detection` findings and
  unverified LLM findings never get a suggestion; (d) with the step disabled,
  posted output equals today's.
- Not verifiable in this repo (carried as in Phases 1, 9 and 10): fix quality
  against a real model on a real repository. The plan lists a manual
  verification against a real repo and endpoint, including a reviewed sample
  of validated fixes for subtle wrongness. Cross-check with Phase 7's open
  items before enabling by default.

## Phases / Units

- **Unit A — Fix model & pure validation.** A typed fix (path, start, end,
  replacement), patch application to file content, range check against a
  PR's hunks, no-op/size/invalid-text rejection, fence-safe suggestion
  rendering, and the syntax gate (Tree-sitter via the same parser access
  `indexing` uses; Python's parser for `.py`). Pure/no-I/O except the parser,
  unit-tested per ADR-0005. Depends on: Phase 2's parsers, Phase 9's
  `review/changed_lines.py`. *Risk:* **medium** — comparing syntax-error
  counts across three Tree-sitter grammars and `ast`, and getting hunk bounds
  right on renames and file edges, are easy to get subtly wrong. *Decision:*
  gates decided (US-AF4, AF5, AF8, AF10); module placement is a plan-time
  choice that respects `architecture.md`'s package boundaries.
  *Acceptance:* US-AF4, AF5, AF8, AF10 unit and integration tests.
- **Unit B — Finding model & suggestion delivery.** `Finding` gains an optional
  suggestion (range + replacement, hashable); the GitHub posting adapter
  renders it as a suggestion block with `start_line`/`line`, bypassing
  `escape_markdown` for the block only; provenance correlation handles the
  multi-line anchor. Depends on: A. *Risk:* **medium** — `adapter/github/posting.py`
  correlates returned comments to findings by `(path, line, body)` and
  GitHub's multi-line comments report their end line; a mismatch silently
  loses provenance (US-AF13). The plan must verify against a recorded cassette
  of a real multi-line suggestion comment. *Acceptance:* US-AF1, AF9, AF10
  integration tests against the GitHub cassette.
- **Unit C — `fix_generation` step.** Macro step: eligibility (US-AF2),
  overlap resolution, content fetch for attempted files, generation call
  (stage `fix_generation`), gates in order (range → no-op/size/text →
  redaction → syntax → re-analysis *or* rubric), cap/timeout/concurrency,
  fail-open, per-finding outcome telemetry. Re-analysis reuses the registered
  analyzers through an injected Protocol (the step-boundary test forbids
  step-to-step imports). Wired into `resolve_review_pipeline_depends_on` and
  `apps/worker/main.py`. Depends on: A, B, Phase 3, Phase 9. *Risk:* **high**
  — it spends money, calls analyzers from outside `static_analysis`, and its
  quality can't be proven in-repo; the `Step`/analyzer-concurrency boundary
  and the chain resolver (hard-coded to the current chain) must be extended
  deliberately. *Assumption vs decision:* mostly decided; thresholds are
  assumptions (A1–A3). *Acceptance:* US-AF1–AF3, AF6, AF7, AF11, AF12.
- **Unit D — Settings, ordering, routing, ops, observability.** Worker
  settings and startup validation, Helm/Compose mirror, `fix_generation` added
  to `KNOWN_MODEL_ROUTING_STAGES` and the Admin UI stage list, runbook
  section in `docs/reference/operations.md`, metrics, dashboard panels.
  Delivered incrementally with C. Depends on: C. *Risk:* low-medium — the
  Admin UI and API hard-code "three stages" (US-A14 of `spec-admin-gui.md`);
  the plan must find every site. *Acceptance:* US-AF14, AF15.
- **Unit E — Memory provenance.** The "carried a suggestion" marker and its
  display in the Memory list; no new capture logic and no consumption.
  Depends on: B. *Risk:* low. *Acceptance:* US-AF13.
- **Unit F — E2E.** `tests/e2e/test_auto_fix.py` over the Compose stack.
  Depends on: A–E. *Risk:* low-medium — the stub GitHub must capture the
  posted review's comment fields (`start_line`, `line`, body) and the stub LLM
  must serve generation and rubric calls deterministically; the Phase 7
  stale-stub lesson applies (the stub GitHub tree and `patch` hunks must match
  what the changed-line and range logic expect). *Acceptance:* the
  phase-acceptance scenario under "Stakeholders & Sign-off" passes and every
  new dashboard panel receives data.

## Testing / Key Scenarios

Per `testing.md`: every story maps to at least one **integration** test; pure
no-I/O logic (range check, patch application, fence rendering, eligibility,
overlap resolution, rejection reasons) is also unit-tested per
[ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md). The LLM is a recorded
cassette in integration tests; live mode is manual/nightly only.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-AF1 | Eligible finding + valid patch → one comment with one suggestion block and unchanged finding text; any gate failure → today's comment | `review/test_fix_generation_step.py` (new), `review/test_output.py` (extended) | Integration |
| US-AF2 | Per-source/severity/status/line/patch/language eligibility matrix; overlapping candidates → one attempt | `review/test_fix_generation_step.py`, `review/test_fix_eligibility.py` (new) | Unit + Integration |
| US-AF3 | One distinct generation call under stage `fix_generation`; input limited to the finding and a file window; off-schema response → rejected | `review/test_fix_generation_step.py` (LLM cassette) | Integration |
| US-AF4 | Range in one hunk accepted; cross-hunk, out-of-finding, over-cap, inverted range rejected; empty replacement accepted as deletion | `review/test_fix_validation.py` (new), `review/test_fix_generation_step.py` | Unit + Integration |
| US-AF5 | Syntactically broken patch rejected per language; pre-existing errors outside the range don't block a clean patch | `review/test_fix_validation.py`; `review/test_fix_generation_step.py` (real parsers) | Unit + Integration |
| US-AF6 | Real analyzer binary on fixtures: fix clears the finding → accepted; leaves it or adds one → rejected; analyzer crash/timeout → rejected | `adapter/analyzers/test_fix_reanalysis.py` (new), `review/test_fix_generation_step.py` | Integration (real binaries) |
| US-AF7 | Verified LLM finding: all four criteria true → accepted; any false, unparseable or unmatched verdict → rejected; rubric call routed under `verification` | `review/test_fix_generation_step.py` (LLM cassette) | Integration |
| US-AF8 | No-op, secret-shaped replacement (real Gitleaks redactor), oversized, `\r`/NUL → rejected with reason | `review/test_fix_validation.py`; `review/test_fix_generation_step.py` | Unit + Integration |
| US-AF9 | A full job's GitHub traffic against the cassette/stub contains only the review and check-run writes; check-run `neutral`, event `COMMENT` | `adapter/github/test_posting.py` (extended), `tests/e2e/test_auto_fix.py` | Integration + E2E |
| US-AF10 | Replacement containing backtick runs renders with a longer fence; block text unescaped, surrounding text escaped | `adapter/github/test_posting.py` (extended); `review/test_fix_validation.py` | Unit + Integration |
| US-AF11 | 8 candidates, cap 5 → 5 attempts by severity/source order, skipped count logged; step timeout keeps completed fixes | `review/test_fix_generation_step.py` | Integration |
| US-AF12 | LLM timeout, bad JSON, fetch failure, analyzer crash, rubric failure, redactor failure → findings unchanged, job completes | `review/test_fix_generation_step.py`, `review/test_review_job.py` (extended) | Integration |
| US-AF13 | Suggestion comment's provenance stored with the marker (multi-line correlation works); reaction/resolution captured `pending`; dismissal suppression precedes fix generation | `adapter/github/test_posting.py`, `adapter/storage/test_memory_repository.py` (extended), `review/test_fix_generation_step.py` | Integration (real Postgres) |
| US-AF14 | Outcome/reason counters, token usage under stage `fix_generation`; logs carry counts | `review/test_fix_generation_step.py`, `tests/e2e/test_observability.py` (extended) | Integration + E2E |
| US-AF15 | Disabled → output equals baseline; invalid settings fail startup; chain wiring in every enablement combination; `fix_generation` resolvable as a routing stage in admin API and UI | `apps/worker/test_settings.py`, `review/test_config.py`, `apps/admin_api/test_model_routing_routes.py` (extended), frontend test (extended) | Unit + Integration |
| All (happy path) | Compose stack, stub GitHub + stub LLM: one suggestion per supported language; each bad-patch gate → no suggestion; ineligible sources → none; disabled reproduces today's output; dashboard panels receive data | `tests/e2e/test_auto_fix.py` (new), `tests/e2e/test_observability.py` (extended) | E2E |

## Resolved decisions

**Decided** (by the documents, the code, or product-owner reasoning from
end-user value and the competitor basis in the research doc):

- **D1 — A separate `fix_generation` macro step, not a sub-step of the findings
  merger.** *Document/code conflict, surfaced:* the roadmap says the "Findings
  merger (Phase 3) gains an optional 'propose fix' sub-step". In the code
  `findings/merge.py` is a pure, no-I/O function in the domain `findings`
  package, and `architecture.md` and ADR-0005 keep LLM calls, content fetches
  and subprocesses out of pure logic and each stage behind its own narrow
  interface. A step after `verification` also sees only the findings that
  survived rubric and Memory suppression, so no money is spent fixing a
  finding that won't post. The step is therefore placed between
  `verification` and `output`. The roadmap wording is corrected when this
  phase ships. (Tagged decided on the rules; the roadmap author's intent —
  "after verification, before posting" — is preserved.)
- **D2 — Eligibility is source, verification and severity, not a "confidence
  score".** No confidence attribute exists on `Finding`, and Phase 3 rejected
  model self-rating as near-random. The Copilot Autofix pattern the roadmap
  cites anchors fixes to a deterministic alert; we generalize it to the
  analyzers whose behavior we can re-run (Ruff, ESLint, golangci-lint,
  PHPStan, Semgrep) and add verified LLM findings because they are the largest
  class of fixable findings. Excluded with reasons: Gitleaks (fix is rotation;
  don't show a secret line to a model), Trivy (IaC fixes are multi-location),
  `clone_detection` (extraction is multi-file), `info` severity (not worth
  the spend). Languages limited to those with a syntax gate we already have
  (TypeScript/JavaScript, PHP, Go via Tree-sitter; Python via its own parser).
- **D3 — One contiguous range in one hunk.** GitHub's suggestion UI is a single
  range per comment and applies only to lines present on the diff's new-file
  side. Anything needing more is a finding without a fix; multi-file delivery
  is a different mechanism and out of scope.
- **D4 — Validation is deterministic first, a separate LLM judge only where no
  deterministic arbiter exists.** Analyzer findings get syntax + the same
  analyzer's verdict (no second LLM call: the tool that raised it is the
  better judge and costs nothing). LLM findings get syntax + a separate,
  fixed-rubric call routed under the existing `verification` stage. This is
  the roadmap's "static analyzer/verification rubric against the *patched*
  code".
- **D5 — Delivered inside the finding's existing inline comment** through the
  existing batched `post_review`, as a native suggestion block. One comment
  per finding, no second comment type, no new GitHub permission.
- **D6 — Never auto-applied** (design goal 9), enforced by absence of any
  write path (US-AF9), not by configuration.
- **D7 — Fail-open.** An omitted fix is a safe outcome; unlike
  `review_generation`'s redaction failure (fatal because posting an unredacted
  secret is worse), a failure here costs only a convenience.
- **D8 — Off by default.** Unlike Phases 9/10 (free, deterministic), this
  feature spends the customer's own LLM budget on calls they didn't ask for
  (cf. ADR-0007's stance on unrequested inference spend). The operator opts in.
- **D9 — Memory: populate and make classifiable, don't consume.** *Document
  tension, surfaced:* the roadmap says "accepted vs. rejected fixes feed Memory
  as `accepted_fix` entries — nothing populates it until this phase exists".
  In the code, capture is already generic for every posted comment (raw signal,
  `pending`, `entry_type` unset) and a human assigns `accepted_fix` at
  approval (ADR-0009 decision 1); decision 4 states `accepted_fix` entries are
  stored but unread until a later phase scopes consumption. This spec
  therefore (a) lets fix threads flow through the existing capture, (b) marks
  fix-bearing provenance so a human can classify them, and (c) leaves
  consumption out. Pushback recorded: "feed Memory" has no end-user benefit
  until something reads it; consumption is a separate phase with its own spec
  (e.g. steering generation, or suppressing fix attempts for categories a team
  keeps rejecting), not smuggled in here.
- **D10 — Routing: one new stage, `fix_generation`; the patch rubric reuses
  `verification`.** Keeps the cheap-model/strong-model tuning story coherent
  (Phase 8) with one added row, not two.
- **D11 — Fixes only for code the PR already changes.** Suggesting edits to
  lines the author did not touch is outside what they can reasonably accept
  in this PR. Applied to the finding: its line must be a changed line
  (US-AF2). The fix's range may extend over adjacent unchanged lines inside
  the same hunk (US-AF4) because a multi-line statement's fix often needs them.

**Assumptions pending validation** (easy to challenge):

- **A1 — Eligibility floor of "severity above `info`", cap of 5 fixes/job, a
  20-line range cap and 40-line replacement cap suit real PRs.** No customer
  data; chosen to bound spend and keep suggestions reviewable at a glance.
  Tune from the pilot's rejection-reason metrics.
- **A2 — Applying the PR author's committed config (honored repo-native
  analyzer configs) in the re-run is correct**, so a fix that satisfies the
  team's own rules is the bar. If a repo config makes the analyzer noisy, fixes
  get rejected, not wrongly accepted.
- **A3 — "No new finding from the same analyzer on the patched range" is the
  right regression bar.** It does not catch a regression another tool or the
  tests would; it is deliberately the smallest deterministic check.
- **A4 — Applied rate cannot be measured automatically in v1.** GitHub sends no
  webhook for "suggestion committed". Detecting it (e.g. comparing the next
  push's head content to the proposed replacement) is plausible but not
  designed here; v1 relies on human-approved `accepted_fix` entries and
  qualitative pilot review. Accepting this means the headline "does it help"
  signal is weak at launch.
- **A5 — Residual prompt-injection risk is acceptable** because the edit is
  confined to the author's own changed lines, gated, and requires their click.
  A reviewer who commits suggestions without reading them is outside the
  control.
- **A6 — Reusing the verification-stage model for the patch rubric** is
  acceptable cost/quality; separating it into its own stage is easy if
  operators want it.
- **A7 — Python is in the supported set even though the index covers only
  TypeScript/PHP/Go**, because Ruff findings are the single biggest source of
  mechanical fixes and Python's own parser is a no-I/O syntax gate. If the
  plan finds the Ruff re-analysis path costly, Python can slip to a follow-up
  without affecting the other languages.
- **A9 — Behavior on a re-push follows whatever findings already do.** This
  spec adds no cross-push de-duplication of suggestion comments and no cache of
  prior fixes, so a re-review of an unchanged finding regenerates (and re-pays
  for) its fix. The plan must check how the existing pipeline treats a finding
  re-posted on a new `head_sha` and either inherit that behavior or record a
  deliberate difference; if re-spend proves material, a per-`(file, rule,
  range-content)` reuse is a follow-up, not silently added here.
- **A8 — A suggestion for a rename-in-PR or very large file is skipped
  (no `patch`/oversize) rather than special-cased.**
