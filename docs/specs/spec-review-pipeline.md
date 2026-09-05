# Phase 1 — Walking Skeleton Review Pipeline PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-02

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 1 ("Walking skeleton: the
core review loop") into a concrete spec. Depends on
[`pipeline-step-framework.md`](pipeline-step-framework.md) for the execution
model each step below runs on — that document specifies *how* steps run;
this one specifies *which* steps exist and what they must do. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`); requirements are stated as user stories with
`Given/When/Then` acceptance criteria per `testing.md`'s mandatory rule.

## Problem & Goal

Phase 0 left the platform able to receive and verify a GitHub webhook, but
that's where it stops: `apps/webhook_api/webhook_routes.py`'s own docstring
states "no DB lookup, no persisted mapping, and no job enqueued yet." A
developer whose team has installed and deployed this platform today gets
**zero review value** — opening a PR does nothing.

**End user: the PR author** (and their reviewers), who today gets no
feedback at all from the platform they installed. Phase 1's entire purpose,
per the roadmap's exit criteria, is: *"a real PR against a real repo gets a
real (if unsophisticated) review posted by the platform."* Concretely: a
developer opens or pushes to a PR and, without any manual action from them
or their admin beyond having installed and configured the platform, sees
inline comments and a check-run on that PR.

**Secondary end user: the platform operator**, whose configured LLM
endpoint and enabled analyzers (see
[`pipeline-step-framework.md`](pipeline-step-framework.md)) must actually
get exercised for the PR author to see anything.

**Goal / success metric:** capability-based, matching the roadmap's exit
criteria exactly — a real PR against a real repo produces a real posted
review. No usage/adoption metric is set for Phase 1 by any project
document; this is walking-skeleton infrastructure, not a metered feature.

## Scope

### In scope

- **Installation → Org/Repo resolution.** Replace the current placeholder
  (webhook accepted, nothing resolved) with a real lookup: resolve the
  installation's Org/Repo rows, auto-provisioning them on first sight (no
  admin UI exists yet to pre-register a repo — Phase 5 territory).
- **Trigger events (decided).** `pull_request` webhook events with action
  `opened`, `synchronize` (new commits pushed), or `reopened` enqueue a
  `REVIEW` job — the minimal set covering "PR gets reviewed initially" and
  "PR gets re-reviewed on new commits," the two behaviors the exit criteria
  actually requires. No comment-triggered re-review (e.g. a `/review`
  slash-command) in Phase 1. Resolved as **implemented via a small
  declarative `(event_type, action) → trigger_decision` mapping**, not
  inline conditionals in the route handler — so adding a new trigger later
  (comment-triggered re-review, a `push`-to-default-branch trigger, etc.)
  is one new table entry, not a rewrite of `webhook_routes.py`'s logic.
  This is the same discrete/extensible posture the step framework applies
  to pipeline stages, applied one layer earlier, at the trigger-resolution
  boundary.
- **Idempotent enqueue.** A redelivered webhook (same GitHub delivery ID)
  must not create a duplicate job.
- **Worker job dispatch.** Claim `REVIEW` jobs from the existing
  `job_queue` (`adapter/storage/queue.py`, already built in Phase 0) and run
  the review pipeline against each claimed job — replacing the Worker's
  current "idle: no job dispatch wired yet" state.
- **Ingestion (bare diff).** Fetch the PR's changed files/patches via the
  GitHub API using the installation token. No AST parsing, no index, no
  Context Builder — that's Phase 2's job; Phase 1's diff is intentionally
  bare, per the roadmap.
- **Pre-filter step.** Exclude changed files that shouldn't reach static
  analysis or the LLM (default ignore patterns: common lockfiles, vendored
  paths, generated files, binary files).
- **Static-analysis step.** Gitleaks (secret scanning) + Semgrep
  (auto/default ruleset) run as independent, concurrent sub-steps (per
  `pipeline-step-framework.md`) against the changed files; output
  normalized into the findings domain model. Chosen because both are
  language-agnostic (don't require assuming the reviewed repo's language,
  unlike e.g. Ruff/Bandit) and mirror the research doc's documented
  vendor-convergent pattern (§2.2) of a deterministic first pass as the
  free trust anchor (design goal 4).
- **LLM-review step.** Exactly one LLM Gateway completion call (via the
  already-built `llmgateway.client.LLMClient` Protocol /
  `adapter/llm/litellm_client.LiteLLMClient`), given the bare diff and the
  static-analysis findings as context, forced into a structured pydantic
  output schema (file, line, severity, message) — per the research doc
  §2.3's structured-output pattern. No verification/self-critique pass —
  that's Phase 3, and design goal 6 explicitly rules out self-critique as
  ineffective regardless.
- **Findings-merge-and-output step.** Dedupe/combine static + LLM findings
  into one list; post as inline PR review comments and one check-run via
  the GitHub Adapter.
- **GitHub Adapter: new write capability.** A PR-comment-posting client and
  a check-run create/update client — net new; today `adapter/github` only
  verifies webhook signatures and handles the manifest/callback flow, no
  posting capability exists.
- **Dry-run mode.** A bootstrap config flag that runs the full pipeline
  (including posting-step logic) but suppresses the actual GitHub write
  calls, logging what would have been posted instead — see Rollout &
  Release Safety.

### Out of scope (deferred to later roadmap phases)

- Real indexing/RAG / Context Builder — Phase 2.
- A structurally-separate verification pass — Phase 3. Phase 1's LLM
  findings are posted directly, unverified; this is a deliberate, stated
  trade-off of "walking skeleton," not an oversight.
- Memory / continual learning, dismissed-finding suppression — Phase 4.
- Admin UI, DB-backed day-two step/org/repo/team configuration — Phase 5
  (also out of scope per `pipeline-step-framework.md`).
- Multi-org RBAC beyond simple auto-provisioned Org/Repo rows — Phase 6.
- Knowhow augmentation — Phase 7.
- **Comment-triggered re-review** (e.g. a `/review` slash-command like
  PR-Agent's) — not part of the roadmap's Phase 1 scope; only the PR
  lifecycle events listed above trigger a review.
- **Severity-based / blocking check-run conclusions.** Confirmed decision:
  the check-run always completes as informational ("neutral"), regardless
  of what's found — Phase 1 has no verification/confidence pipeline to
  justify blocking a merge on unverified single-pass output.

### Priority

- **Must have:** the whole webhook → enqueue → claim → pre-filter →
  static-analysis → llm-review → findings-merge-and-output → GitHub-posted
  loop — this collectively *is* the roadmap's exit criteria, not divisible
  further without failing it. Idempotent enqueue is Must (not
  Should/Could): without it, the very first flaky network blip in a real
  deployment double-posts and double-spends LLM calls on a customer's real
  PR, which is a bad first impression for a walking skeleton whose whole
  point is to be trusted enough to build on.
- **Should have:** dry-run mode (materially reduces first-deployment risk,
  but the roadmap's exit criteria is satisfiable without it — an operator
  could technically just test on a throwaway repo first).
- **Could have:** operator-configurable pre-filter ignore patterns (vs. a
  fixed default list) — a fixed sane default still satisfies the exit
  criteria.

## Requirements

Stated as user stories per `testing.md`'s mandatory format.

**US1 — Automatic review on PR open/update**
As a PR author, I want the platform to start reviewing my PR the moment I
open it or push new commits, so that I get feedback without asking anyone
to trigger it.
- Given a verified GitHub webhook payload for a `pull_request` event with
  action `opened`, `synchronize`, or `reopened` from a known installation,
  When the Webhook API processes it, Then the installation's Org/Repo is
  resolved (auto-provisioned if new) and exactly one `REVIEW` job is
  enqueued with the correct org/repo and PR metadata in its payload.

**US2 — No duplicate reviews from webhook redelivery**
As a platform operator, I want a redelivered webhook to never create a
second review job, so that I'm not billed twice for LLM calls or spamming
the PR author with duplicate comments.
- Given the same GitHub delivery ID is received twice, When both deliveries
  are processed, Then only one `REVIEW` job exists for that delivery.

**US3 — Irrelevant files don't reach review**
As a platform operator, I want generated/vendored/lockfile changes excluded
from review automatically, so that my team isn't shown noise on files no
human wrote by hand.
- Given a PR's changed-file list includes both `package-lock.json` and
  `app.py`, When the pre-filter step runs with default ignore patterns,
  Then only `app.py` proceeds to static analysis and the LLM step.

**US4 — Static analysis runs as the free, deterministic first pass**
As a PR author, I want deterministic checks (secrets, common security
patterns) run on my changes, so that I get the free, cheap-to-trust
findings that don't depend on any LLM's judgment.
- Given a claimed `REVIEW` job for a PR with a real diff, When the
  static-analysis step runs, Then Gitleaks and Semgrep both run against the
  changed files and their findings are normalized into the findings domain
  model.

**US5 — LLM review runs using static findings as context**
As a PR author, I want an LLM pass that also sees what static analysis
already found, so that its own review is grounded rather than starting
from nothing.
- Given the static-analysis step's output for a claimed job, When the
  llm-review step runs, Then it makes exactly one LLM Gateway completion
  call supplying the bare diff plus the static-analysis findings as
  context, and parses the response against the fixed structured findings
  schema (file, line, severity, message).

**US6 — Findings are posted as real, visible PR feedback**
As a PR author, I want the platform's findings to show up as normal inline
PR review comments, so that I can read and respond to them exactly like
any other reviewer's comments.
- Given static-analysis and LLM findings for a claimed job, When the
  findings-merge-and-output step runs, Then a deduplicated combined list is
  posted as inline PR review comments via the GitHub Adapter.

**US7 — Positive confirmation the review ran, even with nothing to report**
As a PR author, I want to know the platform actually reviewed my PR even
when it found nothing, so that silence never leaves me guessing whether the
tool is broken or just had nothing to say.
- Given a review job produces zero findings, When the
  findings-merge-and-output step runs, Then a check-run is still posted
  with conclusion "neutral" and a summary stating no findings were posted
  (zero inline comments).
- Given a review job produces one or more findings, When the
  findings-merge-and-output step runs, Then a check-run is posted with
  conclusion "neutral" and a summary stating how many findings were posted
  — Phase 1 never fails or blocks a check-run based on findings (see Scope
  → Out of scope).

**US8 — Failures are visible, not silent**
As a PR author, I want to be told if the platform's review itself broke,
rather than seeing nothing at all, so that I don't mistake a broken review
for "no issues found."
- Given the llm-review or findings-merge-and-output step fails
  unrecoverably (per `pipeline-step-framework.md`'s US-F6), When the
  Worker handles the failed job, Then it marks the job `FAILED`, posts no
  inline PR comments, and posts a check-run with a failure/error
  conclusion so the PR author sees that the review did not complete.

**US9 — One broken analyzer doesn't cancel the whole review**
As a PR author, I want a single failing analyzer to not zero out my entire
review, so that one flaky tool doesn't cost me all the value of the pass.
- Given one analyzer (e.g. Semgrep) raises an exception mid-run, When the
  pipeline completes, Then the review still finishes using the remaining
  analyzer's (Gitleaks') findings plus the LLM findings, and the posted
  comments/check-run reflect that partial static-analysis result.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- Every new I/O call this PRD introduces (GitHub comment/check-run API
  calls, GitHub diff-fetch call) takes an explicit timeout, per
  `architecture.md`.
- No latency SLA is set for Phase 1 by any project document — the exit
  criteria is correctness of the loop, not speed; a real target belongs to
  Phase 8 (production hardening) once there's a real deployment to measure
  against. Not inventing one here.
- **Security:** the new GitHub write capability (comment/check-run posting)
  reuses the existing GitHub App installation-token exchange — no new
  credential type is introduced. Analyzer execution security (bounded
  resources, no execution of PR-supplied content as code) is specified in
  `pipeline-step-framework.md`'s NFRs, inherited here unchanged.
- **GitHub API rate limits:** a single review posts one comment per finding
  plus one check-run — for a large diff with many findings this is
  multiple write calls per PR. The GitHub Adapter's posting client SHALL
  batch inline comments into a single PR review submission (one API call
  creating a review with multiple comments) rather than one API call per
  comment, both to reduce the chance of hitting GitHub's secondary rate
  limits and because it's the natural GitHub API shape for "post several
  comments at once." No additional cross-review rate-limiting is specified
  for Phase 1 beyond this — GitHub's own per-installation rate limits are
  the operative ceiling, and Phase 1's volume (walking-skeleton scale, one
  installation) is not expected to approach them.
- **Idempotency:** covered by US2.

### Privacy / Compliance

No new customer-data handling beyond what ADR-0001/ADR-0002 already
establish: PR diff content is sent to whichever LLM endpoint the operator
configured (bring-your-own-key, per the C4 doc's design goal 3) — the
operator already consents to this by configuring an endpoint at all;
nothing in this PRD changes that trust boundary.

### Observability

- Every step's start/completion/failure is logged with `job_id`, PR
  identity (org/repo/PR number), and outcome, extending the existing
  `apps/worker/logging_config.py` structured-logging setup.
- Webhook rejections/acceptances already log at `warning`/`info` per
  `webhook_routes.py`'s existing pattern (extended, not replaced, by US1's
  enqueue step).
- No metrics/dashboard system exists in this repo or is specified by any
  roadmap phase through Phase 1 — observability here is structured logs
  only, consistent with `pipeline-step-framework.md`.

### Rollout & Release Safety

- **Dry-run mode (decided)** (Should-have, per Scope): a single bootstrap
  config flag (`REVIEW_WORKER_DRY_RUN`) — not per-step granularity. Per-step
  dry-run was considered and rejected: in Phase 1 exactly one step
  (findings-merge-and-output) has an external side effect to suppress, so
  N per-step flags would mean N-1 flags that never do anything, which is
  the "no interface without a second real implementation already in view"
  problem (`architecture.md`'s simplicity rule) applied to config surface
  instead of code. The flag stays extensible without becoming per-step: it
  is carried on the shared execution context every step's `run()` call
  receives (see `pipeline-step-framework.md`'s `Step` Protocol), not
  special-cased only inside `review/output.py`'s own logic. A future
  side-effecting step (e.g. a Phase 4 memory write, a notification step)
  can honor the same global flag by checking that context, with zero new
  config surface added per step — the single boolean is the right MVP
  scope, and the context-threading is what keeps it a general mechanism
  rather than a one-off escape hatch.
- **Backwards compatibility:** `webhook_routes.py`'s current behavior
  (accept and log, never enqueue) is being replaced with real enqueue
  behavior — this is additive from the caller's perspective (GitHub sees no
  API contract change, still gets 200/401/413/422/429 the same way) but is
  a real behavior change for anyone who deployed Phase 0's build already;
  flagged here so it's a deliberate, known change at rollout, not a
  surprise.
- **No migration required:** no existing review data or posted comments
  exist to migrate — this is new capability, not a change to existing
  stored state.

## UX & Design

No web UI exists in Phase 1 (Admin UI is Phase 5) — but posted GitHub PR
comments and check-runs *are* a real, directly user-facing surface (GitHub's
own UI renders them), so this is not N/A.

- **Primary flow:** PR author opens/pushes a PR → within some bounded time
  (no SLA set, see NFRs) → inline comments appear on the relevant
  lines/files, and a check-run appears in the PR's checks list.
- **Empty/no-findings state:** a check-run still posts with a clear
  "no findings" summary (US7) — never silence, which would read as broken
  rather than clean.
- **Error state:** a failure posts a check-run with a failure conclusion
  and a human-readable reason, never silence and never a garbled partial
  comment (US8).
- **Content correctness:** comment bodies must be valid, safely-escaped
  Markdown (fenced code blocks, no unescaped user/PR content that could
  break GitHub's renderer) — the platform's only "UI" surface in Phase 1 is
  the text it writes into GitHub's own UI.
- **Accessibility/responsiveness:** inherited entirely from GitHub's own PR
  UI — nothing to design here.
- **Performance as UX:** no quantified perceived-performance target is set
  (see Non-Functional Requirements → Performance — deliberately not
  fabricated), but the *shape* of the experience is specified: findings
  are posted as one batched PR review (see the GitHub API rate-limit NFR
  above) rather than trickling in comment-by-comment, so the PR author
  sees the review appear as one coherent event rather than a slow drip of
  individual comments.
- **Consistency with a design system:** N/A — no component library
  involved; this is API-driven content, not rendered UI.

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author, `R`/`A` on every existing
  ADR's RACI table; no separate design/eng/QA/legal stakeholders exist for
  this project at this stage.
- **Acceptance:** every `US*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios); the roadmap's own exit
  criteria — "a real PR against a real repo gets a real (if
  unsophisticated) review posted by the platform" — is demonstrated by the
  E2E scenario below against the Docker Compose stack; `mypy --strict`/
  `ruff` clean per `architecture.md`.

