# Plan: Phase 4 — Memory

**Status: Proposed.** Sequences [`docs/specs/spec-memory.md`](../specs/spec-memory.md)
— [`docs/roadmap.md`](../roadmap.md)'s Phase 4 — into buildable,
independently-testable phases, following the same phase/tests/acceptance
shape `docs/plans/plan-deliver-verification-pipeline.md` established as
this repo's `docs/plans/` convention (confirmed via `git show
HEAD:docs/plans/plan-deliver-verification-pipeline.md` — this repo's other
plans are removed from the working tree once shipped, per its own
`docs/roadmap.md` "sequencing, not a Kanban board" convention, but remain
in git history to mimic). Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

`spec-memory.md` was itself self-reviewed to "ready to hand to a planning
agent" against `prd-review`'s ten dimensions (two full review passes; every
Weak finding closed with real content). It also builds directly on
[ADR-0009](../adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md),
which already decided the Memory entry schema shape, the similarity-check
mechanism, and the opportunistic-polling design for reactions. This plan
does not re-litigate any of that PRD's or ADR's product/architecture calls —
it only sequences the work and resolves the *implementation-shape*
questions neither document specifies. Those resolutions are recorded below
as **Assumptions this plan makes**, including two genuine technical
corrections this plan's own drafting surfaced by verifying real GitHub REST
API behavior (see Assumptions 2 and 5) — not invented, checked.

This is additive work on real, running code (Phase 1's `adapter/github/posting.py`/
`adapter/github/triggers.py`/`apps/webhook_api/webhook_routes.py`, Phase 3's
`review/verification.py`), not greenfield, and the first real content in
the `memory` bounded-context package Phase 0 scaffolded but left empty.
Every phase below names the existing file/module/function it extends; every
cross-file reference was checked against the real file during drafting
(`review/step.py`, `review/output.py`, `review/verification.py`,
`review/pipeline.py`, `adapter/github/posting.py`,
`adapter/github/triggers.py`, `adapter/github/manifest.py`,
`adapter/github/schemas.py`, `apps/webhook_api/webhook_routes.py`,
`apps/webhook_api/manifest_routes.py`, `apps/webhook_api/settings.py`,
`apps/worker/main.py`, `apps/worker/settings.py`, `adapter/storage/models.py`,
`adapter/storage/context_query.py`, `adapter/storage/queue.py`,
`adapter/storage/unit_of_work.py`, `adapter/embedding/local_onnx.py`,
`findings/redact.py`, `adapter/storage/migrations/versions/0003_indexing_schema.py`).

## Scope

**In scope** — `spec-memory.md`'s Units A–G plus US-M12's kill switch, in
full, sequenced 1:1 as Phases 1–7 below: the `memory_entry`/
`posted_finding_comment` schema, `posting.py`'s provenance persistence,
webhook-driven capture of replies and thread-resolution, opportunistic
reaction polling, the scoped admin API with static-key auth,
similarity-based suppression inside `VerificationStep` (with its own
`memory_suppression_enabled` kill switch), and the GitHub App manifest
update plus composition-root wiring.

**Out of scope** — the spec's own out-of-scope list applies unchanged: org-
or team-wide similarity search (Phase 6), consumption of `accepted_fix`/
`correction` entries, real per-user identity/SSO/OIDC, the Admin UI (Phase
5), a dedicated reaction-polling scheduler, sentiment/keyword
classification of reply text, feeding a similarity match into the rubric
LLM call as context, a numeric/graded confidence score, and changes to
`findings/merge.py`'s dedupe/ceiling logic. Not repeated in full here; see
the spec's own Scope section.

## Assumptions this plan makes (flagged, not decided)

1. **`memory` becomes this phase's first real domain package content**,
   holding: pydantic domain models (`MemoryEntry`, `PostedComment`), three
   narrow Protocols (`MemorySignalRecorder`, `DismissedFindingMatcher`,
   `MemoryAdminRepository` — see Phase 1), and pure decay-math logic. Per
   `docs/adr/0006-protocol-defined-at-data-model.md`'s exemption:
   `MemorySignalRecorder` has two real consumers spanning two entrypoints
   (`adapter/github/posting.py` in the Worker, `apps/webhook_api/webhook_routes.py`
   in the Webhook API) — defined here, at the data model, rather than
   arbitrarily at one consumer, the same reasoning that already placed
   `findings.redact.TextRedactor` in `findings/` instead of one of its own
   two step-module consumers.
2. **The create-review API response does not carry per-comment ids — a
   second GitHub API call is required, and this plan verified that rather
   than assuming it.** Checked directly against GitHub's own REST API docs
   during drafting: `POST .../pulls/{pull_number}/reviews`'s response body
   is a single Pull Request Review object (`id`, `user`, `body`, `state`,
   ...) with **no `comments` array** — contradicting a naive reading of
   `spec-memory.md`'s US-M11, which doesn't specify how comment ids are
   obtained. `GET .../pulls/{pull_number}/reviews/{review_id}/comments`
   is the correct follow-up call, returning each posted comment's own
   `id`/`path`/`line`/`body`. `GitHubFindingsPublisher.post_review` (Phase
   2) issues this second call immediately after a successful review post
   and correlates each returned comment back to the `Finding` it came from
   by matching `(path, line, body)` — the same three fields the request
   itself submitted — rather than trusting response array order, since
   GitHub's docs make no ordering guarantee explicit.
3. **`issue_comment` events never correlate to a specific posted finding
   comment; only `pull_request_review_comment` with a non-null
   `in_reply_to_id` can.** `spec-memory.md`'s US-M1 says "GitHub sends
   `issue_comment` or `pull_request_review_comment`" — technically
   imprecise, since `issue_comment` fires for the PR's top-level
   conversation tab, never tied to a specific review/line comment GitHub's
   payload could correlate back to a `posted_finding_comment` row. This
   plan narrows US-M1's actual implementation to `pull_request_review_comment`
   `created` events whose `comment.in_reply_to_id` matches a captured
   `posted_finding_comment.id` — the only shape GitHub's API actually
   supports correlating. `issue_comment` is not subscribed to at all (see
   Phase 3); this doesn't reduce end-user value, since a truly top-level PR
   comment was never "a reply to a finding" in the first place — the PRD's
   own intent (capture reactions/replies/thread-resolution *on the
   platform's own posted comments*) is fully satisfied by the narrower,
   technically-correct scope.
4. **Reaction polling and PR-close capture run as a dedicated, lightweight
   job type (`JobType.MEMORY_REACTION_POLL`), not inline inside the
   `REVIEW` pipeline's own macro steps.** `spec-memory.md`'s US-M3 says
   "the platform fetches ... reactions" on a later push or PR-close, but
   doesn't specify the mechanical wiring. Folding this into `VerificationStep`
   or any other macro step would mix an unrelated I/O concern (fetching
   *this PR's own history*, not *this job's own diff*) into the review
   pipeline, and a PR-`closed` event has no diff to review at all — running
   a full `REVIEW` job for it would be wrong, not just wasteful. A third
   `JobType` reuses the existing job-queue infrastructure (`SKIP LOCKED`
   claiming, retries) the same way `INDEX` already sits alongside `REVIEW`,
   dispatched by `apps/worker/dispatch.py`'s existing job-type switch, and
   is enqueued via the exact same `_JobCandidate` fan-out
   `apps/webhook_api/webhook_routes.py` already uses to let one
   `pull_request opened`/`synchronize` delivery enqueue both `REVIEW` and
   `INDEX` from a shared transaction — extended to a third candidate,
   matched by a new `is_memory_reaction_poll_trigger` (a superset of the
   `REVIEW` trigger set, adding `closed`).
5. **Whether `job_queue.job_type`'s column needs a migration to add the new
   enum value is verified at implementation time, not assumed here.**
   `adapter/storage/models.py`'s `JobType` `Enum` column is declared with
   `native_enum=False` — SQLAlchemy renders this as a plain `VARCHAR`
   rather than a Postgres native `ENUM` type, which would need `ALTER TYPE
   ... ADD VALUE`. This plan's own reading is that no migration is needed
   (a Python-level enum addition, no `CHECK` constraint was declared on
   this column in `0001_baseline_schema.py`) — Phase 4 confirms this by
   inspecting the actual generated DDL before writing its own migration,
   rather than trusting this inference silently.
6. **`memory_suppression_enabled`/`memory_similarity_threshold`/
   `memory_decay_half_life_days` are plain `WorkerSettings` fields, not
   part of `review.config.StepEnablement`/`StepTimeouts`.** Unlike the
   seven macro steps, similarity suppression isn't a `depends_on`-resolved
   pipeline stage — it's an internal behavior toggle inside the existing
   `VerificationStep`, read directly by `apps/worker/main.py`'s
   `_build_review_registry` and passed to `VerificationStep`'s constructor,
   the same way `settings.llm_max_diff_bytes` already reaches
   `ReviewGenerationStep` without going through `StepEnablement`.
   `memory_similarity_threshold`/`memory_decay_half_life_days` are bootstrap
   config (not hardcoded constants) per `spec-memory.md`'s own "Resolved
   decisions" #1 ("tunable later without a schema change... a config
   value, not a structural one").
7. **The admin API's `admin_api_key` is a required `WebhookAPISettings`
   field with no default** — mirrors `github_webhook_secret`'s existing
   "no safe default, dangerous to silently default" pattern, not
   `setup_state_secret`'s conditionally-required pattern (US-M10's write
   capability isn't behind an opt-in flag the way the one-time manifest
   flow is). The admin router registers unconditionally (like
   `webhook_routes.py`), enforcing auth per-request via a Bearer-token
   `Depends`, rather than being registration-gated like
   `manifest_routes.py` — there's no "off by default" state for Phase 4's
   admin API the way there is for the one-time setup flow.
8. **`PostgresMemoryRepository` (one concrete class) implements all three
   Memory Protocols**, mirroring `IndexingRepository`'s own precedent
   (`_build_indexing_deps`'s docstring: "implements both `IndexRepository`
   and `IndexCommitTracker` ... so the same instance is passed for both
   roles"). One class, one `session_factory`, constructed once per
   entrypoint (Worker and Webhook API each build their own instance from
   their own session factory, the same way `JobQueueRepository`/
   `PostgresTenancyStore` already are).
9. **A new `captured_reaction` junction table (`posted_finding_comment_id`,
   `reaction_id` composite PK) backs US-M3's dedup, rather than a JSONB
   containment query against `memory_entry`.** Fast, O(1)-lookup dedup via
   a real composite primary key (the same "the natural key already
   guarantees the invariant" pattern `webhook_delivery`'s own `id` PK
   already establishes), instead of a `raw_signal_detail->>'reaction_id'`
   predicate whose cost grows with total captured-signal volume.

## Phase 1 — Memory domain models, schema, and repository (Unit A)

**Why first:** every later phase reads or writes `memory_entry`/
`posted_finding_comment`/`captured_reaction`; nothing else in this plan
compiles without the domain models and Protocols existing first.

- New `memory/models.py`: `MemoryStatus` (`Literal["pending", "approved",
  "rejected"]`), `MemoryEntryType` (`Literal["dismissed_finding",
  "accepted_fix", "correction"] | None`), `RawSignalType` (`Literal["reaction",
  "reply", "thread_resolution"]`), `MemoryEntry` (frozen pydantic model:
  `id`, `repo_id`, `org_id: str | None`, `team_id: str | None`,
  `entry_type: MemoryEntryType`, `status: MemoryStatus`,
  `raw_signal_type: RawSignalType`, `raw_signal_detail: dict[str, object]`,
  `posted_finding_comment_id: int`, `embedding: tuple[float, ...] | None`,
  `approved_at`, `approved_by: str | None`, `created_at`, `updated_at`),
  `PostedComment` (frozen: `comment_id: int`, `file: str`, `line: int
  | None`, `rule_id: str | None`, `message: str`).
- New `memory/decay.py`: `effective_similarity(raw_similarity: float, *,
  approved_at: datetime, half_life_days: float, now: datetime) -> float`
  — pure, no-I/O function implementing `spec-memory.md`'s US-M9 formula
  (`raw_cosine_similarity * 0.5 ** (days_since_approved_at / half_life)`)
  exactly. Per [ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md),
  this is Unit-tested despite living in a domain package that otherwise
  has no I/O tier of its own yet.
- New `memory/store.py`: three Protocols (Assumption 1), each 1–3 methods
  per architecture.md's Protocol-size rule:
  - `MemorySignalRecorder`: `async def record_comment_provenance(self, *,
    repo_id: str, pr_number: int, commit_sha: str, comments:
    Sequence[PostedComment]) -> None` (Phase 2's consumer) and `async def
    record_signal(self, *, posted_finding_comment_id: int, raw_signal_type:
    RawSignalType, raw_signal_detail: dict[str, object], github_comment_id:
    int | None) -> None` (Phases 3 and 4's consumer; `github_comment_id`
    backs the redelivery-safe uniqueness constraint Phase 3 relies on —
    `None` for a `reaction` signal, whose own dedup is `captured_reaction`'s
    job instead) — batched-write and single-write, the two real shapes
    this plan needs, not a speculative generic one.
  - `DismissedFindingMatcher` (1 method): `async def
    best_effective_similarity(self, *, repo_id: str, embedding:
    tuple[float, ...], half_life_days: float, now: datetime) -> float |
    None` — `None` when no approved `dismissed_finding` entries exist for
    `repo_id` (Phase 6's consumer).
  - `MemoryAdminRepository` (3 methods): `async def list_entries(self, *,
    org_id: str | None, repo_id: str | None, team_id: str | None, status:
    MemoryStatus | None, limit: int, offset: int) -> tuple[tuple[MemoryEntry,
    ...], int]` (entries, total count) — `org_id`/`repo_id`/`team_id` are
    all optional query-scope filters per `spec-memory.md`'s own "Admin key
    scope" clarification ("`org_id`/`repo_id`/`team_id` are filter
    parameters on the list endpoint only"); a plan draft of this Protocol
    dropped `org_id`/`team_id` down to `repo_id` alone, a real regression
    from what the PRD already specified, caught and fixed during this
    plan's own review pass;
    `async def set_status(self, *, entry_id: str, status:
    Literal["approved", "rejected"], entry_type: MemoryEntryType,
    embedding: tuple[float, ...] | None) -> MemoryEntry` (raises a new
    `MemoryEntryNotFoundError` for an unknown id — US-M5's 404 case;
    approve and reject share this one method, per the spec's "approve and
    reject are reversible state transitions in either direction" design —
    a single `set_status` avoids two near-identical methods diverging);
    `async def delete_entry(self, *, entry_id: str) -> None` (also raises
    `MemoryEntryNotFoundError` on a missing id, per US-M5's own 404
    requirement).
- `adapter/storage/models.py` additions, following this file's existing
  conventions exactly (nullable `org_id`/`repo_id`/`team_id` scoping per
  `OrgRepoTeamSetting`, `Vector(384)` per `IndexChunkRow`'s existing
  `_EMBEDDING_DIMENSION`, `BigInteger` PK for a GitHub-native id per
  `Repo.installation_id`'s own precedent):
  - `PostedFindingCommentRow` (`__tablename__ = "posted_finding_comment"`):
    `id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)`
    (GitHub's own comment id — no separate surrogate key, mirroring
    `WebhookDeliveryRow`'s "the natural key already guarantees the
    invariant" reasoning), `repo_id` (FK, `CASCADE`), `pr_number`,
    `commit_sha: String(64)`, `finding_file: String(4096)`, `finding_line:
    int | None`, `finding_rule_id: String(255) | None`, `finding_message:
    Text` (already redacted — see Phase 2), `created_at`.
  - `MemoryEntryRow` (`__tablename__ = "memory_entry"`): `id: UUID` PK,
    `org_id`/`repo_id`/`team_id` (all nullable FKs, `CASCADE`), `entry_type:
    String(32) | None`, `status: Enum(..., native_enum=False)` default
    `"pending"`, `raw_signal_type: String(32)`, `raw_signal_detail: JSONB`,
    `posted_finding_comment_id` (FK to `posted_finding_comment.id`,
    `CASCADE`, not null), `github_comment_id: BigInteger | None` (the
    reply/thread-event's own GitHub comment id — `None` for a `reaction`
    entry, whose own dedup already lives in `captured_reaction`; not null
    for `reply`/`thread_resolution` entries, backing the redelivery-safe
    `UniqueConstraint` Phase 3 relies on), `embedding: Vector(384) | None`,
    `approved_at: DateTime | None`, `approved_by: String(255) | None`,
    `created_at`/`updated_at`. `UniqueConstraint("posted_finding_comment_id",
    "raw_signal_type", "github_comment_id", name="uq_memory_entry_signal")`
    (Phase 3's redelivery-idempotency key — see that phase's own section).
  - `CapturedReactionRow` (`__tablename__ = "captured_reaction"`):
    `posted_finding_comment_id` (FK, `CASCADE`), `reaction_id: BigInteger`,
    composite `PrimaryKeyConstraint("posted_finding_comment_id",
    "reaction_id")` (Assumption 9 — no separate surrogate key, same
    "natural key is the invariant" pattern).
- New Alembic migration `0005_memory_schema.py` (revises `0004`), following
  `0003_indexing_schema.py`'s exact shape (explicit `sa.Column`/
  `ForeignKeyConstraint`/`UniqueConstraint` calls, `Vector` import from
  `pgvector.sqlalchemy`, a duplicated `_EMBEDDING_DIMENSION = 384` constant
  per that file's own "duplicated ... rather than importing across a
  package boundary for one int" precedent) — creates all three tables, no
  changes to any existing table.
- New `adapter/storage/memory_repository.py`: `PostgresMemoryRepository`
  (Assumption 8) implementing all three Protocols, constructed from a
  `session_factory` — reuses `adapter/storage/unit_of_work.py`'s
  `run_in_session`/`adapter/storage/indexing_repository.py`'s established
  session-open/`(SQLAlchemyError, OSError)`-translation `_run` wrapper
  shape, the same precedent `adapter/storage/context_query.py`'s
  `PostgresGraphQuery`/`PostgresSimilarityQuery` already follow.
  `best_effective_similarity` queries `memory_entry` filtered by
  `repo_id`/`status="approved"`/`entry_type="dismissed_finding"`, ordered
  by `embedding.cosine_distance(vector)` (mirroring
  `PostgresSimilarityQuery.find_similar`'s exact query shape against a new
  table instead of `index_chunk`), takes the single closest match, computes
  `1 - cosine_distance` for a raw cosine similarity, and applies
  `memory.decay.effective_similarity` before returning — no ANN index, no
  latency SLA (per `spec-memory.md`'s NFRs, matching
  `PostgresSimilarityQuery`'s own v1 posture).

**Tests:** Unit (`memory/test_decay.py`, new — `effective_similarity`
against known `(raw_similarity, days, half_life)` triples with hand-computed
expected outputs, including the exact US-M9 acceptance case). Unit
(`memory/test_models.py`, new — field defaults/shapes). Integration
(`adapter/storage/test_memory_repository.py`, new, against a real
Postgres/pgvector via `testcontainers-python` — `record_comment_provenance`/
`record_signal` persist correctly; `list_entries` filters/paginates;
`set_status` transitions correctly in both directions and raises
`MemoryEntryNotFoundError` for a missing id; `delete_entry` removes a row
and raises the same error on a repeat call; `best_effective_similarity`
returns `None` with zero approved entries and the correct decayed value
against a hand-seeded approved entry at a known age).

**Observability & rollback:** New tables and domain code only — nothing
reads or writes them yet (no composition-root wiring until later phases),
so this phase is fully reversible by reverting its migration and commit
with zero interaction with anything already shipped.

**Acceptance:** the migration applies cleanly against a fresh
Postgres/pgvector instance (`alembic upgrade head` from `0004`); every
`PostgresMemoryRepository` method is proven against a real database per the
Integration tests above.

## Phase 2 — `posting.py` provenance persistence (Unit B, US-M11)

**Why second:** every later capture phase (3, 4) needs a
`posted_finding_comment` row to attribute an inbound signal against;
nothing can be captured before a finding has actually been posted and
recorded.

- `review/output.py`'s `FindingsPublisher.post_review` Protocol gains
  `head_sha: str` (currently absent from this method's signature, unlike
  `post_check_run`/`post_failure_check_run`, which already take it) — its
  one caller, `FindingsOutputStep.run`, already has `head_sha` in scope
  (used two lines later for `post_check_run`), so this is a same-file,
  zero-new-plumbing change.
- `adapter/github/posting.py`'s `GitHubFindingsPublisher.__init__` gains a
  `provenance_recorder: MemorySignalRecorder` constructor parameter
  (Phase 1's Protocol). `post_review` — after its existing `POST
  .../reviews` call succeeds — issues one follow-up `GET
  .../reviews/{review_id}/comments` call (Assumption 2), correlates each
  returned comment back to the `Finding` it came from by matching
  `(path, line, body)` against `_review_comments`'s own submitted shape,
  and calls `provenance_recorder.record_comment_provenance(...)` once with
  every matched `PostedComment` — a single batched call, not one per
  comment, mirroring this codebase's existing "one call per meaningful
  batch, not per item" convention (`post_review` itself, `redact_findings`'s
  fan-out). A comment GitHub returns with no matching `(path, line, body)`
  triple (should not happen given `_review_comments`'s own deterministic
  construction, but not assumed impossible) is skipped and logged at
  `warning`, never raised — provenance capture is a secondary write-path
  concern, not something that should fail an already-successful review
  post. **Aggregated, not just per-occurrence, so a persistent breakage is
  actually noticeable** (a gap this plan's own review pass found: a
  per-comment `warning` alone would bury a systemic issue, e.g. a GitHub
  response-shape change breaking every correlation, inside routine-looking
  log noise): `post_review` also logs one summary line per call at
  `info`/`warning` (`warning` when `unmatched_count > 0`) with
  `matched_count`/`unmatched_count`, the same "per-item detail plus one
  aggregate summary line" shape `review/verification.py`'s own
  `criterion_failures` logging already establishes — an operator or alert
  rule watching for a sustained non-zero `unmatched_count` rate catches
  systemic correlation breakage without needing to grep individual
  warnings. File-level findings (no `line`) are never eligible for provenance
  (GitHub's line-comment API has nothing to correlate them against) — only
  `_review_comments`'s own line-attributed findings are ever recorded.
  `finding.message` is already the post-redaction text
  (`review/review_generation.py`'s existing `_redact_all` pass ran before
  this step ever sees it) — `finding_message` persisted here carries no new
  redaction gap.
- `apps/worker/main.py`'s `_build_findings_publisher` gains a
  `provenance_recorder` parameter, threaded from a new
  `PostgresMemoryRepository` instance built alongside `graph_query`/
  `similarity_query` in `_amain` (same `session_factory`, no second
  connection pool).

**Tests:** Integration (`adapter/github/test_posting.py`, extended — a
successful `post_review` call issues the follow-up comments-GET and calls
`record_comment_provenance` with correctly correlated `PostedComment`
entries; a comment that doesn't correlate is skipped, logged, and counted
in the new aggregate summary line; a zero-line-findings review never calls
the provenance recorder at all; the new GET call uses a recorded
`respx`-mocked cassette, matching this file's existing convention for
every other GitHub call it tests). No change to this module's existing
retry/idempotency tests — the new GET call is a plain `request_with_retry`
call like every other read this module could add, not a write needing
`safe_writes_only`. **`apps/worker/test_main.py`'s `_NoOpPublisher` test
fake also needs its `post_review` signature updated to accept `head_sha`**
(a real second implementation of `FindingsPublisher` this plan's own
review pass found — not just `GitHubFindingsPublisher` — three existing
composition-root tests construct/use it and would otherwise break with a
`TypeError` the moment `FindingsOutputStep` starts calling `post_review`
with the new keyword argument).

**Observability & rollback:** Additive-only change to an already-shipped,
already-tested module — no existing behavior changes (the new GET call and
provenance write happen strictly after the existing review post already
succeeded). Fully reversible by reverting this phase's commit; no data
migration needed to roll back (the new table simply stops being written
to).

**Acceptance:** US-M11's `Given/When/Then` passes as an integration test.

## Phase 3 — Webhook-driven capture: replies and thread-resolution (Unit C, US-M1/US-M2)

**Why third:** depends on Phase 1 (schema) and Phase 2
(`posted_finding_comment` rows to correlate against); independent of
Phases 4–7.

- `adapter/github/schemas.py` gains two new payload models:
  `PullRequestReviewCommentWebhookEnvelope` (action, `comment.id`,
  `comment.in_reply_to_id: int | None`, `comment.body`, `repository`,
  `pull_request.number`) and `PullRequestReviewThreadWebhookEnvelope`
  (action `resolved`/`unresolved`, `thread.comments: list[{id: int}]`,
  `repository`, `pull_request.number`) — both validated with the same
  optionality conventions `GitHubWebhookEnvelope` already establishes.
- `adapter/github/triggers.py` gains `is_memory_capture_trigger(event_type:
  str, action: str | None) -> bool` — true for
  `("pull_request_review_comment", "created")` and
  `("pull_request_review_thread", "resolved")`/`("pull_request_review_thread",
  "unresolved")` — a third pure, no-I/O trigger table alongside
  `is_enqueue_trigger`/`is_index_trigger`, tested at Unit tier per
  ADR-0005 like its two siblings.
- `apps/webhook_api/webhook_routes.py`'s `receive_github_webhook` handler:
  after building `event`, when `is_memory_capture_trigger` matches, parses
  the event-type-specific envelope and looks up the correlated
  `posted_finding_comment` row —
  `comment.in_reply_to_id` for a reply (Assumption 3; a `created` comment
  with `in_reply_to_id=None` is a fresh top-level review comment, not a
  reply, and is never captured), or any id in `thread.comments` for a
  thread-resolution event. No match (the comment isn't one this platform
  posted, or isn't a reply to one) is accepted (200) and logged at `info`
  with reason `"not_a_platform_comment"`, the same "still accepted, not
  enqueuing" posture this route already applies to every other
  non-matching outcome. A match calls
  `provenance_recorder.record_signal(...)` (Phase 1's `MemorySignalRecorder`,
  reused — no separate Memory-specific recorder instance) with
  `raw_signal_type="reply"` (redacted reply body — see below) or
  `"thread_resolution"` (`{"resolved": bool}`) — this call happens
  **outside** the existing `_handle_matched_triggers`/job-queue
  transaction (it writes directly to `memory_entry`, not `job_queue`; no
  job is ever enqueued for this trigger kind), logged at `info` on success.
- Reply body redaction: `apps/webhook_api/main.py`'s composition root gains
  a `TextRedactor` dependency (currently only `apps/worker/main.py` builds
  one) — reused, not reinvented: the same `GitleaksTextRedactor`/
  `NoOpTextRedactor` wiring choice `_build_text_redactor` already makes,
  gated by a new `WebhookAPISettings.secret_redaction_enabled: bool = True`
  field mirroring `WorkerSettings`' own field name/default exactly. A
  reply's raw body is redacted before being persisted as
  `raw_signal_detail`, per `spec-memory.md`'s own "Reply-text redaction
  before storage" scope bullet.
- **Redelivery-safe, not just first-delivery-safe.** GitHub's own
  documented at-least-once webhook delivery semantics (already the reason
  `webhook_delivery` exists for the `REVIEW`/`INDEX` trigger path) apply
  here too, but this route's own capture write sits outside
  `_handle_matched_triggers`' existing delivery-idempotency guard (this
  phase's own design — the guard is scoped to job-queue enqueuing, and no
  job is ever enqueued for a capture trigger). `record_signal` is therefore
  itself idempotent on `(posted_finding_comment_id, raw_signal_type,
  github_comment_id)` — a `UniqueConstraint` on `memory_entry` covering
  those three columns (`github_comment_id` a new not-null column: the
  reply/thread-event's own comment id, not `posted_finding_comment_id`,
  which only identifies what's being replied *to*) — `record_signal`
  upserts via `ON CONFLICT DO NOTHING`, the same pattern
  `adapter/storage/tenancy.py`'s `record_delivery_if_new` already
  establishes for exactly this class of problem, closing a real gap an
  earlier draft of this plan left open (a GitHub redelivery of the same
  reply/resolution event would otherwise create a duplicate `pending`
  entry every retry).
- **A per-repo capture ceiling bounds unauthenticated write volume.** Any
  GitHub account able to comment/react on a public repo's PR (not a
  privileged permission) can generate valid, signed webhook deliveries that
  each attempt to create a `pending` `memory_entry` row — a real gap this
  plan's own review pass found, unlike Phase 4's reaction-poll cost, which
  was already bounded. `WorkerSettings.memory_capture_ceiling_per_repo_per_day`
  (default `500`, reusing the existing per-repo `InMemoryRateLimiter`
  class `apps/webhook_api/rate_limiter.py` already provides, the same
  concrete reuse `review_enqueue_rate_limit_per_minute` already
  establishes for a materially identical shape of problem) caps captures
  per repo per rolling day; a capture beyond the ceiling is accepted (200,
  matching this route's existing "never reject a well-formed signed
  webhook" posture) but not persisted, logged at `warning` with
  `reason="capture_ceiling_exceeded"`.

**Tests:** Unit (`adapter/github/test_triggers.py`, extended —
`is_memory_capture_trigger`'s table). Integration
(`apps/webhook_api/test_webhook_routes.py`, extended — a
`pull_request_review_comment created` event with a matching
`in_reply_to_id` creates a `pending` entry with redacted content; the same
event with `in_reply_to_id=None` is accepted but captures nothing; a
`pull_request_review_thread resolved`/`unresolved` event with a matching
thread comment id creates a `pending` entry; a non-matching comment/thread
is accepted and logged, nothing captured).

**Observability & rollback:** New webhook event handling only — no change
to the existing `REVIEW`/`INDEX` trigger paths or their transaction
boundary. Fully reversible by reverting this phase's commit; the GitHub App
manifest change actually subscribing to these events is deferred to Phase
7 (this phase's own handler code is safe to deploy ahead of that
subscription, the same "safe to deploy ahead of the kill switch" pattern
`0003_indexing_schema.py`'s own docstring already establishes for schema
migrations landing before the code that activates them).

**Acceptance:** US-M1 and US-M2's `Given/When/Then` pass as integration
tests.

## Phase 4 — Opportunistic reaction polling (Unit D, US-M3)

**Why fourth:** depends on Phase 1 (schema, `captured_reaction` dedup) and
Phase 2 (`posted_finding_comment` rows); independent of Phase 3.

- Verify Assumption 5 (whether `job_queue.job_type` needs a migration) by
  inspecting the actual `0001_baseline_schema.py`-generated DDL before
  writing this phase's own migration; add `0006_job_type_reaction_poll.py`
  only if that inspection finds a `CHECK` constraint this new value would
  violate.
- `adapter/storage/models.py`'s `JobType` gains `MEMORY_REACTION_POLL =
  "memory_reaction_poll"`.
- `adapter/github/triggers.py` gains `is_memory_reaction_poll_trigger(event_type:
  str, action: str | None) -> bool` (Assumption 4) — true for
  `pull_request` actions in `{"opened", "synchronize", "reopened",
  "closed"}` (a superset of `_ENQUEUE_REVIEW_TRIGGERS`, adding `"closed"`).
- `apps/webhook_api/webhook_routes.py` gains a fourth `_JobCandidate`
  builder, `_memory_reaction_poll_payload(event, result) -> dict | None`
  (needs only `pr_number`; `None` when absent — mirrors
  `_review_enqueue_payload`'s own shape), added to `candidates` alongside
  the existing `REVIEW`/`INDEX` candidates whenever
  `is_memory_reaction_poll_trigger` matches — reusing
  `_handle_matched_triggers`'s existing fan-out unchanged (Assumption 4);
  no new rate limiter (this job type does no LLM/expensive work, so it
  doesn't need one of its own — `rate_limiter=None` on its `_JobCandidate`,
  the same shape the "indexing disabled" candidate already uses).
- **Bounded cost, not just "bounded per-job" (a real gap this plan's own
  review pass found: an earlier draft's "bounded per-PR" claim didn't
  account for cost growing with PR age/comment count across repeated
  polls).** `run_memory_reaction_poll_job` fetches reactions only for the
  `MEMORY_REACTION_POLL_MAX_COMMENTS_PER_POLL` (a new
  `WorkerSettings` field, default `50`) most-recently-posted
  `posted_finding_comment` rows for that PR, ordered by `created_at`
  descending — not every comment ever posted on a long-lived PR. This
  bounds worst-case GET-call volume per poll independent of how many pushes
  or findings a PR has accumulated, the same "a concrete ceiling, not
  unbounded fan-out" posture `findings/redact.py`'s
  `apply_redaction_ceiling`/`review/merge.py`'s `POSTED_FINDINGS_CEILING`
  already establish — a PR with more comments than the ceiling simply never
  has its oldest comments' reactions polled again, an accepted, logged
  (`info`, comment count vs. ceiling) gap rather than an unbounded cost
  curve against the installation's shared GitHub API rate limit.
- New `memory/reaction_poll.py`: `run_memory_reaction_poll_job(*, repo_id:
  str, pr_number: int, installation_id: int, repo_full_name: str,
  reaction_fetcher: ReactionFetcher, signal_recorder: MemorySignalRecorder,
  dedup_store: ReactionDedupStore, max_comments_per_poll: int) -> None` —
  a plain async function, not a
  `review.step.Step` (this job type never enters `StepRegistry`; it has no
  diff, no findings, nothing that framework's dependency-chain machinery
  is for). New Protocols in `memory/store.py`: `ReactionFetcher` (1
  method: `async def fetch_reactions(self, *, installation_id: int,
  repo_full_name: str, comment_ids: Sequence[int]) -> dict[int,
  tuple[ReactionRecord, ...]]`) and `ReactionDedupStore` (1 method: `async
  def filter_new(self, *, posted_finding_comment_id: int, reaction_ids:
  Sequence[int]) -> tuple[int, ...]` — returns only the ids not already in
  `captured_reaction`, and records the ones it returns as now-captured in
  the same call, closing the check-then-record race a separate
  check/record pair would leave open under concurrent polls of the same
  PR).
- New `adapter/github/reactions.py`: `GitHubReactionFetcher` implementing
  `ReactionFetcher` — one `GET
  .../pulls/comments/{comment_id}/reactions` call per comment id (GitHub's
  API has no batched multi-comment reactions endpoint), fanned out
  concurrently through the exact same helpers every other GitHub-calling
  module in this codebase uses together (`adapter/github/http.py`'s
  `request_with_retry`/`auth_headers`/`client_context`, per `diff.py`'s and
  `posting.py`'s own established pattern — named explicitly here, not left
  implicit, after this plan's own review pass found the reuse claim stated
  only in the Security review section, not in this build bullet). Bounded
  by the same concurrency-limit constant `findings/redact.py`'s
  `_MAX_CONCURRENT_REDACTIONS`/`adapter/github/diff.py`'s own
  fetch-concurrency bound already use (`10`) — this plan extracts that
  duplicated magic number into one shared, importable constant
  (`adapter/github/http.py`'s new `DEFAULT_FAN_OUT_CONCURRENCY = 10`) that
  all three call sites import, rather than adding a third independent
  hardcoded `10` and perpetuating the existing duplication. **One comment's
  fetch failing never aborts the batch**: each per-comment fetch is
  wrapped individually (`asyncio.gather(..., return_exceptions=True)`,
  mirroring `redact_findings`'s own per-item fan-out shape but tolerant
  here rather than fail-closed, since a missed reaction is a lost secondary
  signal, not a security-relevant redaction gap) — a failed comment's
  reactions are simply not captured this poll, logged individually at
  `warning` (`comment_id`, `error`), and the job still completes and marks
  `DONE` for every comment that did succeed, reuses the existing
  `GitHubInstallationTokenProvider`/shared `httpx.AsyncClient` this
  composition root already builds, no new auth code.
  `PostgresMemoryRepository` (Phase 1) also implements `ReactionDedupStore`
  (a fourth Protocol on the same class, per Assumption 8's precedent).
- `apps/worker/dispatch.py`'s job-type dispatch switch (already
  distinguishing `INDEX`/`REVIEW` per its own docstring) gains a third
  branch calling `run_memory_reaction_poll_job` for
  `JobType.MEMORY_REACTION_POLL`, using the same
  `IndexingJobDependencies`-adjacent bundle pattern this composition root
  already builds for `INDEX` (a small new
  `MemoryReactionPollJobDependencies` bundle, mirroring that precedent
  rather than passing five loose positional arguments).

**Tests:** Unit (`adapter/github/test_triggers.py`, extended —
`is_memory_reaction_poll_trigger`'s table, including the "closed" case
`is_enqueue_trigger`/`is_index_trigger` never match). Integration
(`memory/test_reaction_poll.py`, new — `run_memory_reaction_poll_job`
against a fake `ReactionFetcher`/real `PostgresMemoryRepository`: a new
reaction creates a pending entry; a previously-captured reaction is not
recaptured on a repeat call). Integration
(`adapter/github/test_reactions.py`, new, recorded GitHub REST cassette —
`GitHubReactionFetcher` against a synthetic multi-comment response).
Integration (`apps/webhook_api/test_webhook_routes.py`, extended — a
`pull_request closed` event enqueues a `MEMORY_REACTION_POLL` job and no
`REVIEW`/`INDEX` job; a `pull_request synchronize` event enqueues all
three job types from one shared transaction).

**Observability & rollback:** A new job type is inert until this phase's
own dispatch-loop wiring ships — safe to deploy the migration/trigger
changes ahead of the dispatch handler, the same staged-deploy safety
`0003_indexing_schema.py`'s own precedent establishes. Fully reversible: no
persisted data needs cleanup on revert (captured `memory_entry`/
`captured_reaction` rows are additive and harmless to leave in place).

**Acceptance:** US-M3's both `Given/When/Then` branches (new-push trigger,
PR-closed trigger) pass as integration tests.

## Phase 5 — Admin API: list/approve/reject/delete (Unit E, US-M4/US-M5/US-M8/US-M10)

**Why fifth:** depends only on Phase 1's schema/repository; independent of
Phases 2–4, sequenced here (not earlier) purely because it's the largest
remaining independent unit and Phase 6 (similarity suppression) needs a
real `approve` path to exist before its own end-to-end contract test can
exercise it without hand-seeding.

- `apps/webhook_api/settings.py`'s `WebhookAPISettings` gains
  `admin_api_key: SecretStr` (Assumption 7 — required, no default).
- New `apps/webhook_api/admin_routes.py`, following
  `manifest_routes.py`'s exact factory-router shape
  (`create_admin_router(*, admin_api_key: SecretStr, repository:
  MemoryAdminRepository) -> APIRouter`):
  - `GET /admin/memory-entries` — query params `org_id: str | None`,
    `repo_id: str | None`, `team_id: str | None`, `status: MemoryStatus |
    None`, `limit: int = 50` (capped at `200`), `offset: int = 0`; returns
    `{"entries": [...], "total": int}` (US-M8). All three scope params are
    optional filters, never an access-control boundary the key itself
    enforces (Assumption 7/Security review) — the same one static key can
    already read/write every org/repo/team regardless of what's passed
    here, per `spec-memory.md`'s own decided design.
  - `POST /admin/memory-entries/{entry_id}/approve` — body `{"entry_type":
    MemoryEntryType}`; `422` if `entry_type` missing/invalid (US-M4); on a
    `dismissed_finding` approval, embeds the linked finding's fingerprint
    (`file`+`rule_id`+`message`, read via the entry's own
    `posted_finding_comment_id` FK) through the same
    `LocalONNXEmbeddingClient` instance the composition root already builds
    (Phase 2 of the indexing delivery plan) — wrapped in
    `asyncio.to_thread` (`embed` is a synchronous, CPU-bound call, per
    `adapter/embedding/local_onnx.py`'s own signature; this route handler
    is the first `embed` call site outside the Worker's own job pipeline,
    so this wrapping is new here, not reused code) — then calls
    `repository.set_status(..., embedding=computed)`. `404` for a
    nonexistent `entry_id` (`MemoryEntryNotFoundError` → `HTTPException(404)`).
  - `POST /admin/memory-entries/{entry_id}/reject` — idempotent (US-M5);
    same `404` handling.
  - `DELETE /admin/memory-entries/{entry_id}?confirm=true` — `400` without
    `confirm=true`; `404` for a nonexistent id.
  - Every route depends on a shared `_require_admin_key` FastAPI
    dependency (`Authorization: Bearer <key>`,
    `hmac.compare_digest`-checked against `admin_api_key`, mirroring
    `adapter/github/manifest.py`'s own constant-time-comparison precedent
    for a security-sensitive string check) — `401` on missing/incorrect
    key (US-M10).
  - Every approve/reject/delete call is logged at `info` (entry id, action,
    timestamp — the audit trail `spec-memory.md`'s Observability section
    names).
- `apps/webhook_api/main.py`'s `create_app` registers this router
  unconditionally (Assumption 7), constructing one
  `PostgresMemoryRepository` from the same `session_factory` its existing
  `PostgresTenancyStore`/`JobQueueRepository` already share, plus the
  `LocalONNXEmbeddingClient` (this entrypoint's first use of it — the
  indexing delivery plan built it only into the Worker; this phase adds it
  to Webhook API's own composition root, with the equivalent startup
  fail-fast check `apps/worker/main.py`'s `_check_embedding_model_startup`
  already establishes, reused via the same
  `adapter.embedding.startup_check.check_embedding_model_loaded` function
  rather than a second copy).

**Tests:** Integration (`apps/webhook_api/test_admin_routes.py`, new,
against a real Postgres — every US-M4/US-M5/US-M8/US-M10 `Given/When/Then`:
approve requires/validates `entry_type` and computes a real embedding only
for `dismissed_finding`; reject is idempotent and reversible in either
direction with approve; delete requires `confirm=true` and 404s on a
repeat call; list filters/paginates correctly; every route 401s without a
valid key).

**Observability & rollback:** A wholly new, additive API surface — no
existing route or behavior changes. Fully reversible by reverting this
phase's commit; no data cleanup needed (approved/rejected rows are
harmless to leave in place).

**Acceptance:** US-M4, US-M5, US-M8, and US-M10's `Given/When/Then` all
pass as integration tests against a real Postgres via
`testcontainers-python`.

## Phase 6 — Similarity-based suppression inside `VerificationStep` (Unit F, US-M6/US-M7/US-M9/US-M12)

**Why sixth:** depends on Phase 1 (schema/repository) and, functionally
(not just structurally — see `spec-memory.md`'s own Phases/Units section),
Phase 5: nothing reaches a non-empty Memory store for this phase to read
until an entry has actually been approved through the real admin API.
Sequenced last among the storage-touching phases so its own acceptance
test can exercise the real Phase 5 → Phase 6 contract, not only hand-seeded
fixtures.

- `apps/worker/settings.py`'s `WorkerSettings` gains three fields
  (Assumption 6): `memory_suppression_enabled: bool = True`,
  `memory_similarity_threshold: float = 0.90`,
  `memory_decay_half_life_days: float = 180.0`.
- `review/verification.py`'s `VerificationStep.__init__` gains one new
  parameter, `memory_suppression: MemorySuppressionConfig | None` — a
  small frozen dataclass (`memory_matcher: DismissedFindingMatcher`,
  `embedding_client: EmbeddingClient`, `similarity_threshold: float`,
  `decay_half_life_days: float`, `enabled: bool`) grouping the four
  mutually-dependent memory-suppression fields into one parameter rather
  than five independent ones (a plan-review finding: `threshold`/
  `half_life` are meaningless without `matcher`+`embedding_client` present
  together, so bundling them removes the invalid states five independent
  optional parameters would otherwise allow — e.g. a threshold set with no
  matcher). `None` (the default) means suppression is off entirely — every
  existing unit/integration test constructing a bare `VerificationStep`
  (per `testing.md`'s "new domain logic a story touches still needs unit
  tests" convention already exercised throughout
  `review/test_verification.py`) keeps working unchanged, matching every
  other "absent upstream data degrades gracefully" precedent this codebase
  already establishes for `context_bundle`/`intent_summary`. Because the
  four fields are grouped, there is no construction-time state where
  `embedding_client` is missing while `matcher`/`threshold` are present —
  the invalid combination a plan-review pass flagged against the
  five-independent-parameters design is structurally unrepresentable here,
  not merely validated after the fact.
- `VerificationStep.run`: when `suppression_enabled` and `memory_matcher`
  is set, **before** building the rubric batch, embeds each candidate
  `source="llm"` finding's fingerprint (`file`+`rule_id`+`message`) via
  `embedding_client.embed` (`asyncio.to_thread`-wrapped, the first
  `VerificationStep`-side embed call — this step has never called the
  embedding client before), calls
  `memory_matcher.best_effective_similarity(repo_id=..., embedding=...,
  half_life_days=self._decay_half_life_days, now=...)` per finding, and
  removes any finding whose returned value exceeds
  `similarity_threshold` from the candidate batch entirely — routed
  through the exact same rejection accounting `_passes_rubric`-failed
  findings already use (`rejected_count`/logged aggregate), under a new,
  distinct `criterion_failures`-adjacent counter (`memory_suppressed_count`)
  so the existing per-criterion log line can distinguish "the rubric
  rejected this" from "Memory suppressed this before the rubric ever saw
  it" — a finding suppressed this way never enters the LLM Gateway call at
  all (US-M6's own "no LLM call spent on it" requirement). `repo_id` comes
  from `_VerificationInput`'s existing `JobIdentity.repo_id` field, already
  present on every step's input — no new field needed. `repo_id` is
  included on this log line (not just `job_id`), so an operator's own log
  platform can aggregate `memory_suppressed_count` per repo over time —
  this is `spec-memory.md`'s own already-decided Observability posture
  ("no new metrics/alerting pipeline — logging-only... detection is
  aggregated over time in whatever log platform the customer's own
  deployment already uses"), not a gap this plan could close by adding a
  metrics pipeline this codebase has nowhere else.
- Composition-root wiring (`apps/worker/main.py`'s `_build_review_registry`):
  `VerificationStep(...)` gains one new `memory_suppression=MemorySuppressionConfig(...)`
  keyword argument when `settings.memory_suppression_enabled` is `True`
  (else `None`), sourced from a `PostgresMemoryRepository` instance
  (implementing `DismissedFindingMatcher`, Assumption 8 — the same instance
  Phase 2's `provenance_recorder` already is, no second one) and the
  already-built `embedding_client`/`WorkerSettings` fields above. A new
  `_log_memory_suppression_disabled_warning` mirrors
  `_log_verification_disabled_warning`'s exact shape: a one-time `warning`
  at startup when `memory_suppression_enabled=False`.

**Tests:** Integration (`review/test_verification_step.py`, extended —
US-M6: a candidate finding whose embedding matches an approved,
hand-seeded `dismissed_finding` entry above threshold is excluded before
the rubric call and never appears in the LLM Gateway request payload; zero
approved entries skips the comparison entirely. US-M7: a non-matching
finding proceeds through the rubric unchanged. US-M12: `suppression_enabled=False`
restores byte-for-byte prior behavior even with a matching entry present).
Integration, not Unit — `adapter/storage/test_memory_repository.py`
(extended from Phase 1, against a real Postgres/pgvector via
`testcontainers-python`, matching every other test already in that file):
US-M9's exact decay-weighted comparison, hand-computed against a real
`best_effective_similarity` query result. `memory/decay.py`'s own pure
math is already Unit-tested in `memory/test_decay.py` (Phase 1); this
file's job is proving the *query* applies that math correctly against
real data, which is Integration per `testing.md`'s "no real I/O, adapters,
network, or database" Unit definition — mislabeling this as Unit would
mean it either can't run in the no-DB every-push Unit tier `testing.md`
gates on, or silently misrepresents what CI tier actually covers it. New
integration
test, **not hand-seeded** — the real Unit E → Unit F contract: approve a
pending entry via the real admin API (Phase 5's actual HTTP route, not a
direct repository call), confirm a real embedding was written, then run
`VerificationStep` against that same real row and confirm the match fires
— proving Phase 5 must functionally precede Phase 6 in production, not
merely in this plan's own phase numbering.

**Observability & rollback:** `memory_suppression_enabled=false` is this
phase's own operational rollback lever — restores exactly Phase 3
(verification pipeline)'s prior `VerificationStep` behavior with zero
interaction with capture (Phases 2–4) or the admin API (Phase 5), which
keep running regardless. Toggling requires a Worker restart (bootstrap
config), identical to every other per-step flag.

**Acceptance:** US-M6, US-M7, US-M9, and US-M12's `Given/When/Then` all
pass as integration tests; the Unit E → Unit F contract test (above) passes
without any hand-seeded fixture.

## Phase 7 — GitHub App manifest update (Unit G)

**Why last:** activates the webhook subscriptions Phases 3 and 4's own
handler code has been safe to run without since it shipped (per those
phases' own "safe to deploy ahead of" notes) — sequenced last so every
consuming code path already exists and is tested before the events that
trigger it start actually arriving from GitHub.

- `apps/webhook_api/manifest_routes.py`'s `_build_app_manifest`'s
  `"default_events"` list gains `"pull_request_review_comment"` and
  `"pull_request_review_thread"` (Phases 3/4's new webhook subscriptions) —
  `"issue_comment"` is deliberately **not** added, per Assumption 3
  (`issue_comment` events can never correlate to a specific posted
  finding, so subscribing to them would only add unused webhook volume).
  `"pull_request"`/`"push"` are already subscribed (Phase 4 of the
  walking-skeleton plan) — Phase 4's `MEMORY_REACTION_POLL` trigger needs
  no new event subscription, only a new action (`"closed"`) within the
  already-subscribed `pull_request` event.
- **Existing installations require re-consent** — an already-installed
  GitHub App instance won't receive the two new event types until an
  operator re-approves the App's updated permissions on GitHub's own UI.
  Documented in `docs/reference/operations.md` as this phase's own upgrade
  note (a new subsection, following that doc's existing runbook-entry
  shape), per `spec-memory.md`'s own Rollout & Release Safety section.

**Tests:** Unit (`adapter/github/test_manifest.py`, extended — the
manifest's `default_events` list contains exactly the expected set,
including the explicit absence of `"issue_comment"`).

**Observability & rollback:** Purely additive to a static manifest
dictionary — no runtime behavior change of its own; the actual new webhook
traffic only starts once an operator re-consents, which is itself a
reversible action from GitHub's own App-settings UI (an operator can
decline the updated permissions and keep running Phase 1–6's code
unaffected, simply without replies/thread-resolution/reaction capture).

**Acceptance:** `adapter/github/test_manifest.py`'s extended assertion
passes.

## Security review

One pass across every new input/trust boundary this plan as a whole
introduces:

- **A new, write-capable HTTP surface (the admin API, Phase 5) protected
  by a single static, coarse-grained credential — the blast radius is
  larger than "no audit identity," and is stated plainly here rather than
  undersold** (a real gap this plan's own review pass found in an earlier
  draft's framing): this is a *write* surface, not read-only visibility —
  a leaked or over-shared `admin_api_key` lets its holder silently
  **approve or delete** another internal org/team's `dismissed_finding`
  entries, materially changing that org's future review outcomes (which
  findings get suppressed) with no consent and no notification, exactly
  the "not a tenant boundary" collapsing into "no boundary at all" ADR-0004
  itself warns against — now with write/delete power behind it, not mere
  read access. `admin_api_key` is compared via `hmac.compare_digest`
  (never `==`), never logged (`SecretStr`), and delivered as a Kubernetes
  `Secret` — the same credential-handling pattern every other secret in
  this codebase already follows, which mitigates casual leakage but not
  the scope-collapse risk above. **Named, accepted residual, not fixed
  here** (per `spec-memory.md`'s own Resolved decisions #3, confirmed with
  the user during PRD drafting, not re-litigated by this plan): every
  deployment running this phase's code has exactly one admin credential
  for its entire Memory surface until Phase 5's admin UI brings real
  per-user SSO/OIDC — an enterprise customer with genuinely
  security-sensitive internal-org separation should weigh that
  consciously before enabling capture/admin routes, not discover it
  later.
- **Reply text is new, redacted, admin-deletable data at rest.**
  Redacted via the same `TextRedactor` (`Gitleaks`-backed by default)
  every static-analysis/LLM finding already passes through before this
  platform stores or logs it — no new redaction mechanism, no new secret
  class introduced. `apps/webhook_api/main.py`'s new `TextRedactor`
  dependency (Phase 3) is a second wiring site for an already-shipped,
  already-hardened concrete class (`GitleaksTextRedactor`), not new
  redaction logic.
- **A new outbound GitHub REST read path** (`GET .../reviews/{id}/comments`,
  Phase 2; `GET .../comments/{id}/reactions`, Phase 4) — both reuse the
  existing `GitHubInstallationTokenProvider`/shared `httpx.AsyncClient`/
  `request_with_retry` machinery unchanged; no new credential type, no new
  write capability, no new trust boundary beyond what `adapter/github/diff.py`'s
  existing read calls already cross.
- **Correlating an inbound webhook signal to a `posted_finding_comment`
  row (Phase 3) is itself a trust boundary worth naming explicitly:** a
  reply/thread-resolution event is only ever captured when its
  `comment.in_reply_to_id`/`thread.comments` ids match a row this platform
  itself inserted at post time — an attacker-controlled webhook payload
  (already excluded by this route's existing HMAC signature verification,
  unchanged by this plan) cannot forge a match against an id it doesn't
  already know, and a non-matching id is inert (accepted, nothing
  captured), never an error path an attacker could use to probe for valid
  ids.
- **Resource exhaustion — reaction polling (Phase 4)**: bounded per-job by
  the number of `posted_finding_comment` rows for one PR (never all rows
  platform-wide), fanned out through the same concurrency-bounded
  `Semaphore` pattern `redact_findings`/`diff.py`'s `fetch_content` already
  establish — no unbounded fan-out this plan introduces newly.
  `MEMORY_REACTION_POLL` jobs share the same `job_queue` claiming/retry
  infrastructure as `REVIEW`/`INDEX`, inheriting whatever operational
  ceilings already apply there (no new ceiling needed at this layer).
- **The similarity-suppression pre-check (Phase 6) is a silent-by-design
  filter, not a new attacker-facing surface** — it only ever *removes* a
  finding an attacker-controlled diff might otherwise have triggered, and
  its own input (a candidate finding's `file`/`rule_id`/`message`) is
  already fully attacker-influenced content `review_generation`'s own
  existing prompt/parse path already handles; suppression adds no new
  parsing of untrusted content GitHub's own webhook/diff surfaces didn't
  already expose. The one genuine new risk this introduces — a team's own
  approved entry accidentally suppressing a real, valuable finding — is a
  product-quality risk `memory_suppression_enabled` (Phase 6's kill switch)
  and the admin API's reject/delete actions (Phase 5) both exist precisely
  to bound, not a security defect.
- **Admin-key embedding compute (Phase 5's approve route) is the first
  place outside the Worker's own job pipeline that runs model inference —
  named, not a new risk class.** Bounded to one call per approve action (an
  authenticated, rate-limited-by-human-action-cadence operation, not
  attacker-triggerable volume) — no new resource-exhaustion vector beyond
  what an authenticated admin's own action already implies.

## Compatibility & rollout

- **No existing API/schema contract breaks.** Every new table is additive
  (Phase 1); `FindingsPublisher.post_review`'s signature change (Phase 2,
  adding `head_sha`) has exactly one implementation and one call site, both
  updated in the same commit — an in-process Protocol, never a persisted or
  external contract, so no migration/versioning concern. `VerificationStep`'s
  new constructor parameters (Phase 6) are all optional/defaulted — every
  existing test construction site is unaffected.
- **Staged rollout via two independent kill switches**: `memory_suppression_enabled`
  (Phase 6) gates only the read/suppression side; capture (Phases 2–4) and
  the admin API (Phase 5) are not gated behind any flag of their own — per
  `spec-memory.md`'s own "capture and admin API keep running regardless"
  requirement (US-M12), disabling suppression is the one lever that matters
  for containing a bad-threshold incident, and gating capture separately
  would only lose data an admin might still want to review later.
- **Migration ordering is safe to deploy ahead of the code that activates
  it**, matching `0003_indexing_schema.py`'s own established precedent:
  Phase 1's schema, Phase 3/4's trigger/handler code, and Phase 7's
  manifest update can each land in separate deploys without any of them
  being individually broken — the manifest update (Phase 7) is what
  actually starts new webhook traffic flowing, so it's safe to be the last
  link in the chain.
- **Existing GitHub App installations require an explicit operator action**
  (re-consenting to the two new webhook event subscriptions, Phase 7) — not
  automatic, and not required for Phases 1–6 to function: a deployment that
  never re-consents simply never captures replies/thread-resolution
  (reactions still partially work via Phase 4's polling, since that reuses
  the already-subscribed `pull_request` event), with zero impact on the
  existing review pipeline.
- **The change is fully undoable** via `memory_suppression_enabled=false`
  for the read side, or a plain code revert for the write side — no data
  cleanup ever required (captured/approved rows are harmless to leave in
  place; nothing here mutates or migrates existing `finding`/`job_queue`
  data).

## Traceability: PRD user story → test

| Story | Phase | Test path | Level |
| --- | --- | --- | --- |
| — (schema/decay math) | 1 | `memory/test_decay.py`, `memory/test_models.py` | Unit |
| — (repository CRUD/similarity query) | 1 | `adapter/storage/test_memory_repository.py` | Integration |
| US-M11 | 2 | `adapter/github/test_posting.py` | Integration |
| US-M1 | 3 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| US-M2 | 3 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| — (memory capture trigger table) | 3 | `adapter/github/test_triggers.py` | Unit |
| US-M3 | 4 | `memory/test_reaction_poll.py`, `adapter/github/test_reactions.py`, `apps/webhook_api/test_webhook_routes.py` | Integration |
| — (reaction-poll trigger table) | 4 | `adapter/github/test_triggers.py` | Unit |
| US-M4 | 5 | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M5 | 5 | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M8 | 5 | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M10 | 5 | `apps/webhook_api/test_admin_routes.py` | Integration |
| US-M6 | 6 | `review/test_verification_step.py` | Integration |
| US-M7 | 6 | `review/test_verification_step.py` | Integration |
| US-M9 | 1, 6 | `memory/test_decay.py` (pure math, Unit) + `adapter/storage/test_memory_repository.py` (query-level weighting against real Postgres, Integration) | Unit + Integration |
| US-M12 | 6 | `review/test_verification_step.py` | Integration |
| Unit E → Unit F contract | 6 | `review/test_verification_step.py` (extended, real admin route, no hand-seeding) | Integration |
| — (manifest event list) | 7 | `adapter/github/test_manifest.py` | Unit |
| US-M1–M12 (happy path) | 1–7 | `tests/e2e/test_memory_pipeline.py` (new) | E2E |
