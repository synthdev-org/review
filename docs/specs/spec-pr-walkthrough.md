# Phase 12 — PR-Level Walkthrough Artifact PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-10-18

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 12 ("PR-level
walkthrough/summary artifact") into a concrete spec. Adds one review-pipeline
step that posts a single top-level PR comment: the intent summary Phase 3
already computes plus a logically ordered, grouped walkthrough of the changed
files. Built on Phase 3 ([`spec-verification-pipeline.md`](spec-verification-pipeline.md))
and the Phase 1 posting path ([`spec-review-pipeline.md`](spec-review-pipeline.md)).
Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`); requirements are user stories with
`Given/When/Then` acceptance criteria per `testing.md`'s mandatory rule.

**Conventions found / fallbacks.** Spec format: `docs/specs/spec-*.md`
(structure mimicked from `spec-auto-fix.md`; `docs/documentation_index.md`
lists them). Testing pyramid: `docs/rules/testing.md`. Design system: N/A (no
UI surface of review's own; the comment renders in GitHub's native UI, see
"UX & Design"). The interview was answered from documents and code where they
could; every decision is tagged **decided** or **assumption pending
validation** under "Resolved decisions". Two choices were made by the
interviewer's recorded instruction ("accept the recommendations"): D2
(deployment-wide toggle) and D3 (no diagrams); they are decided, not
assumptions. One document conflict (D2: roadmap vs. the `*_enabled`
convention) is surfaced there.

## Problem & Goal

A PR author's teammates open a PR and see a diff. The platform has already
worked out, in `intent_summary`, what the PR is trying to do, but throws it
away after steering the review prompt. The reviewer must rebuild that
understanding by reading files alphabetically in GitHub's file list, which is
the main reason large PRs get shallow human review. Commercial reviewers close
this with a top-of-PR walkthrough ([`research-current-art.md`](../research/research-current-art.md)
§2.3: CodeRabbit's cohort-ordered walkthrough, PR-Agent's `/describe`), and the
research notes this artifact is available "largely for free" once intent
reconstruction exists.

**End users.** (1) The **human reviewer** of a PR, who wants to know what the
change is for and which files to read in what order. (2) The **PR author**,
who gets a free, readable description of their change and a check that the
platform understood it. Findings comments already serve the author; this
serves the reviewer. The **platform operator** is the secondary user: the
feature adds an LLM call per push on their endpoint, so it must be off by
default, capped and visible in spend.

**Goal / success metric (capability-based, matching Phases 1/2/9/10/11):**
with the step enabled, a PR touching at least `walkthrough_min_files` files
gets exactly one platform walkthrough comment containing the intent summary
and a grouped, logically ordered file list, updated in place on each later
push; the comment contains no secret, link, image, HTML or @-mention taken
from untrusted content; no walkthrough problem ever fails or delays a review
beyond the step timeout; and with the step disabled, everything posted is
byte-identical to today's. **No numeric reader-satisfaction target is set:**
there is no pilot data to ground one; the operator observes post/skip/failure
rates (US-W12), and a target is set by recorded decision after a pilot.

## Scope

### In scope

- A new macro review-pipeline step, `walkthrough`, after `fix_generation` (or
  its fallback predecessor) and before `output` (D4), that builds the
  walkthrough and posts it itself, fail-open, inside its own timeout.
- Carrying the existing `intent_summary` text and the prefilter-narrowed
  diff (as `reviewed_diff`) forward, unchanged, to that step (D5).
- One new LLM call, under its own model-routing stage `walkthrough`, that
  groups and orders the changed files.
- A pure, deterministic sanitizer/renderer that turns the model's structured
  output into the comment, enforcing size and content limits (US-W5, US-W6).
- Delivery as one top-level PR comment, created once and edited in place
  thereafter, identified by a hidden marker, through the existing GitHub
  adapter's comment-posting path (D6).
- Settings (enable, minimum files, timeout, size bounds), startup validation,
  chain wiring, Helm/Compose mirroring, a model-routing stage row, runbook
  entry, metrics and dashboard panels, E2E.

### Out of scope

- **Mermaid/sequence/architecture diagrams** (D3). Deferred to a follow-up
  phase with its own validation design (and likely an ADR): LLM-generated
  Mermaid frequently fails to render, the Python Worker has no Mermaid parser,
  and every earlier phase refused to show LLM output it could not validate.
- **A per-org/repo toggle** (D2). Deployment-wide only; a per-repo override
  reading the existing `OrgRepoTeamSetting` scaffold is a named follow-up,
  triggered by pilot feedback that the comment is noisy on specific repos.
- **Replacing or restyling the findings comments** (D1). Inline comments, the
  review body and the check-run are untouched.
- **Citing Phase 7 product-knowledge items** in the walkthrough.
  `spec-product-knowledge.md` already lists this out of scope; Phase 7 is not
  yet closed.
- **Walkthrough content derived from the PR description, commit messages or
  linked tickets.** The summary is the existing diff-only `intent_summary`.
- **Posting on non-PR events, or a walkthrough for PRs below the minimum file
  count** (US-W3).
- **Memory capture of reactions or replies on the walkthrough comment.** It
  is not a finding; Phase 4's capture path only tracks review comments.
- **Admin UI controls** beyond the new model-routing stage row; settings are
  bootstrap/Helm like every `*_enabled` flag.
- **Walkthrough in the check-run output**, and any GitLab/Bitbucket delivery.

### Priority

| Priority | Unit | Why |
| --- | --- | --- |
| Must | A — Walkthrough model, sanitizer & renderer (pure) | The trust boundary: nothing untrusted reaches the PR unsanitized. |
| Must | B — Comment delivery (create/update in place) | The only end-user-visible unit. |
| Must | C — `walkthrough` step, intent-summary carry, settings, chain, routing stage | Produces the artifact and makes it switchable and capped. |
| Must | D — Ops, observability, runbook | No releasable LLM-spending feature without a kill switch, caps and spend visibility. |
| Must | E — E2E | Proves the goal end to end and exercises new dashboard panels. |

If time forces a cut, cut the ordering sophistication (group-and-order falls
back to diff order in a single group) before cutting the sanitizer or the
off-by-default switch. Never cut US-W5/US-W6.

## Requirements

**US-W1 — A reviewer opens the PR and sees what it is for**
As a PR reviewer, I want a summary of the change's intent at the top of the
PR, so that I understand the purpose before reading code.
- Given `walkthrough_enabled=true` and a PR with at least
  `walkthrough_min_files` reviewable files, When the review runs, Then exactly
  one top-level PR comment authored by the platform's GitHub App exists,
  beginning with a "Summary" section containing the `intent_summary` text
  unchanged apart from sanitization (US-W5).
- Given `intent_summary` is disabled or produced no text, Then the comment
  omits the Summary section and still contains the file walkthrough.

**US-W2 — Files are grouped and ordered the way an author would explain them**
As a PR reviewer, I want changed files grouped by role and ordered logically,
so that I know what to read first.
- Given a review runs, When the `walkthrough` call returns, Then the comment
  lists every reviewable changed file at most once, in named groups (each with
  a one-line description) ordered by the model's returned order, with files
  inside a group in the returned order.
- Given the model returns a path not in the PR's reviewable files, a path
  twice, or omits reviewable files, Then unknown/duplicate paths are dropped,
  and omitted files are appended in a final "Other files" group in diff order,
  so the list is always complete and never invents a file.
- Given the model returns an unparseable shape, Then see US-W8.

**US-W3 — Small PRs get no walkthrough**
As a PR author, I want no extra comment on a trivial PR, so that the PR
conversation stays quiet.
- Given fewer than `walkthrough_min_files` (default 2) reviewable files,
  Then no walkthrough LLM call is made and no comment is created or edited.

**US-W4 — One comment, kept current**
As a PR reviewer, I want one walkthrough comment that tracks the PR, so that
the conversation isn't spammed on every push.
- Given no platform walkthrough comment exists on the PR, When the step posts,
  Then one is created containing a hidden marker identifying it as the
  platform's walkthrough, and a footer naming the head commit it describes.
- Given a platform walkthrough comment already exists (found by marker and by
  the App as author), When a later push is reviewed, Then that comment is
  edited in place; no second walkthrough comment is created.
- Given a comment with the marker text authored by anyone other than the
  App, Then it is ignored (never edited, never deleted, never trusted as the
  platform's). Identity fails closed: a comment whose `performed_via_github_app`
  is absent or null, or whose id differs from the configured App id, never
  matches, and no fallback to login or marker-only matching exists. A match
  also requires the fixed footer line.
- Given a PR with more than 500 comments (5 pages of 100) and no match in
  them, Then nothing is created, edited or deleted and the outcome is
  `skipped_comment_limit`.
- Given two review jobs for the same PR run concurrently and no walkthrough
  comment exists yet, Then after both finish exactly one platform walkthrough
  comment exists: after creating, a job re-lists, and if an older platform
  comment exists edits it with its own content and deletes only the comment
  its own create call returned (by id). If the re-list cannot see the older
  comment, or the delete returns 404, nothing further is done.
- Given the pipeline runs with `context.dry_run`, Then no GitHub write is
  made.

**US-W5 — Nothing sensitive or active leaves in the comment**
As an operator, I want walkthrough text scrubbed before it posts, so that an
LLM echoing the diff can't leak a secret or smuggle active content.
- Given any text bound for the comment (summary, group names/descriptions,
  file descriptions, file paths), Then it passes the existing secret redactor
  (`findings.redact`) before rendering, exactly as finding text does. This is
  best-effort, as it is for findings: a secret split across fields or
  paraphrased by the model may survive.
- Given any string containing an inline `gitleaks:allow` marker (which the
  secret scanner honors by skipping the line), Then the marker is removed
  before redaction, so it cannot be used to smuggle a secret through.
- Given `secret_redaction_enabled=false` (the redactor is the no-op), Then no
  walkthrough is posted: the step is not effective, and startup logs that it is
  enabled but ineffective.
  (`intent_summary` was never posted before, so it had no redaction pass; this
  is the first one.)
- Given text containing markdown/HTML links, images, raw HTML, `@mentions`,
  `#123`/`owner/repo#123` references, or code fences, Then the rendered
  comment contains none as active markup: links/images/HTML are rendered as
  inert text, and mentions, issue references (`#123`, `owner/repo#123`,
  `GH-123`), commit-SHA-like hex runs, bare URLs and email addresses in
  prose fields are broken, and control and Unicode format characters (including
  bidi overrides) are removed, so GitHub neither
  notifies nor creates a cross-reference (backslash escaping alone does not
  achieve this, because GitHub links references on the rendered text), and file paths are shown as inline code.
- Given any text not from the model, Then it is passed through
  `escape_markdown`, matching `adapter/github/posting.py`'s existing posture.
- Given model-supplied names, descriptions or notes containing newlines or
  other control characters, Then all whitespace runs are collapsed to single
  spaces, so a string can never inject a heading, a second marker or a fake
  footer.
- Given a file path containing backticks, newlines or markup, Then it renders
  inside a code span whose delimiter is longer than the longest backtick run
  in the path, with control characters removed.
- Given any walkthrough comment, Then its footer states, in fixed text, that
  it is an AI-generated summary and not a review verdict. The model cannot
  alter this text.

**US-W6 — The comment stays readable on huge PRs**
As a PR reviewer, I want a bounded comment, so that a 400-file PR doesn't
produce an unreadable wall.
- Given the rendered comment would exceed `walkthrough_max_chars` (default
  6000), Then it is cut at a group boundary with a final line "N more files
  not shown", and the comment never exceeds GitHub's 65,536-character limit.
- Given the Summary plus the first group alone exceed the limit, Then that
  group's file list is truncated to fit (Summary and group header always
  kept), and the remaining files are counted in the same "not shown" line.
