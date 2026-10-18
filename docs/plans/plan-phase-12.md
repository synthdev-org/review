# Plan: Phase 12 — PR-level walkthrough artifact

**Status: Proposed.** Sequences
[`docs/specs/spec-pr-walkthrough.md`](../specs/spec-pr-walkthrough.md) —
[`docs/roadmap.md`](../roadmap.md)'s Phase 12 — into five buildable,
independently-testable phases, following the phase / tests / observability &
rollback / acceptance shape of [`plan-phase-11.md`](plan-phase-11.md). Built
against [`docs/rules/architecture.md`](../rules/architecture.md) (verification
tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

This plan does not re-litigate the spec's calls: supplement not replace (D1),
deployment-wide default-off toggle (D2), no diagrams (D3), a new step before
`output` (D4), carrying `intent_summary` and `reviewed_diff` forward (D5),
comment identity by marker plus App author (D6), the existing permission (D7)
and the default numbers (D8). It sequences the work and records the
implementation-shape choices the spec leaves open as **Assumptions** below.
Reading the code during drafting and four review passes produced these spec
corrections, all applied to the spec on 2026-10-18: `VerificationOutput` and
`FixGenerationOutput` carry no `diff`, and `FixGenerationStep` fetches the diff
when its mirrored `diff` is `None` (`fix_generation.py:350-353`), so the carried
field is named `reviewed_diff`, not `diff`, to leave Phase 11's fetch untouched
(D5); `output` is the fatal terminal step with a 30 s timeout, so the
walkthrough step posts the comment itself (D4); the LLM call keeps the existing
`llm_timeout_seconds` while the step reserves time to publish (US-W8, US-W11);
three outcomes were added, `skipped_comment_limit`, `dedupe_failed` and
`internal_error`, and `internal_error` lines carry `stage` and `exc_type`
(US-W12); identity matching fails closed (US-W4); backslash escaping does not
stop GitHub linking issue references, so they are broken explicitly (US-W5); no
walkthrough is posted when redaction is the no-op, and a `gitleaks:allow` marker
is stripped before redaction (US-W5).

**Delivery unit.** Phases 3 and 4 land in one release (one branch, as Phases
9–11 did): Phase 3 wires the chain to a `walkthrough` step only when the step is
effective, so a build with Phase 3 but not Phase 4 would reference an
unregistered step if an operator enabled the flag. The setting is not
documented for operators until Phase 5.

This is additive work on running code. Each reference below was checked against
the file during drafting or review: `review/step.py` (`Step`, `StepContext`
including `dry_run`, `JobIdentity`, `job_identity_fields`),
`review/clone_detection.py` and `review/fix_generation.py` (the fail-open macro
steps to copy: injected `internal_timeout_seconds`, `asyncio.wait_for`,
constructor-injected `depends_on`, `model_resolver`, module-level `meter`
instruments named `review.<step>.outcome_total` including a per-phase duration
histogram), `review/_telemetry.py` (`start_span`, `suppress_telemetry_errors`),
`review/intent_summary.py` (`_cap_intent_summary`, `MAX_INTENT_SUMMARY_CHARS`),
`review/diff_prompt.py` (`render_diff`, `cap_sequence_length`,
`INERT_REFERENCE_DATA_FRAMING`), `review/product_knowledge.py`
(`_STRIP_CATEGORIES`, `_clean_text`), `review/review_generation.py`
(`ReviewGenerationOutput`: `diff`, no `intent_summary`), `review/verification.py`
(`VerificationOutput`: `findings`, `head_sha`, `context_bundle`; built from
explicit kwargs), `review/prefilter.py` (`PrefilterOutput.diff`),
`review/llm_response.py` (`build_completion_request`, `parse_llm_json_envelope`),
`review/llm_telemetry.py` (`record_llm_token_usage`), `review/config.py`
(`StepEnablement.as_mapping`, `StepTimeouts.as_mapping`,
`resolve_review_pipeline_depends_on`, `check_step_configuration_viable`),
`review/output.py` (`FindingsOutputStep`, `FindingsOutputInput`: required
`findings`, identity fields, `head_sha`), `review/pipeline.py` (single-dependency
steps; `self._timeouts[step.name]` lookup; no output serialization),
`review/test_step_boundaries.py`, `findings/markdown.py` (`escape_markdown`,
`_ZERO_WIDTH_SPACE`, `suggestion_block`'s longest-backtick-run fence) and
`findings/test_markdown.py`, `findings/redact.py` (`TextRedactor.redact`,
`NoOpTextRedactor`), `adapter/analyzers/gitleaks.py` (`GitleaksTextRedactor`:
one subprocess per call, whole-line replacement, default config) and
`adapter/analyzers/test_gitleaks.py`, `ingestion/diff.py` (`Diff`,
`DiffFile.path/status/patch`), `adapter/github/posting.py`
(`GitHubFindingsPublisher`: constructor shape, `_post`, `_split_owner_repo`
wrapping `tenancy.resolution.split_owner_repo`), `adapter/github/http.py`
(`auth_headers`, `client_context`, `request_with_retry`), `adapter/github/diff.py`
(following `response.links.get("next")`), `adapter/github/exceptions.py`
(`GitHubAdapterError`, `GitHubPostingError`, `GitHubPostingUnreachableError`),
`llmgateway/routing.py` (`KNOWN_MODEL_ROUTING_STAGES`), `apps/worker/settings.py`
(`fix_generation_*` fields, `_fix_generation_settings_are_valid`,
`_stuck_job_warning_threshold_covers_worst_case_chain`, `github_app_id`,
`secret_redaction_enabled`), `apps/worker/main.py` (`_build_step_enablement`,
`_build_step_timeouts`, `_check_step_configuration`, `_build_review_registry`, the
`enabled_mapping` overrides for `context_builder`/`intent_summary`),
`apps/worker/test_main.py`, `charts/review/templates/configmap.yaml`,
`docker-compose.yml`, `charts/review/dashboards/review.json`,
`tests/e2e/test_review_pipeline.py` (`_StubGitHubHandler`),
`tests/e2e/test_auto_fix.py` (E2E pattern to copy), `tests/e2e/test_observability.py`
(`_PANELS_WITHOUT_E2E_TRAFFIC`), and `docs/reference/operations.md`.

No migration is needed: the spec adds no table or column. Test files this plan
creates are marked **(new)**; everything else named already exists. All
integration tests carry `@pytest.mark.integration`.

## Scope

**In scope.** Spec Units A–E: the pure walkthrough model, sanitizer and
renderer; GitHub issue-comment create/edit-in-place/dedupe with fail-closed
identity; the `walkthrough` step with `intent_summary` and `reviewed_diff`
carried forward; settings, chain wiring, `walkthrough` routing stage,
Helm/Compose; metrics, alerts, dashboard panels, runbook, E2E.

**Out of scope** (all named in the spec's "Out of scope"):
- Mermaid/diagram generation — a follow-up phase with its own validation
  design, likely an ADR; not yet tracked beyond the spec.
- A per-org/repo toggle via `OrgRepoTeamSetting` — follow-up, triggered by
  pilot feedback on noise.
- Replacing or restyling findings comments; product-knowledge citations;
  Memory capture on the walkthrough comment; Admin UI controls beyond the
  routing stage row; GitLab/Bitbucket.

## Assumptions this plan makes (flagged, not decided)

1. **The step posts the comment itself** through a `WalkthroughPublisher`
   Protocol and passes its input through to `output` unchanged.
   `FindingsPublisher`, `FindingsOutputStep` and `FindingsOutputInput` gain no
   field or method.
2. **Reviewable files** = the carried diff's files (already narrowed by
   `prefilter`) whose `patch` is not `None`. The `min_files` check (US-W3) and
   the "not shown" count use that set. The step input accepts `reviewed_diff`
   (after `verification` or `fix_generation`) or `diff` (after
   `review_generation`), preferring `reviewed_diff`. A diff that
   `fix_generation` fetched itself is never the one carried.
3. **The step is effective only when** `walkthrough_enabled`,
   `review_generation_enabled` (the one configuration where every predecessor
   shape is known to carry a diff) and `secret_redaction_enabled` (a real
   redactor; with `NoOpTextRedactor` the posted text would be unredacted, and
   `intent_summary` text is posted here for the first time) all hold. To keep one
   answer everywhere, `StepEnablement` gains `walkthrough_enabled: bool = False`
   and `secret_redaction_enabled: bool = True` (both defaulted, matching
   `WorkerSettings` and the `fix_generation_enabled` pattern, so the roughly 40
   existing partial `StepEnablement(...)` constructions need no edit). The
   `secret_redaction_enabled` field is not a step: it is excluded from
   `as_mapping()` and the class docstring says so. `_build_step_enablement` sets
   both, and the resolver, `check_step_configuration_viable` and the `main.py`
   `enabled_mapping` override all read the same pure
   `walkthrough_can_produce_value(enablement)`.
4. **App authorship** is matched on `performed_via_github_app.id` equalling
   `settings.github_app_id` (compared with `type(x) is int`, so a string, a
   float or a boolean never matches) **and** the marker **and** the fixed footer
   line. A missing or null field never matches (fail closed); no login or
   marker-only fallback exists. The field's real shape is pinned by a recorded
   cassette in Phase 2.
5. **Marker** is `<!-- review-walkthrough:v1 -->` as the first body line.
6. **Prompt input bounds** are fixed module constants (per-file patch slice of
   2000 characters; at most `walkthrough_max_files` files; a per-path length cap),
   assembled with the existing `diff_prompt` helpers and
   `INERT_REFERENCE_DATA_FRAMING`. Patch text goes to the endpoint raw, exactly as
   `intent_summary` already sends the diff.
7. **Redaction is one call, on exactly the display strings.** For every string
   bound for the comment (summary, group names and descriptions, notes, paths):
   (a) a path is first reconciled against the raw diff path; (b) the display
   string is derived by removing the characters in the shared strip set
   (`_STRIP_CATEGORIES`: Cc, Cf, Cs, Co, Zl, Zp, hoisted from
   `review/product_knowledge.py` into a helper beside `escape_markdown`, which
   `product_knowledge.py` then imports back) and collapsing whitespace with
   `" ".join(s.split())`; (c) every `gitleaks:allow` marker is removed in a loop
   until none remain (a single replace is defeated by
   `gitleaks:gitleaks:allowallow`); (d) the string is capped with
   `cap_sequence_length` to a generous constant that exists only to bound the
   single redaction blob (the summary keeps `_cap_intent_summary`'s existing bound
   and gains no new cap; group names and descriptions share one constant; notes
   and paths have their own); (e) all strings, trimmed first to
   `walkthrough_max_files` entries and a total budget of about
   `walkthrough_max_chars` plus slack (with the group count and notes per group
   also capped in `parse_walkthrough_response`), are joined with a sentinel line
   `WT-BOUNDARY-<nonce>`, where the nonce is `secrets.token_hex(16)` from an
   injectable generator, never logged and never in the prompt, and the prefix is
   keyword-free so a Gitleaks rule cannot blank it; (f) the blob is redacted once
   (one Gitleaks subprocess, not hundreds) and split; (g) if any input contained
   the nonce, or the split count is not the input count, the result is
   `internal_error` and nothing is posted (count plus containment is sufficient:
   `GitleaksTextRedactor` replaces whole lines, so a sentinel line can only
   disappear or be replaced); (h) the user-visible length caps are then applied to
   the redacted text, so a secret straddling a cap is redacted before it is cut;
   and only that redacted result is rendered. Redaction is best-effort, as for
   findings: a secret split across fields or paraphrased may survive. A known
   `internal_error` cause is a multi-line scanner rule (a private-key block)
   spanning sentinel lines when a hostile PR supplies a BEGIN-style path and an
   END-style note; it fails closed and the runbook lists it. The join, redact and
   split live in one helper, `redact_all(strings, redactor, nonce_factory)`, in
   `review/walkthrough_render.py`, unit-tested and on the ADR-0005 list.
8. **The Protocol lives with its data model** in `review/walkthrough_render.py`
   (ADR-0006), since the renderer's output model is what it publishes.
9. **Stuck-job threshold is unchanged at 700.** The validator sums every step's
   timeout whether or not it is enabled, so the walkthrough timeout is added
   only when `walkthrough_enabled` is true (a conservative superset of
   "effective"; the same raw-flag keying applies to the `llm_timeout_seconds`
   coupling, and the validator docstring and runbook say so). With the flag on
   the worst case is 680 s (3% headroom, below the ~10% Phase 11 precedent —
   accepted and stated in the runbook); with it off the sum and every operator's
   existing override are unchanged.
10. **Concurrency.** The queue claims jobs with `SKIP LOCKED` and has no per-PR
    key (`adapter/storage/queue.py`), so two jobs for one PR can overlap and a
    create race is possible. The dedupe is kept but bounded: it can delete only
    the comment its own `POST` returned, by id. On a race the surviving comment
    may briefly carry an older push's content; its footer names the commit it
    describes — accepted.
11. **Latency trade-off, accepted:** the step runs before `output`, so findings
    post up to `walkthrough_step_timeout_seconds` later when the LLM or GitHub
    is slow (typically one LLM call). Stated in the runbook.
12. **The step owns the time budget; the adapter owns none.** Following
    `FixGenerationStep`, the step wraps its body in `asyncio.wait_for(
    internal_timeout_seconds)` and wraps `await publisher.upsert(...)` in
    `asyncio.timeout(min(publish_reserve, remaining))` itself. The 45 s default
    covers an LLM call at the existing 30 s limit, a redaction subprocess (10 s
    default) and a publish reserve only when redaction is fast; the validator
    guarantees `step timeout >= llm_timeout_seconds + 15`, and a slow redactor
    can consume the publish reserve. Because of that, a timeout is mapped to its
    outcome by the stage reached (see Phase 4), not always to `llm_timeout`.
13. **Rendered GitHub behavior is not CI-testable.** No new dependency is added
    for a Markdown parser. Unit tests assert the neutralized output; the rendered
    behavior of the hostile fixtures (no link, image, notification or
    cross-reference) is verified by hand on a throwaway test-org PR before release
    and after any change to `neutralize`, with a recorded result of "passed".
14. **Metric names** follow `fix_generation.py`: meter `review.walkthrough`,
    counter `outcome_total{outcome}` (the nine values, a closed set), histograms
    `duration_ms{phase}` (`llm`, `redact`, `publish`) and `rendered_chars`. The
    exported Prometheus names (`review_walkthrough_outcome_total` and so on) are
    asserted in a Phase 4 test and used verbatim by the Phase 5 alerts and panels.

## Phase 1 — Pure walkthrough model, sanitizer and renderer (spec Unit A)

**Why first:** nothing untrusted may reach a PR unsanitized, and everything
later builds on this model. Pure code, no I/O, so it is the cheapest place to
fix the trust boundary.

- New `review/walkthrough_render.py`:
  - pydantic models for the model's structured output (groups: name,
    description, ordered files each with a one-line note) and the rendered
    comment, plus the `WalkthroughPublisher` Protocol (`upsert(...)`, one
    method, taking the job identity so adapter logs can carry `job_id` and
    `head_sha`) per Assumption 8.
  - `parse_walkthrough_response` via the existing
    `review.llm_response.parse_llm_json_envelope`. That helper drops malformed
    items and only logs when all are dropped, so this function itself raises the
    typed parse error when no valid group survives (an all-garbage response must
    be `parse_failure`, never an empty "Other files" comment), and caps the group
    count and the notes per group (US-W8).
  - `reconcile_files`: drop unknown and duplicate paths, append omitted
    reviewable files as a final "Other files" group in diff order (US-W2),
    always on raw paths.
  - `redact_all(strings, redactor, nonce_factory)` per Assumption 7.
  - `render_comment`: fixed structure (title, Summary, groups, "not shown"
    line, footer), the marker, a fixed footer stating it is an AI-generated
    summary and not a review verdict plus the head commit, the
    `walkthrough_max_chars` cut at a group boundary, the oversize-first-group
    rule, and a hard ceiling under 65,536 characters (US-W6). File paths render
    in a code span whose delimiter is longer than the longest backtick run in
    the path (the `suggestion_block` technique), padded with a space when the
    path starts or ends with a backtick; a path's display form is the already-
    redacted string of Assumption 7, not the raw path.
- In `findings/markdown.py`, one **added** function (walkthrough-only;
  `escape_markdown` behavior and findings output are unchanged), the module
  docstring explaining why it is not folded into `escape_markdown` (it would
  corrupt finding messages). `escape_markdown` backslash-escapes Markdown
  punctuation, entity-encodes `<` `>` and breaks `@mentions`; it does **not**
  stop GitHub linking issue references, because GitHub links them on the
  rendered text (so `\#123` still renders and links as `#123`). For prose
  fields (summary, group names and descriptions, notes) the pipeline order is:
  (1) the shared strip set and whitespace collapse (Assumption 7(b), done
  first because the zero-width space this helper inserts is itself a Cf
  character and must not be stripped afterwards); (2) on that raw text, break
  `#N`, `owner/repo#N`, `GH-N` and runs of 7–40 hex characters, and break `://`,
  `www.` and the `@` of an email address, using the module's `_ZERO_WIDTH_SPACE`
  (before `escape_markdown`, because its backslashes would otherwise sit between
  the characters these patterns match); (3) escape `&` to `&amp;` (before
  `escape_markdown` emits its own `&lt;` and `&gt;`, so they are not
  double-escaped); (4) `escape_markdown`. File paths get no hex or reference
  breaking (GitHub does not autolink inside a code span, and breaking would
  corrupt a real path such as `migrations/0001_abcdef1234.sql`). This strips
  control and format characters from paths too, which is covered by US-W5's
  general bullet on text bound for the comment (US-W5).
- Redaction itself is the single `await TextRedactor.redact` inside
  `redact_all`; the Phase 4 step calls it.

**Tests:** unit — the majority of this phase, pure no-I/O logic under ADR-0005.
This phase's PR edits `docs/adr/0005-pure-no-io-logic-unit-tested.md`'s scope
list to add `review/walkthrough_render.py` (including `redact_all`'s join/split
logic) and the `findings/markdown.py` helper; later phases add their own pure
pieces as they land.
- `review/test_walkthrough_render.py` **(new)**: hostile fixtures (image
  beacons, `javascript:` and bare-URL links, `@org/team`, `#1`, `owner/repo#1`,
  `GH-1`, SHA-like hex, `www.` and email forms, bidi overrides, nested fences,
  multi-line names containing a second marker, filenames containing backticks,
  newlines, `[x](http://evil)`, `@team` and `<img ...>`); an all-garbage
  response; every truncation boundary; a ceiling test with multi-byte and
  zero-width-space-inflated input; the same string `migrations/0001_abcdef1234.sql`
  appearing in a note (hex run broken) and as a path (byte-identical in its code
  span); a path whose own backtick run forces a longer delimiter, and one that
  starts or ends with a backtick; a path containing U+202E whose stripped form
  still reconciles against the raw diff path.
- `redact_all`: nonce planted in each field kind (via the injected generator),
  a sentinel dropped or duplicated by a fake redactor (count mismatch), an
  over-cap field truncated before the join and a secret straddling the cap
  surviving redaction, a multi-line field collapsed before the join, an empty
  string, the split count equal to the input count, the nested
  `gitleaks:gitleaks:allowallow` form stripped, and a path containing a newline
  plus a secret.
- `findings/test_markdown.py`: the full prose pipeline applied to `&#35;123`,
  `&#64;user` and `\#123`; the inserted zero-width spaces survive in the final
  output while an input containing U+200B or U+202E is cleaned; the existing
  `escape_markdown` tests unchanged; and `review/test_product_knowledge*.py`
  still green after the strip-set hoist.

The story-level integration tests live in Phases 2 and 4, where a boundary
exists; the rendered-behavior check is Assumption 13.

**Observability & rollback:** none (no runtime behavior). Rollback is deleting
the new function and module; the only edit to existing code is the hoist of the
strip set, covered by the existing product-knowledge tests.

**Acceptance:** the Phase 1 unit tests pass; `ruff`, `mypy --strict` clean; the
ADR-0005 scope list is edited.

## Phase 2 — Comment delivery through the GitHub adapter (spec Unit B)

**Why second:** the only end-user-visible boundary, and the riskiest new
adapter code (identity matching, dedupe, the only destructive write). It needs
only Phase 1's model.

- New `adapter/github/walkthrough.py`: `GitHubWalkthroughPublisher`,
  constructed like `GitHubFindingsPublisher` (`token_provider`, base URL,
  timeout, retry attempts, optional shared client) plus `app_id: int`, using
  `auth_headers`, `client_context` and `request_with_retry`. Owner/repo
  validation calls the shared `tenancy.resolution.split_owner_repo`
  (re-exported by `adapter/github/http.py`) and wraps its `ValueError` as
  `posting.py::_split_owner_repo` does; it is not re-implemented.
  `repo_full_name` and `pr_number` come from the trusted job identity; a fork PR
  posts on the base repo's PR. The adapter holds **no** deadline (Assumption 12);
  it takes only the per-call HTTP timeout.
- Every `POST` passes `safe_writes_only=True`, as `posting.py::_post` does, so a
  5xx retry cannot create a duplicate comment.
- `upsert`: list the PR's issue comments (`per_page=100`) following httpx's
  `response.links.get("next")`, exactly as `adapter/github/diff.py:357-358` does
  (no hand-parsing, no dependency), capped at 5 pages. Before following a `next`
  URL, require its scheme and host to equal the configured base URL, else raise
  `GitHubPostingUnreachableError`, so the installation token is never sent to a
  host a response names. A comment matches only under Assumption 4. `PATCH` the
  oldest match, else `POST`. After a `POST`, re-list within the same cap,
  excluding the id the job's own `POST` just returned from the "older comment"
  choice; if an older platform comment exists, `PATCH` it with this job's content
  and `DELETE` the comment this job's `POST` returned, by that numeric id and no
  other. If the older comment cannot be seen, or the `DELETE` returns 404, do
  nothing more. A non-404 failure of the re-list, `PATCH` or `DELETE` after a
  successful `POST` raises the dedupe error, leaving the first comment in place.
- Errors: reuse `GitHubPostingUnreachableError` for the `github_error` path and
  add exactly two classes to `adapter/github/exceptions.py`,
  `GitHubWalkthroughCommentLimitError` (more than 5 pages with no match; nothing
  created, edited or deleted → `skipped_comment_limit`) and
  `GitHubWalkthroughDedupeError` (→ `dedupe_failed`), both subclassing
  `GitHubPostingError`. That base's docstring currently says every subclass fails
  the job; it is updated to say walkthrough errors are caught by the fail-open
  walkthrough step and are not job-fatal. The classes carry status and URL path
  only, never a request or response body; `Authorization` is scrubbed from every
  cassette following the repo's existing `respx` cassette convention.

**Tests:** integration (`adapter/github/test_walkthrough_posting.py` **(new)**),
`respx` cassettes per `testing.md`:
- create-then-edit; the `Link`-header walk and the page-5 boundary giving the
  comment-limit error; a `next` link naming a foreign host being refused;
- identity, fail-closed: a foreign-authored marker comment is never edited or
  deleted; `performed_via_github_app` absent or null is ignored; the same marker
  with a different App id is ignored; an App id returned as a string, a float or
  `true` never matches; a match missing the footer is ignored;
- oldest-wins among several platform comments;
- dedupe: the concurrent-create race converging to one comment (two interleaved
  `upsert` calls against a stateful fake endpoint) with the `DELETE` target
  always the id of the caller's own `POST`; the re-list never picking the job's
  own new comment as the older one; the older comment not visible on re-list (no
  `PATCH`, no `DELETE`); a 404 on `DELETE` is a no-op; a non-404 failure of the
  `PATCH` or `DELETE` gives the dedupe error with the first comment left in place;
- `safe_writes_only`: a 5xx on the create `POST` is not retried (exactly one
  `POST` recorded), while a 429 on it is retried;
- cancellation mid-flow against a stub endpoint that sleeps past a short cancel,
  leaving no stuck state (the step-level deadline test is in Phase 4); a
  malformed `repo_full_name` rejected with the wrapped error; a fork PR posting
  on the base repo with the repo and PR number taken from the job identity;
  adapter logs asserted free of comment text; 4xx/5xx mapped to typed errors with
  no body in the message; and the cassette pins the `performed_via_github_app`
  field shape (Assumption 4). Unit tests only for any pure matching helper (added
  to the ADR-0005 scope list).

**Observability & rollback:** structured logs with `job_id`, `pr_number`,
`head_sha`, page count and `action` (`created`/`edited`/`deduped`/
`dedupe_failed`), never comment text; GitHub-call latency is measured by the
step's `publish` phase histogram in Phase 4. Rollback is deleting the module;
nothing calls it until Phase 4.

**Acceptance:** US-W4's tests pass against the cassettes; the ADR-0005 scope
list includes the pure matching helper.

## Phase 3 — Settings, carried fields, chain wiring and the routing stage (spec Unit C config half, Unit D)

**Why third:** Phase 4's step needs its enablement, timeout, chain position and
routing stage to exist, and the carried fields are a prerequisite of its input.

- `apps/worker/settings.py`: `walkthrough_enabled` (default `False`),
  `walkthrough_step_timeout_seconds` (45), `walkthrough_min_files` (2),
  `walkthrough_max_files` (60), `walkthrough_max_chars` (6000); a
  `_walkthrough_settings_are_valid` validator mirroring
  `_fix_generation_settings_are_valid`, implementing US-W11 exactly and reading
  `llm_timeout_seconds` from the same settings instance (the step timeout must
  be at least `llm_timeout_seconds` + 15; skipped entirely when disabled, so a
  rollback is never blocked). Both this validator and the stuck-job term key on
  the raw flag, not effectiveness (Assumption 9), and its error message ends
  with "or set walkthrough_enabled=false". In
  `_stuck_job_warning_threshold_covers_worst_case_chain` the walkthrough timeout
  is added to the sum **only when enabled**, and the settings comment's sum and
  the validator docstring are updated. `stuck_job_warning_threshold_seconds` stays
  700, so none of its hard-coded sites (`docker-compose.yml`,
  `charts/review/test-render.sh`, `apps/worker/test_settings.py`,
  `operations.md`) change.
- `apps/worker/main.py`: `_build_step_enablement` and `_build_step_timeouts` (the
  only places `WorkerSettings` is copied into `StepEnablement`/`StepTimeouts`)
  get the new fields, including `secret_redaction_enabled` on the enablement.
  Without this the flag is never applied and the pipeline's timeout (looked up at
  `pipeline.py:549`) would differ from the one the validator and the stuck-job
  sum use. In `_check_step_configuration`, the logged `enabled_steps` becomes
  `{**enablement.as_mapping(), "walkthrough": walkthrough_can_produce_value(enablement)}`
  (walkthrough is the one entry overlaid; `context_builder` and `intent_summary`
  stay raw there, as today), and the "enabled but ineffective" warning is emitted
  from the same function with a fixed message the runbook quotes. In
  `_build_review_registry`, `enabled_mapping["walkthrough"]` is overridden with
  `walkthrough_can_produce_value`, following the `context_builder` /
  `intent_summary` pattern, so the registry never runs an ineffective step.
