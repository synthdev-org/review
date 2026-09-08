# Phase 3 — Verification Pipeline PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-07

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 3 ("Verification /
multi-pass pipeline") into a concrete spec: *"Multi-pass pipeline: intent
summary → review generation → verification. Verification runs as a distinct
call against a fixed rubric — never self-critique. Findings merger: dedupes,
applies confidence/severity, drops low-value findings before a human sees
them."* Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`); requirements are stated as user stories with
`Given/When/Then` acceptance criteria per `testing.md`'s mandatory rule.

**One PRD, not several — unlike Phase 2's indexing/context-builder split.**
The three roadmap bullets above don't have an independent-value seam the way
indexing (zero standalone value) vs. context-builder (the actual value)
did: intent-summary, review-generation, and verification are one causal
chain producing a single observable outcome for the PR author — a review
that's verified and deduped, not raw single-pass model output — and the
"how do steps execute" framework question is already solved by
[`spec-pipeline-step-framework.md`](spec-pipeline-step-framework.md), which
this spec adds steps to rather than extends.

**Extends, not replaces, three already-shipped modules.** This is a
targeted extension of working Phase 1/2 code, not new architectural
synthesis:
- `review/llm_review.py`'s `LLMReviewStep` — split into two macro steps
  (`intent_summary`, `review_generation`), with `review_generation`
  otherwise unchanged from today's `llm_review` (same structured-findings
  schema, same redaction, same diff-truncation, same context-bundle prompt
  section).
- `findings/merge.py`'s `merge_findings` — already dedupes and applies a
  posted-findings ceiling (`POSTED_FINDINGS_CEILING`); this spec adds a
  verification-status filter before dedupe and a severity/trust ordering to
  the existing ceiling truncation, not a new merge mechanism.
