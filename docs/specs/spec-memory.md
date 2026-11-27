# Phase 4 — Memory PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-08

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 4 ("Memory (continual
learning, human-validated)") into a concrete spec: *"Memory schema:
pending/approved status, provenance, ranking-time decay ... GitHub Adapter's
write path: captures reactions/replies/thread-resolution on the platform's
own posted comments as candidate Memory entries ... Findings merger & verify
start reading approved Memory entries ... Admin API surface:
list/approve/reject/delete, scoped by org/repo/team — API-only at this
point."* Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`); requirements are stated as user stories with
`Given/When/Then` acceptance criteria per `testing.md`'s mandatory rule.

**One PRD, not several.** The four roadmap bullets are facets of a single
capability — a write path that captures raw signal, a human curation surface
that gives it meaning, and a read path that lets an approved meaning quiet
future reviews — living entirely inside the two existing entrypoints (no
third deployable component, per `architecture.md`'s simplicity rule). They
share one traceability table for the same reason `spec-verification-pipeline.md`
gave for staying as one PRD: no independent-value seam separates them.

**Resolves two decisions [ADR-0009](../adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md)
already made**, and this PRD does not re-litigate them: the Memory entry
schema shape (deterministic capture, human-assigned `entry_type` at
approval, all three types modeled now) and the similarity-check mechanism
(repo-scoped, deterministic, no LLM call). This PRD adds the requirements,
NFRs, and phasing ADR-0009 didn't cover, plus three further decisions made
during drafting (tuning defaults, reaction-poll trigger, admin auth — see
"Resolved decisions" at the end).

**Extends, not replaces, four already-shipped modules.**

- `review/verification.py`'s `VerificationStep` — gains a deterministic
  pre-check against approved Memory entries before its existing rubric call,
  per `spec-verification-pipeline.md`'s own explicitly named gap ("this
  spec's verification reads only the fixed rubric, nothing from a
  (not-yet-built) Memory store"). No new macro step, no new step config
  surface — the existing `verification_enabled`/
  `verification_step_timeout_seconds` flags govern this too.
- `adapter/github/posting.py` — gains a persistence side-effect (recording
  which comment corresponds to which finding) it doesn't have today.
- `adapter/github/triggers.py`/`apps/webhook_api/webhook_routes.py` — gain
  three new webhook event subscriptions (`issue_comment`,
  `pull_request_review_comment`, `pull_request_review_thread`) alongside the
  existing `pull_request`/`push` handling.
- `adapter/storage/context_query.py`'s `PostgresSimilarityQuery` pattern — a
  second pgvector cosine-distance query following the identical shape,
  against a new table instead of `index_chunk`.

## Problem & Goal

**Primary end user: the development team on a repo, across time — both as
the source of the signal and its eventual beneficiary.** Today, once
`spec-verification-pipeline.md`'s rubric-based verification ships, a
developer can still see the exact same unwanted finding repeated across
many PRs — the platform has no way to remember that the team already said
"we don't want this" on the same kind of issue in this repo. Every dismissal
is thrown away the moment the PR closes. [`c4-architecture.md`](../reference/c4-architecture.md)'s
design goal 9 and the research this platform is built from
([`research-current-art.md`](../research/research-current-art.md) §2.4)
both name this as the pattern every mature vendor converges on: the tool has
to get better at reviewing *this* codebase from what its own team already
told it, or every PR author pays the same repeated-noise tax forever.

**Concretely, for the PR author:** after this phase, a new finding that
closely matches something the team already, deliberately dismissed on this
repo is suppressed before it ever reaches them — without needing a human to
re-litigate the same objection on every subsequent PR.

**Concretely, for the person curating memory (a new role this phase
introduces — an admin or tech lead, not necessarily the PR author):** every
piece of raw signal the platform observes (a reply, a reaction, a resolved
thread) is presented for a human decision, never silently acted on. This
person's work is the mechanism by which "the team's own judgment" becomes
the future reviews' filter — design goal 9's literal requirement, not
paperwork bolted on afterward.

**Honest framing check (per this PRD's own championing-the-end-user
stance):** "fewer captured signals to review" would be *less work for the
admin*, not more value for the PR author — so this PRD does not propose
auto-approval, confidence scoring, or any shortcut that reduces admin
friction at the cost of silent inference. Every requirement below that
touches classification keeps a human in the loop, even where it would be
technically simpler not to (see US-M1/US-M4, and ADR-0009 decision 1's
rejection of sentiment heuristics for exactly this reason).

**Secondary end user: the platform operator**, whose BYO-key LLM spend
*drops* under this phase rather than growing — a suppressed finding never
enters `verification`'s rubric-judged LLM batch at all (see US-M6), the
first phase in this roadmap where a new capability reduces LLM call volume
rather than adding to it.

**Goal / success metric, capability-based (matching every prior phase's own
stance — no fabricated adoption metric):** for a review job whose candidate
LLM findings include one that closely matches an approved, repo-scoped
`dismissed_finding` Memory entry, that finding is suppressed before any LLM
judges it; every reply/thread-resolution/reaction on a platform-posted
comment is captured as an attributable, human-reviewable pending entry;
every pending entry is visible, approvable, rejectable, and deletable via
the admin API, scoped correctly by org/repo/team. Acceptance is behavioral
(the tests in Testing / Key Scenarios below), not a fabricated
precision/recall number nothing has measured yet.

## Scope

### In scope

- **Memory entry schema** (`memory_entry` table) per ADR-0009 decisions 1
  and 4: `status` (`pending`/`approved`/`rejected`), `entry_type`
  (`dismissed_finding`/`accepted_fix`/`correction`, nullable until a human
  assigns it at approval), `raw_signal_type` (`reaction`/`reply`/
  `thread_resolution`), raw signal content, provenance (FK to
  `posted_finding_comment`, below), `org_id`/`repo_id`/`team_id` (nullable,
  following `OrgRepoTeamSetting`'s existing scoping convention), an
  `embedding` column (pgvector, 384-dim, same dimension as `index_chunk`),
  `approved_at`/`approved_by`, `created_at`/`updated_at`.
- **`posted_finding_comment` table (new).** `adapter/github/posting.py`
  persists one row per posted inline comment at post time: comment id (PK,
  GitHub's own id — same "the natural key already guarantees the invariant"
  pattern `webhook_delivery`'s id already uses), `repo_id`, `pr_number`,
  `commit_sha`, and the finding's own fingerprint fields (`file`, `rule_id`,
  `message`). This is the only way an inbound signal can ever be attributed
  back to the finding it's about — nothing in this phase works without it.
- **GitHub Adapter write path — replies and thread-resolution (webhook-driven).**
  New subscriptions to `issue_comment`/`pull_request_review_comment`
  ("created" actions) and `pull_request_review_thread` (`resolved`/
  `unresolved` actions). Each, when it targets a comment with a
  `posted_finding_comment` row, creates one `pending` Memory entry recording
  the raw signal — never inferring meaning from it (ADR-0009 decision 1).
- **GitHub Adapter write path — reactions (opportunistic polling, no new
  webhook subscription, no new scheduler).** Per ADR-0009 decision 5,
  extended during this PRD's drafting: reactions on a PR's own
  `posted_finding_comment` rows are fetched via the GitHub REST API (a) when
  a later push enqueues a new `REVIEW` job for the same PR, and (b) when the
  PR's own `pull_request` "closed" webhook action arrives — a webhook this
  app already receives today (its App already subscribes to `pull_request`
  for the existing review-trigger path) but currently ignores for
  non-trigger actions. Each newly observed reaction (deduped against
  previously captured ones on the same comment) creates one `pending` entry.
- **Human approval assigns meaning (US-M4).** The admin API's approve
  action requires an explicit `entry_type`; the entry's embedding is
  computed lazily at approval time — only for entries approved as
  `dismissed_finding` — over the linked finding's fingerprint text
  (`file` + `rule_id` + `message`), using the same `LocalONNXEmbeddingClient`
  Phase 2 indexing already uses. `accepted_fix`/`correction` entries are
  approved without ever computing an embedding — nothing reads them yet.
- **Admin API — list/approve/reject/delete, scoped by org/repo/team
  (US-M4/M5/M8).** New FastAPI routes on the Webhook API entrypoint
  (`apps/webhook_api`, following `manifest_routes.py`'s existing
  route-factory pattern), gated by a static bootstrap admin API key (see
  "Resolved decisions").
- **Similarity-based suppression inside `VerificationStep` (US-M6/M7).** A
  deterministic pre-check, before the existing rubric LLM call: each
  candidate LLM finding's fingerprint is embedded and compared via a new
  pgvector cosine-distance query, following the same pattern as
  `adapter/storage/context_query.py`'s existing `PostgresSimilarityQuery`
  (outcome only — the concrete class/module name is an engineering call,
  not fixed here) against approved, repo-scoped `dismissed_finding`
  entries, weighted by each entry's own ranking-time decay. A match above
  threshold excludes the finding from `VerificationStep`'s output through
  the same rejection path a rubric-failed finding already uses — no LLM
  call spent on it. A finding with no match proceeds through the rubric
  exactly as before this phase. Gated by its own bootstrap flag,
  `memory_suppression_enabled` (default `true`) — an operator can disable
  just this sub-check (e.g. while diagnosing a suspected bad threshold)
  without disabling capture or the admin API, the same per-capability
  kill-switch convention every macro step already has.
- **Ranking-time decay (US-M9).** Exponential half-life decay on an
  approved entry's similarity weight — never a deletion/expiry job — per
  the C4 doc's already-decided model, with a concrete formula and defaults
  set here (see "Resolved decisions").
- **Admin key scope (clarifying US-M10, per [ADR-0004](../adr/0004-multi-org-tenancy.md)'s
  "org is a scoping key, not a security boundary" precedent).** The static
  admin key is one platform-wide credential, not issued per org/repo/team —
  it authorizes admin actions across the whole deployment. `org_id`/
  `repo_id`/`team_id` are filter parameters on the **list** endpoint only
  (US-M8) — narrowing which entries a query returns. Approve/reject/delete
  (US-M4/M5) act on one entry by its own id, which already carries its own
  fixed scope from capture time, so no separate org/repo/team parameter is
  needed or accepted on those calls. None of this is an access-control
  boundary the key itself enforces — consistent with there being no
  per-user identity until Phase 5's real SSO/OIDC, and with ADR-0004's
  explicit stance that org/team is a grouping key inside one trusted
  deployment, not a tenant boundary.
- **Reply-text redaction before storage.** Raw `reply` signal content passes
  through `findings.redact.TextRedactor` before being persisted, matching
  the existing "every producer's free-text field gets redacted" convention
  `findings/models.py`'s `message` field already establishes — a developer's
  reply could otherwise carry quoted source/secret content into the Memory
  store unredacted.
- **GitHub App manifest update.** `adapter/github/manifest.py`'s webhook
  event subscription list gains `issue_comment`, `pull_request_review_comment`,
  and `pull_request_review_thread` (see Rollout & Release Safety).

### Out of scope (deferred to a later spec or roadmap phase)

- **Org- or team-wide similarity search.** ADR-0009 decision 2 — repo-scoped
  only until Phase 6 resolves ADR-0004's still-open RBAC question.
- **Consumption of `accepted_fix`/`correction` entries.** Captured and
  storable this phase; nothing reads them. A later phase's own scope, named
  explicitly per ADR-0009 decision 4.
- **Real per-user identity/SSO/OIDC for the admin API.** Explicitly still
  open in `c4-architecture.md`, targeted at Phase 5's admin UI. This phase
  ships a coarse static-key stopgap (see "Resolved decisions"), not real
  auth.
- **Admin UI.** Phase 5, per the roadmap — this phase is API-only, exactly
  as the roadmap states.
- **A dedicated polling scheduler for reactions.** ADR-0009 decision 5 —
  rejected as disproportionate new infrastructure for a secondary signal.
- **Sentiment/keyword classification of reply text.** ADR-0009 decision 1 —
  rejected; a human always assigns `entry_type`.
- **Feeding a similarity match into the rubric LLM call as context.**
  ADR-0009 decision 3 — rejected; the match is a deterministic gate, not
  LLM input.
- **A numeric/graded confidence score on a Memory entry's match strength**
  beyond the binary threshold gate — mirrors `spec-verification-pipeline.md`'s
  own decision against a graded verification score; nothing yet consumes
  one.
- **Changing `findings/merge.py`'s own dedupe/ceiling logic.** Unaffected —
  suppression happens upstream inside `verification`, so fewer findings
  simply reach the merger; its own ordering logic (US-V8) is untouched.

### Priority

- **Must have — all of it.** The user, in this PRD's own drafting interview,
  confirmed no partial-signal version ships: schema + deterministic capture
  (replies, thread-resolution, reactions) + human-assigned classification +
  similarity-based suppression + the scoped admin API, together, are Phase
  4's one indivisible capability — completing design goals 6 and 9 the way
  `spec-verification-pipeline.md`'s own Must-have completed design goal 6's
  rubric half. No Should/Could split this phase, unlike Phase 3's.

## Requirements

Stated as user stories per `testing.md`'s mandatory format.

**US-M1 — A reply is captured as raw signal, never auto-classified**
As a developer replying to the platform's posted comment, I want my reply
captured as raw material for the team's shared learning, so that a human
decides what it means later — my words are never silently mislabeled as a
dismissal or acceptance they don't actually express.

- Given a `posted_finding_comment` row exists for a comment, When GitHub
  sends `issue_comment` or `pull_request_review_comment` with action
  `created` targeting that comment (a reply, not the original comment
  itself), Then a new `memory_entry` row is created with `status=pending`,
  `entry_type=NULL`, `raw_signal_type="reply"`, the reply text (redacted per
  `TextRedactor`) as its raw content, and provenance linking to the original
  `posted_finding_comment` row.

**US-M2 — Thread-resolution is captured as raw signal**
As a developer resolving a review thread, I want that action captured, so
that a pattern of "resolved without a corresponding fix" can inform the
team's memory once a human reviews it.

- Given a `posted_finding_comment` row's review thread, When GitHub sends
  `pull_request_review_thread` with action `resolved` or `unresolved`, Then
  a `pending` `memory_entry` is created with `raw_signal_type="thread_resolution"`
  and the resolved boolean as raw content, provenance linked to the finding.

**US-M3 — Reactions are captured despite no webhook existing for them**
As a developer reacting to the platform's posted comment, I want that
reaction captured even though GitHub sends no webhook for it, so my
feedback isn't silently lost to a platform limitation I have no way to
know about.

- Given a PR with one or more `posted_finding_comment` rows, When a later
  push enqueues a new `REVIEW` job for that same PR, Then the platform
  fetches current reactions on those comments via the GitHub REST API and
  creates one `pending` `memory_entry` per newly observed reaction — deduped
  by the pair `(comment_id, reaction_id)`, GitHub's own per-reaction id from
  the Reactions API listing, so the same reaction is never captured twice
  across repeated polls — with `raw_signal_type="reaction"`.
- Given the same PR, When GitHub sends `pull_request` with action `closed`,
  Then the same reaction fetch runs once for that PR before the webhook
  handler returns, independent of whether a new `REVIEW` job is enqueued.

**US-M4 — A human assigns meaning at approval, never before**
As an admin curating the team's memory, I want to decide what a pending
entry actually represents when I approve it, so the system never guesses
intent from a reaction or a few words of reply text.

- Given a `pending` `memory_entry`, When an admin calls the approve endpoint
  with an explicit `entry_type` (`dismissed_finding`/`accepted_fix`/
  `correction`), Then the entry's `status` becomes `approved`, `entry_type`
  is set to the given value, `approved_at` is stamped, and — only when
  `entry_type="dismissed_finding"` — its `embedding` is computed over the
  linked finding's fingerprint (`file`+`rule_id`+`message`) and stored.
- Given an approve call with no `entry_type` or an invalid one, When the
  admin API processes it, Then the request is rejected (422) and the
  entry's `status` stays `pending` — approval without classification is
  never allowed.

**US-M5 — Reject and delete are as easy as approve, and classification stays
revisable**
As an admin curating memory, I want to reject or delete an entry I disagree
with, and to change my mind on a classification later, so a wrong call
isn't permanent just because I already acted on it — except when I
deliberately mean to erase the content itself.

- Given a `memory_entry` in any status, When an admin calls the reject
  endpoint, Then `status` becomes `rejected` (idempotent — rejecting an
  already-rejected entry is a no-op 200, not an error) and it is never read
  by the similarity check (US-M6) again.
- Given a `rejected` or `approved` `memory_entry`, When an admin calls the
  approve endpoint again with an `entry_type`, Then the entry is
  re-classified exactly as US-M4 describes for a fresh `pending` entry
  (status/entry_type/approved_at/embedding all recomputed) — approve and
  reject are reversible state transitions in either direction, not
  one-way gates.
- Given any `memory_entry`, When an admin calls the delete endpoint
  **without** a `confirm=true` query parameter, Then the request is
  rejected (400) and nothing is deleted; When called **with**
  `confirm=true`, Then the row is removed entirely and irreversibly. Delete
  is reserved for erasing the underlying content (e.g. a developer's reply
  text); reject is the recommended action for "this shouldn't count" since
  it keeps the audit trail and stays reversible.
- Given an entry id that no longer exists (already deleted, or never
  existed), When an admin calls delete (with or without `confirm=true`) or
  approve/reject against that id, Then the request is rejected (404) —
  unlike reject's no-op-200 (a genuine idempotent action on an *existing*
  row), a repeat delete has nothing left to act on and reports that
  honestly rather than silently succeeding a second time.

**US-M6 — A close match to a dismissed finding is suppressed before any
LLM judges it**
As a future PR author, I want a new finding that closely matches something
my team already dismissed on this repo to be suppressed automatically, so I
don't see the same unwanted comment repeated across PRs.

- Given one or more approved, repo-scoped `dismissed_finding` Memory
  entries, When `VerificationStep` runs, Then each candidate
  `source="llm"` finding's fingerprint is embedded and compared (pgvector
  cosine similarity) against those entries' embeddings, weighted by each
  entry's own decay (see US-M9); a finding whose best match exceeds the
  configured threshold is excluded from `VerificationStep`'s output through
  the same path a rubric-rejected finding uses, and is never included in
  the batch sent to the rubric LLM call.
