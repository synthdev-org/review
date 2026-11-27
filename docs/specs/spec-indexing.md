# Phase 2 — Incremental Indexing PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-04

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 2 indexing bullet
("Incremental indexing job: AST-aware chunking, per-node embeddings,
call/import graph — kept fresh on every push") into a concrete spec. Scoped
by [ADR-0007](../adr/0007-llm-free-indexing-deferred-rag-enrichment.md): the
indexing job makes **zero generative LLM calls** — this is a hard
non-functional requirement, not an implementation detail left to engineering
discretion. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`); requirements are stated as user stories with
`Given/When/Then` acceptance criteria per `testing.md`'s mandatory rule.

**Companion, not a duplicate, of [`spec-review-pipeline.md`](spec-review-pipeline.md).**
That spec covers Phase 1's review pipeline (webhook → bare diff → LLM →
posted comments). This spec covers only the **indexing job** — fetching
repo content, chunking, embedding, and building the call/import graph. The
Context Builder that *queries* this index during review, and the review
pipeline's upgrade from bare-diff to built-context, are the *other* Phase 2
capability, specified separately once this indexing spec is built — see
"Out of scope" below.

## Problem & Goal

**Honest framing, not inflated:** this spec's own deliverable has **no
independently observable end-user value**. Nothing a PR author sees changes
because this ships — Phase 1's review pipeline keeps consuming the bare
diff until the companion Context Builder/review-upgrade spec lands and
starts querying what this job builds. This is enabling infrastructure, and
it's described honestly as such rather than dressed up with a fabricated
user story for "the index" itself.

**Where the real end-user value eventually lands:** the PR author, once the
companion spec ships, gets a review that understands blast radius (what
else in the repo calls a function whose signature just changed) instead of
seeing only the changed hunk in isolation — [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)
states the concrete requirement this chain exists to satisfy: *"a PR review
needs the index current as of the exact commit under review, ideally within
seconds of push."* This spec is the half of that chain that keeps the index
current; the companion spec is the half that makes review actually use it.

**Goal / success metric, capability-based (no fabricated usage/adoption
metric, matching Phase 1's own stance):** for a repo configured with this
platform, a push to the default branch or to an open PR's head branch
results in the index (chunks + embeddings + call/import graph, scoped to
the languages in scope below) reflecting that exact commit, without any
generative LLM call, within the cost ceilings this spec sets (see
Non-Functional Requirements).

## Scope

### In scope

- **Language scope: TypeScript, PHP, and Go** (decided — matches the
  platform's actual target customer repos, not this platform's own
  implementation language, which is Python per
  [ADR-0002](../adr/0002-implement-in-python.md) and irrelevant to what gets
  *reviewed*). A file in any other language is excluded from this phase's
  indexing entirely (see US9) — not indexed as an unstructured blob, not
  partially chunked. Adding a fourth language later is a new Tree-sitter
  grammar behind the same chunker interface, per architecture.md's "new
  adapter = zero domain changes" rule — not a redesign.
- **AST-aware chunking** (Tree-sitter-based) for in-scope languages,
  splitting at syntactic boundaries (function/method/class/interface/type
  declarations, re-attaching stripped context like imports and enclosing
  class) rather than fixed line/character windows — per
  [`rag-techniques.md`](../reference/rag-techniques.md) §1's cAST-style
  rationale.
- **Chunk embedding via a bundled local model** (decided — see below), never
  a customer-configured/BYO endpoint for this phase.
- **Call/import graph via name-based heuristic matching** (decided — see
  below): parse each function/method/class/interface/exported-symbol
  definition into the index keyed by `(repo, file, symbol name, language)`;
  resolve a call/import edge by matching identifier names against the
  repo's own indexed symbols. No per-language module-resolution engine (no
  Go module-graph resolution, no Composer autoload resolution, no
  TypeScript tsconfig path-alias resolution).
- **Incremental delta reindex**, keyed on the pushed commit: only
  added/modified files are re-chunked/re-embedded/re-graphed; removed files
  are pruned (US6, US7) — never a full repo reindex on every push.
- **Two trigger events** (decided): a `push` to the repo's default branch,
  and a `pull_request` webhook with action `opened` or `synchronize`
  (mirroring `adapter/github/triggers.py`'s existing review-trigger
  actions) — the head commit of the PR is what gets indexed in the latter
  case, satisfying ADR-0003's per-PR-commit freshness requirement.
  "Default branch" is resolved from the `push` payload's own
  `repository.default_branch` field (compared against the payload's `ref`),
  not a persisted config value — `adapter/storage/models.py`'s `Repo` model
  has no `default_branch` column today and this spec does not add one,
  since the payload already carries the answer on every delivery.
- **File-change detection via the push payload**, with a full-tree
  fallback (decided — see below): GitHub's `push` webhook payload's own
  per-commit `added`/`removed`/`modified` lists are the primary source of
  "what changed"; a full Git Trees diff against the repo's last-indexed
  commit is the fallback when the payload's `commits` array looks
  truncated (GitHub caps it at 20 entries for large pushes) or the push is
  non-fast-forward (a force-push/rebase, where the payload's per-commit
  lists don't reliably describe the net file-level change).
- **New storage schema**: a chunk table (content, language, symbol name,
  file path, byte range, embedding vector column), a graph-edge table
  (source symbol → target symbol reference), and a per-repo
  last-indexed-commit tracker — all new Alembic migrations on top of the
  Phase 0 baseline (`0001_baseline_schema.py`), additive only.
- **`INDEX` job real implementation**: `JobType.INDEX` already exists in
  `adapter/storage/models.py` and is enqueue-able, but
  `apps/worker/dispatch.py` currently just logs it as unimplemented and
  marks it `DONE` — this spec replaces that stub with the real pipeline.
- **Webhook envelope extension**: `adapter/github/schemas.py`'s
  `GitHubWebhookEnvelope` currently models no `push`-specific fields (only
  `pull_request`/`action`) — this spec adds the `ref`, `before`/`after`
  commit SHAs, `commits[].added/removed/modified`/`head_commit` fields a
  `push` payload carries, and `repository.default_branch` (see the trigger
  bullet above — the `Repository` model currently only carries
  `full_name`) — the same "model only what's consumed, ignore the rest"
  discipline the existing envelope already follows.
- **Failure isolation**: one file's parse failure (a malformed/adversarial
  file, an edge case the Tree-sitter grammar doesn't handle) doesn't fail
  the whole indexing job — mirrors Phase 1's static-analysis failure
  isolation (`spec-review-pipeline.md` US9).
- **Idempotent indexing**: a redelivered `push`/`pull_request` webhook must
  not double-process — reuses the existing `WebhookDeliveryRow`
  anti-replay mechanism (`adapter/storage/tenancy.py`), which is already
  keyed on GitHub's delivery ID regardless of event type, not something
  this spec needs to rebuild.

### Out of scope (deferred to a later spec or roadmap phase)

- **The Context Builder and its blast-radius queries against this index,
  and the review pipeline's upgrade from bare diff to built context.** This
  is the *other* Phase 2 capability — a separate spec, written once this
  indexing spec's schema and query shape actually exist to build against.
- **Docstring/summary embedding, hierarchical (RAPTOR-style) summarization,
  LLM-based entity/relationship graph extraction.** Explicitly deferred per
  [ADR-0007](../adr/0007-llm-free-indexing-deferred-rag-enrichment.md) — not
  this phase, not a default behavior of any later phase either, only an
  opt-in capability if it ever ships.
- **Precise per-language symbol resolution** (real Go module-graph
  resolution, Composer autoload resolution, tsconfig-aware TypeScript
  module resolution). Decided against for this phase — name-based
  heuristic matching only (see Scope → In scope). Revisit only if the
  heuristic's false-positive/false-negative rate proves it insufficient
  once the Context Builder spec is actually querying it.
- **Cross-repo graph edges.** The call/import graph is scoped to one repo;
  a multi-repo org (Phase 6 tenancy) gets one independent index per repo,
  never a merged graph across repos.
- **On-demand/admin-triggered reindex** ("reindex this repo now," bypassing
  the push trigger). No admin API/UI exists yet (Phase 5) to expose such a
  control — see Rollout & Release Safety for the accepted gap this leaves.
- **A customer-configurable/BYO embedding endpoint.** Decided against for
  this phase (see Scope → In scope) — the bundled local model is the only
  option; a swappable/BYO embedding endpoint is a real possible future
  Should-have, not built now.
- **Languages beyond TypeScript, PHP, Go.** A fourth language is new scope
  for a later spec, not a gap in this one.

### Priority

- **Must have:** the whole push/PR-webhook → trigger → claim → fetch →
  detect-changes → chunk → embed → graph-extract → upsert → prune-deleted
  loop for the three in-scope languages — this collectively *is* the
  spec's goal, not divisible further without failing it. Idempotent
  indexing and failure isolation are Must (not Should/Could), for the same
  reason Phase 1 made their review-pipeline equivalents Must: a flaky
  webhook redelivery or one malformed file must not double-spend indexing
  work or zero out an entire repo's index update.
- **Should have:** the full-tree-diff fallback for truncated/force-push
  payloads (US8) — materially reduces index-drift risk on large or
  history-rewriting pushes, but the platform is still functional without
  it for the overwhelmingly common small-push case.
- **Could have:** a per-repo indexing-cost/ceiling override (vs. one fixed
  platform-wide default) — a fixed sane default (see Non-Functional
  Requirements) still satisfies the goal.

## Requirements

Stated as user stories per `testing.md`'s mandatory format.

**US1 — Default-branch push keeps the index fresh**
As a platform operator, I want every push to a repo's default branch to
update that repo's index, so that the baseline index a future
blast-radius query walks is never more than one push stale.

- Given a verified `push` webhook payload whose `ref` matches that same
  payload's `repository.default_branch`, When the Webhook API processes
  it, Then exactly one `INDEX` job is enqueued carrying the pushed `after`
  commit SHA and the per-commit added/removed/modified file lists from the
  payload.

**US2 — PR head-branch push indexes the exact reviewed commit**
As a PR author, I want the commit my PR actually points at to be indexed,
so that a future context-aware review of my PR reasons about my code as it
actually is, not a stale snapshot.

- Given a verified `pull_request` webhook payload with action `opened` or
  `synchronize`, When the Webhook API processes it, Then exactly one
  `INDEX` job is enqueued carrying the PR's head commit SHA (independent of
  and in addition to whatever `REVIEW` job the existing trigger table also
  enqueues for the same event).

**US3 — Structural chunking for in-scope languages**
As a platform operator, I want TypeScript/PHP/Go source split at real
syntactic boundaries, so that a stored chunk is always a coherent,
self-contained unit instead of an arbitrary line window.

- Given a `.ts`/`.tsx`, `.php`, or `.go` file's content, When the chunker
  processes it, Then the file is split into chunks at
  function/method/class/interface/type-declaration boundaries (per
  `rag-techniques.md`'s cAST rationale), each within the configured token
  budget (~512 tokens with 64-token overlap — see "Open assumptions
  pending validation" #2 for the exact default and its rationale), with
  enclosing context (surrounding class, relevant imports) re-attached so
  the chunk is self-contained.

**US4 — Chunk embeddings are stored for retrieval**
As a platform operator, I want every stored chunk to carry an embedding
vector, so that a future semantic-similarity query has something to search
against.

- Given a chunk produced by US3, When the embedding step runs, Then the
  bundled local embedding model (no network call, no generative LLM call)
  produces a fixed-dimension vector for the chunk's content and both the
  chunk and its vector are upserted into Postgres/pgvector.

**US5 — Call/import graph edges via name-based matching**
As a platform operator, I want a graph of which symbols call/import which
other symbols in the same repo, so that a future blast-radius query has
edges to walk.

- Given a parsed file's function/method/class/interface/exported-symbol
  definitions and its call/import expressions, When the graph-extraction
  step runs, Then each definition is upserted as a node keyed by `(repo,
  file, symbol name, language)`, and each call/import expression whose
  identifier matches an existing node's symbol name in the same repo is
  upserted as an edge — no match found is not an error, just no edge
  created (a call to an external/third-party symbol, or one static
  name-matching simply misses).

**US6 — Incremental delta reindex processes only changed files**
As a platform operator, I want a push that touches 3 files in a
10,000-file repo to only reprocess those 3 files, so that indexing cost
and latency scale with the size of a push, not the size of the repo.

- Given an `INDEX` job whose payload lists specific added/modified file
  paths (from US1/US2's trigger), When the indexing job runs, Then only
  those paths are fetched, re-chunked, re-embedded, and have their
  outgoing graph edges rebuilt — files untouched by the push are not
  re-fetched, re-chunked, or re-embedded. If a modified file renames or
  removes a symbol other files' edges pointed at, those other files are
  **not** reparsed to fix up the reference — the now-unmatched edge is left
  in place as a dangling reference, resolved as "no match" the same way
  US5's never-matched edges and US7's deletion case are (never a full-repo
  rescan to keep every cross-file edge current).

**US7 — Deleted files are pruned from the index**
As a platform operator, I want a file removed from the repo to disappear
from the index too, so that a future query never surfaces chunks or graph
nodes for code that no longer exists.

- Given an `INDEX` job whose payload lists a removed file path, When the
  indexing job runs, Then every chunk and graph node keyed to that
  `(repo, file)` is deleted, along with any graph edge whose source node
  was just deleted (an edge whose *target* was that file's now-deleted
  symbol is left in place as a dangling reference, resolved as "no match"
  by any future query the same way a never-matched edge is per US5 — not
  specially cleaned up, since finding every referencing file would require
  the same full-repo scan this design deliberately avoids).

**US8 — Truncated or force-push payloads fall back to a full-tree diff**
As a platform operator, I want a large push or a force-push to still
produce a correct index, so that the fast-path optimization in US6 never
silently produces a wrong index on the less common case.

- Given a `push` payload whose `commits` array is at GitHub's 20-entry cap,
  or a payload representing a non-fast-forward update, When the indexing
  job runs, Then it fetches a full Git Trees listing at the pushed commit,
  diffs it against the repo's stored last-indexed-commit tree, and uses
  that diff (not the payload's own per-commit lists) as the
  added/modified/removed file set.

**US9 — Unsupported-language and ignored files never reach the indexer**
As a platform operator, I want lockfiles, vendored paths, generated files,
and files in languages outside this phase's scope excluded automatically,
so that indexing cost isn't spent on content that will never usefully
answer a code query.

- Given a pushed file list that includes `go.sum`, a `node_modules/`
  path, and a `.py` file, When the indexing job's pre-filter runs, Then
  none of those three reach the chunker — only files matching an in-scope
  extension (`.ts`/`.tsx`, `.php`, `.go`) and not matching an ignore
  pattern (reusing `review/prefilter.py`'s existing ignore-pattern
  categories) proceed.

**US10 — One file's parse failure doesn't fail the whole indexing job**
As a platform operator, I want one malformed or unparseable file to not
zero out an entire push's index update, so that one edge-case file doesn't
cost me the value of indexing every other file in the same push.

- Given one file in a push's file set raises a parse exception (e.g. a
  Tree-sitter grammar edge case), When the indexing job completes, Then
  every other file in that push is still chunked/embedded/graphed and
  upserted, and the failure is logged with the specific file path rather
  than failing the job.

**US11 — Duplicate push delivery doesn't double-index**
As a platform operator, I want a redelivered `push` or `pull_request`
webhook to never enqueue a second `INDEX` job for the same commit, so that
indexing cost isn't spent twice on the same content.

- Given the same GitHub delivery ID is received twice, When both
  deliveries are processed, Then only one `INDEX` job exists for that
  delivery (identical mechanism to `spec-review-pipeline.md`'s US2, reused
  unchanged).

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- Every new I/O call this spec introduces (GitHub Trees/Blobs API calls,
  Postgres upserts) takes an explicit timeout, per `architecture.md`.
- **Cost ceilings (assumption pending validation — no real-repo load data
  exists yet to set these precisely; recommended starting defaults, tuned
  during implementation):**
  - Incremental (per-push) run: reuse `review/prefilter.py`'s existing
    500-file / 200MB ceilings — an incremental push's changed-file set is
    the same shape of input Phase 1's prefilter already bounds.
  - Full/initial run (the first push after a repo is registered, when no
    prior index exists — see Rollout & Release Safety): a separate, larger
    ceiling (recommended default: 20,000 files / 2GB aggregate) since a
    repo's total size can vastly exceed one push's diff size. Files beyond
    either ceiling are dropped and logged at `warning`, mirroring
    `prefilter.py`'s existing non-fatal drop pattern — never a job failure.
- **Security:** Tree-sitter is a pure parsing library, not a subprocess or
  an interpreter — parsing untrusted repo content here carries a
  materially lower risk profile than Phase 1's static-analysis step
  (`review/static_analysis.py`), which does exec real analyzer binaries.
  No new credential type is introduced; GitHub content fetch reuses the
  existing installation-token exchange.
- **No latency SLA is set**, matching Phase 1's own stance — this spec's
  goal is index correctness within the cost ceilings above, not a speed
  target; a real target belongs to Phase 8 (production hardening) once a
  real deployment exists to measure against.
- **Idempotency:** covered by US11.
- **Local embedding model startup check:** the Worker verifies the bundled
  local embedding model loads successfully at startup, failing fast with a
  clear error rather than the first `INDEX` job silently hanging or failing
  deep inside US4 — the same fail-fast pattern
  `adapter/storage/pgvector_check.py` already applies to the pgvector
  extension's presence, applied here to Unit D's own named risk (a
  model-weights bundling/download issue).

### Privacy / Compliance

Stronger privacy story than Phase 1's LLM-review step, worth stating
explicitly: embedding runs against a **bundled local model**, so unlike
Phase 1's LLM-review call (customer's diff content sent to whatever LLM
endpoint the operator configured), no repo content or embedding leaves the
deployment for this spec's indexing step at all — not even to a
customer-configured endpoint. Chunk content and vectors are stored only in
the customer's own Postgres, per [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md).

### Observability

- Every indexing job's start/completion/failure is logged with `job_id`,
  repo identity, trigger type (`push`/`pull_request`), commit SHA, files
  processed/skipped/failed counts, chunks/edges created, and duration —
  extending the existing `apps/worker/logging_config.py` structured-logging
  setup, the same shape Phase 1's steps already log.
- No metrics/dashboard system exists in this repo or is specified by any
  roadmap phase through Phase 2 — observability here is structured logs
  only, consistent with `spec-review-pipeline.md`'s own stance.

### Rollout & Release Safety

- **New Alembic migrations, additive only** — new chunk/graph-edge/
  last-indexed-commit tables on top of the Phase 0 baseline; no existing
  table is altered in a breaking way.
- **No separate "initial full index" job type or trigger (decided by
  design):** the incremental indexer's own upsert logic treats "no
  existing chunk rows for this repo" as the degenerate case of "every
  in-scope file changed" — the same code path as a normal incremental run,
  just with a larger file set and the full-run cost ceiling from
  Non-Functional Requirements applied instead of the incremental one.
  Simpler than adding a second job type or a repo-provisioning hook, and
  it's exactly what `rag-techniques.md` §6's upsert-keyed pattern already
  implies for free.
- **Accepted gap, named rather than left implicit:** a repo registered
  before this ships (or one that simply isn't pushed to for a while after
  it ships) has an empty index until its next qualifying push — there is
  no backfill mechanism and no admin-triggered "reindex now" control in
  this phase (Phase 5 territory, once an admin API exists to expose it).
  The companion Context Builder spec must treat "no index data for this
  repo yet" as a normal, non-error state, not an invariant violation.
- **Backwards compatibility:** `apps/worker/dispatch.py`'s current
  `INDEX`-job handling (log and mark `DONE`, no real work) is replaced
  with real processing — additive from the Worker's own external
  contract (still consumes from the same queue, same job type), but a
  real behavior change for anyone who deployed a pre-Phase-2 build already;
  flagged here so it's deliberate, not a surprise.
- **Kill switch (decided):** a single bootstrap config flag
  (`INDEXING_ENABLED`, default `true`) checked at the trigger-enqueue point
  in `apps/webhook_api/webhook_routes.py` (alongside the existing
  `is_enqueue_trigger` check) — when `false`, `push`/`pull_request`
  webhooks are still accepted and delivery-recorded exactly as today, but
  no `INDEX` job is enqueued. This is the indexing-pipeline equivalent of
  Phase 1's `REVIEW_WORKER_DRY_RUN` flag (`spec-review-pipeline.md`'s
  Rollout & Release Safety section): a cheap, real operational safety
  valve an operator can flip if a bad Tree-sitter grammar edge case or a
  misbehaving embedding step starts consuming Worker capacity or filling
  Postgres with bad data, without needing to redeploy or touch the
  webhook's trust boundary. A single flag, not per-job-type-and-per-repo
  granularity, for the same "no interface without a second real
  implementation already in view" reason Phase 1's dry-run flag stayed a
  single boolean.

## UX & Design

N/A — this spec has no user-facing surface. The indexing job is a
backend-only capability with no rendered output of its own; its eventual
observable effect (better review context) is entirely mediated through the
companion Context Builder/review-upgrade spec, which is where a UX section
will actually apply.

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author, `R`/`A` on every existing
  ADR's RACI table; no separate design/eng/QA/legal stakeholders exist for
  this project at this stage.
- **Acceptance:** every `US*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios); `mypy --strict`/`ruff`
  clean per `architecture.md`; the goal statement's cost ceilings are
  exercised by at least one test fixture large enough to trigger them.

