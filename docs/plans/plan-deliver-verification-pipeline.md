# Plan: Phase 3 — Verification Pipeline

**Status: Proposed.** Sequences [`docs/specs/spec-verification-pipeline.md`](../specs/spec-verification-pipeline.md)
— [`docs/roadmap.md`](../roadmap.md)'s Phase 3 — into buildable,
independently-testable phases, following the same phase/tests/acceptance
shape `docs/plans/plan-deliver-context-builder.md` and
`docs/plans/plan-indexing-delivery.md` established as this repo's
`docs/plans/` convention. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

`spec-verification-pipeline.md` was itself self-reviewed to "ready to hand
to a planning agent" against `prd-review`'s ten dimensions (two full review
passes; every Weak finding closed with real content, not softened
requirements). This plan does not re-litigate any of that spec's
product/architecture calls — it only sequences the work and resolves the
*implementation-shape* questions the spec deliberately left unspecified.
Those resolutions are recorded below as **Assumptions this plan makes**, not
silent guesses.

This is additive work on real, running code (Phase 1's `e99a88f`-merged
`review/llm_review.py`/`findings/merge.py`/`review/output.py`, Phase 2's
`a7ee9da`-merged Context Builder), not greenfield. Every phase below names
the existing file/module/function it extends, renames, or reuses; every
cross-file reference this plan makes to an existing symbol was checked
against the real file during drafting (`review/step.py`, `review/pipeline.py`,
`review/config.py`, `review/context_bundle.py`, `findings/models.py`,
`findings/merge.py`, `findings/redact.py`, `llmgateway/models.py`,
`llmgateway/client.py`, `apps/worker/main.py`, `apps/worker/settings.py`).

## Scope

**In scope** — `spec-verification-pipeline.md`'s Units A–E in full, sequenced
1:1 as Phases 1–5 below: the `intent_summary` step, the `review_generation`
step (renamed/extended `llm_review`), the `verification` step,
`findings/merge.py`'s severity/trust ceiling ordering, and composition-root
wiring.

**Out of scope** — the spec's own out-of-scope list applies unchanged:
DB-backed/admin-UI-driven per-stage model routing (Phase 5), Memory-informed
verification (Phase 4), one LLM call per candidate finding, a numeric/graded
confidence score, a customer-configurable rubric, verifying static-analysis
findings, exposing the intent summary to the PR author, and
code-execution-based verification. Not repeated in full here; see the
spec's own Scope section.

## Assumptions this plan makes (flagged, not decided)

1. **`intent_summary`'s own prompt uses the diff only — not static findings,
   not the context bundle.** Rejected alternative: including the same
   static-findings/context-bundle sections `review_generation` already
   builds. Rejected because "what is this PR trying to accomplish" is fully
   derivable from the diff alone; including the other sections would add
   real token cost to a one-paragraph summarization call for no stated
   benefit. The spec's own Scope text ("the same diff... that today's
   `llm_review` step receives") describes what this step's **input
   model** forwards downstream, not what its own prompt must contain — this
   plan reads that as an input-shape statement, not a prompt-content
   mandate.
2. **A new shared module, `review/diff_prompt.py`, holds `render_diff`/
   `truncate_diff_text`** (extracted from `review/llm_review.py`'s current
   private `_render_diff`/`_truncate_diff_text`), reused by both
   `intent_summary.py` (Phase 1) and the renamed `review_generation.py`
   (Phase 2). Avoids duplicating the same diff-rendering/truncation logic
   across two step modules, mirroring this codebase's own established
   precedent for exactly this situation (`findings/redact.py`'s
   `redact_findings`/`apply_redaction_ceiling`, extracted once two step
   modules needed the identical logic).
3. **`review/context_bundle.py` gains a public `render_context_bundle_section`
   function**, moved from `review/llm_review.py`'s current private
   `_render_context_bundle_section` — it now has two real consumers
   (`review_generation.py`, `verification.py`), and `review/context_bundle.py`
   is the module that already owns `ContextBundle`/`ContextBundleItem`, the
   data this function renders.
4. **Intent-summary text is capped at a fixed module-level constant**
   (`MAX_INTENT_SUMMARY_CHARS`), not a new bootstrap-config field —
   truncated and logged at `warning` if exceeded, matching this codebase's
   existing pattern for a fixed-not-configurable ceiling
   (`REDACTION_FINDING_CEILING`, `POSTED_FINDINGS_CEILING`); nothing in the
   spec asks for this to be operator-tunable.