- Given more than `walkthrough_max_files` (default 60) files, Then only that
  many are sent to the model; the rest are counted in the same "not shown"
  line.

**US-W7 — The model call is separate, narrow and untrusted**
As an operator, I want the walkthrough call isolated, so that PR content can't
steer anything beyond the text of the comment.
- Given the step runs, Then exactly one LLM Gateway call is made, distinct from
  every other pipeline call, routed under the model-routing stage `walkthrough`.
- The call's input is limited to the bounded list of file paths with statuses
  and per-file change counts, and a size-capped slice of each file's patch;
  never the findings, other files' contents, product-knowledge context or
  repository secrets. The diff is already sent to the endpoint by
  `intent_summary`; no new data class leaves the deployment.
- The required output is a fixed structured shape (groups: name, description,
  ordered file paths); anything else is a parse failure (US-W8). PR content is
  delimited as data and the output is never executed or followed as
  instructions.

**US-W8 — A walkthrough problem never costs the author a review**
As a PR author, I want my findings even if the walkthrough fails, so that an
optional extra never blocks the core review.
- Given the LLM call times out, errors, or returns an unparseable shape, or
  the GitHub create/edit call fails, When the review runs, Then findings
  inline comments and the check-run post exactly as with the step disabled.
- Given a regeneration fails on a later push while a walkthrough comment
  already exists, Then the existing comment is left unchanged (its footer
  names the older commit, so staleness is visible).