## Phases / Units

No hard dependency on `spec-review-pipeline.md`'s pipeline-step framework —
this spec's orchestration is a separate, simpler linear pipeline (fetch →
detect-changes → chunk → embed → graph-extract → upsert), not a
step-registry DAG with independently-registrable sub-steps the way
static-analysis's Gitleaks/Semgrep pair is. **Open note for the
implementing/planning agent, not resolved here:** whether that orchestration
reuses `review/step.py`'s `Step`/`StepContext` framework (promoting it out
of the `review` package to a shared location, since indexing would be a
second real consumer) or is built as its own lightweight sequence is an
implementation-architecture call, not a product requirement — flagged so
it isn't silently decided by whichever engineer touches it first.

- **Unit A — Push/PR-webhook trigger wiring.** Extends
  `adapter/github/schemas.py`'s `GitHubWebhookEnvelope` with push-specific
  fields (`ref`, `before`/`after`, `commits[]`, `head_commit`) and its
  `Repository` model with `default_branch`, plus
  `adapter/github/triggers.py`'s trigger table with an index-trigger
  lookup. Depends on: nothing new. Risk: low — same declarative-table
  pattern Phase 1 already proved out. **Acceptance:** US1, US2, US11's
  `Given/When/Then` pass as integration tests.
- **Unit B — Repo content fetch for indexing.** A new
  `ingestion`-owned Protocol (fetch a file listing + content at a given
  commit; fetch content for a specific path set) implemented by
  `adapter/github`, extending the existing Trees/Blobs API usage
  `adapter/github/diff.py` already has, generalized beyond a PR diff's
  changed-file set to an arbitrary path set. Depends on: nothing new
  (parallel to Unit A). Risk: low-medium — the Blobs/Trees API calls
  already exist; new is the "arbitrary path set, not diff-scoped" shape.
  **Acceptance:** US8's full-tree-diff fallback passes as an integration
  test against a recorded GitHub cassette.