5. **Verification's batched response schema correlates verdicts to
   findings by an explicit, LLM-echoed `index` field** (0-based, matching
   each candidate finding's position in the array supplied in the prompt) —
   not by array-position-of-the-response-array alone. Rejected alternative:
   trusting the response array's own order to line up with the request
   array's order. Rejected because nothing enforces that positional
   contract on a completion response the way pydantic enforces a typed
   field — an LLM dropping or reordering one item would silently
   misattribute every verdict after it. A finding whose index is missing
   from the response, duplicated, or out of range is treated as
   **rejected** (fail-closed) — never silently kept, mirroring this
   codebase's existing fail-closed posture for exactly this kind of gap
   (`findings/redact.py`'s "a redaction call's own failure ... is fatal";
   `review/llm_review.py`'s per-item validation dropping a malformed
   finding rather than keeping it).
6. **One concrete, non-generic helper resolves `depends_on` for the five
   dynamically-wired steps, in `review/config.py`, shared by both the
   startup viability check and the composition-root wiring — not a
   general-purpose, arbitrary-chain-length utility.** Today's
   `apps/worker/main.py` hand-computes one fallback variable
   (`llm_review_depends_on`) for the one optional stage that exists
   (`context_builder`). This plan adds three more optional, dynamically-wired
   stages (`intent_summary`, `review_generation`, `verification`) ahead of
   the existing terminal `output` step — five dynamically-wired steps
   total, not two, all sharing the same fixed, always-the-same ordering
   (`context_builder → intent_summary → review_generation → verification →
   output`). **Revised after review**: an earlier draft of this assumption
   proposed a generic `resolve_chain_depends_on(chain: Sequence[str],
   enabled: Mapping[str, bool])` utility — correctly flagged as speculative
   generality (one concrete chain will ever exist; this codebase's own
   `architecture.md` rejects an abstraction without a second real
   implementation already in view). The chain-*computation* itself is
   genuinely just five sequential lines and doesn't need a reusable
   function at all. What does need to stay shared, though, is the
   *validation* question `check_step_configuration_viable` must answer —
   "is every enabled step's resolved dependency also enabled?" — because
   hand-deriving that as a fifth independent boolean expression (on top of
   the two the check already has) requires correctly re-deriving the same
   override-aware logic (`context_builder_can_produce_value`/
   `intent_summary_can_produce_value`) a second time, which is exactly the
   kind of "two definitions could drift" risk this file's own
   `static_analysis_can_produce_findings` docstring already warns against
   for a simpler, one-predicate case. The resolution: one small, concrete,
   **hard-coded-to-this-one-chain** function,
   `resolve_review_pipeline_depends_on(enablement: StepEnablement) ->
   dict[str, str]` (no `Sequence[str]` parameter, nothing reusable for a
   hypothetical different chain), used by both
   `check_step_configuration_viable` (to validate) and
   `_build_review_registry` (to wire) — see Phase 5.
7. **The bootstrap-config rename (`llm_review_enabled`/
   `llm_review_step_timeout_seconds` → `review_generation_*`) gets an
   explicit, fail-loud startup check for the old env var names, not just a
   grep confirming no *committed* Helm/Compose file references them.**
   `WorkerSettings.model_config` uses `SettingsConfigDict(extra="ignore")`
   (`apps/worker/settings.py:121-126`) — an operator's own,
   externally-owned deployment config (a customer's Helm values override,
   a `.env` file) setting `REVIEW_WORKER_LLM_REVIEW_ENABLED` today would
   silently stop taking effect after this rename, falling back to the new
   field's default (`True`) with zero error or log line. This is a real
   gap the repo-level grep in Compatibility & rollout doesn't cover, since
   it can only ever confirm what this *repo* references, never what a
   deployed customer's own config sets. See Phase 5's new startup-check
   deliverable.
8. **Docstring-only cross-references to `review.llm_review`/`LLMReviewStep`/
   `LLMReviewOutput`** in modules that don't import them (`ingestion/diff.py`,
   `review/context_builder.py`, `review/citation.py`,
   `review/context_symbols.py`, `review/static_analysis.py`,
   `findings/models.py`, `findings/redact.py`, `findings/merge.py`,
   `review/context_bundle.py`, and the two e2e test docstrings) are swept
   at the end of Phase 2 via a repo-wide grep, not enumerated file-by-file
   here — each is a narrative comment, not a functional reference, so
   there's no behavioral risk in batching the sweep.

## Phase 1 — `intent_summary` step (Unit A)

**Why first:** produces the field `review_generation` (Phase 2) needs to
build its own new prompt section; has no dependency on any other phase in
this plan.

- New `review/diff_prompt.py` — `render_diff(diff: Diff) -> str` and
  `truncate_diff_text(text: str, max_bytes: int) -> tuple[str, int, int]`,
  extracted verbatim from `review/llm_review.py`'s current
  `_render_diff`/`_truncate_diff_text` (Assumption 2). Zero behavior
  change to the extracted logic itself.
- New `review/intent_summary.py`:
  - `_IntentSummaryInput(JobIdentity)` — mirrors whichever upstream step's
    output actually reaches it (`ContextBuilderOutput`/
    `StaticAnalysisOutput`/`PrefilterOutput`, per the existing
    `from_attributes=True`-mirroring pattern every step module already
    uses): `diff: Diff`, `findings: list[Finding] = Field(default_factory=list)`,
    `context_bundle: ContextBundle | None = None`.
  - `IntentSummaryOutput(JobIdentity)` — forwards `diff`, `findings`,
    `context_bundle` unchanged, plus the new field `intent_summary: str`.
  - `MAX_INTENT_SUMMARY_CHARS` module constant (Assumption 4) — a system
    prompt instructing a concise (≤ a stated sentence/paragraph count)
    natural-language summary of what the diff is trying to accomplish, one
    `LLMClient.complete()` call via `render_diff`/`truncate_diff_text`
    (Assumption 2) on the diff alone (Assumption 1). The raw text response
    is used directly (no JSON parsing) — capped at
    `MAX_INTENT_SUMMARY_CHARS`, truncated and logged at `warning` if
    exceeded, mirroring `review/llm_review.py`'s existing diff-truncation
    log shape.
  - `IntentSummaryStep` implementing `review.step.Step` — `name =
    "intent_summary"`; `depends_on` is a constructor parameter (mirrors
    `LLMReviewStep`'s/`ContextBuilderStep`'s existing pattern), wired by
    Phase 5.
- **No redaction call for the intent summary's own text.** The intent
  summary never becomes a `Finding.message` or any other posted/logged
  field directly — its only downstream consumer is `review_generation`'s
  own prompt (Phase 2), and any content it echoes from the diff that ends
  up quoted back in a `source="llm"` finding's `message` field is already
  caught by that finding's own existing redaction pass
  (`review/review_generation.py`'s `_redact_all`, unchanged from today's
  `llm_review.py`). Logged only as presence/length
  (`intent_summary_chars: int`), never the text itself, matching the
  spec's own "never logged in full" NFR.

**Tests:** Unit (`review/test_diff_prompt.py`, new — `render_diff`/
`truncate_diff_text`'s existing test cases moved here unchanged from
`review/test_llm_review_step.py`, per ADR-0005). Integration
(`review/test_intent_summary_step.py`, new — one completion call is made;
the response is forwarded as `intent_summary`; a response exceeding
`MAX_INTENT_SUMMARY_CHARS` is truncated and logged; the step's `input`
validates correctly whichever upstream shape it's handed, mirroring
`review/test_context_builder_step.py`'s existing multi-upstream-shape
coverage) against a recorded LiteLLM cassette.