- `review/output.py`'s `FindingsOutputStep` — unchanged; it already accepts
  whatever `depends_on` step the composition root points it at
  (`review/output.py:190-219`'s existing fallback pattern), so it needs no
  changes to consume `verification`'s output instead of `llm_review`'s.

## Problem & Goal

**End user: the PR author.** Per `spec-review-pipeline.md`'s own honest
framing, Phase 1 shipped with a stated, deliberate gap: *"No
verification/self-critique pass — that's Phase 3, and design goal 6
explicitly rules out self-critique as ineffective regardless"*
(`review/llm_review.py`'s current docstring says the same). Today, every
`source="llm"` finding `LLMReviewStep` produces is posted directly —
whatever the single review-generation call says, unverified, reaches the PR
author's inbox. [`research-current-art.md`](../research/research-current-art.md)
documents this as the single most consistent, hard-won lesson across four
independent vendors (Greptile, Sourcery, Qodo, CodeRabbit): ~79% of one
vendor's own posted comments were technically-true-but-unwanted noise, and
asking the *same* model to re-rate its own output was "nearly random" —
what worked was a structurally separate second call against a fixed rubric.
[C4 doc design goal 6](../reference/c4-architecture.md) commits this
platform to exactly that fix, and design goal 9 not yet aside, this is the
platform's first implementation of it.

**Concretely:** a PR author today gets every LLM finding regardless of
whether it's actionable, specific, or valuable — some of it noise. After
this spec, they get fewer, verified findings, and the review itself is
grounded in a stated summary of what the PR is trying to do rather than a
diff read in isolation.

**Secondary end user: the platform operator**, whose BYO-key LLM spend goes
from one completion call per review (Phase 1/2) to three — bounded,
predictable growth (never scaling per-finding, see Scope/NFRs below), which
the operator can still opt out of per-step, exactly as they can today with
`static_analysis`/`llm_review`.

**Goal / success metric, capability-based (matching every prior phase's own
stance — no fabricated adoption metric):** for a review job that produces
one or more LLM-sourced findings, every one of them has been judged against
the fixed rubric by a structurally separate call before any of them reach
`findings-merge-and-output`; a finding that fails the rubric is never posted;
static-analysis findings are unaffected. Acceptance is behavioral (proven by
the tests in Testing / Key Scenarios below), not a fabricated
quality/precision number no document has measured yet.

## Scope

### In scope

- **`intent_summary` macro step (new).** Given the same diff (and, if
  static-analysis/context-builder ran, their forwarded output) that today's
  `llm_review` step receives, makes exactly one LLM Gateway completion call
  producing a short natural-language summary of what the PR is trying to
  accomplish. Purely internal — forwarded to `review_generation` as a new,
  clearly labeled prompt section; never posted, never logged in full (only
  presence/length, matching the existing convention of never logging full
  diff content).
- **`review_generation` macro step (renamed/extended `llm_review`).**
  Identical to today's `LLMReviewStep` (`review/llm_review.py`) — same
  structured-findings JSON schema, same diff-truncation ceiling, same
  redaction-before-forwarding, same context-bundle prompt section — with one
  addition: the intent summary is included as its own labeled prompt
  section alongside the diff, static findings, and context bundle.
- **`verification` macro step (new) — one batched call per review, never
  one per finding (decided).** Given `review_generation`'s output, makes
  exactly one LLM Gateway completion call — structurally separate from the
  call that generated the findings, never a follow-up in the same
  conversation, per design goal 6 — supplying every `source="llm"` finding
  from that output (file, line, message, any `context_ref_ids`) plus the
  diff and context bundle, and parses a per-finding verdict from the
  response. One call regardless of finding count keeps LLM spend bounded
  and predictable (see NFRs), and matches Sourcery's own documented
  "second, separate LLM call that screens the whole draft" shape
  (`research-current-art.md` §2.3) rather than CodeRabbit's/PR-Agent's
  per-item scoring, for which this codebase has no existing per-job-fan-out
  precedent.
- **Fixed rubric (decided): Sourcery's four criteria** — valid-to-the-code,
  actionable, specific, valuable-to-the-author — each a binary pass/fail
  gate; **all four must pass** for a finding to survive. Chosen because it
  is the one rubric `research-current-art.md` documents in concrete,
  reproducible detail (raised Sourcery's own usefulness from ~40% to ~60%),
  not an invented, untested alternative. Fixed in code for this phase — a
  customer-editable rubric is Admin-UI territory (Phase 5), not built here.
- **Static-analysis findings bypass verification entirely (decided).**
  Per design goal 4, Gitleaks/Semgrep findings are the deterministic, free
  trust anchor everything else gets checked against — they are never
  included in the rubric-judged batch and are forwarded through the
  `verification` step's output completely unchanged.
- **`Finding.verification_status` (new field): binary, not numeric
  (decided).** `Literal["verified", "rejected"] | None`, defaulting to
  `None`. Set to `"verified"` or `"rejected"` only for `source="llm"`
  findings that verification actually judged; stays `None` for every
  static-analysis finding and for any `source="llm"` finding produced while
  `verification_enabled=False` (see below). No numeric/graded confidence
  score — nothing in this codebase consumes a graded score yet, and a
  binary gate is sufficient to satisfy "drops low-value findings."
- **Rejected findings are dropped, not merely deprioritized.** A finding
  with `verification_status="rejected"` is excluded from the `verification`
  step's own output entirely — it never reaches `findings-merge-and-output`,
  is never posted, and is never counted in the check-run's finding count.
  The drop is logged (count + which of the four criteria failed, in
  aggregate) at `warning`, never at a level that fails the job — matching
  every other "capped and logged" drop pattern already in this codebase
  (`findings/merge.py`'s ceiling drop, `review/llm_review.py`'s malformed-item
  drop).
- **Findings-merger severity/trust ordering (decided — Should-have, see
  Priority).** `findings/merge.py`'s existing ceiling truncation
  (`POSTED_FINDINGS_CEILING`, currently a blind first-`N`-in-list-order
  slice after dedupe) is extended to sort by severity first
  (`critical > high > medium > low > info`), then static-analysis-sourced
  findings ahead of verified-LLM-sourced findings at equal severity (per
  design goal 4's stated trust hierarchy — static findings are at least as
  trustworthy as a verified LLM finding), with original list order as the
  final tiebreak. Dedupe logic itself (`_dedupe`, the similarity-match rule)
  is unchanged — only what survives the ceiling changes.
- **Per-step configuration, matching every existing macro step (US-F4).**
  `intent_summary`/`review_generation`/`verification` each get their own
  bootstrap enable flag and timeout, following the exact naming convention
  every existing step's config already uses. `llm_review_enabled`/
  `llm_review_step_timeout_seconds` are **renamed** to
  `review_generation_enabled`/`review_generation_step_timeout_seconds` (a
  deliberate breaking bootstrap-config rename — see Rollout & Release
  Safety). Disabling `verification` specifically restores exactly Phase 1's
  original behavior for that deployment: `review_generation`'s raw,
  unverified LLM findings proceed directly to `findings-merge-and-output`.
  Disabling `intent_summary` means `review_generation`'s prompt omits that
  section entirely (byte-for-byte today's existing prompt), the same
  backwards-compatible-when-absent pattern the context-bundle section
  already establishes.
- **Reuses the existing single LLM endpoint/model bootstrap config
  (decided) — no per-stage model routing yet.** `intent_summary` and
  `verification` both use the same `llm_endpoint`/`llm_api_key`/`llm_model`/
  `llm_timeout_seconds` fields `review_generation` already reuses from
  `llm_review`. The [C4 doc](../reference/c4-architecture.md) explicitly
  scopes real per-org/per-team, per-pipeline-stage model routing as
  **Day-two, Postgres-backed, admin-API-managed config** — and the roadmap's
  own Phase 5 bullet list names "Model routing configuration (which
  endpoint/model serves which pipeline stage)" as that phase's own scope,
  not Phase 3's. Building a second, bootstrap-only per-stage config surface
  now, only to replace it with the real thing in two phases, would be
  exactly the "no interface without a second real implementation already in
  view" problem `architecture.md`'s simplicity rule warns against.

### Out of scope (deferred to a later spec or roadmap phase)

- **DB-backed / admin-UI-driven per-stage model routing.** Phase 5, per the
  C4 doc and roadmap, as above.
- **Memory-informed verification/suppression** (checking a candidate finding
  against approved dismissal history). Phase 4, per the roadmap's own
  "Findings merger & verify start *reading* approved Memory entries" bullet
  — this spec's verification reads only the fixed rubric, nothing from a
  (not-yet-built) Memory store.
- **One LLM call per candidate finding.** Decided against — see Scope's
  batched-call reasoning above; a per-finding fan-out has no cost ceiling
  tied to finding count and no precedent in this codebase.
- **A numeric/graded confidence score.** Decided against — see Scope's
  binary-status reasoning above.
- **A customer-configurable/editable rubric.** Fixed in code for this
  phase; a natural Phase 5 admin-UI extension, not built here.
- **Verifying static-analysis findings.** Never, per design goal 4 — see
  Scope above.
- **Exposing the intent summary to the PR author** (e.g. as posted
  check-run text). Not requested by the roadmap; the intent summary is an
  internal prompt artifact only for this phase — a future enhancement, not
  a gap in this one.
- **Executing code or tests to verify a finding** (CodeRabbit's sandboxed
  verification agent, Greptile's TREX). This platform has no code-execution
  sandbox infrastructure at all; building one is materially larger scope
  than the roadmap's Phase 3 bullet describes, and the rubric-based LLM
  judgment call above is what the roadmap's own wording ("a distinct call
  against a fixed rubric") asks for.
- **Changing `review/output.py`'s posting/citation/dry-run/failure-path
  logic.** Unchanged — it already accepts whichever step
  `depends_on` names as its upstream dependency.

### Priority

- **Must have:** the `intent_summary` → `review_generation` → `verification`
  three-step split; the batched rubric-based verification call; dropping
  rejected findings and marking verified ones; static findings bypassing
  verification unconditionally; a verification failure being fatal to the
  job (US-V7 — anything less would silently reintroduce Phase 1's
  unverified-single-pass risk this whole spec exists to close); per-step
  enable/timeout configuration for all three steps. This collectively *is*
  the roadmap's Phase 3 capability, not divisible further without leaving
  design goal 6 unimplemented.
- **Should have:** the findings-merger severity/trust ceiling ordering
  (US-V8) — a real, well-motivated fix (per the roadmap's own "applies
  confidence/severity" wording), but `findings/merge.py`'s own docstring
  already notes `POSTED_FINDINGS_CEILING=100` is rarely reached in
  practice, so the core Phase 3 capability holds without it.
- **Could have:** none identified — this spec stays deliberately narrow to
  the roadmap's three stated bullets.

## Requirements

Stated as user stories per `testing.md`'s mandatory format.

**US-V1 — Intent summary grounds the review call**
As a PR author, I want the platform to understand what my change is trying
to accomplish, not just see a raw diff, so that its findings are grounded in
intent rather than surface-level pattern matching.
- Given a claimed `REVIEW` job whose upstream step (context-builder,
  static-analysis, or prefilter, whichever actually ran) has produced its
  output, When the `intent_summary` step runs, Then exactly one LLM Gateway
  completion call is made and its response is forwarded, unchanged, as a new
  field on this step's own output.

**US-V2 — Review generation reasons from stated intent**
As a PR author, I want the review-generation call to see the intent summary
alongside the diff and retrieved context, so that its findings reflect what
I was trying to do.
- Given `intent_summary`'s output, When `review_generation` runs, Then its
  LLM Gateway prompt includes the intent summary as its own labeled section,
  and every other aspect of its behavior (structured-findings parsing,
  redaction, diff truncation, context-bundle section) is byte-for-byte
  unchanged from today's `llm_review` step.
- Given `intent_summary` is disabled, When `review_generation` runs, Then
  its prompt omits the intent-summary section entirely, producing the exact
  same prompt as before this spec (backwards compatible when absent, same
  pattern the context-bundle section already establishes).

**US-V3 — Verification judges every LLM finding against a fixed rubric, in
one structurally separate call**
As a PR author, I want the platform's own LLM-sourced findings checked
against a fixed standard before I see them, so that low-value or
hallucinated comments don't reach me.
- Given `review_generation`'s output contains one or more `source="llm"`
  findings, When the `verification` step runs, Then exactly one LLM Gateway
  completion call is made — never a follow-up in the same call/conversation
  that produced the findings — supplying every candidate LLM-sourced
  finding plus the diff and context bundle, and the response is parsed into
  a per-finding verdict against all four fixed criteria (valid-to-the-code,
  actionable, specific, valuable-to-the-author).
- Given `review_generation`'s output contains zero `source="llm"` findings,
  When `verification` runs, Then no LLM Gateway call is made at all (nothing
  to judge), and static findings still pass through unchanged.

**US-V4 — Findings that fail the rubric are dropped before anyone sees them**
As a PR author, I want to only see findings that already passed a quality
check, so that I'm not shown noise the platform's own verification step
already flagged as low-value.
- Given a candidate LLM-sourced finding fails at least one of the four
  rubric criteria, When `verification` completes, Then that finding is
  excluded from this step's output entirely, and the drop (count plus which
  criteria most often failed, in aggregate) is logged at `warning`.

**US-V5 — Findings that pass are marked verified and proceed unchanged
otherwise**
- Given a candidate LLM-sourced finding passes all four criteria, When
  `verification` completes, Then it is included in this step's output with
  `verification_status="verified"`, every other field (including
  `context_ref_ids`, used later by `review/output.py`'s existing citation
  logic) unchanged.

**US-V6 — Static-analysis findings bypass verification entirely**
As a PR author, I want deterministic, tool-based findings to reach me
regardless of the verification pass, so that the platform's own free trust
anchor is never filtered by a second-guessing LLM call.
- Given `review_generation`'s output contains findings with `source` in
  `{"gitleaks", "semgrep"}`, When `verification` runs, Then those findings
  are forwarded to this step's output completely unchanged
  (`verification_status` stays `None`) and are never included in the
  rubric-judged batch.

**US-V7 — A verification failure fails the job, not a silent unverified
post**
As a PR author, I want to never see an LLM finding that skipped verification
just because the verification call itself broke, so that a broken
verification pass can't quietly reintroduce Phase 1's unverified-single-pass
risk.
- Given the `verification` step's LLM Gateway call raises an unhandled
  exception or exceeds its configured timeout, When the pipeline runs, Then
  the job is marked `FAILED` (`FailurePolicy.FATAL`, matching every existing
  macro step's failure semantics per `pipeline-step-framework.md`'s US-F6)
  and no findings — verified or not — are posted; the terminal `output` step
  still posts a failure-conclusion check-run (US8's existing mechanism,
  unchanged).

**US-V8 — Findings merger orders by severity and trust before applying its
ceiling**
As a PR author, I want the most severe, most trustworthy findings to survive
the merger's posted-findings ceiling, so that an arbitrary ordering never
silently drops a critical issue in favor of a low-severity one.
- Given a deduped findings list exceeding `POSTED_FINDINGS_CEILING`, When
  `merge_findings` applies its ceiling, Then the kept findings are the
  top-`N` ordered first by severity (`critical > high > medium > low >
  info`), then static-analysis-sourced findings ahead of
  verified-LLM-sourced findings at equal severity, with original list order
  as the final tiebreak.

**US-V9 — Per-step configuration, matching every existing macro step**
As a platform operator, I want to disable or retime any of the three new
steps independently, so that I can trade off cost/latency against
verification's benefit for my own deployment.
- Given `intent_summary_enabled`/`review_generation_enabled`/
  `verification_enabled` bootstrap flags, When any is set to disabled, Then
  that step never executes and `depends_on` resolution falls back to the
  nearest enabled upstream step, exactly matching the existing
  `context_builder`/`llm_review` fallback pattern.
- Given `verification_enabled=False`, When the Worker starts, Then a
  one-time `warning`-level startup log states that unverified LLM findings
  will be posted directly — matching `secret_redaction_enabled=False`'s
  existing "the deviation is visible, not silent" pattern.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- Every new LLM Gateway call (`intent_summary`, `verification`) takes an
  explicit timeout and respects cancellation, per `architecture.md`.
- **Cost is bounded, never per-finding.** `verification` adds exactly one
  more LLM Gateway completion call per review job regardless of how many
  candidate findings exist — see Scope's batched-call decision.
- **Sequential latency grows; the stuck-job threshold is recalculated, not
  left stale.** The macro chain is now `prefilter(30) + static_analysis(240)
  + context_builder(35) + intent_summary(30) + review_generation(90) +
  verification(60) + output(30) = 515s` worst-case healthy-run sum (new
  step defaults: `intent_summary_step_timeout_seconds=30`,
  `verification_step_timeout_seconds=60`, matching the existing "a concrete
  default, not left unspecified" convention — a short summarization call
  and a moderate batched-judgment call respectively, both lighter than
  `review_generation`'s own 90s). `stuck_job_warning_threshold_seconds` is
  raised from 450 to 600, preserving the existing ~15% headroom convention
  (`apps/worker/settings.py`'s own documented reasoning) rather than left at
  a now-stale value.
- **No new credential types.** `intent_summary`/`verification` reuse the
  existing `llm_endpoint`/`llm_api_key`/`llm_model`/`llm_timeout_seconds`
  bootstrap fields — see Scope's "no per-stage model routing yet" decision.
- **No new redaction surface.** Verification's own response schema carries
  only structured per-finding verdicts (a finding reference plus four
  booleans) — no new free-text field that could echo diff content back out.
  Verification operates on findings `review_generation` already redacted
  (`review/llm_review.py`'s existing `_redact_all` pass runs before that
  step returns), so no second redaction call is needed at this layer.

### Privacy / Compliance

No new data-handling category beyond what ADR-0001/ADR-0002 and
`spec-review-pipeline.md` already establish: the same diff, static findings,
and context-bundle content already reaching the operator's configured LLM
endpoint is what `intent_summary` and `verification` also see — more calls
to the same already-consented endpoint, not a new category of data leaving
the deployment boundary.

### Observability

- `intent_summary`, `review_generation`, and `verification` each log their
  own start/completion/failure with `job_id`/`step_name`/`duration_ms`/
  `status`, extending the existing structured-logging pattern
  (`review/pipeline.py`'s `_run_one`) with zero framework changes.
- `verification` additionally logs, per job: the count of candidate findings
  judged, how many were rejected, and which of the four criteria most often
  caused a rejection (aggregate counts, never a rejected finding's own raw
  message content, matching every other drop-log's redacted-content
  posture) — operator-facing tuning signal, never posted to the PR.
- The `verification_enabled=False` startup log (US-V9) is `warning`-level,
  one-time at startup, mirroring `secret_redaction_enabled=False`'s existing
  precedent exactly.

### Rollout & Release Safety

- **Deliberate breaking bootstrap-config rename (decided).**
  `llm_review_enabled`/`llm_review_step_timeout_seconds` become
  `review_generation_enabled`/`review_generation_step_timeout_seconds`; any
  existing deployment's env vars/Helm values referencing the old names must
  be updated at upgrade time. Justified now, before the roadmap's own MVP
  line (Phase 5) — there is no pilot customer yet to carry a permanently
  confusing "llm_review" name for what is now specifically one of three
  review-generation-stage steps.
- **No database migration required.** `Finding` is not yet a persisted
  model anywhere in this codebase (confirmed: no `findings`/`findings
  history` table exists in `adapter/storage/models.py` or its migrations —
  the C4 doc's "findings history" Postgres table is Memory-adjacent future
  scope, not built yet). `verification_status` is an in-memory pydantic
  field only; nothing to migrate.
- **Backwards compatibility for a disabled `verification` step (decided,
  see US-V9):** disabling it restores exactly Phase 1's original,
  already-shipped behavior (unverified LLM findings posted directly) — a
  supported, intentional fallback, not a degraded/broken state.

## UX & Design

No new user-facing surface — inherits `spec-review-pipeline.md`'s existing
PR-comment/check-run UX entirely. The only observable change to the PR
author is a **quieter, more trustworthy review**: fewer LLM-sourced comments
(rejected ones never post), with no new interaction, state, or visual
element added to GitHub's own PR UI. The intent summary is never rendered
anywhere the PR author sees (see Scope → Out of scope) — this phase's UX
change is entirely a *reduction* in noise, not a new surface.

- **Primary flow:** unchanged from Phase 1/2 — inline comments and a
  check-run appear after the (now three-call) review completes.
- **Empty/no-findings state:** unchanged (US7, inherited) — if verification
  rejects every candidate LLM finding and static analysis found nothing
  either, the check-run still posts a clear "no findings" summary.
- **Error state:** unchanged in shape (US8, inherited) — a verification
  failure posts a failure-conclusion check-run exactly like any other fatal
  macro-step failure (US-V7).
- **In-progress state:** unchanged — no new interim/pending status is
  posted while the now-six-step pipeline runs (matching Phase 1/2's own
  stance: no check-run is posted until the terminal `output` step runs, no
  "review in progress" placeholder exists today, and this spec adds no new
  one).
- **Accessibility/responsiveness:** inherited entirely from GitHub's own PR
  UI, exactly as `spec-review-pipeline.md`'s own UX section already states
  for this surface — nothing to design here, and nothing this spec changes
  about that.
- **Usability heuristic spot-check (visibility of system status):** a
  "quieter" review (fewer, verified findings) does not read as the tool
  silently hiding things from the PR author — nothing about Phase 1/2's
  existing contract ever promised full recall of every possible issue, and
  US-V4's `warning`-level drop logging (operator-facing, not PR-author-facing
  by design) is the same "visible to an operator, not fabricated as a
  PR-author-facing feature" posture this spec's Observability section
  already takes — no new heuristic risk introduced.
- **Performance as UX:** worst-case time-to-review grows (see NFRs'
  recalculated stuck-job threshold) — no quantified perceived-performance
  target is set here either, consistent with every prior phase's stance
  (not fabricating one that no document or stakeholder has given).
- **Consistency with a design system:** N/A — no component library
  involved, same as every prior phase.

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author, `R`/`A` on every existing
  ADR's RACI table; no separate design/eng/QA/legal stakeholders exist for
  this project at this stage.
- **Acceptance:** every `US-V*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios); `mypy --strict`/`ruff` clean
  per `architecture.md`; `review/test_step_boundaries.py`'s existing
  import-boundary check passes unmodified for the two new step modules
  (`intent_summary`, `verification`), proving US-F1 still holds with the
  pipeline extended to six macro steps.

## Phases / Units

Builds on the already-shipped `spec-pipeline-step-framework.md` (Unit C of
`spec-review-pipeline.md`) and `spec-review-pipeline.md` itself — no
framework changes; every unit below is a new or renamed macro step
registered into the same `StepRegistry`.

- **Unit A — `intent_summary` step (new).** Depends on: nothing new (reuses
  `LLMClient`, same as `llm_review` today). Risk: low — a single completion
  call with a simpler, unstructured (plain-text, not JSON-schema) response,
  the lowest-risk of the three new/changed steps. **Acceptance:** US-V1's
  `Given/When/Then` passes as an integration test against a recorded LiteLLM
  cassette.
- **Unit B — `review_generation` step (rename/extend `llm_review`).**
  Depends on: Unit A's output shape. Risk: low — additive prompt-section
  change to an already-shipped, already-tested step; existing
  `review/test_llm_review_step.py` coverage carries over almost unchanged
  under the new module/class name. **Acceptance:** US-V2's
  `Given/When/Then` (both the intent-summary-present and
  intent-summary-disabled cases) passes as an integration test.
- **Unit C — `verification` step (new), including the new
  `Finding.verification_status` field.** Depends on: Unit B's output. This
  unit owns the `findings/models.py` schema change
  (`verification_status: Literal["verified","rejected"] | None = None`) —
  it's the only step that ever sets it to a non-`None` value, so the field
  is added here rather than as an unassigned, ownerless prerequisite. Risk:
  medium — the first batched-judgment structured-output call in this
  codebase (a list-in, list-of-verdicts-out shape, distinct from
  `llm_review`'s single-list-out shape) and the step carrying this spec's
  core design-goal-6 requirement. **Acceptance:** US-V3, US-V4, US-V5, and
  US-V6's `Given/When/Then` all pass as integration tests against a
  recorded LiteLLM cassette, plus a `findings/test_models.py` unit test
  asserting the new field's default/shape. (US-V7 — the fatal-failure
  behavior — is Unit E's acceptance criterion, not this unit's: it's an
  emergent property of registering `verification` into the existing
  `FailurePolicy.FATAL` registry, not new logic this step's own
  implementation adds — see Unit E below, matching
  `spec-review-pipeline.md`'s own precedent of assigning its equivalent
  failure story, US8, to the terminal-wiring unit rather than the failing
  step's own unit.)
- **Unit D — `findings/merge.py` severity/trust ordering.** Depends on:
  Unit C's `Finding.verification_status` field *definition* only, not its
  runtime-setting logic — buildable and testable in parallel with Unit C's
  own implementation, using hand-built `Finding` fixtures that set the
  field directly. Risk: low. **Acceptance:** US-V8's `Given/When/Then`
  passes as a unit test.
- **Unit E — Composition-root wiring** (`apps/worker/main.py`,
  `apps/worker/settings.py`). Depends on: Units A-D. Risk: low — mechanical
  extension of the existing four-combination `depends_on`-fallback pattern
  `_build_review_registry` already establishes for `context_builder`/
  `llm_review`, now extended to three additional steps' worth of
  enable/fallback combinations. **Acceptance:** US-V9's `Given/When/Then`
  passes as an integration test; US-V7's `Given/When/Then` passes as an
  integration test against the real `StepRegistry` with a raising fake
  `LLMClient` injected at the verification step's call site (the same
  mechanism `spec-review-pipeline.md`'s own US8 test already uses in
  `review/test_review_job.py`, not a cassette-simulated fault); the
  recalculated `stuck_job_warning_threshold_seconds` default is applied.

## Testing / Key Scenarios

Per `testing.md`'s pyramid.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-V1 | One LLM call produces an intent summary, forwarded unchanged | `review/test_intent_summary_step.py` (new) | Integration (recorded LiteLLM cassette) |
| US-V2 | Review-generation prompt includes the intent-summary section when present, omits it when `intent_summary` is disabled | `review/test_review_generation_step.py` (renamed from `test_llm_review_step.py`) | Integration (recorded LiteLLM cassette) |
| US-V3 | One batched call judges every candidate LLM finding against all four criteria; zero LLM findings skips the call entirely | `review/test_verification_step.py` (new) | Integration (recorded LiteLLM cassette) |
| US-V4 | A finding failing any criterion is dropped and the drop is logged | `review/test_verification_step.py` | Integration |
| US-V5 | A finding passing all four criteria is marked `verified` with every other field unchanged | `review/test_verification_step.py` | Integration |
| US-V6 | Gitleaks/Semgrep findings bypass the rubric batch and forward unchanged | `review/test_verification_step.py` | Integration |
| — | `Finding.verification_status` field defaults to `None` and accepts only `"verified"`/`"rejected"`/`None` | `findings/test_models.py` (extended) | Unit |
| US-V7 | A verification timeout/exception marks the job `FAILED`, posts no findings, posts a failure check-run — proven against the real `StepRegistry` (`FailurePolicy.FATAL`) with a raising fake `LLMClient`, not a cassette-simulated fault, mirroring `spec-review-pipeline.md`'s own US8 test exactly | `review/test_review_job.py` (extended) | Integration¹ |
| US-V8 | A deduped list exceeding the ceiling keeps the top-`N` by severity, then static-over-verified-LLM, then original order | `findings/test_merge.py` (extended) | Unit |
| US-V9 | Each of the three new/renamed steps' disable flag falls back `depends_on` correctly; `verification_enabled=False` logs the one-time startup warning | `apps/worker/test_main.py` (extended) | Unit/Integration (mixed, matching the existing split between composition-root wiring tests and real-registry tests) |
| US-V1–V9 (happy path) | Full Docker Compose stack, synthetic GitHub webhook payload, six-step pipeline runs end-to-end, posts a verified-and-deduped review | `tests/e2e/test_review_pipeline.py` (extended) | E2E |

¹ Labeled `Integration`, not `Unit`, despite exercising a Protocol-level fake (`LLMClient`) with no real I/O — `testing.md`'s own boundary definition would literally classify this as Unit. Kept as `Integration` deliberately, for consistency with the already-shipped, identical-shape precedent (`spec-review-pipeline.md`'s US8 row, same test file, same fake-at-the-registry-level mechanism) rather than introducing a differently-labeled test for the same kind of scenario across two sibling specs. A future documentation pass could reconcile `testing.md`'s definition with this established "exercise the real `StepRegistry`'s failure-propagation wiring, not just one step's own logic" case — not this PRD's scope to resolve unilaterally.

## Resolved decisions (formerly open assumptions)

Four product/architecture decisions were confirmed with the user before
drafting (no project document resolved these):

1. **Verification call shape:** one batched call per review, not one call
   per finding — bounded, predictable cost; matches Sourcery's documented
   pattern; no existing per-job-fan-out precedent in this codebase.
2. **Rubric:** Sourcery's four criteria (valid-to-the-code, actionable,
   specific, valuable-to-the-author), binary pass/fail, all four required —
   the one rubric the research doc documents in reproducible detail.
3. **Verification-outcome representation:** binary
   `verification_status: Literal["verified","rejected"] | None`, not a
   numeric confidence score — nothing yet consumes a graded score.
4. **Findings-merger ceiling ordering:** sort by severity then
   static-over-verified-LLM trust before truncating, rather than leaving the
   existing blind first-`N`-in-list-order slice unchanged.

All four are reflected as decided requirements above (US-V3, US-V4/V5, US-V8)
— none remain open.