- `review/config.py`: `walkthrough_enabled: bool = False` and
  `secret_redaction_enabled: bool = True` on `StepEnablement` (Assumption 3; its
  docstring updated to name the one non-step input field and why),
  `walkthrough_step_timeout_seconds` on `StepTimeouts`, **and a `"walkthrough"`
  key in both `as_mapping()` methods** (the pipeline looks up
  `self._timeouts[step.name]`, so a missing key raises `KeyError` on every job);
  `walkthrough_can_produce_value`. In `resolve_review_pipeline_depends_on` the
  `"walkthrough"` key is always present: its value is the previous `output`
  target (`fix_generation`, else `verification`, else the earlier chain), and
  `output` depends on `walkthrough` only when effective, so with the step
  ineffective `output`'s dependency is unchanged.
  `check_step_configuration_viable`'s `effectively_enabled` map gets a
  `"walkthrough"` entry, otherwise its dependency loop raises `KeyError`. The
  exact-dict assertions are updated: `apps/worker/test_main.py` near line 1286
  gains `"walkthrough": "verification"` for the default settings, and the
  `review/test_config.py` exact-dict assertions near lines 430-495 gain the
  matching `walkthrough` and `effectively_enabled` entries.
- Carried fields, each optional with a `None` default, forwarded unchanged and
  **set explicitly on every return path** (these outputs are built from explicit
  kwargs, so a path that omits a field silently drops it): `intent_summary` on
  `ReviewGenerationOutput`, `VerificationInput`/`Output`, `FixGenerationInput`/
  `Output`; `reviewed_diff` on `VerificationOutput`, taken from
  `VerificationInput.diff`; and on `FixGenerationInput` **as its own field,
  separate from `diff`** (so the `parsed.diff is None` fetch at
  `fix_generation.py:350-353` is untouched), with `FixGenerationOutput` carrying
  `parsed.reviewed_diff`, or `parsed.diff` when the predecessor was
  `review_generation`, and never the diff the step fetched itself.