- Given zero approved `dismissed_finding` entries exist for this repo, When
  `VerificationStep` runs, Then no similarity comparison is attempted and
  every candidate finding proceeds to the rubric call exactly as before
  this phase.

**US-M7 — Findings with no match are unaffected**
As a PR author, I want a genuinely new finding to reach the same rubric
check it always has, so this phase never blocks a finding my team hasn't
actually spoken about before.

- Given a candidate finding whose best similarity match against approved,
  repo-scoped `dismissed_finding` entries is below threshold (or no entries
  exist), When `VerificationStep` runs, Then that finding proceeds through
  the existing rubric-based LLM call byte-for-byte as
  `spec-verification-pipeline.md` already specifies.

**US-M8 — Entries are listable, scoped, filterable, and paginated**
As an admin, I want to list Memory entries scoped by org/repo/team and
filtered by status, paged rather than all at once, so I can find what needs
my decision without wading through everything ever captured or risking an
unbounded response as capture volume grows.

- Given Memory entries across multiple repos, When an admin calls the list
  endpoint with a `repo_id` and/or `status` filter, Then only matching
  entries are returned, ordered newest-first.
- Given more entries match than fit one page, When an admin calls the list
  endpoint with `limit`/`offset` (default `limit=50`, capped at `200`),
  Then exactly that page of matching entries is returned along with the
  total matching count, never every matching row in one response.