- Given the step timeout is `walkthrough_step_timeout_seconds` (default 45),
  Then the LLM call uses the existing `llm_timeout_seconds` and the step's
  whole inner work (input validation, LLM call, publish) runs under a wait of
  that value minus 5 s (the rule `fix_generation` uses), so the step always
  returns before the framework's timeout and at least 10 s remain for
  publishing after a slow LLM call. On expiry the step passes its input
  through unchanged with `outcome=llm_timeout` (or `github_error` if the LLM
  call had finished). A timeout is a skipped step, not a failed job. A
  timeout mid-publish may leave a duplicate comment, which the next push
  converges (US-W4).
- Given any other exception, including one raised while validating the step's
  input, Then the step passes its input through unchanged with
  `outcome=internal_error`; no exception escapes the step.

**US-W9 — The operator can switch the feature off**
As an operator, I want a kill switch, so that I can bound spend.
- Given `walkthrough_enabled=false` (the default), Then no LLM call, no GitHub
  read/write, the step is absent from the chain, and all posted output is
  byte-identical to the previous release.

**US-W11 — A misconfigured Worker refuses to start**
As an operator, I want bad walkthrough settings rejected at startup, so that I
find out at deploy time rather than on a customer's PR.
- Given `walkthrough_enabled=true` and any of: `walkthrough_min_files` < 1,
  `walkthrough_max_files` outside 1–200, `walkthrough_max_chars` outside
  500–60000, or `walkthrough_step_timeout_seconds` less than
  `llm_timeout_seconds` + 15, Then the Worker exits at startup with an error
  naming the offending setting.