- `llmgateway/routing.py`: add `"walkthrough"` to `KNOWN_MODEL_ROUTING_STAGES`
  and fix the comment above it (four stages becoming five). Update every place
  that asserts or hard-codes the set:
  `apps/admin_api/test_model_routing_routes.py` (the exact set assertion near
  lines 119-123), `apps/admin_api/frontend/src/api/types.ts`
  (`MODEL_ROUTING_STAGES`), `apps/admin_api/frontend/src/screens/ModelRouting.test.tsx`
  and `ModelRouting.tsx` where it renders the list; `apps/worker/test_model_routing_integration.py`
  is run and updated if it enumerates stages.
- `review/test_step_boundaries.py`: the new module gets the same
  no-cross-step-import treatment as its siblings.
- Helm (`values.yaml`, `templates/configmap.yaml`, `values-production.yaml` if it
  lists the sibling settings) and `docker-compose.yml` env, mirrored from the
  `fix_generation` block; `charts/review/test-render.sh` renders with the flag
  on and off.

**Tests:** unit for settings validation (every US-W11 bound,
disabled-skips-validation, the conditional stuck-job sum, the
`llm_timeout_seconds` coupling and the error-message suffix) and for
`resolve_review_pipeline_depends_on`/`check_step_configuration_viable`/
`as_mapping` across **all** predecessor shapes: walkthrough after
`fix_generation`; after `verification` with `fix_generation` off (including
static analysis off); after `review_generation` with both off; and not effective
(flag off, `review_generation` off, or redaction off), in `review/test_config.py`
and `apps/worker/test_settings.py` (ADR-0005 scope list extended in this PR). In
`review/test_config.py`: `"secret_redaction_enabled"` is not a key of
`as_mapping()`, `"walkthrough"` is a key of both mappings, and the
`as_mapping()` keys equal the registered step names plus the analyzer keys. In
`apps/worker/test_main.py`: `_build_step_enablement` maps
`settings.secret_redaction_enabled` and `walkthrough_enabled`. Integration:
- per-return-path forwarding tests for `intent_summary` and `reviewed_diff` in
  `review/test_review_generation_step.py`, `review/test_verification_step.py`
  and `review/test_fix_generation_step.py` (the no-findings early return, the
  timeout path and the normal path), including the `review_generation` →
  `fix_generation` → `walkthrough` chain, and an extension of
  `review/test_fix_generation_step.py` pinning that its diff-fetch count and the
  base passed to `fetch_content` are unchanged when `reviewed_diff` is present
  and that a fetched diff is never carried forward;