- **Unit C — AST-aware chunker (TypeScript, PHP, Go).** Pure,
  no-I/O logic living in the `indexing` domain package (Tree-sitter is a
  parsing library, not I/O — see architecture.md's infra-import list,
  which Tree-sitter isn't on). Depends on: nothing new. Risk: medium — new
  grammar integration for three languages at once, and the first
  in-repo use of Tree-sitter. **Acceptance:** US3, US9, US10's
  `Given/When/Then` pass as unit tests against language-specific fixtures.
- **Unit D — Local embedding client.** A new `EmbeddingClient` Protocol
  (defined in `indexing`, its consumer) plus a local-model adapter
  (`adapter/embedding`, new package) running the bundled ONNX model
  in-process. Depends on: nothing new (parallel to Unit C). Risk: medium —
  first non-LiteLLM model-inference dependency in this codebase; needs a
  new `pyproject.toml` dependency and a model-weights bundling/download
  strategy decided at implementation time. **Acceptance:** US4's
  `Given/When/Then` passes as an integration test asserting a real
  fixed-dimension vector is produced and stored; the Non-Functional
  Requirements' startup-check requirement passes both when the model loads
  successfully and when it's deliberately made unloadable (fails fast with
  a clear error, per `pgvector_check.py`'s precedent).
- **Unit E — Call/import graph extraction.** Depends on: Unit C's parse
  trees. Risk: medium — first graph-shaped domain logic in this codebase;
  correctness of "what counts as a definition vs. a reference" per
  language needs real fixtures per language, not just one. **Acceptance:**
  US5's `Given/When/Then` passes as a unit test (pure logic: parsed
  definitions/references in, nodes/edges out, per US5's own text — the
  matching itself is pure, no I/O) against fixtures for all three
  languages.