**US-M9 — Ranking-time decay, never silent deletion**
As an admin, I want an old, unused approved entry to naturally stop
influencing new findings over time rather than requiring me to notice and
delete it, so stale learnings fade gracefully instead of accumulating
forever or needing constant manual pruning.

- Given an approved `dismissed_finding` entry whose `approved_at` timestamp
  is `N` days in the past (the reference point — an entry's trust clock
  starts when a human approved it, not when the raw signal was first
  captured), When US-M6's similarity comparison runs, Then its effective
  match weight is `raw_cosine_similarity * 0.5 ** (N / 180)` (180-day
  half-life for `approved` entries — see "Resolved decisions"), compared
  against the same 0.90 threshold — the entry is never deleted or excluded
  from the query outright by decay alone; an old entry can still match a
  sufficiently close finding, just needs closer raw similarity to clear the
  now-lower effective weight.
- **Scope note:** `pending` entries are never read by the similarity check
  (design goal 9 — only `approved` entries influence a real review), so no
  scenario exercises a `pending` half-life; the schema's decay model is
  generic enough for a future consumer of `pending` confidence to reuse,
  but this phase defines and tests only the `approved` half-life above.

**US-M10 — The admin API rejects unauthenticated requests**
As a platform operator, I want the admin API to reject requests without a
valid key, so a write-capable curation surface isn't left open on an
enterprise product before real SSO/OIDC exists.