- Given `walkthrough_enabled=false`, Then the companion settings are not
  validated (a stale value never blocks a rollback).

**US-W12 — The operator can see what the feature is doing**
As an operator, I want per-outcome visibility, so that I notice failures and
spend.
- Given every walkthrough outcome (posted, updated, skipped_small_pr,
  skipped_comment_limit, llm_timeout, parse_failure, github_error,
  dedupe_failed, internal_error), Then one counter increments with that `outcome`; call
  duration (labelled by phase: `llm`, `publish`) and rendered size are
  recorded; the dashboard has a panel for each. `dedupe_failed` means the
  comment was posted but a duplicate may remain. `internal_error` is the
  catch-all for any other exception the step swallows (US-W8), logged with the
  failing stage and exception type, never text.
- Given any outcome, Then one structured log line carries `job_id`,
  `pr_number`, `head_sha`, `outcome`, `files_in`, `files_shown` and `chars`
  (never the text), so an operator can find the PR behind a failure. Given
  `outcome=internal_error`, Then the line also carries `stage` (one of
  `validate`, `prompt`, `llm`, `parse`, `redact`, `sentinel`, `render`,
  `publish`) and `exc_type`.
- Given a telemetry-recording failure, Then it is logged and the step
  continues (ADR-0012).

**US-W10 — Model routing**
As an operator, I want the walkthrough on a model I choose, so that I can use a
cheap model for it.
- Given the Admin UI's model-routing screen, Then `walkthrough` appears as a
  stage row; with no row configured the deployment default model is used.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Latency:** one added LLM call; the step timeout default is 45 s and is
  accounted in the per-job step-timeout arithmetic documented in
  `docs/reference/operations.md`. **Assumption:** a sequential position in the
  existing linear chain is acceptable for a text-only call; if pilot latency
  proves unacceptable the step moves to a concurrent branch (a pipeline-
  framework change, out of this phase).