## Phases / Units

Builds on [`pipeline-step-framework.md`](pipeline-step-framework.md)'s Unit
C (the step framework itself), which is a hard prerequisite for Units D–G
below. Units A/B have no dependency on Unit C and can be built in parallel
with it.

- **Unit A — Installation/Org/Repo resolution + idempotent enqueue**
  (`webhook_routes.py` changes). Depends on: nothing new (storage adapter
  already exists from Phase 0). Risk: low — mechanical DB lookup /
  insert-if-missing plus a delivery-ID uniqueness check. **Acceptance:**
  US1 and US2's `Given/When/Then` both pass as integration tests.
- **Unit B — Worker job dispatch loop** (claim + dispatch to pipeline).
  Depends on: nothing beyond the existing `job_queue` claim logic;
  testable with directly-enqueued test jobs independent of Unit A. Risk:
  low. **Acceptance:** a directly-enqueued `REVIEW` job is claimed and
  handed to the step registry from `pipeline-step-framework.md`; job status
  transitions `PENDING` → `CLAIMED` → `DONE`/`FAILED` correctly.
- **Unit C — Pipeline step framework.** See
  [`pipeline-step-framework.md`](pipeline-step-framework.md). Risk: medium
  (genuinely new architectural synthesis, no direct precedent in-repo).
  **Acceptance:** that document's own US-F1–US-F7 acceptance criteria.