- a new `apps/worker/test_walkthrough_wiring.py` **(new)** building the registry
  with the flag off: the chain and the calls the fake publisher receives are
  identical to a run of the same input without the new fields (US-W9), and an
  ineffective configuration (flag on with redaction off, or with
  `review_generation` off) registers no step and leaves `output` depending on the
  previous step, through the resolver, the viability check and the registry. Its
  flag-on half lands in Phase 4 with the step, since it cannot pass before;
- `apps/admin_api/test_model_routing_routes.py` for the `walkthrough` routing
  stage resolving and falling back (US-W10); `apps/worker/test_model_routing_integration.py`
  gains a `walkthrough` assertion if it enumerates stages, else is dropped from
  the traceability table.

**Observability & rollback:** configuration only, plus the startup warning.
Rollback is leaving the flag off (the default); the added optional fields are
inert. Because the flag is a Worker bootstrap setting, turning it off takes a
Worker restart (a rolling deploy), not a live toggle; the runbook says so.

**Acceptance:** all Phase 3 tests pass, the ADR-0005 scope list includes the
config predicates and validators, `test-render.sh` passes with the flag on and
off, `FindingsOutputInput` still validates against `StepFailureInfo.seed_input`
(no field added), and posted output is unchanged with the flag off.

## Phase 4 — The `walkthrough` step (spec Unit C)