- **Cost:** at most one walkthrough call per reviewed push, per PR above the
  minimum; bounded input (US-W6/US-W7). No per-finding fan-out.
- **Security:** untrusted-content posture is US-W5/US-W7. Comment identity
  (App author plus marker) is US-W4. No new GitHub permission is expected
  (D7). Public-repo PRs: comment content is derived from content already
  public, but the diff still goes to the BYO endpoint exactly as in
  `intent_summary`.
- **Reliability:** fail-open (US-W8); idempotent re-run on the same commit
  edits rather than duplicates.
- **Scale:** comment listing reads at most 5 pages of 100 comments (D6, D8);
  a PR with more comments than that creates nothing new and records
  `skipped_comment_limit` (US-W4), never a duplicate.

### Privacy / Compliance

Fork PRs are in scope: the comment is posted on the base repository's PR using
the repo and PR number from the trusted job identity, never from the fork, and
every control above applies unchanged.

No new data class leaves the deployment: the walkthrough call sends diff-
derived text (path list plus capped patch slices) to the same customer-chosen
LLM endpoint `intent_summary` already uses, with the same posture: the raw
diff goes to the endpoint exactly as `intent_summary` sends it today, and
redaction applies to everything posted back. Walkthrough text is not logged in full (length and counts only),
matching `intent_summary`'s posture. Document this in the runbook's "what is
sent where" section.

### Observability

Counters, duration and size metrics per US-W12; structured logs with
`job_id`, `outcome`, `files_in`, `files_shown`, `chars` (never the text); OTel
span on the step (ADR-0011). A Grafana panel set added to the existing
dashboard ConfigMap, and E2E traffic that exercises every new panel (Phase 7's
`test_observability` failure shows panels with no test traffic break the
suite).

### Rollout & Release Safety

Ships `walkthrough_enabled=false`. No migration, no schema change, no new
table. Rollback is the setting. An operator can find any walkthrough comment by its marker and App identity and delete it (runbook). Enabling on a live deployment affects only
subsequent pushes; existing PRs get a walkthrough on their next review.
Disabling leaves already-posted walkthrough comments in place (they are not
deleted by the platform). Helm values and Compose env mirror the setting, and
`charts/review/test-render.sh` must render with it on and off.

## UX & Design

N/A as a review-owned UI: the artifact is a GitHub PR comment, rendered by
GitHub. Design-system consistency is therefore N/A. The comment's own design:

- **Flow:** first push → comment appears; later pushes → same comment updates;
  its footer shows the head commit and "Updated for <short sha>".
