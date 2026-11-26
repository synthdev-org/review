# PR review-bot comments on `andybeak/chaptered`: what to learn for `review`

Researched 2026-11-24 from GitHub PR comments on https://github.com/andybeak/chaptered (557 PRs).
Purpose: inform improvements to this platform (what to catch, how to rank, what noise to avoid).

Note: "chaptered" is a private repository. Its pull request comments are quoted here as examples only, and readers cannot open it. Some quoted comments name plan documents, such as paths under `plans/`. Those are third-party text from that other repository. They are left as written, and they do not exist in this repository.

## Coverage and caveats

- **Codacy** (`codacy-production[bot]`): all 207 inline comments, PRs #464-#557 (about Aug-Nov 2026). About 70 read in full, rest keyword-bucketed. Comments carry no rule names, only a HIGH/MEDIUM/LOW badge, so themes are inferred. Fix/dismiss status is not visible.
- **CodeRabbit** (`coderabbitai[bot]`): active only until PR #478 (2026-09-08). **No CodeRabbit comments exist on PRs #480-#557**, so the requested "last two months" window is empty. Cause unverified (`.coderabbit.yaml` is still in the repo; likely uninstalled, quota, or replaced by Codacy). The sample therefore comes from PRs #400-#478 (Aug 8 - Sep 8): 208 inline comments over 19 PRs, none before #432. Review-body "nitpick" tier sampled for only 4 PRs. PRs before ~#400 were not swept.
- CodeRabbit "acted on" = its own "Addressed in commit" marker, not verified against diffs.

## Cross-cutting conclusions

1. **Highest-value findings are failure semantics with a concrete scenario**: unbounded timeouts (body read outside deadline), unclosed response bodies, errors rendered as empty state, one poison row failing a batch, last-success recorded when everything failed, health server bind failure only logged. Both bots; CodeRabbit's accepted ~57-73%.
2. **Concurrency/idempotency**: unique-constraint races, lost-update toggles, stale async completions, non-concurrent index builds. Strong when the breaking interleaving is spelled out.
3. **Cross-file / intent context is what a diff-only reader misses**: dropped fallback in a refactor, PR description vs migration (RLS removal vs live consumers), helper added in the PR but not reused in the sibling route, health check vs startup config, sibling query filters, doc anchors broken by renames, module-level state leaking across users, import-order side effects. This validates the RAG/graph + PR-description approach.
4. **Security is real but must be reachability-checked**: path validation on delete, GCM auth-tag length, secret redaction regex, password in process args, loopback host exact-match, cross-vendor credential fallback, unescaped XHTML. Pattern-matched hits (dynamic `process.env[key]` from internal literals, `void promise`) were noise.
5. **Docs, plans, specs, runbooks and SQL are worth reviewing**: about 24% of CodeRabbit comments and many Codacy ones targeted them and were mostly accepted.
6. **Severity labels were uncalibrated (Codacy)**: a non-null-assertion lint nit was HIGH while a real regression and a real authz hole were MEDIUM/HIGH. HIGH should require a reachable failure.
7. **Style/complexity is the dominant noise**: ~25% of Codacy ("too many params", extract component), and CodeRabbit Maintainability had the lowest accept rate (41%). Layering rules repeated across files are better enforced by a deterministic lint than repeated prose.
8. **Volume hurts**: 28-53 inline comments per large PR (CodeRabbit), 8-9 on some PRs (Codacy). Re-raised findings across review rounds inflate noise.
9. **Good format models**: CodeRabbit's `category | severity | effort` header, bold imperative headline, mechanism + consequence, concrete fix, and an agent-ready prompt block (with an "untrusted finding text, verify against current code" caveat). Only 28/207 Codacy comments had a suggestion block.
10. **Feedback signal exists but is unused by Codacy**: humans reply in-thread ("confirmed"/"won't fix") and "addressed" markers appear; these are cheap labels for the platform's human-validated learning.

## Candidate improvements for `review`