- **Unit D — Pre-filter step.** Depends on: Unit C. Risk: low — pure
  filtering logic, no I/O. **Acceptance:** US3's `Given/When/Then` passes
  as a unit test.
- **Unit E — Static-analysis step + Gitleaks/Semgrep adapters.** Depends
  on: Unit C. Risk: medium — first subprocess-exec-against-untrusted-input
  code in this repo; tool-output normalization for two different tools.
  **Acceptance:** US4 and US9's `Given/When/Then` both pass as integration
  tests against real analyzer binaries on fixtures.
- **Unit F — LLM-review step.** Depends on: Unit C; reuses the already-built
  `llmgateway.client.LLMClient`/`LiteLLMClient` from Phase 0. Risk:
  low-medium — the LLM Gateway plumbing already works; what's new is
  prompt/schema design for structured findings output. **Acceptance:** US5's
  `Given/When/Then` passes as an integration test against a recorded
  LiteLLM cassette.
- **Unit G — Findings-merge-and-output step + GitHub Adapter posting
  client.** Depends on: Unit C, E, F (needs their output shapes finalized).
  Risk: medium — the first code in this repo that writes to GitHub's API
  (today `adapter/github` only verifies/reads); this is also the unit that
  proves the roadmap's exit criteria end-to-end, so it carries the most
  product risk if the shape of a "good enough" posted comment is wrong.
  **Acceptance:** US6, US7, and US8's `Given/When/Then` all pass as
  integration tests, plus the full-stack E2E scenario in Testing / Key
  Scenarios passes — this is the unit that satisfies the roadmap's Phase 1
  exit criteria.