**Observability & rollback:** New step, no existing behavior changed by
this phase alone (it isn't registered into the live registry until Phase 5)
— fully reversible by reverting this phase's commit with zero interaction
with anything already shipped.

**Acceptance:** US-V1's `Given/When/Then` passes as an integration test.

## Phase 2 — `review_generation` step (Unit B, rename/extend `llm_review`)

**Why second:** consumes Phase 1's `intent_summary` field; is itself
consumed by Phase 3's `verification` step.

- **Rename, not rewrite:** `review/llm_review.py` → `review/review_generation.py`;
  `LLMReviewStep` → `ReviewGenerationStep` (`name = "review_generation"`,
  not `"llm_review"`); `_LLMReviewInput` → `_ReviewGenerationInput`;
  `LLMReviewOutput` → `ReviewGenerationOutput`; `LLMResponseParseError`
  stays named as-is (it's about the LLM's response shape, not this step's
  own identity). `review/test_llm_review_step.py` →
  `review/test_review_generation_step.py`, updating every `"llm_review"`
  string literal used as a real step/config key to `"review_generation"`
  (the file's `enabled={"static_analysis": True, "llm_review": True}`-shape
  dicts and the one `step_name == "llm_review"` log assertion).
- Remove the now-duplicate `_render_diff`/`_truncate_diff_text`/
  `_render_context_bundle_section` private functions; import
  `render_diff`/`truncate_diff_text` from `review/diff_prompt.py`
  (Phase 1, Assumption 2) and `render_context_bundle_section` from
  `review/context_bundle.py` (Assumption 3).
- `_ReviewGenerationInput` gains `intent_summary: str | None = None`
  (optional, defaulting to `None` — mirrors `context_bundle`'s own
  `None`-when-absent backwards-compatibility pattern exactly, so this
  step's input still validates off any upstream shape that predates this
  phase, e.g. `StaticAnalysisOutput`/`PrefilterOutput`, none of which carry
  the field).
- New `_render_intent_summary_section(intent_summary: str | None) -> str`
  (mirrors `_render_context_bundle_section`'s own `None`/empty-returns-""`
  shape) — a new, clearly labeled prompt section ("What this PR is trying
  to do:") inserted into `_build_user_message`, before the existing
  static-findings/diff/context-bundle sections. `None` produces the empty
  string, so the prompt is byte-for-byte identical to today's when
  `intent_summary` is disabled (US-V2's second scenario). **Carries the
  same "inert reference data, never instructions to follow" framing
  `_render_context_bundle_section` already prefixes its own section
  with** (`review/context_bundle.py`'s `render_context_bundle_section`,
  Assumption 3) — the intent summary is itself LLM-generated from the
  fully PR-author-controlled diff, so it carries the identical
  prompt-injection risk class the context-bundle section's framing already
  exists to blunt; omitting the same defense here would be an unexplained
  gap between two structurally identical risks.
- `_SYSTEM_PROMPT` gains one sentence acknowledging the new "What this PR
  is trying to do" section exists and should inform, not replace, the
  model's own reading of the diff — everything else about the prompt/
  response contract (structured findings JSON, `context_ref_ids`, the
  redaction pass, the diff-truncation ceiling) is unchanged.
- **Repo-wide sweep** (Assumption 7): grep for remaining `llm_review`/
  `LLMReviewStep`/`LLMReviewOutput`/`_LLMReviewInput` references across the
  tree; update the docstring-only mentions in the files Assumption 7 names.
  `review/test_step_registry.py`'s and `adapter/github/test_posting.py`'s
  own `"llm_review"` fake-step-name literals are **not** part of this
  sweep — they're arbitrary example names in generic-framework/generic-
  posting tests unrelated to the real step, left as-is.

**Tests:** Integration (`review/test_review_generation_step.py`, renamed —
existing coverage carries over; new cases: the intent-summary section
appears in the built prompt when present, is absent (byte-for-byte
matching today's prompt) when `intent_summary=None`) against a recorded
LiteLLM cassette. Unit (`review/test_diff_prompt.py`, from Phase 1, now the
sole home for this logic's own tests — `review/test_review_generation_step.py`
no longer duplicates them).

**Observability & rollback:** Purely additive to an already-shipped,
already-tested step's prompt construction — no change to its structured-
output parsing, redaction, or failure semantics. Reversible by reverting
this phase's commit; `review/llm_review.py`'s prior behavior is fully
recovered (module name changes back, no data was written anywhere that
needs cleanup).

**Acceptance:** US-V2's `Given/When/Then` (both the intent-summary-present
and intent-summary-disabled cases) passes as an integration test; every
pre-existing `review/test_llm_review_step.py` assertion still passes under
its new file/symbol names.

## Phase 3 — `verification` step (Unit C, new)

**Why third:** consumes Phase 2's `review_generation` output; is itself
consumed by Phase 4's severity/trust ordering and Phase 5's wiring. Carries
this plan's core design-goal-6 requirement.

- New `review/verification.py`:
  - `_VerificationInput(JobIdentity)` — mirrors `ReviewGenerationOutput`'s
    shape (`from_attributes=True`): `diff: Diff`, `findings: list[Finding]`,
    `head_sha: str`, `context_bundle: ContextBundle | None = None`.
  - `VerificationOutput(JobIdentity)` — same shape, `findings` replaced by
    the filtered/annotated list this step produces; `head_sha`/
    `context_bundle` forwarded unchanged (mirrors `ReviewGenerationOutput`'s
    own forwarding).
  - `_VerificationVerdictItem(BaseModel, frozen=True)` — one parsed
    response item: `index: int`, `valid: bool`, `actionable: bool`,
    `specific: bool`, `valuable: bool` (the four fixed rubric criteria,
    per the spec's decided Sourcery-derived rubric).
  - `VerificationResponseParseError(Exception)` — mirrors
    `LLMResponseParseError`'s own shape/role, raised (never caught within
    `run()`) on a response that isn't valid JSON or is missing the
    top-level `verdicts` list — propagates as this step's own failure,
    fatal under `FailurePolicy.FATAL`, identical mechanism to
    `review_generation`'s own `LLMResponseParseError` today (US-V7).
  - `VerificationStep` implementing `review.step.Step` — `name =
    "verification"`; `depends_on` a constructor parameter, wired by Phase 5.
    `run()`:
    1. Partitions `parsed.findings` into `llm_findings` (`source == "llm"`)
       and `other_findings` (everything else — US-V6, forwarded unchanged,
       never included in the rubric-judged batch).
    2. If `llm_findings` is empty: returns `VerificationOutput(findings=other_findings,
       ...)` immediately — **no LLM Gateway call is made** (US-V3's second
       scenario).
    3. Otherwise, builds one `CompletionRequest`: a fixed system prompt
       stating the four rubric criteria and the required response shape;
       a user message listing each `llm_findings[i]` with an explicit
       `"index": i` field (its 0-based position in this array — Assumption
       5) alongside `file`/`line`/`message`/`context_ref_ids`, plus the
       diff (via `render_diff`/`truncate_diff_text`, Phase 1) and the
       context bundle (via `render_context_bundle_section`, Assumption 3) —
       the same two supporting-context sections `review_generation`
       already builds, reused unchanged. Makes exactly one
       `LLMClient.complete()` call.
    4. Parses `response.content` as `{"verdicts": [...]}`; each item
       validated independently against `_VerificationVerdictItem` (a
       malformed individual item is dropped and logged at `warning`,
       mirroring `_parse_llm_findings`'s own per-item-tolerant pattern —
       never fails the whole response for one bad item).
    5. Builds `verdict_by_index: dict[int, _VerificationVerdictItem]` from
       the parsed items. A duplicate index keeps the first occurrence,
       logged at `warning` **with the offending `index` value and
       `job_id` as structured `extra` fields** (not just a bare message —
       an operator needs the index to correlate the warning back to which
       candidate finding it concerns). **An index outside
       `range(len(llm_findings))` is logged at `warning` too**, same
       structured-field shape, its own distinct message (e.g.
       "verification: dropping out-of-range verdict index") — the
       duplicate case and the out-of-range case previously got asymmetric
       treatment (only the former was logged); both are now logged the
       same way, matching the "a manipulated or malformed response
       degrades visibly, never silently" posture the rest of this step
       already follows.
    6. For each `llm_findings[i]`: if `i not in verdict_by_index`, or any
       of `valid`/`actionable`/`specific`/`valuable` is `False` →
       **rejected**, excluded from the output entirely (US-V4, fail-closed
       per Assumption 5). Otherwise → **verified**:
       `finding.model_copy(update={"verification_status": "verified"})`
       (US-V5 — every other field, including `context_ref_ids`, unchanged).
    7. Logs, once per job: `judged_count`, `rejected_count`,
       **`unmatched_count`** (how many of `rejected_count` were rejected
       specifically because no verdict correlated to their index — a
       schema-adherence/correlation failure, distinct from a genuine
       rubric-criteria failure, added so an operator watching
       `rejected_count` spike can tell "the model is dropping/misindexing
       verdicts" apart from "the model is genuinely rejecting more
       findings"), and aggregate per-criterion failure counts
       (`valid_failures`, `actionable_failures`, `specific_failures`,
       `valuable_failures`, counted only over correlated verdicts) computed
       from `verdict_by_index` before filtering — at `warning` if
       `rejected_count > 0`, `info` otherwise. Never a rejected finding's
       own raw `message` text (US-V4's "never at a level/content that
       fails the job" plus the spec's own redacted-content-only
       observability posture).
    8. Returns `VerificationOutput(findings=[*other_findings, *kept_llm_findings],
       head_sha=parsed.head_sha, context_bundle=parsed.context_bundle, ...)`.

**Tests:** Unit (`review/test_verification.py` — `_VerificationVerdictItem`
parsing/dropping-malformed-items logic, isolated from any I/O, per
ADR-0005; plus the fail-closed correlation cases: a verdict list missing an
entry for one candidate finding, a duplicate index (first occurrence wins,
logged), and an out-of-range index (ignored, logged) — each proven to
produce `rejected` for the affected finding, not a crash or a silent
`verified`). Integration (`review/test_verification_step.py`, new — US-V3
(one batched call, zero-LLM-findings-skips-the-call), US-V4 (a
finding failing one/more criteria is dropped, drop is logged), US-V5 (a
finding passing all four is marked `verified` with `context_ref_ids`
preserved), US-V6 (static findings bypass the batch, forwarded unchanged),
and a case mirroring
`review/test_llm_review_step.py`'s existing
`test_malformed_llm_response_content_is_a_fatal_step_failure_logged_at_error`
— a response that isn't valid JSON or is missing the top-level `verdicts`
list raises `VerificationResponseParseError`, uncaught, propagating as this
step's own failure) against a recorded LiteLLM cassette.

**Observability & rollback:** New step, not yet registered into the live
registry until Phase 5 — fully reversible by reverting this phase's commit
in isolation.

**Acceptance:** US-V3, US-V4, US-V5, and US-V6's `Given/When/Then` all pass
as integration tests. (US-V7 — the fatal-failure behavior — is Phase 5's
acceptance criterion, not this phase's: it's an emergent property of
registering `verification` into the existing `FailurePolicy.FATAL`
registry, not new logic this step's own implementation adds, mirroring
`plan-deliver-context-builder.md`'s own precedent of testing its equivalent
failure story — the LLM Gateway timeout case — at the composition-root/job
level, not inside the failing step's own unit.)

## Phase 4 — `findings/merge.py` severity/trust ceiling ordering (Unit D)

**Why fourth:** depends only on `Finding.verification_status` *existing* as
a field (added in Phase 3) — not on any step's runtime logic — so it's
buildable and testable in parallel with Phases 1–3 using hand-built
`Finding` fixtures, and is sequenced here only because it's the last piece
before wiring (Phase 5) needs every model change to already exist.

- `findings/models.py`: **this is where `Finding.verification_status` is
  actually added** — `verification_status: Literal["verified", "rejected"] |
  None = None` (a plain field, not needing the `context_ref_ids`-style
  tuple treatment: it's a scalar, not a collection, so `Finding`'s existing
  `frozen=True`/hashability is unaffected). Defaults to `None` for every
  existing (static-analysis, and any pre-verification LLM) `Finding`
  construction site, unaffected. (Phase 3's `VerificationStep` is the only
  code that ever sets it to a non-`None` value — see Phase 3 above; this
  phase adds the field definition, not the logic that populates it.)
- `findings/merge.py`: add `_SEVERITY_RANK: dict[Severity, int]`
  (`critical=0, high=1, medium=2, low=3, info=4`). Change `merge_findings`'s
  ceiling-truncation from `deduped[:POSTED_FINDINGS_CEILING]` to
  `sorted(deduped, key=lambda f: (_SEVERITY_RANK[f.severity], 0 if f.source
  != "llm" else 1))[:POSTED_FINDINGS_CEILING]` — Python's `sorted()` is
  stable, so original relative order is preserved as the tiebreak for
  free, with no extra tiebreak code needed (US-V8's third ordering
  criterion). `_dedupe`'s own logic (the similarity-match rule, the
  `context_ref_ids`-forwarding merge) is completely unchanged — only what
  survives the ceiling changes, exactly as the spec requires.
  **Trust ranking is source-based, not `verification_status`-based**
  (Assumption, implicit in the spec's own decided wording): by the time a
  finding reaches `merge_findings`, any `source == "llm"` finding has
  already survived verification (or verification was disabled entirely,
  restoring exact Phase 1 behavior for that deployment) — `source` alone
  is a well-defined, always-present signal for this tiebreak regardless of
  whether verification ran, so no additional check against
  `verification_status` is needed here.

**Tests:** Unit (`findings/test_merge.py`, extended — a deduped list
exceeding `POSTED_FINDINGS_CEILING` keeps the top-`N` ordered by severity
then source-trust then original order; a mixed-severity list under the
ceiling is unaffected — the sort only matters once truncation actually
happens). Unit (`findings/test_models.py`, extended — `verification_status`
defaults to `None`, accepts only `"verified"`/`"rejected"`/`None`).

**Observability & rollback:** Pure logic change, no I/O, no migration
(`Finding` is not a persisted model anywhere in this codebase today —
confirmed: no `findings`/`findings_history` table exists in
`adapter/storage/models.py` or its migrations). Reversible by a plain code
revert.

**Acceptance:** US-V8's `Given/When/Then` passes as a unit test; the new
`verification_status` field's default/shape is proven by a unit test.

## Phase 5 — Composition-root wiring (Unit E)

**Why last:** every step this phase registers must already exist (Phases
1–3) and every model change it depends on must already exist (Phase 4).

- `review/config.py`:
  - Rename `StepEnablement.llm_review_enabled` →
    `review_generation_enabled`; `StepTimeouts.llm_review_step_timeout_seconds`
    → `review_generation_step_timeout_seconds`. Add
    `intent_summary_enabled: bool = True`, `verification_enabled: bool =
    True` to `StepEnablement`; `intent_summary_step_timeout_seconds: int =
    30`, `verification_step_timeout_seconds: int = 60` to `StepTimeouts`
    (defaults per the spec's own NFR-stated values). Update both
    `as_mapping()` methods' dict keys: `"llm_review"` → `"review_generation"`,
    plus new `"intent_summary"`/`"verification"` entries.
  - Add `intent_summary_can_produce_value(enablement: StepEnablement) ->
    bool` — `enablement.intent_summary_enabled and
    enablement.review_generation_enabled` — mirroring
    `context_builder_can_produce_value`'s own shape/reasoning exactly
    (its one real consumer, `review_generation`, must also be enabled, or
    running it wastes a full LLM call on an `intent_summary` field nothing
    reads).
  - Add **one small, concrete, hard-coded-to-this-one-chain** function
    (Assumption 6, revised after review to drop the originally-proposed
    generic `Sequence[str]` utility): `resolve_review_pipeline_depends_on(
    enablement: StepEnablement) -> dict[str, str]`. Internally: computes
    `sa_can_produce_findings`/`upstream_of_context_builder` exactly as
    today, applies `context_builder_can_produce_value`/
    `intent_summary_can_produce_value` to get each step's own effective
    enabled state, then resolves `depends_on` for exactly the five named
    steps (`context_builder`, `intent_summary`, `review_generation`,
    `verification`, `output`) by walking that fixed, named order —
    nothing here accepts or expects a different chain; there is exactly
    one review pipeline. Returns the five-entry `dict[str, str]`. Pure, no
    I/O, ADR-0005-registered per its own process (this phase's deliverable
    includes updating that ADR's scope list).
  - `check_step_configuration_viable`: rename the `llm_review_enabled`
    reference to `review_generation_enabled`. Keep both existing hand-written
    checks unchanged (`static_analysis` enabled + `prefilter` disabled;
    `review_generation`\* enabled + SA can't produce + `prefilter`
    disabled — \*renamed from `llm_review_enabled`, logic otherwise
    identical). **Add one more check, additive, not a replacement**: call
    `resolve_review_pipeline_depends_on(enablement)`, then for each of
    its five named entries, if that step's own effective-enabled state is
    `True` and its resolved target's effective-enabled state is `False`,
    raise `NoViableStepConfigurationError` naming both. This is a strict
    superset of what the two existing checks already catch for
    `review_generation` specifically — kept alongside them, not replacing
    their (already-shipped, already-tested) logic, so this phase's diff to
    already-working validation code is additive only.
  - **`review/output.py` is genuinely left unchanged**, per the PRD's own
    "extends, not replaces" scope statement — `FindingsOutputStep`'s
    default `depends_on: str = "llm_review"` parameter is dead code in
    production (the composition root always passes an explicit value) and
    is **not** edited by this plan; touching it would be exactly the kind
    of unrequested "while I'm in here" cleanup the PRD's own scope
    boundary already rules out for this file.
- `apps/worker/settings.py`:
  - Rename `llm_review_enabled` → `review_generation_enabled`,
    `llm_review_step_timeout_seconds` → `review_generation_step_timeout_seconds`.
    Add `intent_summary_enabled: bool = True`,
    `intent_summary_step_timeout_seconds: int = 30`,
    `verification_enabled: bool = True`,
    `verification_step_timeout_seconds: int = 60`.
  - `stuck_job_warning_threshold_seconds` default: `450.0` → `600.0`.
    Update the field's own comment: the macro chain's worst-case healthy-run
    sum is now `30 (prefilter) + 240 (static_analysis) + 35
    (context_builder) + 30 (intent_summary) + 90 (review_generation) + 60
    (verification) + 30 (output) = 515s`; `600` preserves the existing
    ~15% headroom convention (515 × 1.15 ≈ 592, rounded up).
- `apps/worker/main.py`:
  - Replace `from review.llm_review import LLMReviewStep` with
    `from review.intent_summary import IntentSummaryStep`,
    `from review.review_generation import ReviewGenerationStep`,
    `from review.verification import VerificationStep`.
  - `_build_step_enablement`/`_build_step_timeouts`: pass through the four
    new/renamed fields.
  - `_build_review_registry`: replace the current inline
    `enabled_mapping["context_builder"] = context_builder_can_produce_value(enablement)`
    override and the single `llm_review_depends_on` ternary with:
    `enabled_mapping` still built the same way as today (`enablement.as_mapping()`
    plus the `context_builder`/new `intent_summary` overrides — the raw
    dict-building stays in `apps/worker/main.py`, only the *chain
    resolution* moves), then `resolved =
    review.config.resolve_review_pipeline_depends_on(enablement)`.
    Construct `IntentSummaryStep(depends_on=resolved["intent_summary"])`,
    `ReviewGenerationStep(depends_on=resolved["review_generation"])`
    (replacing `LLMReviewStep(...)`), `VerificationStep(depends_on=resolved["verification"])`,
    and `FindingsOutputStep(depends_on=resolved["output"])` (replacing its
    current `depends_on="llm_review" if enablement.llm_review_enabled else
    "static_analysis"` ternary). `terminal_step="output"` is unchanged.
  - **New: a one-time startup check for deprecated settings names, covering
    both real process env vars and a `.env` file** (Assumption 7/8,
    revised after a second review round found the first version
    incomplete). `WorkerSettings.model_config` sets `env_file=".env"`
    (`apps/worker/settings.py:121-126`); pydantic-settings' dotenv source
    reads that file via `dotenv_values()` and **never populates
    `os.environ`** with its contents — so a deprecated key set only in a
    `.env` file (one of the two scenarios Assumption 7 itself names as
    motivating this check) would bypass a plain `os.environ`-only lookup
    entirely, silently no-opping exactly as before this fix. The check —
    `_check_deprecated_settings_env_vars(logger: logging.Logger, *,
    env_file: str = WorkerSettings.model_config["env_file"]) -> None`
    (the default is **read from `WorkerSettings.model_config` itself**,
    not independently hard-coded to `".env"` a second time — the exact
    "two definitions could drift" risk Assumption 6 already names
    elsewhere: if `WorkerSettings`' own `env_file` location ever changes,
    this check must never silently stop reading the file the real
    settings loader actually uses) — builds one case-insensitive set of
    **key names** (membership, never value truthiness — a deprecated var
    present but set to an empty string, e.g.
    `REVIEW_WORKER_LLM_REVIEW_ENABLED=` in `.env`, must still trigger the
    warning) from **both** `os.environ` and, if the file exists,
    `dotenv.dotenv_values(env_file)` (the same loader pydantic-settings
    itself uses internally, already an installed transitive dependency via
    `pydantic-settings`'s own dependency on `python-dotenv` — no new
    dependency added), before comparing against the (case-folded)
    deprecated names `REVIEW_WORKER_LLM_REVIEW_ENABLED`/
    `REVIEW_WORKER_LLM_REVIEW_STEP_TIMEOUT_SECONDS`. Each name present logs
    its own `warning` naming that specific old/new pair (not one combined
    message referencing both regardless of which is actually set) —
    called from `main()` right after `configure_logging`, before
    settings-dependent startup checks run. A `warning`, not a fail-fast
    `SystemExit(1)`, because the deployment can still boot and run
    correctly on the new field's default; what's lost is only the
    operator's *prior, now-silently-overridden* intent — the same
    "deviation visible, not silent" severity this codebase already applies
    to `secret_redaction_enabled=False`, not the "this configuration
    cannot work" severity the existing `SystemExit(1)` checks are reserved
    for.
  - **New: the `verification_enabled=False` startup warning US-V9
    requires.** A small addition alongside `_build_text_redactor`'s
    existing `secret_redaction_enabled=False` one-time `warning` log
    (`apps/worker/main.py:364-380`) — the identical pattern, applied to
    `settings.verification_enabled is False`: logs a `warning` stating
    unverified LLM findings will be posted directly. (This was named in
    the PRD's US-V9 and NFR sections but was missing from an earlier draft
    of this plan's own deliverables — added here.)
  - **New: the resolved effective-enabled/depends_on mapping is logged at
    startup.** `_check_step_configuration`'s existing "step configuration
    valid" `info` log (`apps/worker/main.py:434-437`) already logs
    `enabled_mapping`/`timeouts_mapping`; extended to also log
    `resolve_review_pipeline_depends_on(enablement)`'s own result — so an
    operator can confirm from Worker startup logs alone which of the five
    dynamically-wired steps ended up pointing at which upstream, closing
    the gap where a silently-dropped deprecated env var (the check above
    catches the *old-name* case, but not every possible misconfiguration)
    would otherwise be invisible until a job's own step-level logs are
    inspected.

**Tests:** Unit (`review/test_config.py`, new —
`resolve_review_pipeline_depends_on` against a representative set of
enabled/disabled combinations, including: all enabled; each of the five
named steps individually disabled; `context_builder`/`intent_summary`
disabled via their own `*_can_produce_value` override rather than the raw
flag; `intent_summary_can_produce_value`'s own two-input truth table; and
`check_step_configuration_viable`'s new additive check rejecting a
configuration the two existing checks don't already catch, while
confirming both existing checks still reject exactly what they rejected
before this phase). Integration/Unit (`apps/worker/test_main.py`, extended —
the real `_build_review_registry` output for representative
enabled/disabled combinations, mirroring its existing
`context_builder`/`llm_review` coverage now extended to the three new
steps; new cases for the deprecated-settings startup warning covering
both sources and the case-insensitivity fix independently: (1) only
`REVIEW_WORKER_LLM_REVIEW_ENABLED` set via `os.environ` → only that
name's warning fires; (2) only the timeout var set via a temp `.env` file
(not `os.environ`) → its own warning fires, proving the dotenv-file path
is actually read, not just the process-env path; (3) a differently-cased
variant (e.g. `review_worker_llm_review_enabled`) → still detected; in
every case the Worker still starts successfully; a new case for the
`verification_enabled=False` startup warning, mirroring the existing
`secret_redaction_enabled=False` test case; a new case asserting the
resolved depends_on mapping appears in the "step configuration valid"
startup log. `apps/worker/test_settings.py`,
extended — the renamed/new settings fields round-trip. `review/test_review_job.py`,
extended — US-V7: a raising fake `LLMClient` injected at the
`verification` step's call site, run against the real `StepRegistry`
(`FailurePolicy.FATAL`), asserts the job is marked `FAILED`, no findings
are posted, and a failure check-run is posted — mirroring
`plan-deliver-context-builder.md`'s own precedent for testing its
equivalent failure story at this same level, not inside the failing
step's own unit test).

**Observability & rollback:** The `review_generation_enabled`/
`review_generation_step_timeout_seconds` bootstrap-config rename (from
`llm_review_*`) is a deliberate breaking change — confirmed via a
repo-wide grep that no `docker-compose.yml`/Helm chart value references
either old name, so the rename's blast radius is contained to this
codebase's own settings/wiring/tests. Every new step defaults to enabled;
disabling any one of `intent_summary_enabled`/`verification_enabled` falls
back correctly via the resolved chain, restoring exactly Phase 1/2's
existing behavior for that deployment (US-V9). Toggling any flag requires
a Worker restart (bootstrap config, read once at startup), identical to
every existing per-step flag.

**Acceptance:** US-V9's `Given/When/Then` passes as an integration test,
including its second scenario (the `verification_enabled=False` startup
warning); US-V7's `Given/When/Then` passes as an integration test (see
Phase 3's own acceptance note); the recalculated
`stuck_job_warning_threshold_seconds` default is applied; both of
`check_step_configuration_viable`'s existing hand-written checks still
pass their existing test cases unchanged, and the new additive check is
proven against a case neither existing check already caught; the
deprecated-env-var startup warning fires for the old settings names and
the Worker still starts successfully.

## Security review

One pass across every new input/trust boundary this plan as a whole
introduces:

- **No new credential type, no new write capability, no new Postgres
  table.** `intent_summary` and `verification` both reuse the existing
  `LLMClient`/LLM Gateway bootstrap config (`llm_endpoint`/`llm_api_key`/
  `llm_model`) `review_generation` already uses — no new secret, no new
  outbound connection type.
- **Diff/context-bundle content reaching two additional LLM calls** —
  not a new trust boundary, the same diff/context content
  `review_generation` already sends to the operator's configured
  endpoint, sent twice more per review (once for intent summarization,
  once for verification). No new category of data crosses the deployment
  boundary; `plan-deliver-context-builder.md`'s own Security review
  already named and accepted the "arbitrary existing repo content reaching
  the LLM prompt" risk for the context-bundle section specifically — this
  plan's `verification` step reuses that exact same, already-labeled
  section (`render_context_bundle_section`, Assumption 3) rather than
  introducing a second, unlabeled path for the same content.
- **Verification's own response schema is deliberately narrow enough to
  introduce no new redaction surface** (Phase 3): `_VerificationVerdictItem`
  carries only an `index` and four booleans — no free-text field an LLM
  could use to echo diff/secret content back into a log or a posted
  comment. The intent summary's own free text (Phase 1) never becomes a
  `Finding.message` or any other posted field directly — its only
  downstream consumer is `review_generation`'s own prompt, and any content
  it echoes that later surfaces in a `source="llm"` finding's `message`
  is caught by that finding's own existing, unchanged redaction pass.
- **Fail-closed verdict correlation (Assumption 5)**: an LLM response that
  omits, duplicates, or misindexes a verdict never results in an
  under-verified finding silently passing through — it's excluded
  entirely (treated as rejected), the same direction of failure this
  codebase already applies to a malformed redaction call or a malformed
  individual `_LLMFindingItem`.
- **Resource exhaustion**: bounded by the existing per-finding-count
  ceilings already in place before verification ever runs
  (`REDACTION_FINDING_CEILING=20` on `review_generation`'s own LLM-sourced
  findings, applied before verification ever sees them) — verification's
  own batch size is therefore already capped at 20 candidate findings per
  job, with no new ceiling needed at this layer. The batched-call design
  itself (Scope, spec Assumption 1) is the primary defense against a
  finding-count-scaling cost multiplier a per-finding-call design would
  have introduced.
- **A stale, deployment-owned bootstrap-config value silently no-opping
  after the rename** (Phase 5) — a real gap a subsequent review found:
  `WorkerSettings`' `extra="ignore"` config means an operator's own,
  externally-owned `REVIEW_WORKER_LLM_REVIEW_ENABLED`/
  `REVIEW_WORKER_LLM_REVIEW_STEP_TIMEOUT_SECONDS` would silently stop
  taking effect on upgrade, re-enabling a step (or resetting its timeout)
  the operator had deliberately configured differently, with zero signal.
  **Fixed**, not merely accepted: Phase 5 adds an explicit startup check
  (`_check_deprecated_settings_env_vars`) that logs a `warning` naming
  the old/new pair for whichever names are found. **Revised after a
  second review round found the first version of this fix incomplete**:
  checking `os.environ` alone misses a deprecated key set only in a
  `.env` file — `WorkerSettings` reads `.env` via pydantic-settings' own
  dotenv source, which never populates `os.environ` — so the check now
  also reads `.env` directly via `dotenv.dotenv_values()` (the same
  already-installed loader pydantic-settings itself uses), and matches
  case-insensitively (pydantic-settings' env source itself is
  case-insensitive by default, so a differently-cased deprecated key
  would otherwise still silently take effect in settings parsing while
  bypassing an exact-case-only check).
- **Verification judges LLM output against the same attacker-influenced
  diff/context content that produced it — a structural limitation, not
  something the index-based fail-closed design mitigates.** A subsequent
  review correctly named this: a diff crafted to manipulate
  `review_generation`'s output could, in principle, also manipulate
  `verification`'s own judgment of that same output (e.g. steering it
  toward approving a hallucinated finding as `valid`/`actionable`/
  `specific`/`valuable`), since both calls read the same untrusted input.
  **Named, accepted residual, not fixed here** — the spec's own decided
  design (a second LLM call against a fixed rubric, not code execution or
  an independent non-LLM signal) is what the roadmap's Phase 3 bullet
  asks for, and no control at this layer eliminates a sufficiently
  well-crafted prompt-injection attempt; the two structural mitigations
  this plan does add (the "inert reference data" framing on every section
  reaching either LLM call, per Assumption 3's cross-reference above, and
  the fail-closed correlation design) reduce the attack surface's
  reliability without claiming to close it. Worth revisiting if a
  stronger guarantee is ever needed — the same posture
  `plan-deliver-context-builder.md`'s own Security review already takes
  for its structurally identical "arbitrary existing repo content reaching
  the LLM prompt" risk.

## Compatibility & rollout

- **No existing API/schema contract breaks, beyond the deliberate,
  documented bootstrap-config rename** (`llm_review_enabled`/
  `llm_review_step_timeout_seconds` → `review_generation_*`, Phase 5) —
  every model change is a new, optional (`| None = None`) field on an
  existing shape (`_ReviewGenerationInput.intent_summary`,
  `Finding.verification_status`), defaulting every pre-existing
  construction site to unaffected. No migration: `Finding` is not
  persisted anywhere in this codebase today. Confirmed via grep that no
  `docker-compose.yml`/Helm chart value in this repo references either old
  settings name — but since that only proves this repo's *own* committed
  config is unaffected, not any customer's externally-owned deployment
  config, Phase 5's new `_check_deprecated_settings_env_vars` startup
  check is the actual safety net for the latter (see Security review).
- **Staged rollout via per-step kill switches** (Phase 5):
  `intent_summary_enabled`/`review_generation_enabled`/
  `verification_enabled` each default to `True`; disabling any one
  restores exactly the immediately-prior phase's behavior for that
  deployment via the resolved fallback chain — the same "operator can
  always opt back toward Phase 1/2's shipped behavior" safety net every
  prior phase's own kill switch already provides.
- **Dependency-failure behavior**: `intent_summary`/`review_generation`/
  `verification` are all macro steps under the existing
  `FailurePolicy.FATAL` registry — an LLM Gateway failure/timeout/parse
  error at any of the three fails the job cleanly (US-V7), posts a
  failure-conclusion check-run via the unchanged terminal `output` step,
  and posts no findings — the same, already-proven failure contract
  `review_generation`/`llm_review` has had since Phase 1.
- **The change is fully undoable** via any combination of the three new
  kill switches, or a plain code revert — no data cleanup ever required
  (no new persisted rows at any phase).

## Traceability: PRD user story → test

| Story | Phase | Test path | Level |
| --- | --- | --- | --- |
| US-V1 | 1 | `review/test_intent_summary_step.py` | Integration |
| US-V2 | 2 | `review/test_review_generation_step.py` | Integration |
| US-V3 | 3 | `review/test_verification_step.py` | Integration |
| US-V4 | 3 | `review/test_verification_step.py` | Integration |
| US-V5 | 3 | `review/test_verification_step.py` | Integration |
| US-V6 | 3 | `review/test_verification_step.py` | Integration |
| — (verification_status schema) | 4 | `findings/test_models.py` | Unit |
| US-V8 | 4 | `findings/test_merge.py` | Unit |
| — (verdict parse error, fail-closed correlation) | 3 | `review/test_verification.py` (unit), `review/test_verification_step.py` (integration) | Unit + Integration |
| US-V7 | 5 | `review/test_review_job.py` | Integration¹ |
| US-V9 | 5 | `review/test_config.py` (Unit — chain resolution) + `apps/worker/test_main.py` (Integration — real registry wiring and the two new startup-warning cases) | Unit + Integration |
| US-V1–V9 (happy path) | 5 | `tests/e2e/test_review_pipeline.py` (extended) | E2E |

¹ Labeled `Integration`, not `Unit`, despite exercising a Protocol-level
fake (`LLMClient`) with no real I/O — `testing.md`'s own boundary
definition would literally classify this as Unit. Kept as `Integration`
deliberately, for consistency with the already-shipped, identical-shape
precedent this same story's PRD counterpart cites
(`spec-review-pipeline.md`'s US8 row, same test file, same
fake-at-the-registry-level mechanism) rather than introducing a
differently-labeled test for the same kind of scenario across sibling
specs/plans.