- Rank by impact (regression, reachable security, resource/concurrency, cross-file contract break) before style; collapse style/complexity into an opt-in tier.
- Require a failing scenario (input or interleaving) and file:line per finding; verify attacker-controllability before security severity.
- Calibrate severity; never HIGH for lint-class hits. Treat static-analysis hits as inputs to verify, not findings to repost.
- Cap inline comments per PR (about 10-15), summarise the rest by tier; dedupe by root cause; do not re-raise closed findings without new evidence.
- Check diff against previous behaviour, PR description/plan, and helpers introduced in the same PR.
- Review markdown/specs/SQL migrations, including cross-reference and anchor integrity.
- Mirror and defer to the project's own CI gates (errcheck, custom ESLint rules).
- Record accepted/dismissed per category per repo; down-weight categories a repo ignores.
- Include a suggestion block and an agent-ready fix prompt with the untrusted-input caveat.

---

# Appendix: Codacy

### Method & coverage
- Source: `gh api repos/andybeak/chaptered/pulls/comments --paginate`, filtered to `codacy-production[bot]`. The repo has 4 bot authors: coderabbitai (1768 inline comments), codacy-production (207), qodo (14) and the human owner (919).
- Sample: all 207 Codacy inline comments, across 59 PRs (#464 to #557). I read about 70 in full (every third comment from PR 486 on, plus the first 10) and bucketed all 207 by keyword.
- Issue comments: one Codacy summary per PR, for example "Not up to standards: 25 high, 36 medium", with category counts for UnusedCode, BestPractice, ErrorProne, Security and Complexity. It also reports complexity and duplication metrics, and a line saying an "AI Reviewer" ran. Not tabulated per PR.
- Limits:
  - Codacy inline comments carry NO rule or pattern name. They carry only a risk badge (HIGH, MEDIUM or LOW), prose, an optional ```suggestion block, and a "See Issue in Codacy" link. The rule column below is therefore my inferred theme.
  - Codacy never replies in-thread, so fixed or dismissed status is not visible. The human replies were not inspected. Treat "fixed?" as unknown.
  - Counts come from first-match keyword bucketing. They are approximate.
- Comments sit on files of these types: ts 109, tsx 53, md 15, go 9, sql 7, json 6, other 8.
- Prefixes: 79 start "Suggestion:" (the AI Reviewer style), 22 "Nitpick:", and 28 carry a code suggestion block.

### Category counts (n=207; risk badge: 41 HIGH, 104 MEDIUM, 60 LOW)
| Theme | ~Count |
|---|---|
| Complexity / "too many params/responsibilities", extract component/hook/helper | 53 |
| Docs/plan/comment consistency (broken anchors, stale references, typos) | 29 |
| Security-flavoured (secret leakage, path validation, authz, crypto, dynamic env access) | 28 |
| Correctness: regression, race, stale state, hook deps, missing timeout, resource leak | 26 |
| Test quality (non-null assertion, mock duplication, missing coverage) | 14 |
| Duplication / reuse existing helper | 12 |
| Config / infra / SQL / shell | 11 |
| Performance (memoize) | 3 |
| Uncategorised | 31 |

### Representative comments
| PR | File:line | Theme (inferred) | Risk | Finding | Real? |
|---|---|---|---|---|---|
| 464 | worker shell script | Security | HIGH | Redaction regex class `[^:`@/`]` includes backticks, so a password containing one would leak into logs. | True positive, with a concrete fix |
| 464 | worker/main | Correctness | MED | Register SIGTERM handlers at the start of `main()`, because `boss.start()` could hang and swallow signals. | True positive |
| 464 | test file | Test | HIGH | Non-null assertion on a mock call. Use `?.`. | Lint-level nit, labelled HIGH (over-ranked) |
| 464 | docker script | Correctness | MED | Hostname regex fails when the host is at end of string with no trailing `/` or `:`. | True positive |
| 464 | stub function | Nit | LOW | Intentional Phase-1 placeholder, add a comment. | Noise |
| 486 | domain/rules/restoreProjectBackup.ts:1409 | Regression | HIGH | The refactor dropped the `publishedFolderId` fallback for `folder_id`, so restore fails if `oldFolderId` is missing. | True positive (diff-semantic) |
| 487 | useLoreEntryField.ts:128 | Duplication | MED | Debounced-save/ID-switch logic is duplicated across hooks. | Plausible |
| 487 | PortraitUpload.tsx:78 | Praise | LOW | "Correctly moves interaction logic to CSS". | Noise (non-finding) |
| 490 | ScenePresenceSection.tsx | Race | MED | Shared mutation `variables` for per-row disabling breaks under concurrent actions. | True positive |
| 490 | corkboardRenameScene.test.tsx:272 | Test dup | MED | Extract shared Vitest mocks. | Style |
| 491 | EditorPaneCollaborative.tsx:362 | Correctness | HIGH | `useMemo` deps omit `ydoc` and `awareness`, so a reused editor stays bound to the old doc. | True positive |
| 491 | DocxEpubHeaderSection.tsx:13 | Type safety | MED | `options` prop resolves to `any`, disabling type checking. | True positive (cheap) |
| 491 | ExportOptionsStep.tsx:84 | Dead code | LOW | Prop removed from destructuring but left in the interface. | True positive |
| 492 | rewriteStorageOrigin.test.ts:71 | Lint | n/a | "Forbidden non-null assertion" auto-fix. | Lint noise |
| 492 | scripts/rewrite-storage-origin.ts:111 | Duplication | MED | Pagination and batch error handling repeated, so extract a helper. | Style |
| 493 | provision-moderation-metrics-db-role.sql:36 | Correctness | MED | psql `\quit 1` does not set the exit status. Use `\set EXIT_STATUS`. | True positive (tooling nuance) |
| 498 | docs/operations/backup-cron.md:30 | Docs | MED | A renamed header breaks anchors at lines 3 and 11, and the sections now contradict it. | True positive (cross-ref) |
| 499 | MobileEditorShell.tsx | Complexity | MED | Too many concerns, extract a hook. | Low-value |
| 499 | ChapterGroupedSceneList.tsx | Perf | MED | Memoise `buildTree`. | Speculative |
| 500 | api/upload/backup/route.ts:34 | Reuse | MED | Route re-implements auth and FormData parsing that this PR extracted into `parseAuthorizedUpload`. | True positive, needs repo context |
| 502 | api/upload/project-cover/route.ts:109 | Security | MED | A split-based `storagePath` is permissive, so an authenticated user could delete any file in their project folder. | True positive |
| 503 | fetchCharacterPortraitAssets.ts:35 | Resilience | MED | `fetch` has no timeout, so the nightly cron can hang forever. | True positive |
| 504 | api/import/character-portrait-asset/route.ts:77 | Correctness | MED | `void pushMetrics()` is not awaited, so the push can be lost. | Debatable (serverless) |
| 506 | MarginCommentEditorIntegration.tsx | Complexity | MED | 11 parameters, group them into objects. | Style |
| 507 | docs/migrate-db-auth-self-hosted.md:815 | Security / ops | HIGH | Dropping the `public` schema on Supabase Cloud is risky. Use pg_restore `--clean` or drop tables instead. | True positive in a plan doc |
| 508 | worker/index.ts | Ordering | MED | `setDefaultResultOrder` runs after the infra imports are evaluated, so it is too late. | True positive (subtle) |
| 511 | supabase/migrations/..._retire_hetzner_cutover_storage_rls.sql:23 | Premature change | HIGH | Dropping RLS policies is premature, because hooks still use Supabase for the `project-backups` bucket (PR description cross-check). | True positive |
| 513 | enforceCoachGenerateRateLimit.ts | Security | HIGH | Dynamic `process.env[key]` access, use an allowlist. | Likely false-positive-ish (the key comes from internal literals) |
| 513 | authRepository.ts:46 | Complexity | LOW | `signUp` exceeds the complexity threshold. | Style |
| 513 | coachEntityExtraction.ts:185 | Correctness | LOW | Regex lacks escaped-quote support and truncates dialogue followed by commas. | Plausible edge case |
| 515 | editor/page.tsx:1137 | Perf | LOW | Narrow the effect dependency array. | Speculative |
| 518 | services/moderation/llm/jev_backend.go:109 | Complexity | MED | Method is slightly over the 50-line limit. | Rule-driven nit |
| 523 | workers/export/epubExport.logic.ts | Security | HIGH | Mixed raw HTML plus unescaped metadata in generated XHTML. | Plausibly true (XSS / invalid XML) |
| 525 | docs/operations/environments-and-supabase.md:179 | Docs | MED | "Same IPv6 caveat" now points to a section that no longer describes it. | True positive |
| 526 | supabase/config.toml:210 | Typo | LOW | Stray `#` in the reference. | Trivial |
| 527 | services/status/poller.go:129 | Resource leak | MED | The body is not closed on a redirect error (`Do` can return a non-nil response). | True positive |
| 529 | useOnboardingGate.ts | State bug | MED | Module-level variable persists across logout and login, so a different user may skip the welcome redirect. | True positive |
| 533 | mutationQueue.ts:14 | Complexity | HIGH | Separate lifecycle orchestration from IndexedDB CRUD. | Style, labelled HIGH |
| 537 | CharacterRelationshipGraph.parts.tsx | Complexity | MED | Group node inputs into objects. | Style |
| 542 | AuthorJsonLd.tsx:36 | Complexity | MED | Replace independent args with a props object. | Style |
| 544 | ..._staff_users_page.sql | Logic | MED | Exclude `is_example` projects, matching the overview RPC. | True positive (consistency with sibling code) |
| 553 | services/moderation/main.go | Correctness | HIGH | The health checker ignores the provider fallback used at startup, so it can misreport. | True positive |
| 555 | NodeStaffOidcTransactionStore.ts | Security | HIGH | Set the GCM auth-tag length on the decipher so truncated tags are rejected. | True positive (classic crypto hardening) |
| 555 | completeOidcLoginUseCase.ts:20 | Architecture | MED | The use case depends on concrete repository and metrics infrastructure instead of injected ports. | True positive (layering rule) |
| 555 | evaluateOidcIdentity.ts:17 | Migration | MED | Provide a migration path for existing OIDC bindings when changing provider. | Insightful |
| 557 | StaffMagicLinkLoginForm.tsx:140 | Complexity | MED | Extract the presentational component. | Style |

### Patterns & implications
Observations
1. There are two feeds mixed together. One is the classic Codacy static-analysis feed: non-null assertions, complexity, the 50-line limit, unused code. The other is the newer "AI Reviewer" (prefixed "Suggestion:" or "Nitpick:", with a "run this prompt in your coding agent" footer). The AI feed carries most of the real findings.
2. About 25% of comments are structural style: too many params, split the component, extract a helper. These are repetitive, low-value and applied to nearly every PR, and some are labelled HIGH or MEDIUM. Severity labels do not track real impact. A lint nit got HIGH (#464), while a real regression (#486) and a real security hole (#502) got HIGH or MEDIUM.
3. The best catches need cross-file or intent context:
   - a refactor dropping a fallback (#486)
   - the PR description conflicting with a migration (#511)
   - a new helper not being reused in the sibling route (#500)
   - the health-check config differing from the startup config (#553)
   - sibling RPC filtering not matched (#544)
   - docs cross-references broken by a rename (#498, #525)
   - a module-level state leak across users (#529)
   - an import-order side effect (#508)
   - a hook dependency array omitting deps (#491)
   - resource handling: no fetch timeout, response body not closed on a redirect (#503, #527)
4. Docs and plan files get a lot of attention, and some of it is high value (broken anchors, stale cross-references, risky rollback advice). Review should include markdown and SQL migrations, not just source.
5. Security comments mix real ones (path validation on delete, GCM tag length, secret redaction regex, escaping in XHTML) with pattern-matched ones (dynamic `process.env` key, the `void` promise). A reviewer should check whether the input is attacker-controlled before escalating.
6. Praise or non-findings appear ("this correctly moves logic to CSS"). Those are noise.
7. Comments are never threaded or closed from Codacy's side, and no fix/dismiss signal comes back.

Implications for a review tool
- Rank by impact, not pattern: lead with behaviour regressions, security with a reachable path, resource and concurrency bugs, and cross-file contract breaks. Push complexity, extraction and "group params into objects" to a collapsed "style" tier, or drop them unless the repo's rules ask for them.
- Calibrate severity: HIGH should require evidence of a reachable failure. Don't use HIGH for lint rules (non-null assertions, file size).
- Suppress: praise and non-findings, intentional-placeholder notes, one-line typo nits, speculative perf (memoise, narrow deps) with no profile evidence, and per-PR repeats of the same style advice already given. Dedupe the same finding across a PR's commits.
- Keep and strengthen: diff-versus-previous-behaviour checks (dropped fallbacks, narrowed filters), checks against the PR description or plan, reuse of helpers introduced in the same PR, hook deps, missing timeouts, unclosed bodies, module-level mutable state, import ordering side effects, SQL/RLS policy removal vs live consumers, and doc anchors/cross-refs.
- Attach evidence: file:line, a concrete failing scenario and a ready-to-apply suggestion block (only 28 of 207 had one). Track accept/dismiss per comment so a rule's precision is measurable, which Codacy does not do here.
- Treat static-analysis hits as inputs to verify, not findings to repost, for example the dynamic env key. Verify the data source before raising severity.
- Cap comments per PR (some PRs got 8 to 9) and show a summary count by tier.

# Appendix: CodeRabbit (PRs #400-#478)

### Method & coverage
- Numbering verified: #400 created 2026-08-08, #478 created 2026-09-08. (#358 is 2026-08-02, so "from 07-15" is below ~#350; only #400-478 was swept.)
- Fetched `pulls/N/comments` (inline) for every PR 400-478 sequentially, 1s sleep, no rate-limit hits. Also fetched review bodies and issue comments for 4 PRs (#432, #445, #464, #473).
- 19 of 79 PRs had any CodeRabbit inline comments (first one at #432; #400-431 had none). Large "fix batch"/feature PRs dominate: #432 (28 raw), #445 (38), #463 (44), #464 (53), #451 (34), #473 (34).
- 208 top-level CodeRabbit inline comments analysed (replies excluded). Inline = "actionable" tier. "Nitpick" tier is not inline: it sits in the review body (e.g. #445 15, #464 6, #473 5 nitpicks) and was only sampled for 4 PRs.
- "Acted on" = CodeRabbit's own `Addressed in commit` marker on the comment (118/208 = 57%). This is a proxy (CodeRabbit infers it from later diffs); unmarked comments may have been fixed differently, dismissed, or left. Human replies were mostly short "@andybeak" confirmations followed by a bot "thanks" reply.
- Tags use a 3-part header: `<category> | <severity> | <effort>`. Effort values: Quick win, Heavy lift, Low value, Poor tradeoff.

### Category counts (208 inline comments)
| Category | Total | Marked addressed |
|---|---|---|
| Functional Correctness | 64 (36 Minor, 27 Major, 1 Critical) | 42 (66%) |
| Maintainability & Code Quality | 54 (34 Minor, 20 Major) | 22 (41%) |
| Stability & Availability | 47 (32 Major, 14 Minor, 1 Critical) | 27 (57%) |
| Data Integrity & Integration | 28 (23 Major, 5 Minor) | 17 (61%) |
| Security & Privacy | 11 (9 Major, 1 Minor, 1 Critical) | 8 (73%) |
| Performance & Scalability | 4 | n/a |
| Untagged "Potential issue" | 1 | |

- Severity: Major ~110, Minor ~90, Critical 3. Effort: roughly 85% "Quick win", ~25 "Heavy lift".
- About 49 of 208 (24%) target plan, spec and runbook docs (.md), not code. PRs #444, #455, #464, #470 were largely spec/plan reviews.
- Maintainability is the worst-accepted category (41%) and the largest cluster of what the author left alone: architecture-layer purity (Application importing Infrastructure, 8+ comments, many hitting a custom lint rule), test naming/"assert behaviour not helper calls", token/spacing nits.
- Recurring-by-theme counts (my reading of titles): test-quality/over-mocking ~25; a11y (44x44 target, focus ring, accessible names) ~10; env-restore/fake-timer cleanup in tests ~6; missing-timeout/deadline ~5; race/concurrency ~10; stale comments/docs ~10.

### Representative comments table
| PR | File:line | Label | Paraphrase | Acted on |
|---|---|---|---|---|
| 432 | auto-testing/run-overnight.sh:61 | Security, Critical | Require an exact loopback Supabase host (substring/prefix check could point destructive QA at a real DB) | Yes |
| 432 | auto-testing/run-overnight.sh:49 | Data Integrity, Major | Create QA fix branches only from a verified clean `develop` HEAD | Yes |
| 432 | auto-testing/seed.ts:86 | Data Integrity, Major | Garbage collection must be scoped to overnight-QA records only | Not marked |
| 432 | utils/singleFlightPromise.ts:15 | Stability, Major | Promise returned by `finally` is unhandled, causing unhandled rejection | Not marked |
| 445 | publishing/PublishScreen.tsx:128 | Functional, Major | Publish button silently does nothing when `publication` is missing | Yes |
| 445 | publishing/PublishScreen.tsx:106 | Functional | Re-expanding the Patreon section collapses again immediately (state bug) | Yes |
| 445 | smoke/publish-visible.smoke.test.ts:109 | Data Integrity | Seeding runs outside `try`; partial seed leaks rows | Yes |
| 445 | manage/page.tsx:27 | Functional | Failed identity query treated as "no identity" (error as empty state) | Not marked |
| 445 | hub/PublishAnotherProjectPicker.tsx:42 | Stability | Render query errors rather than an empty state | Yes |
| 447 | publishing/share-kit.spec.ts:58 | Functional | Double URL decode (wrong-value / injection-adjacent) | Yes |
| 447 | CopyLinkButton.tsx:66 | Functional | Ignore stale clipboard completions (async race) | Yes |
| 448 | migrations/...require_author_identity_bio.sql:86 | Data Integrity | Store the normalized bio value, not the raw one | Yes |
| 450 | helpSearch/HelpSearchPanel.tsx:171 | Functional | Do not activate results during IME composition (i18n edge case) | Yes |
| 450 | faqQueryLogRepository.ts:34 | Stability | Add an `outcome` Sentry tag for rejected FAQ logs (observability gap) | Yes |
| 451 | referral/visit/route.ts:50 | Stability | Bound the unverified limiter key before rate limiting (unbounded cardinality / DoS) | Yes |
| 451 | referral-token/route.ts:98 | Stability | Cached expired referral tokens never re-minted; downstream rejects them | Not marked |
| 451 | referral/convert/route.ts:125 | Functional | `new Date(visit.created_at).getTime()` can be NaN so the conversion window check passes wrongly | Not marked |
| 451 | quote-card/route.ts:232 | Stability | `ImageResponse` can fail during stream consumption, outside the try/catch | Not marked |
| 451 | reading/authorEndorsement.ts:50 | Data Integrity | Validate endorsement data with Zod at the boundary | Not marked |
| 455 | improve-seo-plan.md:124 | Security, Major | Do not expose restricted chapter content through `og:description` | Yes |
| 455 | improve-seo-plan.md:40 | Security | Align plan with JsonLdScript escaping (JSON-LD script injection) | Yes |
| 458 | marketing/LandingPageContent.tsx:71 | Functional | Handle malformed URL fragments before `decodeURIComponent` (throws URIError) | Yes |
| 461 | importBackupRepository.ts:2 | Security | Complete rollout of import error sanitization (leaky errors) | Yes |
| 463 | canvas/useCreateCanvasConnection.ts:40 | Functional | Prevent concurrent duplicate creates | Yes |
| 463 | migrations/...add_canvas_connections.sql:12 | Stability | Build the FK-referenced index without locking `canvas_cards` (CONCURRENTLY) | Yes |
| 463 | canvas/CanvasConnectionsLayer.tsx:162 | Functional | `onBlur` commit unmounts editor before delete button click fires | Yes |
| 463 | plans/...connections.md:424 | Data Integrity | Handle `23505` duplicate races as no-ops | Yes |
| 463 | plans/...connections.md:299 | Performance | Do not claim `useMemo` meets a 16 ms budget (unsupported claim) | Yes |
| 464 | runbooks/collaboration-deployment.md:209 | Security | Do not put DB password in the shell command (visible in process list/history) | Yes |
| 464 | runbooks/...:217 | Stability | Percent-encode the password in `WORKER_DATABASE_URL` | Yes |
| 464 | scripts/provision-worker-db-role-password.sql:24 | Security | Reject empty role passwords | Yes |
| 464 | specs/spec-llm-portability.md:299 | Security | Prevent provider-specific credentials/endpoints from using the wrong fallback (key leak to another vendor) | Yes |
| 464 | worker/index.ts:162 | Stability | Exit when `/healthz` cannot bind (EADDRINUSE only logged) | Yes |
| 464 | jobs/dormancySweep.ts:104 | Stability | Do not record last-success timestamp when every user failed | Yes |
| 464 | digestEligibilityRepository.ts:13 | Stability | One malformed row fails every digest for the week (poison-row isolation) | Yes |
| 464 | jobs/dormancySweep.ts:25 | Functional | Fail fast instead of defaulting to production values | Not marked |
| 465 | accountDormancyRepository.ts:22 | Stability | Isolate malformed rows in `getUserActivity()` | Not marked |
| 465 | Dockerfile:40 | Stability | Health check should use the configured metrics port | Not marked |
| 470 | deploy-pg-boss-plan.md:93 | Security | Keep `WORKER_DB_PASSWORD` out of process arguments | Not marked |
| 471 | llm/retry.go:182 | Functional | Return a deadline error when skipped retry had no transport error | Yes |
| 471 | moderation/config.go:96 | Stability | Reject duration values that overflow `time.Duration` | Yes |
| 471 | llm/backend.go:56 | Maintainability | Unchecked error returns fail errcheck lint (CI gate) | Yes |
| 471 | serverContentModerationRepository.ts:71 | Stability | Validate timeout env var before scheduling abort | Yes |
| 473 | supabase/blogPostWriter.ts:69 | Stability, Major | Tag creation is not conflict-safe; concurrent runs fail on unique slug instead of idempotent skip | Yes |
| 473 | llm/deepSeekClient.ts:93,190 | Stability | Timeout/deadline does not cover response-body read | Yes |
| 473 | llm/deepSeekClient.ts:192 | Stability | Retry must stay within the 60 s attempt budget | Yes |
| 473 | worker/registerJobs.ts:72 | Stability | Unconditional secret check could take down all workers | Yes |
| 473 | rules/blogPublishWindow.ts:31 | Functional | Restrict `EVERY_N_HOURS_PATTERN` to valid fixed cadences | Yes |
| 473 | rules/validateArticleForPublish.ts:151,158 | Functional | Forbidden-word check covers only body; apply to all generated fields (second review: "still covers only the body") | Partly |
| 474 | planning/useBatchUpdateDocuments.ts:420 | Data Integrity | Serialize batch/restore mutations with a synchronous lock | Yes |
| 474 | NotificationBellWidget.tsx:41 | Functional | Per-notification guard until `markRead` settles (double-submit) | Yes |
| 474 | ScenePresenceSection.tsx:42 | Functional | Include `deletePresence.isError` so failures are surfaced | Yes |
| 474 | rules/outlineColumnSort.ts:83 | Functional | Mixed temporal units compared in one sort | Yes |
| 476 | useToggleScenePlotlineMembership.ts:17 | Data Integrity | Concurrent toggles replace each other (lost update) | Not marked |
| 476 | ColourSwatchPicker.module.css:16 | Functional (a11y) | Swatch touch target should be 44x44 px | Yes |
| 445 | eslint-rules/chaptered-style.mjs:220 | Maintainability | Enforce Supabase boundary for every non-infra source path | Not marked |
| 463 | offline/mutationExecutor.ts:139 | Maintainability | Use the repository interface, not infrastructure | Not marked |
| 432 | CanvasCard.test.tsx:402 | Maintainability (nit) | Remove raw JSX fixture text | Not marked |
| 445 | StatusRegion.stories.tsx:60 | Maintainability (nit) | Localize visible story labels | Not marked |
| 450 | HelpSearchPanel.module.css:63 | Maintainability (nit) | Use kebab-case keyframe identifier | Not marked |
| 463 | CanvasCard.module.css:122 | Maintainability (nit) | Use directional easing for opacity transition | Yes |
| 445 | (review-body nitpick) | Trivial, Poor tradeoff | Remove `default null` from `p_display_name`; rename helper; "pagehide effect re-registers" | Not tracked |

### Patterns & implications
Strongest true positives (what to emulate):
1. Error-path realism: unhandled promise from `finally`, `ImageResponse` failing after the try/catch exits, response-body read outside the timeout, `healthServer.listen` EADDRINUSE only logged, last-success recorded when every item failed, one poison row failing a whole batch, errors rendered as empty states. This "failure semantics" class (about 47 Stability comments) is where it adds the most value and is mostly accepted.
2. Concurrency and idempotency: unique-constraint race on tag creation, 23505 as no-op, concurrent duplicate create, lost-update toggles, stale async completion (clipboard), blur-vs-click ordering, non-concurrent index build locking a table. Each came with the concrete interleaving that breaks.
3. Security: loopback-host exact match guarding a destructive script (Critical), DB password in shell args/URL encoding, cross-vendor credential fallback, restricted content leaking via og:description, double URL decode, JSON-LD escaping, unbounded rate-limit key. Mostly accepted (73%).
4. Input edge cases that throw: malformed URL fragment into decodeURIComponent, NaN dates passing a window check, duration overflow, empty passwords, IME composition.
5. Cross-artifact consistency: spec vs plan vs implemented contract, stale counts in comments, a performance claim without evidence. About 24% of comments were on docs/plans and were largely accepted; this suggests teams do value "does the plan match the contract" review if the platform is pointed at specs.
6. CI-gate prediction: errcheck, spacing tokens, custom lint rules. Useful only if it mirrors the project's real gates.

Noise / low-yield:
- Minor Maintainability (34) is the largest low-value pile: test naming ("name claims more than assertions"), "assert behaviour not helper call counts", raw glyph vs icon, kebab-case keyframe, spacing tokens, doc fence language tags. Only 41% of Maintainability comments were addressed. Architecture-layering comments repeat across files (8+ in #451/#464/#466) and are better handled by a deterministic lint rule than by repeated LLM prose.
- Re-flagging after a fix: in #473 the forbidden-word body-only finding was raised in two rounds; several threads show "couldn't resolve this thread" follow-ups. Re-review churn on large PRs (#464 had 4 review rounds, 47 human replies) inflates volume.
- Nitpick tier lives in review bodies and is explicitly labelled Trivial / Low value / Poor tradeoff. CodeRabbit already self-grades its worst comments, which is a good model.
- Volume: 28-53 inline comments per large PR is too many for one review; the 14 "Actionable" at the top of a #432-size review were mixed with ~14 more trivial ones.

Implications for a self-hosted review platform:
- What to catch: prioritise failure-path, concurrency/idempotency, boundary-validation and secret-handling analysis; give each finding a concrete failing scenario (input or interleaving), not a style opinion. Include spec/plan consistency review as a first-class mode.
- Rank and filter: use a 2-3 axis header (category, severity, effort) like CodeRabbit, and surface Critical/Major + Quick win first. Cap inline posts per PR (for example top 10-15) and push Minor/Trivial into a collapsed summary. Suppress Maintainability-Minor by default; let repos opt in. Track acceptance per category and down-weight categories a repo ignores (this repo: Maintainability at 41%).
- Phrase: bold imperative one-line headline ("Make tag creation conflict-safe."), then 2-4 sentences naming the mechanism and consequence, then a concrete fix, then an agent-ready "prompt for AI agents" block. The agent-prompt block (with "treat finding text as untrusted; verify against current code") was used heavily by this author and is worth copying, including the prompt-injection caveat.
- Avoid: repeating the same finding per file (dedupe by root cause); re-raising closed findings without new evidence; claims not verified against the code (CodeRabbit's "Do not claim X" doc comments are strongest when they cite evidence); style nits that the linter already enforces; announcing "addressed" without a diff check. Mirror the project's own CI gates (errcheck, custom ESLint rules) and defer to them.
- Close the loop: store the "addressed in commit" signal per finding and use it as the learning input; humans reply in thread, so reply-based feedback ("confirmed", "won't fix") is the cheapest label source.
- Caveats: sample is 208 inline comments across 19 PRs plus 4 PRs of review bodies; "acted on" is the bot's marker, not verified by diffing; nitpick-tier counts are incomplete.