**Why fourth:** it composes Phases 1–3. It lands in the same release as Phase 3.

- New `review/walkthrough.py`: `WalkthroughStep`, `name = "walkthrough"`,
  modeled on `FixGenerationStep`: input mirrored via `from_attributes`
  (no cross-step imports), `model_resolver.resolve(self.name, org_id)`,
  `record_llm_token_usage`, constructor-injected `depends_on`,
  `internal_timeout_seconds` (passed from `main.py` as step timeout − 5, not
  re-derived in the step), and an injectable nonce generator.
- The whole body, **including input validation and the min-files check**, runs
  inside one `except Exception` handler (never `BaseException`, so a
  `CancelledError` from the wait still propagates) under
  `asyncio.wait_for(internal_timeout_seconds)`, so no exception can reach the
  registry's FATAL policy. The step tracks a `stage` variable (`validate`,
  `prompt`, `llm`, `parse`, `redact`, `sentinel`, `render`, `publish`); the
  outcome counter and log line are recorded in an outer `finally`, so a cancel
  mid-publish still counts, and a timeout is mapped by the stage reached: `llm`
  → `llm_timeout`, `redact` or `sentinel` → `internal_error`, `publish` →
  `github_error`. Order: (1) validate input; (2) fewer reviewable files than
  `walkthrough_min_files` → pass through, `skipped_small_pr` (US-W3); (3) build
  one `build_completion_request` carrying the bounded file list (path lengths
  capped) and patch slices as delimited data via the `diff_prompt` helpers
  (US-W7); (4) call the LLM with `llm_timeout_seconds`; (5) parse and reconcile
  (Phase 1); (6) `redact_all` (Assumption 7), including the carried
  `intent_summary` (US-W5); (7) render; (8) publish unless `context.dry_run`,
  with `await publisher.upsert(...)` wrapped in `asyncio.timeout(min(publish
  reserve, remaining inner time))` by the step (Assumption 12), expiry →
  `github_error` (US-W4). Every failure maps to an outcome and returns the input
  unchanged: `llm_timeout`, `parse_failure`, `github_error`,
  `skipped_comment_limit` and `dedupe_failed` (mapped by exception type from the
  Phase 2 errors) and `internal_error` as the catch-all for anything else,
  including a redaction, nonce-containment, count-mismatch or render failure.
  `internal_error` lines carry `stage` and `exc_type`, never text, so a
  persistent redactor outage is visible. A timeout mid-publish may leave a
  duplicate that the next push converges (US-W8, US-W12).
