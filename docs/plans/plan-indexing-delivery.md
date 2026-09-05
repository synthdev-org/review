# Plan: Phase 2 — Incremental Indexing

**Status: Proposed.** Sequences [`docs/specs/spec-indexing.md`](../specs/spec-indexing.md)
— [`docs/roadmap.md`](../roadmap.md)'s Phase 2 indexing bullet — into
buildable, independently-testable phases, following the same
phase/tests/acceptance shape `plan-plan-0-delivery.md` and
`plan-phase-1-walking-skeleton.md` established as this repo's `docs/plans/`
convention. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

`spec-indexing.md` was itself self-reviewed to "ready to hand to a planning
agent" against `prd-review`'s ten dimensions before this plan was drafted —
this plan does not re-litigate any product/architecture call the spec
already made (language scope: TypeScript/PHP/Go; bundled local embedding
model, never BYO; name-based heuristic graph matching, never precise
per-language symbol resolution; push-payload-based diff detection with a
full-tree fallback; zero generative LLM calls per
[ADR-0007](../adr/0007-llm-free-indexing-deferred-rag-enrichment.md); the
`INDEXING_ENABLED` kill switch; the embedding-model startup check). It only
sequences the work and resolves the one open architecture question the spec
explicitly deferred to the planning stage — **indexing's orchestration is
its own separate, lightweight linear pipeline, not review/step.py's
framework promoted to a shared location** — decided directly with the user
before this draft, not guessed (see "Assumptions this plan makes").

**This draft went through three independent `plan-review` passes (seven
dimensions each) and was revised after all three — the arc matters more
than the tally:** round one found foundational gaps (a missing domain
Protocol, an untested `Fake` gap, per-file isolation that only actually
covered chunking). Round two, digging into round one's own fixes, found
several had real teeth missing (a fork-PR data-poisoning vector via
shared row keys, a webhook-schema correctness bug that would 422-reject
real traffic, an embedding-failure catch with no exception type behind
it) and fixed them — but round three then found two of *those* fixes had
each traded one problem for a worse one: a per-PR-keyed rate limiter
closed one DoS path by opening an unbounded-memory-growth one, and a
job-wide batched embedding step (added for throughput the spec never
asked for) widened a single bad chunk's blast radius to an entire
20,000-file run. **Both were reverted to simpler designs** — one
repo-keyed rate-limit bucket (matching `REVIEW`'s own established shape),
and a plain per-file sequential pipeline with no batching — rather than
patched further, per the same review's own finding that the compounding
fixes were inflating scope beyond what the spec's Must-haves actually
require. Round three also caught a real independent bug: `delete_file`
(US7) was defined in Phase 6 but never once invoked anywhere in the
pipeline. See each phase's text below for what changed and why.

Phase 1 (webhook ingestion, worker pipeline, static analysis) is complete
(`d42b83c`) — this is additive work on a real, running scaffold, not
greenfield. Every phase below names the existing file/module it extends.

## Scope

**In scope** — `spec-indexing.md`'s Units A–G in full, sequenced 1:1 as
Phases 1–7 below: push/PR-webhook trigger wiring, repo content fetch,
AST-aware chunking (TypeScript/PHP/Go), local embedding, call/import graph
extraction, storage schema + delta/deletion, and the indexing job pipeline
+ Worker dispatch.

**Out of scope** — the spec's own out-of-scope list applies unchanged: the
Context Builder and review-pipeline upgrade (the companion Phase 2 spec,
not yet written), docstring/summary embedding, hierarchical summarization,
LLM-based entity extraction (all deferred per ADR-0007), precise
per-language symbol resolution, cross-repo graph edges, admin-triggered
reindex, a customer-configurable/BYO embedding endpoint, and languages
beyond TypeScript/PHP/Go. Not repeated in full here; see the spec's own
Scope section. **Named explicitly, not silently inherited:** the spec's
"no admin-triggered reindex" boundary means this plan has no automated way
to force-reindex a repo after bad data has already been written by a buggy
release — Phase 7 documents the one manual (non-automated) recovery step
available today rather than quietly building new scope to solve it (see
Phase 7's Observability & rollback).

**Traceability:** every user story in the spec (`US1`–`US11`) plus the NFR
scenarios added during review maps to at least one **integration** test
named in a phase below, per `testing.md`'s actual mandatory rule (a story
is a boundary crossing; a unit test alone doesn't satisfy it — corrected
during an independent testing-alignment review, which found this
paragraph's original wording ("at least one test") was weaker than the
rule it was supposed to restate). For the three stories whose logic is
pure/no-I/O (US3, US5, US9), the integration coverage comes directly from
Phase 7's `indexing/test_pipeline.py` — a real, dedicated Integration-tier
test (real Postgres) that exercises chunking/graph-extraction/prefiltering
as part of running an actual job, which independently satisfies
`testing.md`'s rule on its own terms. **Citation corrected during a
second independent review:** an earlier draft additionally claimed this
mirrored "`spec-review-pipeline.md`'s own US3" precedent — false on
inspection: that PRD's own Testing table gives US3 a Unit row plus only an
**E2E** row, no dedicated Integration row, so it does not actually
establish the Unit-plus-Integration pattern being claimed here. Dropped;
this plan's own dedicated Phase 7 integration test stands on its own
merits and needs no borrowed precedent. Consolidated table at the end of
this plan, after Phase 7.

## Assumptions this plan makes (flagged, not decided)

1. **Orchestration shape (decided with the user, not this plan's own
   guess):** indexing's pipeline is a new, separate, lightweight linear
   sequence (`indexing/pipeline.py`), not `review/step.py`'s `Step`/
   `StepContext` framework promoted to a shared location. Reasoning
   confirmed directly: indexing's shape is a fixed sequence, not a DAG of
   independently-registrable named steps the way review's static-analysis
   sub-steps are, and architecture.md's own "no abstraction without a
   second real implementation already in view" rule argues against forcing
   a second, structurally different consumer through a DAG-resolution
   framework it doesn't need.
2. **Embedding model, chunk token budget, cost ceilings** — carried
   forward unchanged from `spec-indexing.md`'s own "Open assumptions
   pending validation" (`Xenova/all-MiniLM-L6-v2`/384-dim; ~512 tokens/
   64-token overlap; 500 files/200MB incremental, 20,000 files/2GB
   full-run). This plan implements against these as starting defaults, not
   decisions to re-litigate.
3. **Embedding package choice**: `fastembed` (a thin ONNX wrapper that
   ships its own model download/caching) for Phase 4, over a lower-level
   `onnxruntime`+`tokenizers` pairing or `sentence-transformers` — smaller
   dependency footprint, matching this whole PRD's keep-it-cheap direction.
   Override-able at implementation time; not architecturally significant.
4. **Model bundling strategy**: model weights are baked into the Worker's
   container image at build time, not downloaded on first run — consistent
   with [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md)'s
   self-hosted/air-gapped-friendly trust story. **The Dockerfile change
   must pin the model artifact by content hash/checksum**, not a mutable
   model-name reference alone — a compromised/tampered model artifact
   would otherwise run in-process against every customer's proprietary
   source undetected (found during this plan's own security review).
5. **`EmbeddingClient` and `RepoTreeFetcher` are justified `Protocol`s
   regardless of implementation count — corrected during this plan's own
   review**, which found the original justification (future swappability,
   by analogy to `LLMClient`/`DiffFetcher`) didn't actually hold up:
   `roadmap.md`'s GitLab/Bitbucket adapters (cited for `DiffFetcher`) sit
   in its "Deferred, not scheduled" section, defined there as "not on this
   roadmap because nothing above depends on them landing first" — the
   opposite of "already in view" — and `spec-indexing.md`'s BYO-embedding
   mention is a named-but-unscheduled future Should-have, not a roadmap
   entry either. **The actual, sufficient justification is
   architecture.md's own zero-infra-imports domain-boundary rule,
   independent of implementation count:** `indexing/pipeline.py` is a
   domain package and may not import `adapter/github/tree.py` (which
   imports `httpx`) or `adapter/embedding/local_onnx.py` (which imports
   the ONNX runtime) directly, regardless of whether a second
   implementation of either ever ships — the same reasoning that already
   justifies Phase 6's `IndexRepository`/`IndexCommitTracker` Protocols
   below. Both Protocols stay; this is a corrected justification, not a
   design change.
6. **`RepoTreeFetcher`/`RepoTreeEntry` are defined in `indexing/tree.py`
   (the `indexing` package), not `ingestion/tree.py`** — corrected during
   this plan's own review. [ADR-0006](../adr/0006-protocol-defined-at-data-model.md)'s
   exception for defining a Protocol away from its consumer only applies
   with more than one real consumer sharing an arbitrary "first consumer"
   home; this Protocol has exactly one named consumer (Phase 7's
   pipeline), so ADR-0006's own default rule — defined at the consumer —
   applies unmodified. `indexing/tree.py` is built in Phase 2 (before
   Phase 7 exists) but lives in the package that actually consumes it,
   the same way a module can be authored before every one of its future
   callers is built.
7. **`review/prefilter.py`'s ignore-pattern constants (lockfile names,
   vendored directory segments, generated-file globs, binary-extension
   globs) are duplicated into `indexing/prefilter.py`, not imported
   across packages** — corrected during this plan's own review. Importing
   `review`-package internals from the `indexing` package would cross a
   bounded-context boundary architecture.md's package-structure rule
   argues against, and moving the constants to a new shared location
   would recreate the `common`/`shared`/`utils` dump that same rule
   explicitly forbids. Four small `frozenset`/tuple constants duplicated
   across two files is a cheap, explicit trade-off — not a shared-module
   workaround.