- **Structure (fixed, top to bottom):** title line, Summary, grouped file list
  (group name, one-line description, files as inline code with a one-line
  note each), "not shown" line if truncated, footer.
- **States:** no summary (omitted section), small PR (no comment), truncated
  PR (bounded with a count), failure (nothing new posted; older comment
  stays, footer shows its commit), disabled (no comment).
- **Accessibility:** plain markdown headings and lists only (screen-reader
  friendly in GitHub's renderer); no emoji-only meaning, no tables for layout,
  no images.
- **Responsive:** inherits GitHub's own responsive rendering; no wide tables
  or code blocks.
- **Perceived performance:** the step runs before `output`, so findings post
  at most `walkthrough_step_timeout_seconds` later than without it (typically
  one LLM call). The walkthrough is never a precondition for findings: every
  failure is fail-open (US-W8).

## Stakeholders & Sign-off / Acceptance Criteria

Owner and sole engineer: Andy Beak. Sign-offs needed: ADR-level review not
required (no architecture deviation); security review of US-W5/US-W7 posture
(self-review against the Phase 11 security checklist is the precedent). Each
requirement's acceptance criterion is its `Given/When/Then` above; the phase
is done when its traceability-table tests exist and pass, `ruff`, `mypy
--strict`, unit, integration and E2E gates pass, and the runbook and
documentation index are updated.

## Phases / Units

- **A — Walkthrough model, sanitizer & renderer.** Depends on: nothing.
  Risk: low (pure code). Decided: structured output shape, sanitization
  rules. Acceptance: US-W2, US-W5, US-W6 unit-level plus their integration
  tests below.
- **B — Comment delivery.** Depends on: A. Risk: medium (GitHub issue-comment
  list/create/edit, marker/author identification, pagination bound — new
  adapter code, though on an existing posting path). Assumption: D6, D7.
  Acceptance: US-W4.
- **C — Step, intent-summary carry, settings, chain, routing.** Depends on:
  A, B. Risk: medium (adds a field to three step outputs and a new chain
  position; the byte-identical-when-disabled guarantee must hold). Assumptions:
  D4, D5. Acceptance: US-W1, US-W3, US-W7, US-W8, US-W10.
- **D — Ops, observability, runbook, docs.** Depends on: C. Risk: low.
  Acceptance: US-W9, US-W11, US-W12.
- **E — E2E.** Depends on: C, D. Risk: low. Acceptance: the goal end to end.

## Resolved decisions

| ID | Decision | Tag |
| --- | --- | --- |
| D1 | Supplement, not replace: one extra top-level comment; inline findings, review body and check-run untouched (preserves the byte-identical-when-disabled guarantee). | Assumption pending validation |
| D2 | Deployment-wide `walkthrough_enabled` bootstrap setting, default **false**. Conflict surfaced: the roadmap says "config toggle per org/repo", but every `*_enabled` flag (incl. `fix_generation_enabled`) is a deployment-wide Worker setting and `spec-auto-fix.md` rules out admin controls. Resolved by the interviewer: deployment-wide now; per-repo override via `OrgRepoTeamSetting` is a follow-up. | Decided |
| D3 | No Mermaid/diagram generation in this phase. | Decided |
| D4 | New step positioned after `fix_generation`, before `output`, in the existing linear chain; `resolve_review_pipeline_depends_on` extended. The step posts the comment itself (amended 2026-10-18 during planning): `output` is the fatal terminal step with a 30 s timeout, and walkthrough posting there would put an optional extra inside it. | Assumption pending validation |
| D5 | Roadmap says "post Phase 3's intent-summary output", but `IntentSummaryOutput` is not carried past `review_generation`, and `VerificationOutput`/`FixGenerationOutput` carry no `diff` (verified in code). Resolved by adding optional `intent_summary` and `reviewed_diff` fields forwarded unchanged through those outputs, rather than a second summarization call or a diff re-fetch. The diff field is named `reviewed_diff`, not `diff`, because `FixGenerationStep` fetches the diff when its mirrored `diff` is `None` (`fix_generation.py:350-353`); reusing `diff` would silently change Phase 11 behavior with the flag off. | Assumption pending validation |
| D6 | Update-in-place by listing the PR's issue comments (at most 5 pages of 100), matching hidden marker + App-authored; edit via PATCH. | Assumption pending validation |
| D7 | The App's existing `pull_requests: write` permission is sufficient to create, edit and delete its own PR issue comments; no manifest change. | Assumption pending validation (verify against a real installation before release) |
| D8 | `walkthrough_min_files` default 2; `walkthrough_max_chars` 6000; `walkthrough_max_files` 60; step timeout 45 s; comment listing 5 pages of 100. | Assumption pending validation (no pilot data) |
| D9 | Product-knowledge citation is out of scope. | Decided (`spec-product-knowledge.md`) |

## Testing / Key Scenarios

Levels per `docs/rules/testing.md`: story → integration test; pure logic also
unit-tested (ADR-0005 applies to the pure sanitizer/renderer).

| Story | Given / When / Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-W1 | Enabled, ≥2 files → one comment with Summary; no summary text → section omitted | `review/test_walkthrough_step.py` (new) | Integration (recorded LiteLLM cassette) |
| US-W2 | Unknown/duplicate/omitted paths repaired; full list, one group order | `review/test_walkthrough_render.py` (new), `review/test_walkthrough_step.py` | Unit + Integration |
| US-W3 | 1 reviewable file → no call, no write | `review/test_walkthrough_step.py` | Integration |
| US-W4 | Create then edit in place; foreign-authored marker ignored; two concurrent jobs converge to one comment; dry-run no write | `adapter/github/test_walkthrough_posting.py` (new) | Integration (`respx` cassette) |
| US-W5 | Secrets redacted; links/images/HTML/mentions/refs inert; escaping | `review/test_walkthrough_render.py`, `review/test_walkthrough_step.py` | Unit + Integration |
| US-W6 | Over-length cut at group boundary; oversize first group truncated; file cap and "not shown" | `review/test_walkthrough_render.py`, `review/test_walkthrough_step.py` | Unit + Integration |
| US-W7 | One routed call; bounded input; structured output | `review/test_walkthrough_step.py` | Integration |
| US-W8 | LLM timeout (call timeout = step timeout − 5 s) / bad shape / GitHub 5xx → findings and check-run unchanged; existing comment untouched | `review/test_walkthrough_step.py`, `adapter/github/test_walkthrough_posting.py`; failure mode also in `tests/e2e/test_pr_walkthrough.py` (new) | Integration + E2E |
| US-W9 | Disabled → step absent from chain, no LLM/GitHub call, output byte-identical | `review/test_walkthrough_step.py` | Integration |
| US-W11 | Bad settings fail startup naming the setting; disabled skips validation | `apps/worker/test_settings.py` | Unit (pure config validation, ADR-0005) |
| US-W12 | Every outcome counted; duration/size recorded; telemetry failure logged and ignored; all panels have data | `review/test_walkthrough_step.py`, `tests/e2e/test_observability.py` | Integration + E2E |
| US-W10 | `walkthrough` routing stage row resolves, default fallback | `apps/admin_api/test_model_routing_routes.py` | Integration |
| Goal | Push → comment appears; second push → same comment edited | `tests/e2e/test_pr_walkthrough.py` | E2E |

**Highest-risk edge cases covered above:** hostile diff content telling the
model to emit a link/image/@mention (US-W5); model inventing or dropping files
(US-W2); duplicate comment on rapid successive pushes (US-W4); forged marker
comment from another user (US-W4); GitHub comment-size limit (US-W6); and the
step failing while a prior walkthrough exists (US-W8).