- **Unit F — Storage schema + delta/deletion logic.** New Alembic
  migration (chunk table with a `vector` column, graph-edge table,
  per-repo last-indexed-commit tracker) plus the upsert/prune repository
  logic. Depends on: Units C, D, E for the shapes being stored. Risk:
  medium — first `vector`-typed column and first graph-edge table in this
  schema; deletion logic (US7) needs care to avoid the dangling-edge case
  becoming a silent correctness bug rather than the accepted, named
  limitation it's specified as. **Acceptance:** US6, US7's `Given/When/Then`
  pass as integration tests against a real Postgres/pgvector
  (`testcontainers-python`, per `testing.md`).
- **Unit G — Indexing job pipeline + Worker dispatch.** Orchestrates
  Units A–F into the real `INDEX`-job handling that replaces
  `apps/worker/dispatch.py`'s current stub. Depends on: A, B, C, D, E, F
  all being ready — the integration unit that proves this spec's own
  goal end-to-end. Risk: medium — the first unit that exercises the full
  chain together, including the cost-ceiling and full-tree-fallback
  branches (US8). **Acceptance:** US1–US11 all pass as integration tests,
  plus the full-stack E2E scenario in Testing / Key Scenarios passes.

## Testing / Key Scenarios

Per `testing.md`'s pyramid.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US1 | A `push` to the default branch enqueues exactly one `INDEX` job with the right commit/file-list payload | `apps/webhook_api/test_webhook_routes.py` (extended) | Integration (real Postgres via testcontainers) |
| US2 | `pull_request opened`/`synchronize` enqueues an `INDEX` job for the head commit, alongside the existing `REVIEW` job | `apps/webhook_api/test_webhook_routes.py` (extended) | Integration |
| US3 | A `.go`/`.php`/`.ts` fixture file is split at syntactic boundaries with context re-attached | `indexing/test_chunk.py` | Unit |
| US4 | A chunk's content produces a stored, fixed-dimension embedding vector, no network call | `adapter/embedding/test_local_onnx.py` | Integration (real local model, no external network) |
| US5 | Parsed definitions/references produce the correct nodes/edges, per language | `indexing/test_graph.py` | Unit |
| US6 | A push touching 3 of 10,000 files only re-chunks/re-embeds/re-graphs those 3 | `indexing/test_pipeline.py` | Integration (real Postgres) |
| US7 | A removed file's chunks/nodes are deleted; edges targeting its symbols are left dangling, not specially cleaned up | `adapter/storage/test_indexing_repository.py` | Integration (real Postgres) |
| US8 | A 25-commit push (over GitHub's 20-entry cap) falls back to a full Trees-API diff | `adapter/github/test_tree_fetch.py` | Integration (recorded GitHub cassette) |
| US9 | `go.sum`, `node_modules/`, and a `.py` file are excluded before chunking | `indexing/test_prefilter.py` | Unit |
| US10 | One malformed file's parse failure doesn't prevent the rest of the push's files from being indexed | `indexing/test_pipeline.py` | Integration |
| US11 | The same delivery ID processed twice yields exactly one `INDEX` job | `apps/webhook_api/test_webhook_routes.py` (extended, shared with US1/US2) | Integration |
| US1–US11 (happy path) | Full Docker Compose stack, synthetic `push` webhook against a stubbed GitHub endpoint, asserts chunks/embeddings/edges land in Postgres for the pushed commit | `tests/e2e/test_indexing_pipeline.py` (new) | E2E |
| Cost ceilings (goal statement) | A fixture repo exceeding the incremental-run file/byte ceiling logs a `warning` and drops the excess rather than failing the job | `indexing/test_pipeline.py` | Integration |
| NFR — embedding model startup check | An unloadable local embedding model fails Worker startup fast with a clear error, mirroring `pgvector_check.py`'s pattern; a loadable model starts cleanly | `adapter/embedding/test_startup_check.py` | Integration |
| NFR — kill switch | With `INDEXING_ENABLED=false`, a qualifying `push`/`pull_request` webhook is still accepted (200) and delivery-recorded, but enqueues no `INDEX` job | `apps/webhook_api/test_webhook_routes.py` (extended) | Integration |

## Open assumptions pending validation

Carried forward explicitly, not buried in prose above:

1. **Embedding model choice** — a local ONNX sentence-embedding model,
   recommended default `Xenova/all-MiniLM-L6-v2` (384-dim), the same model
   `knowhow` already runs for the same reason (small, fast, CPU-only). Not
   validated against real code-retrieval quality for this platform's three
   in-scope languages; swappable later, at the cost of re-embedding the
   whole corpus (the embedding dimension is the one thing baked into the
   schema — `rag-techniques.md` §2's own named caveat).
2. **Chunk token budget** — recommended default ~512 tokens with a
   64-token overlap, matching `knowhow`'s own precedent cited in
   `rag-techniques.md` §1. Not tuned against this platform's own retrieval
   quality yet (there is no query path to measure against until the
   companion Context Builder spec exists).
3. **Cost ceilings** (incremental: 500 files/200MB; full-run: 20,000
   files/2GB) — starting defaults with no real-repo load data behind them
   yet; expected to be tuned once Unit G's E2E scenario runs against
   realistically-sized fixture repos.
4. **Step-framework reuse vs. a separate pipeline** (Phases / Units'
   opening note) — left for the implementing/planning agent to decide, not
   a product requirement this PRD mandates either way.