8. **New module/file names** not already pinned by the spec's own Unit
   descriptions follow this codebase's existing one-module-per-concern
   convention: `indexing/tree.py`, `indexing/chunk.py`,
   `indexing/prefilter.py`, `indexing/embedding.py`, `indexing/graph.py`,
   `indexing/pipeline.py`, `indexing/repository.py` (the new domain
   Protocols, see Phase 6), `indexing/conftest.py` (shared test fakes, see
   Phase 4/6), `adapter/github/tree.py`, `adapter/embedding/local_onnx.py`,
   `adapter/embedding/startup_check.py`,
   `adapter/storage/indexing_repository.py`. Naming specifics beyond what's
   written below are implementer discretion.
9. **PR-branch indexing writes to separate, non-canonical rows, scoped by
   `pr_number` (decided during an independent security review, closing a
   blocking data-poisoning gap):** the original schema keyed `index_chunk`/
   `index_graph_node` uniqueness on `(repo_id, file_path, ...)` alone, with
   no distinction between default-branch (trusted) and PR-branch
   (potentially zero-permission-fork-authored) content — an untrusted fork
   contributor's `pull_request synchronize` could therefore overwrite a
   trusted default-branch row for the same file path, poisoning the
   canonical index a future Context Builder will treat as authoritative
   "this repo's code," with no reindex or reversion once the PR closes.
   Fixed by a non-null `pr_number` column (`0` means "default
   branch/canonical") folded into both tables' unique keys —
   `(repo_id, pr_number, file_path, chunk_index)` and `(repo_id, pr_number,
   file_path, symbol_name)` — so PR-branch indexing (`pr_number > 0`)
   writes to entirely separate rows, never touching the canonical
   (`pr_number = 0`) rows a default-branch push produces. See Phases 1, 6,
   7 for the payload/schema/pipeline changes this requires.
   **Simpler alternative considered and rejected:** not persisting
   PR-triggered content at all until the Context Builder spec exists to
   consume it. Rejected because US2 ("PR head-branch push indexes the
   exact reviewed commit") is a spec Must-have, not this plan's to defer —
   the data has to land somewhere for that story to be true today, even
   before anything queries it.
   **Boundaries, kept brief:** no merge-time promotion to `pr_number = 0`
   is needed — a merge commit lands on the default branch via an ordinary
   `push` and gets re-indexed as canonical through the normal path (US1).
   PR-scoped rows are retained indefinitely with a worse growth curve than
   canonical rows (no PR-close cleanup built here — a reasonable smaller
   future improvement, not this plan's problem to solve). Two things this
   fix does *not* cover, left for the companion Context Builder spec: that
   spec's queries must filter `pr_number = 0` when reading "canonical"
   code (this fix is write-side only), and a fork-PR author can still
   poison *their own* PR-scoped content to mislead that same PR's future
   review — the same trust boundary code review already operates under
   (an attacker can already lie in their own diff), not a new one.
10. **One indexing-specific rate-limit bucket, keyed by repo only —
    simplified back to this shape after two rounds of over-fitting proved
    counterproductive:** the concern is real (an unprivileged
    `pull_request` contributor could otherwise spam `synchronize` to
    trigger repeated expensive `INDEX` work), but two successive attempts
    to make the fix more precise each introduced a worse problem than the
    one they solved. Keying by repo alone (round one) left one PR able to
    exhaust the bucket for every other PR on the same repo. Keying by
    `f"{repo_id}:{pr_number}"` instead (round two) fixed that but
    introduced an unbounded-memory-growth vector of its own — opening a
    PR is free for an external contributor, so an attacker could grow the
    limiter's in-memory key space without bound, and it *still* didn't cap
    aggregate per-repo cost, since N open PRs simply meant N independent
    budgets. **Final shape:** a single new `INDEX`-specific
    `InMemoryRateLimiter` instance (`index_enqueue_rate_limit_per_minute`,
    recommended default 5/minute), keyed by `str(repo_id)` alone, covering
    both `push`- and `pull_request`-triggered `INDEX` enqueues together —
    the same bounded, one-bucket-per-repo shape the existing `REVIEW`
    limiter already uses. **Trade-off, named honestly (corrected during a
    fourth review round, which found "bounded to a minute" understated
    it):** since opening or updating a fork PR is free and repeatable at
    will, an unprivileged attacker can re-saturate this bucket every
    window indefinitely — a *sustained*, not one-time or short-lived,
    denial of `INDEX` enqueues for the whole repo, including legitimate
    default-branch push-triggered indexing (the bucket is shared).
    Accepted anyway, for reasons specific to this feature's current
    stage: `INDEX` job availability has no independently observable
    end-user value yet (this plan's own Problem & Goal), so the worst
    case is stale-but-not-wrong index freshness, not data loss or
    corruption; the `INDEXING_ENABLED` kill switch (Assumption 11) is a
    real, already-built manual recourse if this is ever actually
    exploited; and a bounded key space that's merely *sustainably*
    abusable is still strictly safer than the unbounded-memory-growth
    failure the per-PR-keyed alternative introduced. Revisit if/when
    `INDEX` job availability starts to matter to a real user-facing
    consumer. See Phase 1.
11. **`indexing_enabled` defaults to `False`, not `True` (corrected during
    an independent compatibility review):** the original `True` default
    silently contradicted this plan's own documented staged-rollout
    precondition ("the kill switch stays off until Phase 7 is ready") —
    an operator would have had to independently know to override it, a
    step the plan never named. Defaulting to `False` means Phases 1–6 ship
    genuinely inert (no `INDEX` job ever enqueued) until an operator makes
    the affirmative, single-config-value choice to turn indexing on,
    matching the safe-rollout narrative literally rather than only in
    prose.
12. **`EmbeddingClient.embed()` raises a typed `EmbeddingError` on
    failure:** `indexing/embedding.py` defines it;
    `adapter/embedding/local_onnx.py` raises it (never a raw
    provider/runtime exception, per architecture.md's exception-type
    rule); Phase 7's per-file `embed` stage catches it, called with each
    file's own chunks — not batched across the whole job (an earlier
    batched design was tried and reverted; see Phase 7).
13. **`indexing/repository.py` defines two Protocols, not one 5-method
    Protocol (corrected during an independent architecture review):**
    architecture.md caps a `Protocol` at 1–3 methods, defined at the
    consumer, requiring a new ADR for any deviation; the original single
    `IndexingRepository` Protocol had five with no such ADR. Split into
    `IndexRepository` (`upsert_chunks`, `upsert_graph`, `delete_file` — 3
    methods, the per-job write path) and `IndexCommitTracker`
    (`get_last_indexed_commit`, `set_last_indexed_commit` — 2 methods, the
    per-repo cursor consulted *before* a job's write path even runs, to
    decide incremental-vs-full-tree-fallback) — a real seam, not an
    arbitrary split. `adapter/storage/indexing_repository.py`'s concrete
    `IndexingRepository` class implements both Protocols;
    `indexing/pipeline.py` depends on each narrow interface it actually
    needs.
14. **`IndexRepository`'s methods raise a typed `IndexingRepositoryError`
    on failure:** `indexing/repository.py` defines it;
    `adapter/storage/indexing_repository.py`'s concrete methods raise it
    on a Postgres-level upsert/delete failure. Phase 7's per-file `upsert`
    stage (and its `delete_file` call) catches it.

## Phase 1 — Push/PR-webhook trigger wiring (Unit A)

**Why first:** defines the `INDEX` job payload contract (commit SHA,
added/removed/modified file lists, truncation/force-push signal) every
later phase's pipeline consumes — building it first means Phases 2–7 can
be tested against a real, settled payload shape rather than a guess.

- Extend `adapter/github/schemas.py`: add a `PushCommit` model
  (`added`/`removed`/`modified: list[str]`), and add `ref: str | None =
  None`, `before: str | None = None`, `after: str | None = None`,
  `commits: list[PushCommit] = []`, `head_commit: PushCommit | None =
  None`, and `forced: bool | None = None` to `GitHubWebhookEnvelope`; add
  `default_branch: str | None = None` to `Repository`. **Every one of
  these is optional (corrected during an independent compatibility
  review, which found the original draft declared `ref`/`before`/`after`/
  `commits` as required):** a `pull_request`, `ping`, or `installation`
  delivery carries none of these push-only fields, and a required field
  here would reject those already-working, correctly-signed deliveries
  with a 422 — exactly the failure mode this envelope's existing
  `action`/`pull_request` optionality was already designed to avoid. Same
  "model only what's consumed, ignore the rest via `extra=\"ignore\"`"
  discipline the envelope already follows.
- Extend `adapter/github/triggers.py`: add `is_index_trigger(event_type,
  action, ref, default_branch) -> bool` — a second pure, no-I/O function
  alongside the existing `is_enqueue_trigger`, unit-tested at the same tier
  per [ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md)'s existing
  precedent for this exact file. True for `push` where `ref ==
  f"refs/heads/{default_branch}"`; true for `pull_request` with action
  `opened` or `synchronize` (same actions the existing review trigger
  matches — a different job type, same trigger moment).
- Extend `apps/webhook_api/webhook_routes.py`: generalize
  `_handle_matched_trigger`/`_run_in_transaction` (currently hardcoded to
  `JobType.REVIEW`) to accept a job type + payload builder, so the same
  shared-transaction/idempotency machinery serves both trigger kinds
  without duplicating it. Add `_enqueue_index_payload(event, result)`
  carrying the commit SHA, added/removed/modified lists, and the
  truncation/force-push signal (`len(commits) == 20` or `forced`) Phase 7's
  pipeline uses to pick the full-tree-fallback path (US8). Also carries a
  `pr_number: int` field (`0` for a `push`-triggered job, the real PR
  number for a `pull_request`-triggered job) per Assumption 9 — the
  payload contract Phase 6/7's PR-scoped row-writing depends on.
- **One indexing-specific enqueue rate limit (per Assumption 10, final
  shape after two over-fitted attempts were tried and reverted):** the
  existing `enqueue_rate_limiter` is shared per-repo with `REVIEW`
  enqueues and defaults to `0` (disabled) — appropriate for review's
  per-PR frequency, but an `INDEX` job's worst-case cost (a
  full-tree-fallback run, up to 20,000 files/2GB) is materially higher
  than a review job's. Reuses the existing, already-installed
  `apps/webhook_api/rate_limiter.InMemoryRateLimiter` class (the same one
  `_build_enqueue_rate_limiter` already wires up for `REVIEW`) — **one
  new instance**, `index_enqueue_rate_limit_per_minute` (recommended
  default 5/minute), keyed by `str(result.repo_id)`, covering both
  `push`- and `pull_request`-triggered `INDEX` enqueues together — the
  same one-bucket-per-repo shape `REVIEW`'s own limiter already uses.
  Plan-originated default, not spec-mandated — the spec's own NFRs
  specify only the aggregate cost ceilings and the kill switch, not
  per-trigger throttling; override-able, not a product-validated number.
  **Known, accepted trade-off (see Assumption 10 for the full reasoning):**
  a repeat, free-to-mount fork-PR flood can sustain denial of `INDEX`
  enqueues for the whole repo indefinitely, not just briefly — accepted
  because `INDEX` job availability has no independently observable
  end-user value yet, the kill switch is real manual recourse, and this
  is still strictly safer than the unbounded memory-growth failure a
  per-PR-keyed alternative introduced instead.
  **Known inherited limitation, named rather than assumed away:**
  `InMemoryRateLimiter` is per-process/per-pod, not distributed
  (documented in its own module), and `c4-architecture.md` describes the
  Webhook API as stateless and horizontally scalable — under N replicas
  the real ceiling is N× the configured value, the same gap already
  accepted for the existing `REVIEW` limiter (Phase 8 territory), not a
  new one this plan introduces.
- **Kill switch, default `False` (per Assumption 11):** add
  `indexing_enabled: bool = False` to `apps/webhook_api/settings.py` (real
  operator-facing env var, given the existing
  `env_prefix="REVIEW_WEBHOOK_API_"`:
  `REVIEW_WEBHOOK_API_INDEXING_ENABLED`). `receive_github_webhook` passes
  it explicitly into the index-trigger check (not read from settings
  inside the pure `is_index_trigger` function itself, per architecture.md's
  "parse config once, pass down explicitly" rule) — when `False` (the
  shipped default), no `INDEX` job is enqueued regardless of match, while
  `REVIEW` triggers are unaffected; an operator makes the affirmative
  choice to set it `True` once Phase 7 is deployed and ready. **Scope of
  this switch, stated precisely:** it stops new `INDEX` jobs from being
  *enqueued*; it does not stop `INDEX` jobs already sitting in `job_queue`
  from being claimed and run to completion by the Worker — draining those
  requires the Worker's own existing scale-down/pause operational lever,
  the same one an operator would use for a `REVIEW`-job incident today,
  not a new mechanism this plan builds.

**Tests:** Unit (`adapter/github/test_triggers.py`, extended — pure logic,
per ADR-0005). Integration (`apps/webhook_api/test_webhook_routes.py`,
extended — real Postgres via testcontainers, per testing.md) for the
enqueue/idempotency/kill-switch/rate-limit behavior (a `push`-triggered
and a `pull_request`-triggered enqueue for the same repo share, and can
jointly exhaust, the same bucket). The generalization of
`_handle_matched_trigger`/`_run_in_transaction` touches a live, working
code path (today's `REVIEW`-only trigger flow) — every existing `REVIEW`
webhook integration test in this file must keep passing unchanged, which
is this refactor's own regression safety net, not just the new `INDEX`
tests.

**Observability & rollback:** Enqueue/skip logging extends the existing
"webhook accepted" / "webhook accepted; not enqueued" log lines
(`job_type` field already present in that shape; add `skipped_reason`
values for the new `index_rate_limited` and `indexing_disabled` cases). No
schema change in this phase — rollback is a plain code revert, no data
cleanup needed. The kill switch itself doubles as this phase's own
operational rollback lever once deployed (see its stated scope above).

**Acceptance:** US1, US2, US11's `Given/When/Then` pass as integration
tests; `is_index_trigger`'s unit tests pass; the kill-switch integration
test passes; the rate limit is proven shared correctly across both
trigger types for the same repo.

## Phase 2 — Repo content fetch for indexing (Unit B)

**Why next:** no code dependency on Phase 1, but the next building block
Phase 7's pipeline needs; sequenced here for linear delivery.

- New `indexing/tree.py` (not `ingestion/tree.py` — see Assumption 6 for
  why): `RepoTreeEntry` (path, mode, blob_sha, size) and `RepoTreeFetcher`
  Protocol with `fetch_tree(*, installation_id, repo_full_name,
  commit_sha) -> tuple[RepoTreeEntry, ...]` (full listing at a commit —
  the full-run and full-tree-fallback case) and `fetch_content(*,
  installation_id, repo_full_name, paths_with_blob_sha) -> dict[str,
  bytes]` (an arbitrary path set, not diff-scoped).
- **Extract a shared blob-fetch helper** (corrected during an independent
  reuse review, which found the original draft's "reuses the same
  Trees/Blobs API calls" claim overstated: `adapter/github/diff.py`'s
  `GitHubDiffFetcher.fetch_content` inlines its symlink/submodule mode
  guard, ~100MB per-blob size ceiling, and semaphore-bounded concurrent
  fetch directly in that method — none of it is factored into an
  importable helper today). This phase extracts that guard/ceiling/
  concurrency logic into a shared helper both `GitHubDiffFetcher` and the
  new `GitHubTreeFetcher` call, rather than copying it — genuine reuse,
  not a reuse claim that doesn't hold up. **Also reuses, not
  reimplements, the tree-listing call itself** (the same review found this
  gap too): `GitHubDiffFetcher.fetch()` already calls `GET
  /repos/{owner}/{repo}/git/trees/{sha}?recursive=1` and parses
  `tree_data["tree"]` for its own changed-file cross-reference —
  `fetch_tree()` extracts and reuses that exact call/parse logic rather
  than issuing a second, independent implementation of it. Both the new
  helper and `fetch_tree()` route through `adapter/github/http.py`'s
  existing shared `request_with_retry`/`client_context`/`auth_headers`/
  `split_owner_repo` — the documented "one shared retry/backoff +
  redacted-logging helper every `adapter.github` GitHub API call routes
  through," already used by `auth.py`/`diff.py`/`posting.py` — the same
  way every other GitHub API call in this codebase does, not a new
  request path.
- **Trees API's own truncation ceiling, addressed explicitly (found during
  the same reuse review):** GitHub's Git Trees API truncates its response
  at 100,000 entries / 7MB and sets `"truncated": true` on the payload when
  it does — `diff.py`'s existing changed-file-only use of this endpoint
  never approaches that ceiling, but `fetch_tree()`'s full-repo listing
  (up to this plan's own 20,000-file full-run ceiling) is far more exposed
  to it. `RepoTreeTruncatedError` is defined in `indexing/tree.py`
  (alongside the `RepoTreeFetcher` Protocol it's part of that Protocol's
  failure contract) and raised by `adapter/github/tree.py`'s concrete
  `fetch_tree()`, which checks the response's `truncated` field, rather
  than silently returning a partial tree — Phase 7 treats this the same
  as any other file-set-resolution failure for that job (logged, job
  marked `FAILED`), never a silently
  incomplete index.
- New `adapter/github/tree.py`: `GitHubTreeFetcher` implementing
  `RepoTreeFetcher`, using the shared helper above plus the existing
  installation-token exchange. **Filters `RepoTreeEntry` by git object
  mode before fetching content** (skip `120000` symlinks and `160000`
  submodule gitlinks) — the same guard `GitHubDiffFetcher` already applies,
  now shared rather than a gap unique to the new tree-scoped fetch (an
  arbitrary-path fetch without this guard could issue wasted/failing Blob
  API calls against non-file tree entries). **Partial-fetch-failure
  semantics, stated explicitly (found during an independent observability
  review):** if `fetch_content()`'s underlying blob fetch fails for even
  one path in a requested set, the whole call raises rather than silently
  returning a partial `dict[str, bytes]` with failing paths missing — a
  silently incomplete content set is a worse failure mode (an incomplete
  index that looks complete) than a job that fails visibly and retries.

**Tests:** Integration (`adapter/github/test_tree.py`, recorded GitHub
cassette via `respx`, per `testing.md`'s adapter convention) — covers
US8's full-tree fetch, the arbitrary (non-diff-scoped) path-set fetch, the
symlink/submodule exclusion, the `truncated` → `RepoTreeTruncatedError`
case, and the fail-whole-call-on-partial-blob-failure case. A
`FakeRepoTreeFetcher` (in `indexing/conftest.py`, see Phase 4) is added
here too, for Phase 6/7's pipeline-level tests to use instead of a
recorded cassette. **Regression safety net, stated explicitly (found
during an independent scope-discipline review):** extracting the shared
blob-fetch helper touches `adapter/github/diff.py`, a live, already-shipped
production file — every existing test in `adapter/github/test_diff.py`
must keep passing unchanged, the same regression-safety-net requirement
Phase 1 states for its own touch of a live code path.

**Observability & rollback:** Trees/Blobs call failures log with
repo/commit identity, matching `adapter/github/diff.py`'s existing
pattern. No data written at this phase — rollback is a code revert.

**Acceptance:** US8's full-tree-fetch behavior passes as an integration
test against a recorded cassette; the symlink/submodule filter, the
truncation error, and the partial-fetch-failure behavior are each proven
by a dedicated fixture case; `adapter/github/test_diff.py`'s existing
suite passes unchanged.

## Phase 3 — AST-aware chunker (TypeScript, PHP, Go) (Unit C)

**Why next:** needed by Phase 5 (graph extraction consumes its parse
trees) and Phase 7; no dependency on Phases 1–2's code.

- Add `tree-sitter`, `tree-sitter-typescript`, `tree-sitter-php`,
  `tree-sitter-go` to `pyproject.toml` — real PyPI-packaged grammars,
  added as ordinary dependencies the same way Semgrep was in Phase 1.
  **Not covered by an existing dependency:** Semgrep (already installed,
  and itself AST-based for these same three languages) was checked as the
  nearest reuse candidate and rejected — `adapter/analyzers/semgrep.py`
  invokes it as a subprocess for rule-based finding output only
  (`adapter/analyzers/subprocess_runner.py`), exposing no API for the
  underlying chunk/parse-tree boundaries this phase actually needs.
- New `indexing/chunk.py`: `Chunk` model (repo_id, file_path, language,
  symbol_name, byte_range, content, chunk_index); pure `chunk_file(language,
  path, content: bytes) -> ChunkResult` where `ChunkResult` carries both
  the produced `tuple[Chunk, ...]` **and** the parsed Tree-sitter tree —
  returned together so Phase 5's graph extraction doesn't re-parse the
  same file a second time per push. Splits at
  function/method/class/interface/type-declaration boundaries, packs to
  the ~512-token/64-overlap budget, re-attaches enclosing context. Raises
  a typed `ChunkParseError` on failure — never caught here, per
  architecture.md's exception-type rule; Phase 7's pipeline applies US10's
  per-file isolation across the *whole* per-file chain, not just this
  stage (see Phase 7 — corrected during this plan's own observability
  review).
- New `indexing/prefilter.py`: excludes lockfile/vendored/generated/binary
  paths (duplicated constants, per Assumption 7) plus a new "extension not
  in `.ts`/`.tsx`/`.php`/`.go`" filter. **No per-file size ceiling beyond
  the aggregate ceilings** — an accepted limitation, not a new gap this
  plan introduces: `review/prefilter.py` has the identical shape of gap
  today (aggregate file-count/byte ceilings only, no per-file cap), so
  this is inherited, symmetric, existing-precedent behavior, not a new
  inconsistency.

**Tests:** Unit tier — `indexing/test_chunk.py`, `indexing/test_prefilter.py`
(pure logic, per-language fixtures, no I/O). **Tier note, corrected during
this plan's own testing-alignment review:** this is Unit tier directly per
`testing.md`'s own pyramid rule ("Unit: one domain package in isolation,
no real I/O") — `indexing` is a domain package, so no
[ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md) exemption is
needed or being invoked here; ADR-0005 specifically exempts pure logic
living *inside an adapter* package (like `adapter/github/triggers.py`),
which doesn't apply to this phase.

**Observability & rollback:** N/A at this phase — pure functions with no
side effects of their own; the caller (Phase 7) is where job-level logging
happens.

**Acceptance:** US3, US9's `Given/When/Then`, and US10's raising behavior
itself (not yet the "doesn't fail the whole job" isolation, which is Phase
7's), pass as unit tests against fixtures for all three languages.

## Phase 4 — Local embedding client (Unit D)

**Why next:** independent of Phase 3 in code, but the next building block
Phases 6–7 need; also this plan's first new runtime-dependency decision
(model bundling), worth resolving here rather than discovering it late.

- Add `fastembed` to `pyproject.toml` (see Assumption 3).
- New `indexing/embedding.py`: `EmbeddingVector` model (values:
  `tuple[float, ...]`, dimension: int); `EmbeddingError` exception (added
  per Assumption 12); `EmbeddingClient` Protocol (justified per Assumption
  5) with `embed(texts: Sequence[str]) -> tuple[EmbeddingVector, ...]`
  raising `EmbeddingError` on failure. The signature accepts a sequence
  (a single file's own chunk texts can number more than one) but Phase 7
  calls it once per file, not once per job — an earlier job-wide batched
  design was tried and reverted after review found it widened a single
  bad chunk's blast radius to an entire run (see Phase 7).
- New `adapter/embedding/local_onnx.py`: `LocalONNXEmbeddingClient`
  implementing it, running `Xenova/all-MiniLM-L6-v2` in-process — no
  network call at inference time, raising `EmbeddingError` (never a raw
  ONNX-runtime exception) on inference failure. Dockerfile change: bake
  model weights into the Worker image at build time, **verified against a
  pinned checksum, not just referenced by a mutable model name**
  (Assumption 4) — concretely, the build step downloads the model
  artifact and runs a checksum verification (`sha256sum -c` against a
  hash committed to the Dockerfile/lockfile) that fails the image build on
  mismatch, closing the gap an independent security review found (a
  hash-named reference alone doesn't assert the downloaded bytes actually
  match it).
- New `adapter/embedding/startup_check.py`: `check_embedding_model_loaded()`
  — mirrors `adapter/storage/pgvector_check.py`'s fail-fast pattern
  exactly (attempt a real load/inference call once at Worker startup;
  raise a typed error, not a generic exception, on failure). Scoped to
  "does it load and infer" only — the checksum itself is already verified
  at build time above, so this check doesn't re-verify it at runtime.
- **`FakeEmbeddingClient`** in `indexing/conftest.py` — a deterministic,
  fixed-vector implementation of `EmbeddingClient`, per `testing.md`'s
  "fakes over mocks for adapters" convention. **Reuse claim corrected
  during an independent code-reuse review:** the original draft claimed
  this shared-conftest pattern was "already established ... via
  `review/conftest.py`'s fakes" — false; `review/conftest.py` only
  re-exports Postgres testcontainer fixtures, and this codebase's actual
  existing fake convention is private, per-test-file `_FakeXxx` classes
  duplicated across files (e.g. `_FakeDiffFetcher` independently redefined
  in three `review/test_*.py` files). `indexing/conftest.py`'s shared,
  public fakes are a **new, deliberately different pattern for this
  package** — justified because `indexing`'s fakes (`FakeRepoTreeFetcher`,
  `FakeEmbeddingClient`) are each needed by multiple test files across
  Phases 6/7, unlike `review`'s one-off per-file fakes — not a
  misattributed continuation of an existing one.

**Tests:** Integration (`adapter/embedding/test_local_onnx.py` — real
local model, no external network, per `testing.md`'s adapter convention,
including a case asserting a real batched call across multiple texts and
a case asserting `EmbeddingError` (not a raw exception) on a forced
inference failure — **mechanism specified (found unbuildable as originally
stated by an independent testing review):** construct
`LocalONNXEmbeddingClient` against a real-but-deliberately-corrupted local
model artifact path (a truncated/invalid file, not the real bundled
model) so the real ONNX runtime call genuinely fails — a real failure
through the real code path, not a mock, per `testing.md`'s "fakes over
mocks for adapters" convention; `adapter/embedding/test_startup_check.py`
— both the
loadable and deliberately-unloadable cases). The build-time checksum
verification step is proven by a build-time check (a deliberately
mismatched checksum fails the image build), not a pytest case — named
here so it isn't silently missing from the test plan.

**Observability & rollback:** Startup-check failure logs at Worker boot,
extending `apps/worker/main.py`'s existing startup-checks sequence
(alongside the pgvector check). A model failure *after* startup (mid-job,
on a later `INDEX` job) is a distinct failure mode from the startup check
and is covered by Phase 7's broadened per-file isolation, not by this
phase's own startup-only check. Rollback is a dependency + module revert;
no data written yet.

**Acceptance:** `embed()` produces real fixed-dimension vectors for a
batch of sample texts in one call (the "stored" half of US4 is Phase 6's);
the startup check passes and fails correctly in both cases.

## Phase 5 — Call/import graph extraction (Unit E)

**Why next:** depends on Phase 3's parsed Tree-sitter tree
(`ChunkResult`'s tree, per Phase 3's deliverable).

- New `indexing/graph.py`: `GraphNode` (repo_id, file_path, symbol_name,
  language) and `GraphEdge` (source_node, target_symbol_name) models; pure
  `extract_graph(language, path, tree) -> GraphResult` (nodes + edges)
  operating on the tree Phase 3 already parsed — no second parse.
  Definitions become nodes; call expressions and import statements become
  candidate edges, name-matched against nodes (US5's "no match is not an
  error" design). Raises a typed `GraphExtractionError` on failure — never
  caught here (**corrected during this plan's own observability review**,
  which found the original draft claimed this phase's observability
  concerns were "N/A, same as Phase 3" while defining no failure contract
  at all, unlike Phase 3's `ChunkParseError`; this typed exception closes
  that gap and is handled by Phase 7's per-file isolation exactly like
  `ChunkParseError`). This phase's own unit tests match against an
  in-memory fixture symbol set — real cross-file, cross-push matching
  against *stored* symbols is exercised in Phase 7's integration tests
  once Phase 6's storage exists.

**Tests:** Unit (`indexing/test_graph.py`, per-language fixtures, pure
logic, no I/O — direct domain-package Unit tier, same note as Phase 3).

**Observability & rollback:** N/A at this phase for the same reason as
Phase 3 (pure function, no side effects) — but unlike the original draft,
this is now actually true, since `GraphExtractionError` gives Phase 7 a
real contract to observe and isolate, rather than an undefined failure
mode.

**Acceptance:** US5's node/edge extraction `Given/When/Then` passes as
unit tests for all three languages, including the raising behavior for a
malformed/unparseable tree.

## Phase 6 — Storage schema + delta/deletion logic (Unit F)

**Why next:** needs Phase 3's `Chunk`, Phase 4's `EmbeddingVector`, and
Phase 5's `GraphNode`/`GraphEdge` shapes finalized to design table columns
against.

- Add `pgvector` (the Python package providing SQLAlchemy's `Vector` column
  type) to `pyproject.toml` — the Postgres extension is already enabled
  (Phase 0 baseline migration), but no Python-side binding is installed
  yet.
- New `indexing/repository.py`: two **`Protocol`**s, per Assumption 13 —
  `IndexRepository` (`upsert_chunks`, `upsert_graph`, `delete_file` — the
  per-job write path) and `IndexCommitTracker` (`get_last_indexed_commit`,
  `set_last_indexed_commit` — the per-repo cursor read before a job's
  write path runs). Both replace the single 5-method
  `IndexingRepository` Protocol an earlier draft proposed, which exceeded
  architecture.md's 1–3-method-per-Protocol rule with no ADR. Also defines
  `IndexingRepositoryError` (Assumption 14) — every `IndexRepository`
  method raises it, never a raw `SQLAlchemy`/`asyncpg` exception, on a
  Postgres-level failure.
- New Alembic migration, additive on top of `0001_baseline_schema.py`/
  `0002_installation_id_and_webhook_delivery.py`:
  - `index_chunk`: id, `repo_id` (FK to `repo`, `ondelete="CASCADE"` —
    matching every existing table's FK convention: `org`/`repo`/`team`/
    `job_queue`, and this schema's own `index_graph_edge.source_node_id`
    CASCADE below), **`pr_number` (non-null `int`, default `0` — `0` means
    default-branch/canonical, per Assumption 9)**, file_path, language,
    symbol_name, byte_range_start/end, content, `embedding`
    (`Vector(384)`, dimension per Phase 4's model), created_at/updated_at.
    **Unique on `(repo_id, pr_number, file_path, chunk_index)`** — the
    `pr_number` dimension is what keeps a PR-branch row from ever
    colliding with (and overwriting) the canonical `pr_number = 0` row for
    the same file path, closing the fork-PR data-poisoning gap an
    independent security review found in the original single-key design.
  - `index_graph_node`: id, `repo_id` (FK, `ondelete="CASCADE"`, same
    reasoning), `pr_number` (same column/default as above), file_path,
    symbol_name, language. **Unique on `(repo_id, pr_number, file_path,
    symbol_name)`**, same reasoning.
  - `index_graph_edge`: id, source_node_id (FK to `index_graph_node`,
    `ondelete=CASCADE`), target_symbol_name (plain string, **not** a FK —
    per US5/US7's dangling-reference design). **Unique on
    `(source_node_id, target_symbol_name)`** — an independent security
    review found the original draft specified no uniqueness key here at
    all, so ordinary repeated pushes to an active file could accumulate
    duplicate edge rows without bound; `pr_number` doesn't need to be
    repeated here since it's already carried transitively via
    `source_node_id`'s own `(repo_id, pr_number, ...)` scoping.
    `upsert_graph` deletes a source node's prior outgoing edges before
    reinserting the freshly-extracted set, per file, per job — a real
    delete-then-insert step, not just an `ON CONFLICT` upsert, since a
    symbol's outgoing edges can shrink between pushes.
  - `repo.last_indexed_commit_sha` (nullable) added to the existing `repo`
    table, same "`NULL` means not yet resolved" pattern
    `repo.installation_id` already uses. **Scoped to the default branch
    only** — a PR-scoped job's delta calculation (Phase 7) compares
    against that same repo-wide cursor for "what's changed since the
    default branch's last index," never a separate per-PR cursor, since a
    PR's own file-change set already comes from the push/PR payload
    itself (US1/US2), not from a stored PR-specific baseline.
- New `adapter/storage/indexing_repository.py`: `IndexingRepository`
  (concrete) implementing both Protocols above.

**Tests:** Integration (`adapter/storage/test_indexing_repository.py`,
real Postgres/pgvector via `testcontainers-python`, per `testing.md`'s
mandatory DB-integration rule) — covers US6 (delta upsert), US7 (deletion
+ dangling edges), the edge-table uniqueness/delete-then-reinsert
behavior, and dedicated cases proving: a `pr_number > 0` upsert never
collides with or overwrites the `pr_number = 0` canonical row for the same
`(repo_id, file_path)` (closing the traceability table's own claim of
Phase 6 coverage for US4's "stored" half, found unaddressed by an
independent testing review); a stored chunk's embedding vector
round-trips correctly (a real vector written via `upsert_chunks` reads
back byte-identical via a direct query); and — closing a gap a final
review round found (every other typed exception this plan introduces has
a named forced-failure test; this one didn't) — a real Postgres-level
failure (e.g. a forced constraint violation or a dropped connection mid-write)
raises `IndexingRepositoryError`, never a raw `SQLAlchemy`/`asyncpg`
exception, from each of `upsert_chunks`/`upsert_graph`/`delete_file`.

**Observability & rollback:** Migration is additive-only (new
tables/nullable column) — safe to deploy ahead of Phase 7's code, **on the
precondition that the `INDEXING_ENABLED` kill switch stays off and Phase
7's dispatch change isn't deployed yet** (stated explicitly here, not just
in the Compatibility & rollout section, since this subsection's rollback
claim is only true under that precondition). Given that precondition,
rollback is a straight `alembic downgrade` with no risk to existing
tables/data. Repository logs upsert/delete counts at `debug` — a real gap
in production visibility on its own, but moot for the deploy window this
subsection's precondition already describes: with `indexing_enabled`
defaulting to `False` (Assumption 11) and Phase 7 not yet deployed, zero
`INDEX` jobs run against these tables between Phase 6's deploy and Phase
7's activation, so there is no write activity this window's debug-level
logging would need to surface yet. Job-level summary logging is Phase 7's.

**Acceptance:** US6, US7's `Given/When/Then` pass as integration tests
against real Postgres; dedicated cases prove `index_graph_edge`'s
uniqueness constraint and delete-then-reinsert behavior,
`pr_number`-scoped isolation between canonical and PR-branch rows, and
that a real Postgres failure surfaces as `IndexingRepositoryError`.

## Phase 7 — Indexing job pipeline + Worker dispatch (Unit G)

**Why last:** integrates Phases 1–6; the phase that actually proves
`spec-indexing.md`'s stated goal end-to-end.

- New `indexing/pipeline.py`: `run_indexing_job(payload, deps) ->
  IndexingResult` — the lightweight linear orchestration this plan's own
  resolved architecture decision calls for (Assumption 1), typed against
  `RepoTreeFetcher`, `EmbeddingClient`, `IndexRepository`, and
  `IndexCommitTracker` (all domain-defined Protocols per Assumption 5/13
  and Phase 6). Resolves the file-change set (Phase 1's payload lists, or
  Phase 2's full-tree-diff fallback when the truncation/force-push signal
  is set, per US8; a `RepoTreeTruncatedError` from that fallback is a
  fifth expected, job-fatal error — logged and the job marked `FAILED`,
  same as any other file-set-resolution failure, never silently treated
  as "no files changed"). Writes every chunk/node row scoped by the job
  payload's own `pr_number` (Assumption 9) — a `push`-triggered job always
  writes `pr_number = 0`; a `pull_request`-triggered job writes its real
  PR number, never touching the `pr_number = 0` canonical rows.
  - **Per file, sequential, four isolated stages (simplified back to this
    shape after a later review round found the previous batched-embedding
    three-pass design was solving a throughput problem the spec never
    asked for — `spec-indexing.md`'s own NFRs explicitly disclaim any
    latency SLA — while introducing two real regressions: an unspecified
    pass-boundary contract with no test plan, and a materially worse
    failure blast radius, since one bad chunk anywhere in a job-wide batch
    would have failed embedding for every file in that batch, not just the
    offending one):** for each **added/modified** file (the payload's
    `added`/`modified` lists, or the full-tree-diff fallback's equivalent),
    in sequence: `chunk_file()`, `extract_graph()`, `embed()` (called with
    *this file's own chunks only*), then `upsert_chunks`/`upsert_graph`.
    Each stage is wrapped in its own `try`/`except` — `chunk` catches
    `ChunkParseError`, `graph` catches `GraphExtractionError`, `embed`
    catches `EmbeddingError`, `upsert` catches `IndexingRepositoryError`
    (per Assumption 14) — so stage attribution in the failure log is
    unambiguous by construction, never inferred. A failure at any stage
    logs `file_path`, `language`, `stage`
    (`chunk`/`graph`/`embed`/`upsert`/`delete` — see below), and the error
    detail (all passed via `logging`'s `extra=` structured-field
    mechanism, the same pattern every other log call in this codebase
    already uses — `file_path` originates from attacker-controlled
    fork-PR input, and this avoids the string-interpolation-into-a-log-
    template class of log-injection risk by construction, not by
    sanitizing the value), then skips that file's remaining stages and
    moves to the next file — US10's "any single file's failure, at any
    stage, must not abort the whole job" is satisfied per-file, with no
    cross-file blast radius. Only an exception outside these four types
    (a genuine bug) propagates to the Worker's existing generic
    dispatch-loop handler.
  - **Removed files are actually deleted, not just chunked/embedded/
    upserted (closing a real functional gap an independent compatibility
    review found: `IndexRepository.delete_file` was defined in Phase 6
    but never once invoked anywhere in this pipeline, so US7's deletion
    behavior was undefined in practice despite Phase 6's own tests
    covering it in isolation):** a **separate loop** over the payload's
    `removed` list (Phase 1) — distinct from the added/modified loop
    above, since a removed file has no content to chunk/embed — calls
    `delete_file(repo_id, pr_number, path)` for each, wrapped in its own
    `try`/`except IndexingRepositoryError`, logged with `stage="delete"`
    (its own value, not reusing `"upsert"`, so a delete failure is never
    misattributed to an upsert that didn't happen).
  - **`set_last_indexed_commit` is called only for `pr_number == 0` jobs
    (closing a second poisoning path, found independently by both an
    architecture and a security review pass):** the `pr_number` row-scoping
    above protects the *content* tables, but says nothing about the
    repo-wide `last_indexed_commit_sha` cursor `IndexCommitTracker` owns —
    without an explicit rule, a `pull_request`-triggered job completing
    could still advance that cursor to a fork/PR-branch commit SHA,
    corrupting every subsequent default-branch push's delta calculation.
    `run_indexing_job` calls `set_last_indexed_commit` **only when
    `payload.pr_number == 0`**, once, after every file in the job has been
    processed (successfully or isolated-and-skipped) — not incrementally
    per file — so a job that fails entirely (e.g. Postgres unavailable
    mid-job, outside any single file's isolated failure) leaves the cursor
    at its prior value and the next push's delta naturally reprocesses
    from the last known-good state. **Named, not solved:** two overlapping
    `pr_number == 0` jobs on the same repo (rapid successive pushes) have
    no described concurrency control — a slower job finishing after a
    faster one could move the cursor backward. Safe-direction (causes
    redundant reprocessing, never silent data loss), not built out further
    here.
  - **CPU-bound work runs off the event loop:** Tree-sitter parsing
    (`chunk_file()`, `extract_graph()`) and local ONNX inference
    (`embed()`) are synchronous, CPU-bound calls; running them directly on
    the Worker's single `asyncio` event loop would block it for the
    duration of each call, undermining `apps/worker/dispatch.py`'s
    existing stuck-job-warning mechanism. All three run via
    `asyncio.to_thread` — the same mechanism `adapter/github/auth.py`'s
    RS256 signing and every analyzer sub-step
    (`adapter/analyzers/subprocess_runner.py`) already use for
    synchronous/blocking work in this exact codebase, sharing the same
    process-wide default thread pool (no explicitly-sized executor exists
    anywhere in this codebase to reuse instead; pool-sizing/contention
    across concurrently-running jobs is a Phase 8 tuning question, not
    addressed further here).
- **Per-file processing is sequential, not concurrent** (a deliberate
  choice, not an oversight): unlike `adapter/github/diff.py`'s
  semaphore-bounded concurrent blob fetch for a similarly-shaped file set,
  this loop processes one file at a time. Acceptable for this phase given
  the cost ceilings already bound a job's worst-case total duration
  regardless of concurrency, and Postgres connection-pool sizing for
  concurrent upserts is a real, separate question not worth taking on in
  the same phase that first makes this pipeline work at all — revisit at
  Phase 8 if full-run latency in practice proves this too slow.
- Replace `apps/worker/dispatch.py`'s `INDEX`-job stub (currently: log
  "not implemented" and return) with a real call into
  `indexing.pipeline.run_indexing_job`, wiring Phase 2's
  `GitHubTreeFetcher`, Phase 4's `LocalONNXEmbeddingClient`, and Phase 6's
  `IndexingRepository` at the composition root (`apps/worker/main.py`),
  per architecture.md's "wire concrete implementations only at the
  composition root" rule.
- Job-level structured logging per the spec's Observability NFR: `job_id`,
  repo identity, trigger type, commit SHA, files
  processed/skipped/failed, chunks/edges created, duration,
  `used_full_tree_fallback: bool` (an expensive full-tree-fallback run is
  otherwise indistinguishable from a cheap incremental one after the
  fact), and `pr_number` (the one place an operator could otherwise
  correlate a job to a specific PR without cross-referencing GitHub by
  commit SHA).

**Tests:** Integration (`indexing/test_pipeline.py`, real Postgres, using
Phase 2/4's `FakeRepoTreeFetcher`/`FakeEmbeddingClient`) — this dedicated
Integration-tier test is where US3 (chunking), US5 (graph extraction), and
US9 (prefilter) get their mandatory `testing.md` integration coverage,
exercised as part of a real end-to-end job run — alongside US6, US7
(deletion, now actually exercised through the pipeline, not only against
the repository directly), US8, US10 (per-stage isolation: chunk, graph,
embed, upsert, each proven independently — a failure at one stage for one
file never affects another file), the cost-ceiling scenario, a dedicated
case proving a `pull_request`-triggered job never advances
`last_indexed_commit_sha` (closing a gap an independent testing review
found: the `pr_number == 0`-only rule had no named test anywhere), and a
whole-job-abort/resume case (Postgres made unavailable mid-job — asserts
`last_indexed_commit_sha` stays at its prior value and a retried job
reprocesses cleanly). One new E2E scenario
(`tests/e2e/test_indexing_pipeline.py`) driving the full Docker Compose
stack (real embedding model, real Postgres) with a synthetic `push`
webhook against a stubbed GitHub endpoint, asserting chunks/embeddings/
edges land in Postgres for the pushed commit.

**Observability & rollback:** Full job-level logging as specified above.
**Structured logs only, no metrics/alerting — a deliberate, pre-existing
scope boundary, not an oversight this plan introduces (raised in an
independent observability review):** `spec-indexing.md`'s own NFRs
state "no metrics/dashboard system exists in this repo or is specified by
any roadmap phase through Phase 2 — observability here is structured logs
only." **Attribution corrected during a second independent review:** an
earlier draft attributed this "through Phase 2" wording to both
`spec-indexing.md` and `spec-review-pipeline.md` as if it were shared
verbatim text — `spec-review-pipeline.md` actually says "through **Phase
1**" (it predates the Phase 2 spec's existence), so only `spec-indexing.md`
itself states the "through Phase 2" boundary this plan relies on; the
underlying no-metrics-infra conclusion still holds on `spec-indexing.md`'s
own text alone. A job that "completes" while silently failing most of its
files (e.g. a systemic grammar regression failing every file's `chunk`
stage one-by-one — under this plan's per-file-isolated design, a
systemic failure mode fails many files individually, never one batch in
a single shot) is a real risk this logging-only posture doesn't
eliminate — but building alerting infrastructure to close it here would
be new, unrequested scope beyond both PRDs, the same "building it here
would be the inconsistency, not the gap" reasoning
`spec-review-pipeline.md` already applied to its own scope boundary. Two
cheap, no-new-infrastructure mitigations instead: the per-file failure log
detail above (file/language/stage) makes a `grep`-driven manual check
materially more useful than an aggregate count; and the job-level summary
log line logs at `warning` (not `info`) when `files_failed / files_processed
> 0.2` (recommended default threshold, override-able, not a
product-validated number — same status as this plan's other tunables),
so a systemic failure is visible in a normal log scan without requiring
the operator to already suspect one. Proven by a dedicated test asserting
the log level flips to `warning` above the threshold and stays `info`
below it. The `INDEXING_ENABLED` kill switch (Phase 1) stops new jobs being
enqueued without a deploy (scope stated precisely in Phase 1). A full code
revert of this phase's `dispatch.py` change returns to Phase-1-era "log
and mark done" behavior for any newly-claimed jobs — this stops *further*
writes, but **does not clean up rows a buggy release already wrote**
before the revert (found during this plan's own observability review: the
original draft's "loses no data" framing only addressed halting future
writes, not recovering from bad data already persisted, and the spec's own
out-of-scope list excludes admin-triggered reindex/purge). **Documented
manual recovery step, not new automated scope:** an operator/engineer can
directly clear the affected repo's `repo.last_indexed_commit_sha` and
delete its `index_chunk`/`index_graph_node` rows via direct DB access,
which forces that repo's next qualifying push to reprocess it from a
clean-slate incremental run (the same "no existing rows" degenerate case
Phase 6's upsert logic already treats as a normal full-run). This is a
manual runbook step, deliberately not built as an automated
admin-API/UI feature, consistent with the spec's own scope boundary.

**Acceptance:** US1–US11 all pass as integration tests, including US7's
deletion now actually exercised end-to-end through the pipeline with its
own `delete`-stage isolation proven (one removed-file failure doesn't
abort the job); the E2E scenario passes; the cost-ceiling scenario
passes; per-file isolation is proven independently for all four
added/modified stages (chunk/graph/embed/upsert); the `pr_number == 0`-
only commit-advance rule, the `files_failed`-ratio warning-log threshold,
and the whole-job Postgres-failure/resume case all pass.

## Security review

One pass across every new input/trust boundary this plan as a whole
introduces:

- **Push/PR webhook payload's new fields** (`ref`, `commits[]`,
  `head_commit`, `forced`) — already inside Phase 1's existing
  HMAC-signature-verified trust boundary; no new authn/authz surface.
- **Repo content fetch for an arbitrary (non-diff-scoped) path set**
  (Phase 2) — reuses the existing installation-token exchange and now the
  same symlink/submodule mode guard `adapter/github/diff.py` already
  applies (Phase 2's extracted shared helper); worst case is a
  large/malicious repo tree, bounded by this plan's cost ceilings.
  **Realistic threat actor, named explicitly:** `pull_request
  opened`/`synchronize` fires for fork-based PRs too, so an untrusted
  external contributor with zero write access can already make the
  platform fetch and run Tree-sitter parsing plus local embedding compute
  over their own attacker-controlled code (US2) — the same threat class
  Phase 1 already accepts for the LLM-review step, restated here for the
  new parsing/embedding surface. The existing ceilings and Phase 7's
  per-file isolation bound the compute-cost worst case. **A more severe
  worst case than compute cost, found and fixed during an independent
  security review:** without ref-scoping, that same fork contributor's
  indexed content would land in the *same* storage rows a trusted
  default-branch push writes to — a data-poisoning vector against the
  canonical index, not just a cost one. Closed by Assumption 9's
  `pr_number` row-scoping (Phase 6/7): PR-branch content can never
  overwrite a canonical row, full stop, regardless of any ceiling or
  isolation logic.
- **Cross-job-type CPU contention:** local embedding inference (Phase 4)
  is CPU-bound and runs in the same Worker pool as `REVIEW` jobs (per the
  C4 doc's shared-Worker design) — a burst of many near-ceiling `INDEX`
  jobs (triggerable by repeated pushes or fork PRs, though now blunted by
  Phase 1's per-repo rate limit) could still compete for CPU with
  concurrent `REVIEW` job throughput, not just exhaust its own job's
  resources. Not unique to this plan (any new job type sharing the pool
  has this property) and mitigated by horizontal Worker scaling like any
  other capacity concern — named here so it isn't a surprise, not a new
  mechanism this plan builds.
- **Tree-sitter parsing of untrusted repo content** (Phase 3) — a pure
  parsing library, not a subprocess/interpreter; worst case is a parser
  crash (now caught by Phase 7's broadened per-file isolation) or, in
  principle, a pathological-input hang. **Named, accepted residual gap:**
  no hard per-file parse timeout is implemented, and no per-file size
  ceiling beyond the aggregate ceilings exists — a real, symmetric
  limitation `review/prefilter.py` already has today, not a new
  inconsistency this plan introduces. A true hard timeout for a CPU-bound,
  synchronous parse would need a subprocess-per-file architecture, a
  materially heavier design not justified here given Tree-sitter's C-level
  parser has no known practical hang vector on malformed input.
  **Distinct outcome category, not just crash/hang (found during an
  independent security review):** a native, in-process, unsandboxed C
  parser processing fully attacker-controlled bytes (fork-PR content, per
  the bullet above) also carries a memory-safety exploitation risk
  (grammar-code OOB read/buffer overflow) beyond crash/hang — a
  categorically worse outcome than either. **Blast radius, stated
  precisely (a later review found the original analogy understated it):**
  unlike Semgrep and Gitleaks, which run as a separate OS subprocess
  (`adapter/analyzers/subprocess_runner.py`) so a memory-corruption bug in
  their engine is contained there, Tree-sitter runs directly inside the
  Worker's own process (per Phase 7's `asyncio.to_thread` wiring) — the
  same process holding that job's GitHub installation token and Postgres
  credentials. A successful exploit is a full Worker-process compromise,
  not merely a parser crash; this is a materially worse worst case than
  the subprocess-isolated tools it was previously compared to. Not
  mitigated by anything this plan builds — true sandboxing (seccomp, a
  resource-limited subprocess per parse) would be a
  materially heavier architecture change out of scope for this plan,
  revisit if Tree-sitter's own security advisories ever warrant it.
- **`index_chunk` persists full repository source code in Postgres,
  indefinitely, with no retention/pruning policy** (found during this
  plan's own security review — the original draft's "same tenancy posture
  as every existing table" framing didn't engage with this: no existing
  table before this plan stores full file content, so this is the
  platform's first at-rest store of raw proprietary source, including any
  secrets/PII embedded in code or comments, and a materially larger blast
  radius from a Postgres/backup compromise than anything indexed today).
  **Accepted posture, stated explicitly rather than left implicit:**
  consistent with [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md)'s
  self-hosted trust model, protecting this data at rest (encryption,
  access control, backup policy) is the customer's own infrastructure
  responsibility, the same as it already is for every other table in
  their Postgres instance — this plan adds no new encryption/access
  mechanism of its own, matching how the rest of this schema already
  works. The **absence of a retention/pruning policy** is named as an
  accepted, unbounded-growth gap, the same category already named for
  `webhook_delivery` in this codebase — not solved here, not silently
  assumed away either.
- **Indexing-specific enqueue rate limit** (Phase 1, Assumption 10) —
  the shared, disabled-by-default `REVIEW` rate limiter left `INDEX`
  enqueues (materially more expensive per job) with zero throttling
  against a repeated-force-push or fork-PR-`synchronize`-spam cost vector.
  One new, enabled-by-default, repo-keyed bucket closes it — not
  completely: a free, repeatable fork-PR flood can still sustain denial
  of `INDEX` enqueues for the repo indefinitely (see Assumption 10 for
  why this is accepted anyway, and why two more finely-keyed alternatives
  were tried and reverted for introducing worse failure modes). See
  Phase 1 for the fix; note `InMemoryRateLimiter`'s inherited
  per-instance (not distributed) limitation, also named there.
- **Local embedding model** (Phase 4) — no network call, no credential at
  inference time; model-weight provenance is addressed by build-time
  checksum pinning (Assumption 4), closing the gap this plan's own review
  found (a mutable model-name reference alone doesn't guard against a
  tampered artifact running in-process against every customer's code).
- **New Postgres tables** (Phase 6) — same tenancy posture as every
  existing table: scoped by `repo_id` FK, no per-repo row-level isolation
  beyond that (Phase 6 tenancy/RBAC is unchanged, future roadmap work).
- **Kill switch** (Phase 1) — a read-only operational control at the
  webhook route; no new write surface. Its precise scope (new enqueues
  only, doesn't drain already-queued jobs) is stated explicitly in Phase 1
  rather than implied, and it now **defaults to `False`** (Assumption 11)
  so the safe-rollout narrative in Compatibility & rollout holds without
  requiring an operator to remember an undocumented override step.

## Compatibility & rollout

- Phase 6's Alembic migration is additive-only — safe to deploy ahead of
  Phase 7's code change, using the same "migrate first, deploy code
  second" sequencing an additive migration always allows, **on the
  precondition stated in both Phase 6 and here: the kill switch stays off
  until Phase 7 is ready.** (**Citation corrected during an independent
  compatibility review:** this was previously attributed to "this repo's
  Phase 1 plan" — `docs/plans/` currently contains only this plan file, so
  that specific precedent isn't verifiable against the repo as it stands;
  the sequencing stands on its own merits regardless of prior precedent.)
- Phase 7's `apps/worker/dispatch.py` change is a real behavior change for
  any already-deployed Phase-1-era build (the spec's own Rollout note) —
  the `REVIEW_WEBHOOK_API_INDEXING_ENABLED` kill switch (Phase 1,
  defaulting to `False` per Assumption 11) lets an operator stage this
  safely: Phases 1–6 ship inert by default (no `INDEX` jobs enqueued
  unless an operator opts in early), then Phase 7 deploys and the operator
  makes the affirmative choice to flip the switch on when ready, rather
  than one atomic cutover or a silent auto-enable.
- No existing API/schema contract breaks: `GitHubWebhookEnvelope`/
  `Repository` gain new optional fields (backward compatible with any
  payload missing them, matching the envelope's existing
  `extra="ignore"` discipline); `JobType.INDEX` already exists unchanged;
  `repo.last_indexed_commit_sha` is nullable.
- **Dependency-failure behavior, covering all three external dependencies
  this plan introduces or touches (expanded during this plan's own
  compatibility review, which found only the GitHub-outage case was
  originally analyzed):**
  - *GitHub API* outage/slowness during Phase 2/7's fetch calls is
    contained by `adapter/github/http.py`'s existing shared
    timeout/retry mechanism (reused, not newly built, per Phase 2 —
    every new call routes through it the same way `auth.py`/`diff.py`/
    `posting.py` already do) plus the Worker's existing stuck-job-warning
    mechanism (`apps/worker/dispatch.py`) — no new failure-containment
    mechanism needed. A degraded-but-"successful"
    response — the Trees API's own truncation at 100,000 entries/7MB — is
    a distinct case Phase 2 gives its own typed `RepoTreeTruncatedError`
    for, treated as job-fatal (logged, `FAILED`) rather than silently
    indexing a partial tree.
  - *The local embedding model* failing after Worker startup (not just at
    boot, which Phase 4's startup check covers) raises a typed
    `EmbeddingError` (Assumption 12) that Phase 7's per-file `embed` stage
    catches — a failure only ever skips the one file being embedded at
    that moment, never a wider batch (there is no job-wide batch; see
    Phase 7).
  - *Postgres* unavailable or slow mid-job during Phase 6/7's
    upserts: because `last_indexed_commit_sha` is only advanced once,
    after the whole job's files are processed (Phase 7's clarified
    ordering), a mid-job Postgres failure that aborts the job entirely
    leaves that value at its prior state — the next push's delta
    calculation naturally reprocesses from the last known-good commit
    rather than silently leaving a gap. Rows already committed by
    per-file upserts before the outage stay in place (each file's upsert
    is its own transaction), which is safe because upserts are idempotent
    on `(repo_id, pr_number, file_path, chunk_index)` / `(repo_id,
    pr_number, file_path, symbol_name)` (**corrected during a second
    independent review**, which found this bullet still referenced the
    pre-`pr_number`-fix two-part key, inconsistent with Phase 6's actual
    schema) — a retried job re-processing the same file overwrites
    cleanly rather than duplicating.
- The change is undoable if it turns out wrong via the kill switch (stops
  new work) plus a code revert (stops all `INDEX` processing) — with the
  one named, accepted limitation that neither cleans up bad data already
  written by a buggy release; see Phase 7's documented manual recovery
  step for that case.

## Traceability: PRD user story → test

| Story | Phase | Test path | Level |
| --- | --- | --- | --- |
| US1 | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| US2 | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| US3 | 3 (unit) + 7 (integration) | `indexing/test_chunk.py` + `indexing/test_pipeline.py` | Unit + Integration |
| US4 | 4 + 6 | `adapter/embedding/test_local_onnx.py` (produced) + `adapter/storage/test_indexing_repository.py` (stored) | Integration |
| US5 | 5 (unit) + 7 (integration) | `indexing/test_graph.py` + `indexing/test_pipeline.py` | Unit + Integration |
| US6 | 6 + 7 | `adapter/storage/test_indexing_repository.py` + `indexing/test_pipeline.py` (fakes) | Integration |
| US7 | 6 + 7 | `adapter/storage/test_indexing_repository.py` + `indexing/test_pipeline.py` | Integration |
| US8 | 2 + 7 | `adapter/github/test_tree.py` + `indexing/test_pipeline.py` | Integration |
| US9 | 3 (unit) + 7 (integration) | `indexing/test_prefilter.py` + `indexing/test_pipeline.py` | Unit + Integration |
| US10 | 3, 5 (raise) + 7 (per-file isolation: chunk/graph/embed/upsert) | `indexing/test_chunk.py`, `indexing/test_graph.py` + `indexing/test_pipeline.py` | Unit + Integration |
| US11 | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| NFR — embedding model startup check | 4 | `adapter/embedding/test_startup_check.py` | Integration |
| NFR — `EmbeddingError` on inference failure | 4 | `adapter/embedding/test_local_onnx.py` | Integration |
| NFR — model checksum verification | 4 | Docker build-time check (not pytest) | Build-time |
| NFR — kill switch (default `False`, scope: new enqueues only) | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| NFR — indexing enqueue rate limit (shared across trigger types) | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| NFR — `pr_number == 0`-only commit-advance rule | 7 | `indexing/test_pipeline.py` | Integration |
| NFR — `pr_number` row-scoping (no fork-PR overwrite of canonical rows) | 6 | `adapter/storage/test_indexing_repository.py` | Integration |
| NFR — graph-edge uniqueness/delete-then-reinsert | 6 | `adapter/storage/test_indexing_repository.py` | Integration |
| NFR — symlink/submodule tree-entry filtering | 2 | `adapter/github/test_tree.py` | Integration |
| NFR — Trees API truncation → `RepoTreeTruncatedError` | 2 | `adapter/github/test_tree.py` | Integration |
| NFR — partial blob-fetch failure fails whole call | 2 | `adapter/github/test_tree.py` | Integration |
| NFR — `adapter/github/test_diff.py` regression (Phase 2 refactor) | 2 | `adapter/github/test_diff.py` | Integration |
| NFR — existing `REVIEW` webhook tests regression (Phase 1 refactor) | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| NFR — `IndexingRepositoryError` on a real Postgres failure | 6 | `adapter/storage/test_indexing_repository.py` | Integration |
| NFR — `delete` stage isolation (one removed-file failure doesn't abort the job) | 7 | `indexing/test_pipeline.py` | Integration |
| NFR — `files_failed`-ratio warning-level log | 7 | `indexing/test_pipeline.py` | Integration |
| NFR — whole-job Postgres-failure/resume | 7 | `indexing/test_pipeline.py` | Integration |
| NFR — cost ceilings | 7 | `indexing/test_pipeline.py` | Integration |
| All (happy path) | 7 | `tests/e2e/test_indexing_pipeline.py` | E2E |