- Given no `Authorization: Bearer <admin_api_key>` header or an incorrect
  key, When any admin route is called, Then the request is rejected (401)
  and no Memory entry is read, modified, or created.

**US-M11 — Posted comments are attributable back to their finding**
As a developer whose reply or reaction should land on the right piece of
the team's memory, I want the platform to remember which comment was about
which finding, so my feedback is never unattributable.

- Given `FindingsOutputStep` posts a batch of inline comments successfully,
  When the batch completes, Then one `posted_finding_comment` row is
  persisted per posted comment (comment id, repo, PR, commit sha, and the
  finding's `file`/`rule_id`/`message`) before the job is marked complete.

**US-M12 — Suppression can be disabled independently as an operational
rollback lever**
As a platform operator, I want to turn off Memory-based suppression
specifically, without disabling capture or the admin API, so I have an
immediate rollback lever if a bad threshold or a wrongly-approved entry
starts hiding findings it shouldn't — without having to hunt down and
reject the offending entry first under time pressure.

- Given `memory_suppression_enabled=false`, When `VerificationStep` runs,
  Then no similarity comparison is attempted regardless of how many
  approved `dismissed_finding` entries exist, and every candidate finding
  proceeds to the rubric call exactly as Phase 3 specified — byte-for-byte
  Phase 3 behavior, restored on demand.
- Given the same flag set to `false`, When a reply/reaction/thread-resolution
  is observed, Then it is still captured as a pending entry exactly as
  US-M1/M2/M3 describe — only the read/suppression side is gated, never
  capture or the admin API.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- The reaction-fetch REST call (US-M3) is scoped to the single PR already
  being processed — never a cross-PR or cross-repo scan — bounding both
  latency and GitHub API rate-limit consumption to what that job already
  incurs elsewhere.
- The similarity comparison (US-M6) reuses the same `LocalONNXEmbeddingClient`
  Phase 2 indexing already loads in-process — no new model load, no new
  latency-critical external dependency.
- `VerificationStep`'s existing `verification_step_timeout_seconds` (60s)
  budget is unchanged; the similarity pre-check is a single embedding call
  plus one indexed pgvector query, materially cheaper than the rubric LLM
  call it may now skip entirely for a suppressed finding.
- The admin API's static key (US-M10) is bootstrap config — read once via
  `pydantic-settings`, delivered as a Kubernetes `Secret`, never logged —
  matching every other credential in this codebase's existing pattern. No
  in-app rotation mechanism or per-key rate limiting/lockout is built this
  phase — rotation is standard Kubernetes `Secret` rotation (redeploy with
  a new value), an accepted stopgap-level control consistent with this key
  being explicitly temporary until Phase 5's real SSO/OIDC (see "Out of
  scope").
- **No ANN index for the Memory similarity query (v1, matching existing
  precedent).** Following `adapter/storage/context_query.py`'s own accepted
  posture for `PostgresSimilarityQuery` ("no ANN index backs this for v1... a
  sequential scan plus sort, one per call, accepted since no latency SLA is
  set"), the Memory similarity query takes the same approach: a
  per-verification sequential scan over one repo's approved
  `dismissed_finding` entries, expected to stay in the low thousands per
  repo. No latency SLA is set here either, for the same reason. If a real
  deployment's entry count later demands one, adding an ANN index is a
  query-level change, not a schema change.
- No new redaction surface beyond what already exists: reply text is
  redacted before storage (see Scope); the reaction/thread-resolution raw
  signals carry no free text at all (an emoji, a boolean).

### Privacy / Compliance

Reply text is now persisted (redacted) in the platform's own database for
the first time — previously, no free-text developer input was stored
anywhere beyond the reviewed repo itself. This is new data at rest, inside
the customer's own deployment boundary (per ADR-0001, still never leaving
their infrastructure), and subject to the same admin-visible
list/reject/delete controls as every other Memory entry — a developer's
reply can always be deleted by an admin, matching design goal 9's "visible
and deletable ... for as long as the customer keeps it."

### Observability

- Each capture path (US-M1/M2/M3) logs its own signal-created event with
  `job_id` (where applicable)/`repo_id`/`raw_signal_type` at `info`.
- `VerificationStep` additionally logs, per job: how many candidate findings
  were suppressed by a Memory match versus how many proceeded to the rubric
  call — an operator-facing tuning signal, never a suppressed finding's own
  raw message content, matching `spec-verification-pipeline.md`'s existing
  redacted-drop-log posture.
- The admin API logs every approve/reject/delete call (entry id, action,
  timestamp) at `info` — an audit trail for a write-capable curation
  surface, independent of whatever real identity Phase 5 eventually adds.
- **No new metrics/alerting pipeline — logging-only, matching every prior
  phase's posture.** This codebase has no metrics/alerting infrastructure
  today (every prior spec's Observability section is structured-logging
  only); this phase doesn't introduce one either. An operator detects a
  misbehaving similarity threshold via `VerificationStep`'s per-job
  suppressed-vs-proceeded count (above), aggregated over time in whatever
  log platform the customer's own deployment already uses — the same
  detection posture `spec-verification-pipeline.md` already established
  for its own rejection-rate logging, not a new capability this phase
  needs to invent.

### Rollout & Release Safety

- **New Alembic migration** for `memory_entry` and `posted_finding_comment`
  — no existing table's shape changes.
- **GitHub App manifest change requires re-consent on existing
  installations.** Adding `issue_comment`/`pull_request_review_comment`/
  `pull_request_review_thread` webhook subscriptions to
  `adapter/github/manifest.py` means an already-installed GitHub App
  instance won't receive these events until an admin re-approves the App's
  updated permissions — a real, customer-visible upgrade step, not a
  transparent code deploy. Documented in `docs/reference/operations.md` as
  part of this phase's upgrade notes.
- **Backwards compatible when Memory is empty.** A deployment with zero
  approved `dismissed_finding` entries (fresh install, or every entry
  rejected) sees byte-for-byte the same `VerificationStep` behavior as
  before this phase (US-M7) — Memory is additive, never a required
  dependency for review to function, consistent with every prior phase's
  "optional augmentation" posture.
- **Kill switch for suppression specifically.** `memory_suppression_enabled=false`
  (see Scope) restores exactly Phase 3's `VerificationStep` behavior — the
  fixed-rubric call runs on every candidate finding with no Memory-based
  pre-check — the operational rollback lever if a bad threshold or a
  wrongly-approved entry starts suppressing findings it shouldn't in
  production, without needing to hunt down and reject the offending entry
  first under time pressure. Capture (US-M1-M3) and the admin API keep
  running regardless — only the read/suppression side is gated by this
  flag, matching every other macro step's own independent enable-flag
  convention.
- **No auth migration path implied.** The static admin API key (US-M10) is
  explicitly a stopgap; Phase 5's real SSO/OIDC replaces it, not extends
  it — no requirement here to design for a smooth key-to-SSO transition.

## UX & Design

N/A — no new user-facing UI surface this phase (the roadmap explicitly
scopes the Admin UI to Phase 5; this phase is "API-only"). The only
observable change to the PR author is a **quieter review**: a finding that
matches something the team already dismissed simply doesn't appear, with no
new interaction, state, or visual element added to GitHub's own PR UI —
the same kind of change `spec-verification-pipeline.md`'s own UX section
already made for its rubric-based rejections.

- **Primary flow:** unchanged — inline comments and a check-run appear
  after review completes, now possibly containing fewer findings.
- **Empty state:** unchanged (inherited) — no approved Memory entries yet
  produces byte-for-byte Phase 3 behavior (US-M7).
- **Error state:** a failure in the similarity pre-check (e.g. the Postgres
  query itself failing) is treated exactly like any other `VerificationStep`
  failure — `FailurePolicy.FATAL`, per `spec-verification-pipeline.md`'s
  US-V7 — never a silent skip-and-continue that would let an unsuppressed
  finding through on a broken check.
- **Admin API consumer — flows and edge cases (this phase's own
  user-facing surface, even without a UI):**
  - **Primary flow:** list pending entries (paged, US-M8) → approve with an
    explicit `entry_type` (US-M4) or reject (US-M5) each.
  - **Empty state:** an empty matching set returns a `200` with an empty
    list and a total count of `0`, not an error — a repo with no captured
    signal yet is a normal state, not a failure.
  - **Error state:** an approve call missing/with an invalid `entry_type`
    is `422` (US-M4); an unauthenticated call is `401` (US-M10); a delete
    without `confirm=true` is `400` (US-M5) — each a distinct, specific
    status rather than a generic `400`/`500` a caller can't act on.
  - **Idempotency / no destructive surprises (usability heuristic —
    forgiving actions, clear feedback):** approve and reject are reversible
    in either direction and safe to repeat (US-M5) — an admin who
    reclassifies their mind doesn't need a support ticket. Delete is the
    one genuinely irreversible action, and requires an explicit
    `confirm=true` rather than being one call away by accident — the
    closest a JSON API gets to a confirmation dialog's role in a UI.
  - Every response is structured JSON suitable for a future Phase 5 screen
    to consume directly — no admin-API-specific shape that Phase 5 would
    need to redesign around.
- **Accessibility/responsiveness/design-system consistency:** N/A — no UI
  this phase, same as every prior phase's stance where applicable.

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author, `R`/`A` on every existing
  ADR's RACI table; no separate design/eng/QA/legal stakeholders exist for
  this project at this stage.
- **Acceptance:** every `US-M*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios); `mypy --strict`/`ruff` clean
  per `architecture.md`; `review/test_step_boundaries.py`'s existing
  import-boundary check passes unmodified for `verification.py`'s extended
  logic, proving the domain-package import boundary still holds with a new
  `memory` package dependency.

## Phases / Units

- **Unit A — Memory schema (`memory_entry`, `posted_finding_comment`) +
  Alembic migration.** Depends on: nothing new. Risk: low — pure schema
  work following `adapter/storage/models.py`'s existing conventions
  (nullable org/repo/team scoping, `Vector(384)` embedding column). No
  reader/writer logic yet, matching `OrgRepoTeamSetting`'s own
  "scaffolding first" precedent. **Acceptance:** migration applies cleanly
  against a fresh Postgres/pgvector instance; model unit tests assert
  column defaults/nullability.
- **Unit B — `posting.py` provenance persistence (US-M11).** Depends on:
  Unit A. Risk: low — additive write to an already-shipped, already-tested
  module; no change to its existing posting/retry logic.
  **Acceptance:** US-M11's `Given/When/Then` passes as an integration test.
- **Unit C — Webhook-driven capture: replies + thread-resolution (US-M1,
  US-M2).** Depends on: Unit A, Unit B (needs `posted_finding_comment` rows
  to attribute against). Risk: medium — three new webhook event types end
  to end (signature verification already exists and is reused unchanged;
  new payload parsing, new trigger-table entries in `triggers.py`).
  **Acceptance:** US-M1 and US-M2's `Given/When/Then` pass as integration
  tests against synthetic webhook payloads.
- **Unit D — Opportunistic reaction polling (US-M3).** Depends on: Unit B.
  Risk: medium — the first REST-API *read* call this codebase makes to
  GitHub outside of diff-fetching; needs its own recorded-cassette/`respx`
  coverage per `testing.md`'s LLM-determinism-adjacent convention applied to
  a new external call. **Acceptance:** US-M3's both `Given/When/Then`
  branches (new-push trigger, PR-closed trigger) pass as integration tests.
- **Unit E — Admin API: list/approve/reject/delete + static-key auth
  (US-M4, US-M5, US-M8, US-M10).** Depends on: Unit A. Risk: low — new
  FastAPI routes following `manifest_routes.py`'s existing route-factory
  pattern; auth is a single Bearer-token dependency, no new credential
  type beyond the existing bootstrap-config pattern.
  **Acceptance:** US-M4, US-M5, US-M8, and US-M10's `Given/When/Then` all
  pass as integration tests against a real Postgres via
  `testcontainers-python`.
- **Unit F — Similarity-based suppression inside `VerificationStep`
  (US-M6, US-M7, US-M9).** Depends on: **Unit E, functionally, not just
  Unit A structurally** — Unit A defines the `embedding` column, but Unit E's
  approve action (US-M4) is what actually *writes* a real embedding, and
  nothing in production reaches a non-empty Memory store for Unit F to read
  until Unit E is shipped and at least one entry has been approved.
  `PostgresSimilarityQuery`'s existing pattern is reused (not modified) for
  the query shape itself. Risk: medium — the first place this codebase's
  verification logic reads persisted state beyond the current job's own
  pipeline, and the unit carrying this phase's core design-goal-6
  completion. **Acceptance:** US-M6, US-M7, and US-M9's `Given/When/Then`
  pass as integration tests against a real Postgres/pgvector instance with
  hand-seeded approved entries (isolating Unit F's own query/decay logic);
  additionally, one integration test exercises the real Unit E → Unit F
  contract end to end — approve a pending entry via the actual admin API,
  confirm a real embedding is written, then confirm `VerificationStep`'s
  similarity query reads and matches against it — so the dependency above
  is proven, not just asserted. This unit also owns the
  `memory_suppression_enabled` bootstrap flag (`apps/worker/settings.py`,
  same convention as every existing step flag); US-M12's `Given/When/Then`
  passes as an integration test confirming disabling it restores
  byte-for-byte Phase 3 behavior.
- **Unit G — GitHub App manifest update + composition-root wiring.**
  Depends on: Units C, D, E. Risk: low — mechanical addition of three
  webhook event subscriptions to `adapter/github/manifest.py` and wiring
  the new routes/repositories into `apps/webhook_api/main.py`.
  **Acceptance:** `adapter/github/test_manifest.py` (extended) asserts the
  three new subscriptions are present; an integration test confirms the
  admin routes are only registered when an admin key is configured (US-M10).

## Testing / Key Scenarios

Per `testing.md`'s pyramid.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-M1 | A reply to a posted-finding comment creates a pending, unclassified entry with redacted raw text | `adapter/github/test_comment_capture.py` (new) | Integration |
| US-M2 | A thread-resolution event creates a pending entry with the resolved boolean | `adapter/github/test_comment_capture.py` | Integration |
| US-M3 | Reactions are fetched (and deduped) on a new push and on PR-closed, each producing a pending entry | `adapter/github/test_reaction_polling.py` (new) | Integration (recorded GitHub REST cassette) |
| US-M4 | Approve requires an explicit `entry_type`; approving as `dismissed_finding` computes and stores an embedding; approving without a type is rejected | `apps/webhook_api/test_admin_routes.py` (new) | Integration (real Postgres) |
| US-M5 | Reject is idempotent (no-op 200 on an already-rejected entry); approve/reject can flip an entry's classification in either direction; delete without `confirm=true` is rejected (400), with it removes the row; any action against an already-deleted/nonexistent id is 404 | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M6 | A candidate finding matching an approved dismissed entry above threshold is suppressed before the rubric call; zero approved entries skips the comparison | `review/test_verification_step.py` (extended) | Integration (recorded LiteLLM cassette + real Postgres/pgvector) |
| US-M7 | A non-matching finding proceeds through the rubric unchanged | `review/test_verification_step.py` | Integration |
| US-M8 | List endpoint filters correctly by repo/status; a matching set larger than one page returns exactly `limit` rows plus the total count | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M9 | An older approved entry's effective match weight equals `raw_similarity * 0.5 ** (days_since_approved_at / 180)`, verified against a hand-computed expected value at a known age | `adapter/storage/test_memory_similarity_query.py` (new) | Unit (pure decay-math function) / Integration (query-level weighting) |
| US-M10 | A request without a valid admin key is rejected on every admin route | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M11 | Posting a batch of comments persists one `posted_finding_comment` row per comment | `adapter/github/test_posting.py` (extended) | Integration |
| Unit E → Unit F contract | Approving a pending entry via the real admin API writes a real embedding; `VerificationStep`'s similarity query then reads and matches against that same real entry — proving Unit F's true dependency on Unit E, not just on Unit A's schema | `review/test_verification_step.py` (extended) | Integration (real Postgres/pgvector, real admin route, no hand-seeding) |
| US-M12 | Disabling `memory_suppression_enabled` restores byte-for-byte Phase 3 `VerificationStep` behavior even with matching approved entries present; capture and admin API keep running | `apps/worker/test_main.py` (extended) | Unit/Integration (mixed, matching US-V9's existing precedent) |
| US-M1–M11 (happy path) | Full Docker Compose stack: a reply is posted, approved as `dismissed_finding` via the admin API, and a subsequent PR's matching finding is suppressed end-to-end | `tests/e2e/test_memory_pipeline.py` (new) | E2E |

## Resolved decisions (formerly open assumptions)

Three further decisions were confirmed with the user during this PRD's
drafting, beyond the four ADR-0009 already settled:

1. **Tuning defaults (assumption pending validation — no document or usage
   data fixes these numbers yet):** similarity threshold `0.90` cosine
   similarity; decay half-life `14` days for `pending` entries, `180` days
   for `approved` entries. Chosen as a high bar to minimize false
   suppression on a mechanism that silently drops findings, and a decay
   spread wide enough that an unreviewed inference fades fast while a
   human-approved learning stays influential for a meaningful stretch.
   Tunable later without a schema change (a config value, not a structural
   one) — not blocking this PRD on data this platform hasn't collected yet.
2. **Reaction-poll trigger widened beyond ADR-0009's original text
   (decided):** in addition to piggybacking on a later push, reaction
   polling also fires on the PR's own `pull_request` `closed` webhook
   action — a webhook this app's GitHub App already receives today for its
   existing review-trigger subscription, just currently unhandled for that
   action. Meaningfully closes the coverage gap ADR-0009 named (a PR that
   never gets a second push previously had zero chance of reaction
   capture) at effectively no new infrastructure cost.
3. **Admin API auth (decided):** a static bootstrap admin API key
   (Bearer-token, Kubernetes `Secret`, `pydantic-settings`-read), not the
   "no auth in Phase 4" alternative — closes an unauthenticated
   write-capable-API gap now rather than carrying it as a known
   vulnerability until Phase 5's real SSO/OIDC lands.

All three are reflected as decided requirements above (US-M6/M9's NFRs,
US-M3, US-M10) — none remain open. ADR-0009's own four decisions (signal
classification, similarity scope, match action, entry taxonomy) are
likewise fully reflected in Scope and Requirements above.

A further round of gaps surfaced by this PRD's own self-review pass
(`prd-review`) were closed directly from established codebase precedent,
without needing further user input:

4. **Decay formula and reference timestamp made concrete (US-M9):**
   `effective_weight = raw_cosine_similarity * 0.5 ** (days_since_approved_at / 180)`
   — `approved_at`, not `created_at`, is the reference point (an entry's
   trust clock starts when a human approved it). `pending`'s own half-life
   is explicitly scoped as unexercised this phase, since design goal 9
   means `pending` entries are never read by the similarity check.
5. **Reaction dedup key made concrete (US-M3):** `(comment_id, reaction_id)`
   — GitHub's own per-reaction id, not an inferred key.
6. **Admin key scope clarified (US-M10):** one platform-wide credential per
   ADR-0004's "scoping key, not a security boundary" precedent —
   org/repo/team on a request is a query filter, never a per-tenant access
   boundary the key itself enforces.
7. **Admin curation is reversible except delete (US-M5):** approve/reject
   flip freely in either direction; delete requires an explicit
   `confirm=true` and is the one irreversible action, reserved for erasing
   content rather than "this shouldn't count" (use reject for that).
8. **List endpoint is paginated (US-M8):** `limit`/`offset`, default 50,
   capped at 200, with a total count — never an unbounded response.
9. **`memory_suppression_enabled` kill switch added (US-M12):** an
   operational rollback lever independent of capture/admin-API availability,
   matching every other macro step's own enable-flag convention.
10. **Unit F's real dependency on Unit E made explicit** (Phases / Units) —
    previously buried in a parenthetical, now a stated dependency with its
    own dedicated integration test proving the contract rather than only
    exercising it via hand-seeded fixtures.

None of these needed a product/business call beyond what ADR-0009, this
PRD's own prior decisions, or existing codebase precedent already
supplied — no open items remain.