- Output type is a `JobIdentity` subclass forwarding `findings`, `head_sha`,
  `context_bundle` (exactly, since `FindingsOutputInput.context_bundle` drives
  citation resolution), `reviewed_diff`, `intent_summary` and the identity
  fields via `job_identity_fields`, so `FindingsOutputInput` validates
  unchanged.
- Telemetry via `review._telemetry.start_span` and `suppress_telemetry_errors`
  (ADR-0011/0012), not hand-rolled, with the names of Assumption 14: the outcome
  counter (nine values: `posted`, `updated`, `skipped_small_pr`,
  `skipped_comment_limit`, `llm_timeout`, `parse_failure`, `github_error`,
  `dedupe_failed`, `internal_error`), `duration_ms{phase}` and `rendered_chars`.
  One log line per run with `job_id`, `pr_number`, `head_sha`, `outcome`,
  `files_in`, `files_shown`, `chars` (plus `stage` and `exc_type` on
  `internal_error`) — never text (US-W12). No step logs a `model_dump` of its
  input; a test asserts the step's logs contain none of the summary or path text.
- `apps/worker/main.py`: construct `GitHubWalkthroughPublisher` and
  `WalkthroughStep` in `_build_review_registry`, passing `settings.github_app_id`
  and the settings values.

**Tests:** integration (`review/test_walkthrough_step.py` **(new)**), recorded
LiteLLM cassettes plus a fake `WalkthroughPublisher` (a fake over a mock, per
`testing.md`):
- US-W1 happy path with and without a summary, including a `review_generation`-
  shaped input (carrying `diff`, not `reviewed_diff`); US-W2 omitted/duplicate/
  unknown paths; US-W6 oversize PR.
- US-W3 boundaries: exactly `min_files`, `min_files − 1`, and files with
  `patch=None` excluded from the count.
- **US-W5 at the trust boundary:** a secret placed in a path, in the model's
  note, and in `intent_summary` never reaches the fake publisher; redaction is a
  single call; with the injected nonce planted in a path, note or summary, or a
  fake redactor dropping or duplicating a sentinel line, nothing is posted and
  the outcome is `internal_error`; a hostile cassette (`@mentions`, image beacon,
  `#N`/`owner/repo#N`, multi-line name with a fake marker) yields a body with
  none of them active; and with the no-op redactor nothing is posted.
- US-W7: exactly one routed call, bounded input, fixed output shape.
- US-W8, one case each: LLM timeout (a hung fake LLM against a short injected
  `internal_timeout_seconds`, proving the outer step timeout cannot fire), a
  timeout during redaction (→ `internal_error`, stage `redact`), a timeout during
  publish (the step's own `asyncio.timeout` expiring against a hung fake
  publisher → `github_error`), bad shape, all-garbage response, the publisher
  raising `GitHubPostingUnreachableError` (→ `github_error`), the comment-limit
  error (→ `skipped_comment_limit`), the dedupe error (→ `dedupe_failed`), a
  redaction failure, an input validation failure, a cancel during publish still
  recording its outcome, and `context.dry_run` publishing nothing — each leaving
  the input unchanged and the existing comment untouched.
- US-W9: disabled step absent. US-W12: every one of the nine outcomes counted,
  via an in-memory metric reader, with the exported counter name exactly
  `review_walkthrough_outcome_total`; a forced redactor failure and a forced
  sentinel mismatch each yield `outcome=internal_error` with the expected
  `stage` and a log carrying no text; telemetry failure logged and ignored.
- Flag-on wiring, extending `apps/worker/test_walkthrough_wiring.py`:
  `internal_timeout_seconds` equals the step timeout − 5, the settings values
  reach the step, `settings.github_app_id` reaches the publisher, and `output`
  depends on `walkthrough`.
- Field equality of the step's output against its input for the forwarded
  fields. Unit tests only for pure helpers (prompt assembly, file counting),
  added to the ADR-0005 scope list.
- In `adapter/analyzers/test_gitleaks.py` (the real binary, integration): a
  characterization test records whether the pinned Gitleaks skips a line
  carrying `gitleaks:allow`, and a test that the stripped line is redacted. The
  unconditional strip (Assumption 7(c)) makes the walkthrough independent of the
  answer.

**Observability & rollback:** as above. Rollback is the flag; disabling leaves
already-posted comments in place (spec).

**Acceptance:** US-W1, W2, W3, W4 (step side: dry-run and publish deadline), W5,
W6, W7, W8, W9 and W12 integration tests pass, the flag-on wiring test passes,
and the ADR-0005 scope list includes the prompt and file-count helpers.

## Phase 5 — E2E, alerts, dashboard, runbook and docs (spec Unit D remainder, Unit E)

**Why last:** it exercises the whole stack and needs every earlier phase.

- `tests/e2e/test_pr_walkthrough.py` **(new)**, copying `test_auto_fix.py`'s
  scenario machinery. The GitHub stub is the shared `_StubGitHubHandler` in
  `tests/e2e/test_review_pipeline.py`; it is extended with issue-comment list/
  `POST`/`do_PATCH`/`do_DELETE` routes (including a mode that serves 501
  comments), not replaced by a new stub. Scenarios: a push creates the comment,
  a second push edits the same one; a failed regeneration leaves the old
  comment; an LLM timeout, a bad shape and a stub 5xx leave findings and the
  check-run unchanged (US-W8); with the flag off the stub receives only the
  review and check-run writes (US-W9).
- `charts/review/dashboards/review.json` gains one panel per outcome plus
  duration (by phase) and size panels, using the Assumption 14 metric names.
  `tests/e2e/test_observability.py` is given traffic for the panels it can drive:
  `posted`/`updated`, `skipped_small_pr` (a one-file PR), `llm_timeout` (stub LLM
  delay), `parse_failure` (stub garbage), `github_error` (stub 5xx) and
  `skipped_comment_limit` (the 501-comment mode). `dedupe_failed` and
  `internal_error` cannot be provoked from stub traffic; those two panels go in
  `_PANELS_WITHOUT_E2E_TRAFFIC` (the Phase 9/10 precedent) with a pointer to the
  Phase 4 integration tests that cover them.