## Testing / Key Scenarios

Per `testing.md`'s pyramid and its explicitly-named Phase 1 failure modes
("analyzer crash, LLM timeout, no findings, malformed webhook signature").

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US1 | A verified `pull_request opened` webhook resolves org/repo and enqueues exactly one `REVIEW` job | `apps/webhook_api/test_webhook_routes.py` (extended) | Integration (real Postgres via testcontainers) |
| US2 | The same delivery ID processed twice yields exactly one job | `apps/webhook_api/test_webhook_routes.py` (extended) | Integration |
| US3 | `package-lock.json` is filtered, `app.py` is not | `review/test_prefilter_step.py` | Unit |
| US4 | Gitleaks + Semgrep both run and their findings are normalized | `review/test_static_analysis_step.py` (shared with `pipeline-step-framework.md`'s suite) | Integration (real analyzer binaries on fixtures, per `testing.md`'s adapter/analyzers convention) |
| US5 | One LLM Gateway call is made with diff + static findings as context; response parses against the structured schema | `review/test_llm_review_step.py` | Integration (recorded LiteLLM cassette) |
| US6 | Combined findings are posted as inline PR comments | `review/test_findings_output_step.py` | Integration (recorded GitHub cassette via `respx`) |
| US7 | Zero-finding job still posts a "no findings" neutral check-run; non-zero-finding job posts a neutral check-run summarizing count | `review/test_findings_output_step.py` | Integration |
| US8 | An LLM Gateway timeout marks the job `FAILED`, posts no comments, posts a failure check-run | `review/test_review_job.py` | Integration |
| US9 | A raising analyzer doesn't prevent the review from completing with the other analyzer's + LLM's findings | `review/test_review_job.py` | Integration |
| US1–US9 (happy path) | Full Docker Compose stack, synthetic GitHub webhook payload against a stubbed GitHub endpoint, asserts the stub receives the expected comment/check-run API calls | `tests/e2e/test_review_pipeline.py` (new) | E2E |
| US8 (malformed signature, regression) | A malformed webhook signature is still rejected 401 and never reaches enqueue logic | `tests/e2e/test_review_pipeline.py` | E2E |

## Resolved decisions (formerly open assumptions)

Both items previously flagged as open assumptions pending validation are
now decided — see Scope's "Trigger events (decided)" bullet and
Non-Functional Requirements → Rollout & Release Safety's "Dry-run mode
(decided)" bullet above for the resolution and reasoning. Both were
resolved in favor of the smallest MVP-scoped surface that stays genuinely
extensible (a declarative trigger-mapping table; a single dry-run flag
threaded through a shared step-execution context) rather than either
over-building now or leaving a hardcoded shape that would need a rewrite
later.