- `docs/reference/operations.md`: a "Walkthrough" section in the shape of "Fix
  generation":
  - what is sent where (path list and capped patch slices, same endpoint and
    posture as `intent_summary`), the default-off switch and cost bound, the
    fork-PR note, the accepted findings-latency trade-off (Assumption 11), and
    that redaction is best-effort;
  - **the "enabled but ineffective" warning:** its exact message text (fixed in
    Phase 3), its three causes with their fixes (`review_generation` off,
    redaction off, `walkthrough_enabled` mis-set), and that no outcome metric is
    emitted in that state, which is why the rollout check greps for the warning;
  - one entry per `outcome` (nine), including that `llm_timeout`,
    `parse_failure`, `github_error` and `internal_error` on any push after the
    first may leave a stale comment (its footer names the commit it describes),
    that `skipped_comment_limit` means a PR with more than 500 comments
    (documented and accepted, not alerted; watch its panel during the pilot),
    that `dedupe_failed` means a duplicate may remain, and that `internal_error`
    has known causes: a redactor or Gitleaks outage or timeout, and a multi-line
    scanner rule spanning sentinel lines on a hostile PR (both fail closed). A
    LogQL query on the `stage` field of the worker's log stream is given for
    telling them apart;
  - **Alerts, as literal PromQL with thresholds and windows in the Phase 11
    style** (starting values, tuned after a pilot):
    (a) `sum(increase(review_walkthrough_outcome_total{outcome=~"llm_timeout|parse_failure|github_error|dedupe_failed|internal_error"}[1h])) / sum(increase(review_walkthrough_outcome_total[1h])) > 0.33 and sum(increase(review_walkthrough_outcome_total[1h])) >= 6`
    (skipped outcomes stay in the denominator; 6 is the minimum-volume guard);
    (b) `sum(increase(review_walkthrough_outcome_total{outcome=~"github_error|dedupe_failed"}[1h])) > 0`
    sustained for 1 h. Alert (b) fires for the outcomes that leave an orphan
    duplicate or an unupdated comment; alert (a) is what covers the other stale-
    comment outcomes and a redactor outage, which can hide below the 0.33 share
    for a single tenant;
  - **Cleanup procedure:** how to find every walkthrough comment (search a
    repo's PRs for the marker `<!-- review-walkthrough:v1 -->` authored by the
    App) and delete a bad, leaked or duplicate one, including after a sanitizer
    fix;
  - the stuck-job arithmetic (680 s with the flag on, 700 threshold unchanged;
    an operator override below 680 s, or raising `llm_timeout_seconds` above 30
    with the flag on, fails startup naming the setting, and both validators key
    on the raw flag), rollback (a Worker restart, not a live toggle), and a
    rollout check that the "enabled but ineffective" warning is absent.
- **Manual release gate on a throwaway test-org repo, a recorded result of
  "passed" required for both items in the runbook before the flag is recommended
  for any customer, and re-run after any change to `neutralize`:** (1) the App's
  existing permissions allow creating, editing and deleting its own PR comments
  (D7), and the real `performed_via_github_app` shape matches the Phase 2
  cassette; (2) the hostile-fixture PR from Phase 1 renders with no active link,
  image, notification or cross-reference (Assumption 13). Like the roadmap's
  other unexercised real-repo checks, this is a release gate, not a CI gate.
- `docs/documentation_index.md` already carries this plan's row. The spec status
  and the roadmap's Phase 12 status line are updated only after the gates are
  re-run (per the roadmap's own rule).

**Tests:** E2E (few): the first-push/second-push happy path and the failure
modes above, per `testing.md`'s "few, driven over the Compose stack".

**Observability & rollback:** the panels, alerts and runbook are the
observability deliverable. Rollback is the flag.

**Acceptance:** `test_pr_walkthrough.py` and `test_observability.py` pass on the
Compose stack; the runbook (including the warning entry, literal alerts and
cleanup) exists; and a recorded result of "passed" exists for both manual
release-gate items.

## Traceability

| Story | Given/When/Then (summary) | Test path | Level | Phase |
| --- | --- | --- | --- | --- |
| US-W1 | ≥2 files → one comment with Summary; no summary → omitted; both predecessor shapes | `review/test_walkthrough_step.py` (new) | Integration | 4 |
| US-W2 | Unknown/duplicate/omitted paths repaired | `review/test_walkthrough_render.py` (new), `review/test_walkthrough_step.py` (new) | Unit + Integration | 1, 4 |
| US-W3 | Below `min_files` (boundaries, `patch=None`) → no call, no write | `review/test_walkthrough_step.py` (new) | Integration | 4 |
| US-W4 | Create/edit in place; fail-closed identity; foreign marker never edited or deleted; concurrent converge; comment limit; dedupe failure; foreign-host `next` refused; `safe_writes_only`; publish deadline; dry-run | `adapter/github/test_walkthrough_posting.py` (new), `review/test_walkthrough_step.py` (new) | Integration | 2, 4 |
| US-W5 | One redaction call; nonce/count checks; secrets removed at the step; `gitleaks:allow` stripped; no-op redactor posts nothing; hostile output inert; issue refs/URLs/emails broken; paths/newlines/footer | `review/test_walkthrough_render.py` (new), `findings/test_markdown.py`, `review/test_walkthrough_step.py` (new), `adapter/analyzers/test_gitleaks.py`; rendered check manual | Unit + Integration (+ manual gate) | 1, 4, 5 |
| US-W6 | Cut at group boundary; oversize first group; file cap | `review/test_walkthrough_render.py` (new), `review/test_walkthrough_step.py` (new) | Unit + Integration | 1, 4 |
| US-W7 | One routed call; bounded input; fixed shape | `review/test_walkthrough_step.py` (new) | Integration | 4 |
| US-W8 | Timeout by stage, bad shape, publisher/limit/dedupe/internal error → output unchanged; existing comment untouched | `review/test_walkthrough_step.py` (new), `adapter/github/test_walkthrough_posting.py` (new), `tests/e2e/test_pr_walkthrough.py` (new) | Integration + E2E | 2, 4, 5 |
| US-W9 | Disabled/ineffective → step absent, output identical; flag-on wiring; carried fields never change Phase 11 | `apps/worker/test_walkthrough_wiring.py` (new), `review/test_fix_generation_step.py`, `review/test_walkthrough_step.py` (new), `tests/e2e/test_pr_walkthrough.py` (new) | Integration + E2E | 3, 4, 5 |
| US-W10 | `walkthrough` routing stage row resolves | `apps/admin_api/test_model_routing_routes.py`, `apps/worker/test_model_routing_integration.py` (if it enumerates stages) | Integration | 3 |
| US-W11 | Bad settings fail startup naming the setting; disabled skips validation | `apps/worker/test_settings.py`, `review/test_config.py` | Unit (ADR-0005) | 3 |
| US-W12 | Every outcome counted; exported counter name; stage/exc_type on internal_error; log fields; telemetry failure ignored; panels have data | `review/test_walkthrough_step.py` (new), `tests/e2e/test_observability.py` | Integration + E2E | 4, 5 |

## Security review

New inputs and trust boundaries: attacker-influenced PR content (file paths,
patches, and through `intent_summary` the diff) flows into an LLM and back out
into a comment on a customer PR, and the platform gains create/edit/delete on
issue comments.

- **Markup, mention and reference injection.** Anyone who can open a PR can try
  to steer the model, or name a file, to emit active markup: a tracking-pixel
  image, a phishing link, `@mention` notifications, or `#N`/`owner/repo#N`
  references that make the App's comment add cross-reference events on other
  repos. Control: the model output is parsed to a fixed structured shape; prose
  strings get the Phase 1 pipeline in its stated order (character stripping and
  whitespace collapse, reference/SHA/URL/email breaking, `&` escaping,
  `escape_markdown`) and paths get delimiter-safe code spans; tested with hostile
  fixtures at the render level and again at the step level. Unit tests assert the
  neutralized output; the rendered behavior is verified by hand (Assumption 13)
  because GitHub's linkification is not reproduced in CI.
- **Structure injection.** A newline in a name or note could forge a heading, a
  second marker or the footer. Control: whitespace collapse, and a match
  requires marker plus footer plus App id.
- **Secret echo and redaction bypass.** The model may repeat a secret from the
  diff. Control: every string passes the redactor before render, including paths,
  notes and the carried `intent_summary`, which had no redaction pass before this
  phase because it was never posted. This is best-effort, and only a control
  when the redactor is real: with `secret_redaction_enabled=false` the step is
  not effective and posts nothing. Specific bypasses closed: the Gitleaks default
  config honors an inline `gitleaks:allow`, so the marker is stripped from every
  string in a loop (defeating the nested form) before redaction; a cap applied
  before redaction could cut a secret in half, so caps bounding the blob are
  generous and the user-visible caps apply after redaction; and the strings
  redacted are exactly the strings rendered. Redaction is one call on a joined
  blob with an unguessable `secrets`-sourced nonce in a keyword-free sentinel,
  never logged or put in the prompt, with inputs bounded before joining, so a
  hostile PR can neither fan out hundreds of Gitleaks subprocesses, nor push the
  blob past the scanner's timeout, nor forge or shift the split. Patch slices go
  to the endpoint raw, exactly as `intent_summary` already sends the diff, so this
  adds no new data class; the runbook says so.
- **Prompt injection and integrity.** Diff content and file names are delimited
  as data. A successful injection can at worst mis-group or mis-describe files
  or make a note say something false ("LGTM") under the bot's identity; it
  cannot add paths not in the PR (reconcile drops them) or active markup
  (neutralize). This integrity risk is accepted; the footer's fixed text that the
  comment is an AI-generated summary and not a review verdict is not
  model-controllable.
- **Forged or hijacked comment identity.** A collaborator can paste the marker.
  Control: identity matches only on App id (`type(x) is int`, so booleans and
  strings never match), marker and footer together, and fails closed when
  `performed_via_github_app` is absent (PAT or user-token comments). The only
  `DELETE` targets the numeric id returned by this job's own `POST`; it never
  deletes by list position, never a foreign comment, and a 404 is a no-op.
  Pinned by Phase 2 cassettes.
- **Token exfiltration via pagination.** Following a `Link: rel="next"` URL with
  the installation token would send the bearer token wherever a response points.
  The publisher requires the next URL's scheme and host to equal the configured
  base URL and raises otherwise (the copied `diff.py` pattern does not check);
  pinned by a cassette with a foreign-host link.
- **Comment flooding and suppression.** More than 500 comments (any commenter can
  produce them) makes the platform's comment unfindable; the plan creates,
  edits and deletes nothing and records `skipped_comment_limit`, so the result
  is a missing or stale walkthrough, never a flood of duplicates. Accepted and
  documented.
- **Resource exhaustion and cost.** One LLM call per reviewed push, bounded
  input and output (paths, notes, groups and files all capped before they reach
  the prompt or the redactor), one redaction call, at most 5 list pages plus a
  PATCH/POST and a bounded re-list per push, all under the step's own time
  budget, with every `POST` using `safe_writes_only` so a 5xx retry cannot
  duplicate a comment. Rapid pushes still multiply calls: the existing per-repo
  enqueue rate limiter is disabled by default (`docs/reference/operations.md`), so
  the runbook says to enable it before turning this on for busy repos, and the
  feature is default-off.
- **Authn/authz.** No new endpoint or credential: the installation token from
  `GitHubInstallationTokenProvider`, scoped to the one repo, and the App's
  existing `pull_requests: write`. Whether that permission covers editing and
  deleting its own issue comments is assumption D7; it is verified by the Phase
  5 manual gate before release, and the identity guard is the sole protection
  against deleting others' comments, which is why it fails closed.
- **Fork PRs.** In scope. The comment goes on the base repo's PR using the repo
  and PR number from the trusted job identity, never the fork's; patch text from
  an external fork goes to the customer's own LLM endpoint under the same
  controls as for any PR.
- **Cross-tenant, PII and logging.** The comment is posted only on the PR whose
  installation token was used. Logs carry counts, lengths and stage names, never
  comment, path or summary text; adapter errors carry status and path, never a
  body (GitHub echoes bodies); `Authorization` is scrubbed from cassettes.
- **SSRF / egress.** The adapter calls only the configured GitHub API base URL;
  no URL taken from content is fetched, and pagination links are origin-checked.

## Compatibility & rollout

- **Contracts changed.** Optional `intent_summary`/`reviewed_diff` fields on the
  step input/output models listed in Phase 3 (default `None`, additive;
  `FixGenerationInput`'s `diff` and its fetch are untouched); two defaulted
  fields on `StepEnablement` and a new `StepTimeouts` field; a new settings
  group; `stuck_job_warning_threshold_seconds` stays 700 and the new timeout joins
  its sum only when the feature is on, so no existing operator override can start
  failing at upgrade with the flag off; one addition to the closed
  `KNOWN_MODEL_ROUTING_STAGES` set (backend, frontend constant and their tests
  updated together); the hoist of the strip set from `review/product_knowledge.py`
  into `findings/markdown.py` (behavior unchanged, existing tests pin it). No
  schema, migration, API or webhook change.
- **Sequencing.** Expand-only: deploy with the flag off (the default; behavior
  and output identical to the previous release, pinned by Phase 3's tests), run
  the Phase 5 manual gate, then enable on a pilot deployment, watch the Phase 5
  panels and alerts, widen. An operator who enables the flag with a custom
  threshold below 680 s, or with `llm_timeout_seconds` above 30, fails startup
  naming the setting; the runbook says so. No upgrade ordering is needed
  between Worker and Admin API beyond shipping both from one release.
- **Rollback.** Set `walkthrough_enabled=false` and restart the Worker (a
  bootstrap setting, not a live toggle); validation is skipped when disabled, so
  a rollback is never blocked; already-posted comments stay and can be removed
  with the runbook's cleanup procedure. Nothing to un-migrate.
- **Dependency failure during rollout.** A slow or failing LLM endpoint,
  redactor or GitHub API is contained by the step's time budget and fail-open
  policy (US-W8); worst case each review is delayed by the step timeout (45 s
  default) and posts no walkthrough. A timeout mid-publish can leave one
  duplicate comment, which the next push converges.
