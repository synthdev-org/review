# Plan: Phase 2 — Context Builder

**Status: Delivered.** Sequences [`docs/specs/spec-context-builder.md`](../specs/spec-context-builder.md)
— [`docs/roadmap.md`](../roadmap.md)'s Phase 2 Context Builder/review-upgrade
bullet — into buildable, independently-testable phases, following the same
phase/tests/acceptance shape `docs/plans/plan-indexing-delivery.md`
established as this repo's `docs/plans/` convention. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

`spec-context-builder.md` was itself self-reviewed to "ready to hand to a
planning agent" against `prd-review`'s ten dimensions, and every one of its
originally-open assumptions was closed to a firm, cited decision before this
plan was drafted. This plan does not re-litigate any of those
product/architecture calls — it only sequences the work and resolves the
*implementation-shape* questions the spec deliberately left unspecified.
Those resolutions are recorded below as **assumptions this plan makes**, not
silent guesses.

**This draft went through three independent full review rounds (all seven
`plan-review` dimensions in rounds one and two; a targeted round three on
the three dimensions with the deepest round-two findings — architecture,
security, compatibility) before being finalized, hitting `plan-review`'s
own three-pass cap. The arc matters more than the tally — real defects
surfaced and were fixed in place below, not narrated as history:**

- **Round one** found: a required (non-optional) new field on the shared
  `JobIdentity` model that would have broken every existing step's output
  construction on first deploy; two new Protocols placed away from their
  sole consumer citing an ADR exception that doesn't apply to a
  single-consumer case; a per-internal-call timeout design that both raced
  against the pipeline framework's own outer per-step timeout and left
  total step cost unbounded; a missing typed exception on one query path;
  a cross-tenant isolation gap in the blast-radius query with no test
  proving it; and two PRD user stories with claimed-but-missing integration
  coverage.
- **Round two**, re-verifying round one's own fixes, found several had
  real teeth missing: the "batching" fix only reduced round-trip *count*
  for one of three query stages, leaving the graph query unbounded in
  *result size* (no `LIMIT` enforced in the query itself), the
  caller-content lookup completely unbatched, and the similarity query
  still one-per-changed-chunk with no cap applied until *after* the
  queries already ran — the resource-exhaustion concern was capped in
  duration, not in cost. It also found: the new three-way step-dependency
  conditional left one reachable configuration
  (`context_builder_enabled=True` with static analysis disabled)
  completely unspecified, which would crash every job in that
  configuration; the new settings were never actually wired into
  `review/config.py`'s enablement/timeout maps, meaning the kill switch
  would silently not work and the timeout would raise a raw `KeyError`
  instead of ever reaching the carefully-designed internal timeout;
  Tree-sitter's CPU-bound parsing was never moved off the event loop
  despite the plan following that exact pattern elsewhere; two new
  exception classes duplicated an existing shared-exception pattern this
  codebase already has; and the observability fix distinguished per-job
  outcomes but still gave no practical way to notice sustained failure
  across many jobs. **All of these are fixed in the phases below.**
- **Round three**, re-verifying round two's fixes against the real SQL/
  code mechanics, found the resource-exhaustion fix was still incomplete
  (an in-query `LIMIT` via window function bounded transmitted row count
  but not the database work needed to compute it — corrected to a
  `LATERAL`-join design that lets Postgres use the existing index to stop
  scanning early per symbol); the "batched" content-resolution query's SQL
  shape was left vague enough to invite a real cross-product correctness
  bug (independent `IN` lists on two columns instead of row-value
  matching); a third isolation dimension (`pr_number`, distinct from
  `repo_id`) had no dedicated test at any query stage, despite
  `IndexChunkRow`'s own schema documenting `pr_number > 0` rows as
  potentially fork-authored/untrusted; a factual error in how the plan
  described the kill switch (claiming a disabled step is "never
  registered," contradicting this codebase's actual, confirmed mechanism
  of always registering every step and controlling execution purely via
  an enabled-map lookup); and two real gaming vectors in the new
  consecutive-failure log-escalation counter, now explicitly scoped as an
  operational convenience signal rather than a security control. **All of
  these are fixed in the phases below.**

Per `plan-review`'s own three-pass cap, this plan stopped iterating there at
the time; any residual, lower-severity items surfaced during implementation
should be handled as normal code review, not another full planning cycle.

- **Round four**, a separate later review pass across all seven
  `plan-review` dimensions in parallel, found: `review/context_builder.py`
  (a domain package) was required to catch `adapter/github/exceptions.py`'s
  `GitHubDiffFetchUnreachableError`/`GitHubDiffFetchMalformedResponseError`
  by name for graceful degradation — a domain→adapter import forbidden by
  `architecture.md`, with no existing precedent for it anywhere in this
  codebase; US9 was mapped to a Unit test only, with no integration test
  anywhere in the plan, violating `testing.md`'s mandatory per-story rule;
  Phase 2's test list couldn't actually distinguish the round-three
  row-value-matching fix from the cross-product bug it was fixing; the new
  `WorkerSettings` timeout-margin validator had no test of its own; Phase
  6's `render_citation` had no stated boundary against rendering a bundle
  item's raw `Chunk.content`, meaning an unguarded implementation could leak
  another file's real source (and any secret in it) into a posted PR
  comment, entirely bypassing this codebase's existing redaction pipeline;
  and the per-repo consecutive-`degraded_error` log-escalation counter
  turned out to be scope this plan invented rather than the spec asked for,
  and to silently not work under this platform's own supported horizontal
  Worker scaling (Helm `worker.replicaCount`), with its stated
  justification resting on a claim about customer log-alerting setups this
  platform has no way to verify. **All of these are fixed in the phases
  below** — the escalation counter is removed outright rather than
  patched, since none of its residual value survived once its central
  premise and every named benefit turned out unsupported.
- **Round five**, re-running all seven dimensions against round four's own
  fixes, found round four's fixes had exposed real gaps of their own:
  `context_bundle`/`context_ref_ids` were never actually threaded through
  `LLMReviewOutput`/`FindingsOutputInput`/`Finding`, leaving Phase 6's
  citation mechanism with no functioning data path end-to-end; citation
  "line range" (US6's own requirement) had no data source anywhere in the
  schema, since `Chunk`/`IndexChunkRow` only ever stored byte offsets;
  the new `base_sha` webhook wiring and the new `DiffFetcher`
  exception-translation code both had no test coverage, the latter also
  silently conflicting with an existing passing test; `GitHubBlobTooLargeError`
  was left uncaught by the exception-translation fix, able to crash a job
  outright on a single oversized file, contradicting US7; and the
  success-path log for a fully successful run recorded only counts, never
  which files were actually retrieved — leaving the Security review's own
  named prompt-injection residual uninvestigable in practice. **All of
  these are fixed in the phases below**, including one narrow, additive
  Alembic migration (Assumption 5) — the sole schema change in this plan,
  needed because no downstream-fetch or in-memory workaround could resolve
  the line-range gap without reintroducing the exact per-item I/O cost
  Assumption 6 was built to eliminate.
- **Round six**, re-running all seven dimensions against round five's own
  fixes, found round five's fixes were themselves incomplete or partly
  inaccurate: the `index_graph_node` half of the new migration was dead
  schema nothing in this plan reads or can populate; the migration's
  "computed for free from `Node.start_point`/`end_point`" claim didn't
  actually hold against `indexing/chunk.py`'s real chunking logic
  (`_pack()`'s multi-window splitting, `_pack_whole_file()`'s no-`Node`
  fallback); the actual Postgres write path (`upsert_file`) was never named
  as needing the change, so the new columns would have stayed `NULL`
  forever; the `DiffFetcher` exception-translation fix changed a method
  `review/static_analysis.py` also calls, unacknowledged; the Security
  review's claim that all three query paths have dedicated `repo_id`
  isolation tests was false for the similarity query; `base_sha = None` — a
  legitimate, common production case, not just a deploy-window edge case —
  was never handled and could crash a job outright; two more functions
  (`resolve_changed_symbols`, `render_citation`) had the same missing-`job_id`
  gap already fixed once elsewhere; and the new "step isolated: degraded"
  log line was described as reusing an existing framework mechanism that
  cannot actually fire for a step designed to never raise. **All of these
  are fixed in the phases below** — the migration is now `index_chunk`-only
  with a correct computation path and a named write-path change; the
  degradation log is now described as new step-level logic, not framework
  reuse.
- **Round seven**, re-running all seven dimensions against round six's own
  fixes, found round six's "correct computation" for `line_range` was
  *itself* still wrong — independently caught by five separate reviewers
  across architecture, code reuse, testing, scope discipline, and
  observability, all converging on the same root cause: `_pack()`'s
  per-window line state is relative to a declaration's own extracted text,
  never the whole file, so reusing it (or `Node.start_point`/`end_point`
  before it) always needed a base-line offset the plan never named.
  **Fixed by abandoning that whole approach** — `line_range` is now
  computed by uniform newline-counting against the chunk's own
  already-correct `byte_range` and the full file content, with no
  dependency on any chunking-path-specific internal state. Round seven also
  found: `adapter/storage/models.py`'s `IndexChunkRow` itself, not just the
  `upsert_file` write path, needed the new columns named explicitly; the
  `DiffFetcher` exception-translation fix was self-contradictory (claiming
  both "all collapse to one type" and, in its own Tests section, that a
  specific case produces the *other* type) — fixed with an explicit
  type-branch inside one shared catch; the citation-rendering mechanism
  described appending text into `_finding_body`'s return value, which
  doesn't exist as a hook point on a frozen `Finding` and would have
  bypassed `escape_markdown` entirely — fixed via `Finding.model_copy`;
  `ChunkParseError` was listed as a Phase 5 step-level typed failure despite
  being fully absorbed inside Phase 1 and unable to ever reach that level;
  and the Traceability table's `pr_number` isolation tests were collapsed
  into one row while `repo_id`'s identical three-query-path tests each got
  their own — fixed to match. **All of these are fixed in the phases
  below.**
- **Round eight**, re-running all seven dimensions a final time against
  round seven's own fixes, found round seven's two central fixes were
  *themselves* still wrong, each independently confirmed by three or four
  separate reviewers: the `line_range` newline-counting formula had an
  off-by-one error on the end line whenever a chunk's `byte_range` ends
  right after a `\n` — the normal case for a trailing-newline file or a
  non-final multi-window split — fixed by counting up to `byte_range[1] -
  1`, the chunk's actual last included byte, not `byte_range[1]` itself;
  and the `DiffFetcher` exception-translation design's single shared
  `except GitHubGitDataFetchError` catch could never actually receive a
  `GitHubDiffFetchMalformedResponseError`/`GitHubDiffFetchUnreachableError`
  instance at all — those two are subclasses of the sibling
  `GitHubDiffFetchError` hierarchy, not of `GitHubGitDataFetchError` —
  making the malformed-response branch dead code; fixed with two separate
  `except` clauses instead of one shared catch, the design all four
  reviewers who found the bug independently proposed. Round eight also
  found the citation-rendering fix's own `_finding_body(new_finding)` call
  site doesn't exist — `_finding_body` is a private adapter-module helper
  `review/output.py` (a domain module) can't call directly — fixed by
  passing the `model_copy`'d `Finding` through the existing
  `FindingsPublisher` calls instead; plus smaller gaps: `base_sha = None`
  could mask a dropped blast-radius context behind a plain `outcome=ok`
  when similarity hits were nonzero, the new settings validator was never
  registered in ADR-0005's scope list unlike every sibling case, no test
  exercised `_parse_llm_findings`'s `list`→`tuple` conversion for
  `context_ref_ids`, `context_ref_ids` had no cardinality bound against a
  manipulated-completion log-noise vector, the row-value query needed an
  explicit "must bind, never interpolate" callout given it's genuinely new
  SQL-construction territory for this codebase, and `spec-context-builder.md`
  itself was never reconciled with the plan's own migration decision.
  **All of these are fixed in the phases below** — given this specific
  area (`line_range` computation, the `DiffFetcher` exception design) has
  now been independently re-broken and re-fixed across four consecutive
  rounds, both fixes are written to name the exact reasoning an implementer
  needs to verify directly against the real code before writing it, rather
  than asking the implementer to trust this plan's prior track record on
  these two specific points.

Phase 2's indexing job (`spec-indexing.md`) is complete and merged (`e99a88f`)
— this is additive work on real, running code, not greenfield. Every phase
below names the existing file/module it extends or reuses; nothing here
duplicates an already-built capability (independently verified during
review: every reuse claim below was checked against the actual file/symbol
it cites).

## Scope

**In scope** — `spec-context-builder.md`'s Units A–F in full, sequenced 1:1 as
Phases 1–6 below: diff-to-chunk resolution, blast-radius graph query,
semantic similarity query, context assembly + budget enforcement, the
Context Builder step + `llm_review` wiring, and citation rendering in posted
findings.

**Out of scope** — the spec's own out-of-scope list applies unchanged:
the external product-knowledge MCP augmentation, knowhow as its reference
implementation (Phase 7), Memory/dismissed-finding suppression
(Phase 4), a structurally separate verification pass (Phase 3), multi-hop
blast radius, PR-delta graph overlay, precise per-language symbol
resolution, query result caching, a BYO embedding endpoint, and languages
beyond TypeScript/PHP/Go. Not repeated in full here; see the spec's own
Scope section.

**Traceability:** every user story in the spec (`US1`–`US10`) plus the NFR
scenarios maps to at least one **integration** test, per `testing.md`'s
actual mandatory rule. For the stories whose *core matching/rendering
logic* is pure/no-I/O (US1's chunk-overlap resolution, US10's pre-change-name
selection, US6's citation rendering), that pure logic gets its own Unit test
per ADR-0005 — but the **story itself** still crosses a real I/O boundary,
so it also gets integration coverage from Phase 5's/Phase 6's end-to-end
step tests, the identical pattern `plan-indexing-delivery.md` used for its
own US3/US5/US9. Consolidated table at the end of this plan, after Phase 6.

## Assumptions this plan makes (flagged, not decided)

1. **Pre-change content is fetched fresh at the PR's base commit, never read
   back from `index_chunk`'s `pr_number = 0` rows.** Rejected alternative:
   reading the default branch's already-indexed chunks. Rejected because
   `index_chunk` reflects the *current* default-branch tip, not necessarily
   the PR's actual merge-base — an unrelated commit landing on the default
   branch after the PR branched could shift stored byte ranges relative to
   the PR's own diff line numbers, a silent drift that could resolve a hunk
   to the wrong symbol and produce a citation the platform asserts is
   factually accurate but isn't. Worth one extra fetch+parse per changed
   file per review.
2. **The PR's base commit SHA is captured from the webhook payload and
   threaded through the full enqueue → job-payload → step chain.**
   Confirmed by reading the actual code: `adapter/github/schemas.py`'s
   `PullRequest` models only `number` and `head.sha` today;
   `tenancy/resolution.py`'s `WebhookEvent` has no `base_sha` field;
   `apps/webhook_api/webhook_routes.py`'s `_build_webhook_event`/
   `_review_enqueue_payload` don't carry one either. All three need the
   same additive, optional-field treatment `head_sha` already has at each
   of those points — see Phase 1's explicit file list.
   **`base_sha` on `JobIdentity` is optional (`str | None = None`), not
   required** — a required field would raise a pydantic `ValidationError`
   on `PrefilterStep.run`'s very first `PrefilterOutput(...)` construction
   for every job, since every step's Output model lists `JobIdentity`'s
   fields explicitly by keyword rather than forwarding an unpacked base
   object (verified in `review/prefilter.py`, `review/static_analysis.py`,
   `review/llm_review.py`) — a real, first-deploy-breaking bug in this
   plan's original draft, fixed here. Because it's optional, **it must
   still be explicitly forwarded at every Output construction that should
   carry it forward** — this is **four** sites, not three (round two found
   the fourth): `PrefilterOutput`, `StaticAnalysisOutput`, `LLMReviewOutput`
   (all three existing, per round one), and Phase 5's new
   `ContextBuilderOutput` (missed in round one's own "exhaustive" list —
   corrected here). `review/output.py`'s `FindingsOutputInput` is a
   documented exception to the general pattern — a plain `BaseModel`
   duplicating the six identity fields rather than subclassing
   `JobIdentity` — and doesn't need `base_sha` for this plan's scope, so
   it's correctly left untouched.
3. **`GraphQuery` and `SimilarityQuery` are defined in `review/context_builder.py`
   (the ordinary "defined at the consumer" rule), not in a new
   `indexing/query.py` module.** This plan's original draft placed them in
   `indexing/` citing [ADR-0006](../adr/0006-protocol-defined-at-data-model.md)'s
   "alongside the data model" exception — wrong: that ADR's own text states
   "A Protocol with exactly one real consumer still follows the ordinary
   'defined at the consumer' rule unchanged," and both Protocols have
   exactly one consumer (`review/context_builder.py`) and one
   implementation each — unlike `IndexRepository`/`IndexCommitTracker`,
   which an earlier draft of this plan characterized as having genuinely
   earned the away-from-consumer exception. **Two corrections found by
   later review:** first, `IndexRepository`/`IndexCommitTracker` in fact
   have exactly one real consumer each (`indexing/pipeline.py`) today, not
   three; second, ADR-0006's own "Scope, extended per phase" list — its
   authoritative registry of which cases it actually covers — never
   mentions `IndexRepository`/`IndexCommitTracker` at all (it names only
   `ingestion.diff.DiffFetcher` and `findings.redact.TextRedactor`);
   `indexing/repository.py`'s own docstring justifies that module's
   Protocol split via a *different* rule (Assumption 13, a method-count
   Protocol-splitting rule), not ADR-0006. Both are pre-existing
   characterizations from already-merged Phase 2 code, out of this plan's
   scope to correct, and neither changes
   this plan's own conclusion below, which applies ADR-0006's rule directly
   to `GraphQuery`/`SimilarityQuery`'s own consumer count rather than
   relying on the (inaccurate) comparison. Whether a Protocol should exist
   at all for a single-implementation case
   (`architecture.md`'s Simplicity section, "no interface/abstraction
   without a second real implementation already in view") is answered by
   existing precedent already in this codebase — `DiffFetcher`/
   `GitHubDiffFetcher`, `EmbeddingClient`/`LocalONNXEmbeddingClient`, and
   `FindingsPublisher`/`GitHubFindingsPublisher` are all single-
   implementation Protocols at adapter boundaries — so `GraphQuery`/
   `SimilarityQuery` follow established practice, not a new exception.
   Confirmed during review this placement matches real precedent for a
   comparably-scoped module: `review/output.py` already defines
   `FindingsPublisher` (its one real consumer) in the same file as the
   step class it serves, and `review/static_analysis.py`/`review/llm_review.py`
   already hold a comparable mix of Protocol/models/exceptions/step logic
   in one module. The concrete Postgres implementations live in
   `adapter/storage/context_query.py`, importing the Protocols from
   `review/` — an adapter depending on a domain-defined Protocol, since
   `review` is itself one of `architecture.md`'s named domain packages.
4. **`review/context_builder.py` importing `indexing`'s domain types
   directly is a new, deliberate cross-bounded-context dependency, named
   explicitly rather than folded into file-naming discretion.** Not a
   violation of `architecture.md` — both are domain packages, and one
   importing another's pure, zero-infra-import types is the same ordinary
   shape `review/llm_review.py` already has on `llmgateway.models` — but
   worth recording as a boundary decision.
5. **No new pgvector ANN index for v1; one narrow, additive Alembic
   migration, added in round four and corrected in round six, is the sole
   exception to "no new migration."** `index_chunk`'s unique constraint on
   `(repo_id, pr_number, file_path, chunk_index)` already lets a `(repo_id,
   pr_number, file_path)` prefix filter narrow to one file's handful of
   chunks before a `symbol_name` equality check. Phase 3's similarity query
   (`ORDER BY embedding <=> :vector LIMIT N`, no ANN index) does a
   sequential scan + sort; accepted for v1 since no latency SLA is set and
   no real-repo-scale data exists yet to size an ANN index against — a
   purely additive migration later if real usage ever justifies one. **The
   migration exception, `index_chunk` only:** `index_chunk` gains nullable
   `start_line`/`end_line` integer columns (Phase 1) — needed because
   Phase 6's citation rendering (US6's own "line range" requirement) has no
   other source for it: `byte_range` alone can't be converted to a line
   number without fetching a caller/similar-chunk's *entire* file just to
   count newlines, an extra per-item I/O cost this plan's own Assumption 6
   was built specifically to avoid. **`index_graph_node` does NOT get these
   columns** (round six correction) — an earlier draft migrated both
   tables, but three independent reviewers confirmed nothing in this plan
   ever reads a line range off `index_graph_node`: Phase 2's `LATERAL`
   query only ever selects `node.file_path`/`node.symbol_name`, and
   `CallerHit.line_range` is sourced from `index_chunk` via the stage-2
   content-resolution join, not from the graph tables at all (`index_graph_node`
   rows come from `indexing/graph.py`'s `extract_graph()`/`GraphNode` — a
   structurally separate model with no byte-range/line concept and no
   relationship to the chunking pass) — migrating it would be dead schema,
   the opposite of "narrow." **Correct computation — uniform newline-counting
   against the full file content, not any reuse of `_pack()`'s or
   tree-sitter's internal bookkeeping (round seven correction, replacing
   two prior attempts):** round six's own two attempts (first "`Node.start_point`/
   `end_point`," then "`_pack()`'s own per-window line-span state") were
   both independently found wrong by five separate reviewers across two
   rounds — `_pack()`'s tracked `start`/`end` are line indices into
   `body.splitlines()`, where `body` is the *declaration's own extracted
   text* (`content[node.start_byte:node.end_byte]`), never the whole file;
   converting them to an absolute file line still needs the declaration's
   own starting file line, a value `_pack()` never receives or tracks, so
   every attempt to describe this as "no new parsing, just return existing
   state" was false. **The actual fix drops that framing entirely:** once
   `chunk_file(...)` has produced a `Chunk`'s final, file-absolute
   `byte_range` (already correct and already used for hunk-overlap
   resolution throughout this plan), `line_range` is computed by counting
   newlines in the *full file content* (`content`, already in hand — every
   chunking path receives the complete file bytes, not just a declaration's
   slice): `line_range = (content[:byte_range[0]].count(b"\n") + 1,
   content[:byte_range[1] - 1].count(b"\n") + 1)` — note the `- 1` on the
   end offset. **Round eight correction (this exact formula's own bug):**
   a version of this formula without that `- 1` was independently found
   wrong by a later reviewer — `byte_range` follows the codebase's existing
   Python-slice convention (`[start, end)`, exclusive end, confirmed by its
   existing use as `content[node.start_byte:node.end_byte]`), so
   `byte_range[1]` is one *past* the chunk's actual last byte; counting
   newlines up to `byte_range[1]` (not `byte_range[1] - 1`) silently adds
   one extra line whenever that last byte is itself a `\n` — the normal
   case for a `_pack_whole_file()` chunk of a file with a trailing newline
   (near-universal), and for every non-final window of a multi-window
   split (each window boundary lands on a line boundary, so the window's
   last included byte is that line's own `\n`). Counting up to
   `byte_range[1] - 1` (the actual last included byte) instead gives the
   correct line number in both cases. This is a small, real amount of new
   computation — honestly
   O(file size) per chunk, not free — not a zero-cost reuse of parser
   state, but it is uniformly correct for every chunking path (`_pack()`'s
   fast path, its multi-window path, and `_pack_whole_file()`'s fallback)
   without needing to reason about any of their internal, path-specific
   bookkeeping, which is exactly what three prior attempts got wrong.
   Applied once per `Chunk` in `indexing/chunk.py`, alongside where
   `byte_range` is already finalized. **Both the write path and the model
   itself are named explicitly, not left implicit (round six/seven
   corrections):** `adapter/storage/models.py`'s `IndexChunkRow` gains
   `start_line`/`end_line` as new nullable `mapped_column()` attributes —
   round seven found this named only for the write path (`upsert_file`)
   but never for the ORM model itself, meaning `upsert_file`'s planned
   `pg_insert(IndexChunkRow).values({..., "start_line": ...})` would fail
   at insert-construction time against columns `IndexChunkRow` doesn't map,
   not silently no-op. With the model updated, `adapter/storage/indexing_repository.py`'s
   `upsert_file` — which builds its insert/on-conflict-update values as
   explicit per-column dicts (e.g. `"byte_range_start": chunk.byte_range[0]`),
   not automatic ORM attribute mapping — gains `"start_line": chunk.line_range[0]
   if chunk.line_range else None` / `"end_line": ...` alongside the existing
   `byte_range_start`/`byte_range_end` values it already writes; without
   both changes together, the new columns would stay `NULL` for every chunk
   indexed after deploy, silently defeating the migration's entire purpose
   with no error. No backfill required (nullable, populated going forward
   as repos are re-indexed; a pre-migration chunk's `line_range` is simply
   `None`, and Phase 6 renders a citation without the line-range portion in
   that case, logged at `info`, never an error). A dedicated unit test
   (Phase 1) proves a multi-window declaration's *second* window gets a
   `line_range` starting well after line 1 — the specific case every prior
   attempt at this computation would have gotten wrong. **Reconciliation
   needed with `spec-context-builder.md` itself, not just this plan**
   (round eight named this): the spec's own Rollout & Release Safety
   section states "No new Alembic migration... genuinely nothing new to
   migrate," a decided NFR this plan's Assumption 5 now overrides. This
   plan documents the override and its reasoning, but the spec document
   itself should be amended (or the override explicitly signed off) so a
   reader of the spec alone doesn't get a materially wrong picture of this
   feature's actual rollout risk — a follow-up outside this plan's own
   file, not something this plan can close by itself.
6. **Query cost is bounded at three independent points, not one — round
   two found the original single "batch the calls" fix only closed one of
   three gaps:**
   - **An upstream ceiling on how many items generate a query at all**,
     applied to Phase 1's own output *before* Phase 2/3 issue anything:
     `max_changed_symbols_per_job` (blast radius, recommended default 50)
     and `max_changed_chunks_per_job` (similarity, recommended default 50)
     — excess resolved symbols/chunks beyond these ceilings are dropped
     deterministically (in diff order) and logged at `warning`, mirroring
     `spec-indexing.md`'s own non-fatal cost-ceiling drop pattern. **Defined
     as hardcoded module-level constants in `review/context_symbols.py`,
     not `WorkerSettings` fields** (round four resolved an ambiguity the
     original draft left open) — mirroring `indexing/pipeline.py`'s own
     `FULL_RUN_FILE_CEILING`-style constants, not a new operator-facing
     setting, since `spec-context-builder.md`'s own Priority section
     already decided a fixed default is sufficient and an
     independently-tunable override is out of scope. This is
     new, additional scope versus round one's fix — round one's per-result
     caps (10 edges/symbol, 5 matches/chunk, both still in place) bound
     *output size per query*, not the *number of queries issued in the
     first place*; round two found a PR touching hundreds of files could
     still drive hundreds of individual similarity queries before any cap
     ever applied.
   - **A `LIMIT` enforced inside the blast-radius query itself** (a
     window function partitioned by `target_symbol_name`, not a
     post-query truncation), so a single widely-called symbol in a large
     indexed repo can't force one expensive query to fetch and transmit
     every matching edge before Phase 4's cap ever runs. Round one's
     "batch into one query with `target_symbol_name = ANY(...)`" reduced
     round-trip *count* but, as written, left result *size* per symbol
     unbounded inside that one query — fixed here by pushing the
     per-symbol cap into the query's own `LIMIT`/window clause. See Phase
     2.
   - **The caller-content lookup (`index_chunk`) is batched across every
     resolved caller in one query**, not one query per caller — round
     one's batching fix only applied to the edge/node stage, leaving this
     second stage to reintroduce the exact per-item multiplication the fix
     claimed to eliminate. See Phase 2.
   - The embedding call remains batched across every changed chunk in one
     `EmbeddingClient.embed()` call (round one's fix, confirmed correct
     during review), extending — not literally matching, a citation nit
     corrected here — `indexing/pipeline.py`'s own per-*file* batching
     precedent to a broader per-*job* scope. **Named residual, not fixed
     here:** `indexing/embedding.py`'s own docstring records that an
     earlier job-wide batched design in that module was deliberately
     reverted because it "widened a single bad chunk's blast radius to an
     entire run" — this plan reintroduces that exact shape for Phase 3's
     changed-chunk embedding call, so one pathological/malformed changed
     chunk among many files can zero out semantic-similarity context for
     the whole job, not just that file. Phase 5's whole-step graceful
     degradation (US7) prevents this from failing the job, but it's an
     availability trade-off worth naming rather than silently
     reintroducing a shape this codebase already reverted once elsewhere.
7. **The pipeline step framework's outer per-step timeout and
   `ContextBuilderStep`'s own internal timeout are two distinct settings
   with an enforced, validated margin — never the same value** (round
   one's original draft wrapped internal calls in
   `asyncio.wait_for(..., context_builder_step_timeout_seconds)`, the
   *same* setting the outer `StepRegistry` already uses for its own
   per-step enforcement per `review/pipeline.py`'s confirmed
   `asyncio.wait_for(step.run(...), timeout=self._timeouts[step.name])`
   shape — two `wait_for` calls racing on an identical deadline could let
   the outer one win and fail the job). Fixed shape: a new
   `context_builder_internal_timeout_seconds` (default 25s) bounds the
   *entire* internal orchestration in **one** `asyncio.wait_for` wrapping
   all of Phases 1–4's work together; `context_builder_step_timeout_seconds`
   (the value the outer registry reads) defaults to 35s — a real 10-second
   margin. **The margin invariant (`step_timeout >
   internal_timeout`) is validated by a lightweight `@model_validator(mode="after")`
   on `WorkerSettings` itself** (round two simplified this from round
   one's heavier main.py-level startup-check machinery, after finding this
   is a pure cross-field numeric comparison with no I/O — exactly the
   shape `apps/webhook_api/settings.py:114-124`'s existing
   `_require_setup_state_secret_when_setup_mode_enabled` validator already
   handles on the Settings model directly, a closer and lighter precedent
   than the I/O-driven startup checks in `main.py`). **Named limitation,
   not fully closed by this margin alone:** the batched embedding call
   (Phase 3) runs via `asyncio.to_thread`; cancelling the *awaiting*
   coroutine on internal timeout does not forcibly stop the underlying
   inference call already executing in its worker thread — the coroutine
   unblocks and `ContextBuilderStep` degrades to an empty bundle on
   schedule, but the thread's own work may continue briefly in the
   background. Accepted: the job-level timeout contract is still honored
   (the step returns on time), and this is the same kind of
   uncontroversial thread-cleanup nuance every `asyncio.to_thread` caller
   in this codebase already lives with, not a new risk category. **Second
   named limitation (round six):** Phase 1's `chunk_file(...)` calls also
   run via `asyncio.to_thread` on the same default executor as this
   embedding call — a sustained pattern of large/slow diffs whose embedding
   calls repeatedly hit the internal timeout could tie up thread-pool slots
   shared across concurrently running jobs, degrading unrelated jobs'
   parsing/embedding calls rather than staying a single-job cost. Accepted
   for the same reason as the first limitation — a real, named nuance of
   this codebase's existing `asyncio.to_thread` usage pattern, not solved
   by anything this plan introduces, and not a new risk category this plan
   creates versus what Phase 1/3 already independently accept.
8. **`ContextQueryError(operation: str, detail: str)` — one shared
   exception, not two.** Round one's draft defined `GraphQueryError` and
   `SimilarityQueryError` separately; round two found this duplicates an
   established pattern this codebase already has for the identical
   situation: `indexing/repository.py`'s `IndexingRepositoryError(operation,
   detail)` is already shared across two Protocols (`IndexRepository`,
   `IndexCommitTracker`) implemented in one adapter module, discriminated
   by an `operation` field rather than one exception class per method.
   `PostgresGraphQuery`/`PostgresSimilarityQuery` are likewise both
   implemented in one adapter module and both wrap the identical failure
   mode (a Postgres-level failure at the adapter boundary) — `ContextQueryError`,
   defined once in `review/context_builder.py` (mirroring
   `IndexingRepositoryError`'s placement in the domain package owning the
   Protocols), replaces both.
9. **New module/file names** not already pinned above follow this
   codebase's existing one-module-per-concern convention:
   `review/context_symbols.py`, `review/context_bundle.py`,
   `review/context_builder.py`, `adapter/storage/context_query.py`,
   `review/citation.py`. Naming specifics beyond what's written below are
   implementer discretion.

## Phase 1 — Diff-to-chunk resolution (Unit A)

**Why first:** produces the two chunk sets (pre-change, for blast-radius
symbol identity; post-change, for similarity-query embedding) every later
phase consumes as input.

- Extend `adapter/github/schemas.py`: add `PullRequestBase` (`sha: str |
  None = None`, mirroring `PullRequestHead`), add `base:
  PullRequestBase | None = None` to `PullRequest`. Both optional.
- Extend `tenancy/resolution.py`'s `WebhookEvent` with `base_sha: str |
  None = None`; extend `apps/webhook_api/webhook_routes.py`'s
  `_build_webhook_event` to populate it from `pull_request.base.sha`, and
  `_review_enqueue_payload` to carry it into the job payload dict.
- Add `base_sha: str | None = None` to `review.step.JobIdentity`
  (`review/step.py:39-66`). Update `review/prefilter.py`,
  `review/static_analysis.py`, `review/llm_review.py`, **and Phase 5's new
  `ContextBuilderOutput`** (see Assumption 2 — the fourth site, missed in
  an earlier draft) to explicitly forward `base_sha=input.base_sha`
  alongside the fields they already forward.
- New `review/context_symbols.py`, pure and no-I/O for its matching logic
  (per [ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md) — **this
  phase's own deliverable includes updating that ADR's "Scope, extended per
  phase" list to add this case**, per the ADR's own stated registration
  process, not just invoking the exemption without registering it):
  - Parse a `DiffFile.patch`'s `@@ -a,b +c,d @@` hunk headers into
    pre-change and post-change line ranges per changed file — confirmed no
    existing hunk-header parser exists elsewhere to reuse.
  - `resolve_changed_symbols(diff: Diff, pre_content: dict[str, bytes],
    post_content: dict[str, bytes], *, job_id: str) -> ChangedSymbols` —
    `job_id` threaded through purely so its own `ChunkParseError`/
    ceiling-truncation `warning` logs can be correlated to the originating
    job (round six added this, mirroring the identical fix already applied
    to Phase 4's `assemble_context_bundle`; this function had the same gap):
    for each changed
    file, calls `indexing.chunk.chunk_file(...)` on both `pre_content[path]`
    and `post_content[path]`, then finds every `Chunk` (by `byte_range`
    overlap) that intersects at least one hunk. Returns two sets:
    **pre-change hits** (their `symbol_name` is what Phase 2's blast-radius
    query uses — covering modified, renamed, and removed symbols uniformly,
    US10) and **post-change hits** (their `content` is what Phase 3
    embeds). A hunk with no single overlapping chunk contributes nothing
    (US1's "no resolved symbol" case).
  - **Each `chunk_file(...)` call is wrapped in `asyncio.to_thread`**
    (round two found this missing — `indexing/pipeline.py:485-494`
    documents exactly why this CPU-bound Tree-sitter parsing must stay off
    the Worker's event loop, and this plan's own Phase 3 already follows
    that precedent for the embedding call; Phase 1's parsing calls must
    too, to avoid event-loop starvation for other concurrently-running
    jobs on a PR touching many files).
  - **Resolved symbol/chunk sets are capped at `max_changed_symbols_per_job`
    / `max_changed_chunks_per_job`** (Assumption 6's new upstream ceiling,
    recommended default 50 each) before being handed to Phases 2–3 — excess
    dropped deterministically (diff order) and logged at `warning`.
  - `ChunkParseError` is caught per file, logged at `warning`, and that
    file contributes no symbols (US8). A file absent from the base tree
    (a newly-added file) contributes no pre-change chunks — logged at
    `info`, the normal case for every added file.
- **`indexing/chunk.py`'s `Chunk` model gains a `line_range: tuple[int, int]
  | None` field (1-indexed start/end lines), and `index_chunk` gains one
  small, nullable, additive migration to store it** (round four added this;
  rounds six and seven corrected its computation twice and scoped it to
  `index_chunk` only — see Assumption 5's carve-out for the full detail on
  why neither `Node.start_point`/`end_point` nor `_pack()`'s own internal
  bookkeeping is the right source, and why `index_graph_node` doesn't need
  this field). Populated uniformly by counting newlines in the full file
  content up to each end of the chunk's own already-correct, file-absolute
  `byte_range` — not by reusing any chunking-path-specific internal state.
  `resolve_changed_symbols`'s own
  pre/post-change chunk hits carry this field forward like every other
  `Chunk` attribute; `adapter/storage/models.py`'s `IndexChunkRow` gains
  the corresponding nullable columns, and
  `adapter/storage/indexing_repository.py`'s
  `upsert_file` writes them alongside the existing
  `byte_range_start`/`byte_range_end` values it already writes; a chunk
  indexed before this migration backfills simply has `line_range = None`.
- **`ingestion.diff.DiffFetcher`'s Protocol boundary gains two new
  domain-owned exceptions** — `DiffContentUnavailableError` and
  `DiffContentMalformedError`, defined in `ingestion/diff.py` next to the
  `DiffFetcher` Protocol, mirroring `indexing/tree.py`'s existing
  `RepoTreeFetchError`/`RepoTreeTruncatedError` pattern (round four found
  the original draft had `review/context_builder.py` — a domain package —
  catching the adapter-owned `GitHubDiffFetchUnreachableError`/
  `GitHubDiffFetchMalformedResponseError` (`adapter/github/exceptions.py`)
  by name for graceful degradation, which requires a domain→adapter import
  and violates `architecture.md`'s "domain never imports adapter code"
  rule; no existing step establishes this precedent, since
  `review/prefilter.py` only ever lets these propagate generically to
  `review/pipeline.py`'s `StepRegistry`'s own `except Exception` handling).
  The existing GitHub-backed implementation of `DiffFetcher.fetch_content()`
  gets a small additive change: **two separate `except` clauses, not one
  shared base-class catch (round eight correction — the fourth and final
  version of this fix, after three prior versions were each independently
  found broken by up to four reviewers apiece):**
  `except GitHubDiffFetchError as err:` (catching both
  `GitHubDiffFetchUnreachableError` and `GitHubDiffFetchMalformedResponseError`,
  `fetch_content()`'s own existing, already-tested failure contract —
  branch by type inside: `GitHubDiffFetchMalformedResponseError` re-raises
  as `DiffContentMalformedError`, `GitHubDiffFetchUnreachableError`
  re-raises as `DiffContentUnavailableError`), and a second, separate
  `except GitHubBlobTooLargeError as err:` re-raising as
  `DiffContentUnavailableError` (`raise ... from err` throughout, per
  `architecture.md`'s exception-chaining rule). **Why two clauses, not
  one:** four independent reviewers across two rounds confirmed
  `GitHubDiffFetchError` and `GitHubGitDataFetchError` are *siblings*, both
  direct subclasses of `GitHubAdapterError` — not parent and child — so a
  single `except GitHubGitDataFetchError` clause (round seven's design)
  can never catch a `GitHubDiffFetchMalformedResponseError`/
  `GitHubDiffFetchUnreachableError` instance at all, making that branch
  dead code and silently misclassifying every malformed response as
  `DiffContentUnavailableError` — directly contradicting Phase 1's own
  Tests section, which requires proving the malformed case produces
  `DiffContentMalformedError`. `GitHubBlobTooLargeError` is a
  `GitHubGitDataFetchError` subclass instead — a third, disjoint case that
  needs its own clause since it isn't a `GitHubDiffFetchError` at all. This
  two-clause design was independently proposed as the correct fix by all
  four reviewers who found round seven's design broken, so it is not
  guesswork — but given this exact spot has now been wrong three times in
  a row across rounds four, six, and seven, **the implementer must still
  verify the current exception hierarchy directly against
  `adapter/github/exceptions.py` before writing this code**, rather than
  trusting this plan's class names as gospel a fourth time. **Named,
  accepted residual, not fixed
  here:** the token-exchange call (`self._token_provider.get_token(...)`)
  sits outside this translation, so a `GitHubTokenExchangeError` still
  propagates untranslated — the same pre-existing gap
  `indexing.tree.RepoTreeFetcher` already has today, not something this
  plan introduces or is in scope to close.
  Phase 5's `ContextBuilderStep.run()` catches the domain-owned
  `DiffContentUnavailableError`/`DiffContentMalformedError`, never the
  adapter-owned originals. **This change lands in `fetch_content()`'s own
  implementation, not a wrapper scoped to the new caller — `review/static_analysis.py`
  already calls this exact method and is affected too** (round six found
  this unacknowledged in the original draft): `static_analysis.py` doesn't
  catch any of these exception types by name today (it relies on
  `review/pipeline.py`'s generic per-step exception handling), so its
  behavior is unchanged, but the exception *type* now surfacing in its logs
  and error tracking for this path changes silently. `adapter/github/test_diff.py`'s
  updated assertions (Phase 1 Tests) cover the shared type change once at
  the adapter boundary, which is sufficient — no separate
  `static_analysis.py`-specific test is needed since its own exception
  handling is untyped and unaffected, but this is named explicitly rather
  than left as a silent, unacknowledged contract change on an unrelated,
  already-shipped step.
- New orchestration function, given the diff's changed file paths: fetches
  post-change content via `ingestion.diff.DiffFetcher.fetch_content()` at
  `head_sha` (raises the domain-owned `DiffContentUnavailableError`/
  `DiffContentMalformedError` above), and pre-change content via
  `indexing.tree.RepoTreeFetcher.fetch_tree(commit_sha=base_sha)` (raises
  `RepoTreeFetchError`/`RepoTreeTruncatedError`) then
  `RepoTreeFetcher.fetch_content(paths_with_blob_sha=...)` (raises
  `RepoTreeContentFetchError`) — reusing the exact "arbitrary path set at a
  commit" capability `spec-indexing.md`'s own Unit B built for precisely
  this kind of second consumer. **`base_sha is None` is checked explicitly
  before calling `fetch_tree`, not left to fail inside it** (round six
  added this — `base_sha` is optional at every layer per Assumption 2 and
  can legitimately be `None` in production whenever GitHub's payload omits
  `pull_request.base.sha`, not only during a deploy migration window; this
  was previously unhandled and not in Phase 5's typed-failure list, so an
  untyped `TypeError`/similar from `fetch_tree(commit_sha=None)` would have
  propagated uncaught and failed the whole job through the macro registry's
  `FailurePolicy.FATAL`, contradicting US7 for what should be a benign
  missing-data case). When `base_sha is None`, the function skips the
  pre-change fetch entirely and returns an empty pre-change chunk set.
  **Every per-run log (Phase 5) includes a `base_sha_missing: bool` field
  regardless of the job's overall outcome** (round eight added this): Phase
  1 only skips *blast-radius* resolution when `base_sha is None` — Phase 3's
  similarity context still runs off post-change content and is genuinely
  unaffected, so a job with `base_sha is None` but nonzero similarity hits
  would otherwise log `outcome=ok` exactly like a fully healthy run,
  silently masking that blast-radius context was dropped for that job (a
  reviewer found the original draft's "same `degraded_empty` outcome"
  framing only actually held for the zero-similarity-hits case, which
  Phase 5's own outcome rule doesn't guarantee). `base_sha_missing=true`
  makes this visible in the log regardless of what `outcome` ends up being,
  without needing a new outcome value.

**Tests:** Unit (`indexing/test_chunk.py`, extended — a multi-window
declaration's *second and later* windows each get a `line_range` starting
well after line 1 and matching the real file's actual line numbers, proven
against a real multi-hundred-line fixture function, not just a single-window
case that line 1 would pass by coincidence — round seven added this
specifically because three prior computation attempts would each have
passed a naive single-window test while still being wrong for this exact
case; and, round eight added, a whole-file chunk of a fixture file that
*ends with a trailing newline* — the near-universal case for real source
files — gets an `end_line` equal to the file's real last line, not one
past it, proving the `byte_range[1] - 1` off-by-one fix). Unit (`review/test_context_symbols.py` — hunk-header parsing,
byte-offset overlap resolution for TS/PHP/Go fixtures, the "no single
overlapping chunk" case, the renamed/removed-symbol case (US10), the
per-file `ChunkParseError`-doesn't-abort-the-rest case, the
file-absent-from-base-tree case, and the `max_changed_symbols_per_job`/
`max_changed_chunks_per_job` truncation case — all per ADR-0005). The
orchestration function's fetch-at-two-commits behavior is proven as part of
Phase 5's real end-to-end integration test, not a separate fakes-only test
claiming to be Integration-tier on its own — that same Phase 5 integration
test list gets a `base_sha = None` case (round six added this), proving the
job completes with `outcome=degraded_empty` rather than crashing. **Two
more test gaps a subsequent review found and closed:**
`apps/webhook_api/test_webhook_routes.py` gets a
new case extending its existing `head_sha`-assertion pattern (e.g.
`job.payload["head_sha"] == "a1"`) to `base_sha`, proving
`_build_webhook_event`/`_review_enqueue_payload` actually populate it —
without this, a wiring bug in that path (wrong JSON key, dropped field)
would go undetected despite Assumption 2 calling `base_sha` load-bearing
for citation accuracy; and `adapter/github/test_diff.py` gets a new case
proving `fetch_content()`'s re-raise/chaining (`DiffContentMalformedError`
with `__cause__` set to the original `GitHubDiffFetchMalformedResponseError`)
actually happens, with the existing
`test_fetch_content_blob_with_no_size_is_malformed_and_never_fetched` case
updated to assert the new domain-owned exception type instead of the
adapter-owned one it asserts today — otherwise this change would silently
break that existing, passing test; and a new case proving a
`GitHubBlobTooLargeError` (a forced oversized blob) re-raises as
`DiffContentUnavailableError`, not `DiffContentMalformedError` — the
specific branch the round-seven exception-mapping fix depends on.

**Observability & rollback:** The new `line_range` migration is additive
and nullable — rollback is a plain code revert plus leaving the unused
columns in place (never populated again, never read by anything else).
`ChunkParseError`s log at `warning`; the file-not-in-base-tree case and
ceiling-truncation logs use `info`/`warning` respectively as described
above.

**Acceptance:** US1's `Given/When/Then` passes as a unit test for TS/PHP/Go
fixtures each (Unit tier), plus as part of Phase 5's end-to-end integration
test; US10's pre-change-identity behavior passes the same way; ADR-0005's
scope list is updated to include this phase's case (this update itself is
part of this phase's own definition of done, not left implicit).

## Phase 2 — Blast-radius graph query (Unit B)

**Why next:** buildable and testable independently of Phase 1 using fixture
symbol names.

- In `review/context_builder.py`: `GraphQuery` Protocol, one method:
  `async def find_callers(self, *, repo_id: str, symbol_names: Sequence[str],
  limit_per_symbol: int) -> dict[str, tuple[CallerHit, ...]]` — one call
  across every resolved pre-change symbol in the job. `CallerHit` (new
  model, same file) carries the resolved edge's source `file_path`/
  `symbol_name` plus that caller's actual `Chunk`-shaped content, including
  its (possibly `None`, per Assumption 5's migration) `line_range`.
- New `adapter/storage/context_query.py`: `PostgresGraphQuery` implementing
  `GraphQuery`, in two batched stages (round three corrected both stages'
  SQL shape after round two's "batch it" fix left the actual mechanics
  underspecified or wrong). **Both stages reuse
  `adapter/storage/unit_of_work.py`'s existing `run_in_session` helper and
  `adapter/storage/indexing_repository.py`'s established
  session-open/`(SQLAlchemyError, OSError)`-translation wrapper shape** —
  the same pattern already used for these exact tables — rather than
  hand-rolling a second, parallel session-management mechanism:
  1. **Edge/node lookup, capped inside the query itself via a `LATERAL`
     join with a per-symbol `LIMIT`, not a window function over the whole
     matched set** — round three found the window-function design
     (`ROW_NUMBER() OVER (PARTITION BY ...) WHERE rn <= limit`) only bounds
     the number of rows *transmitted*, not the cost of *computing* them:
     Postgres must still gather and sort every matching edge in a
     partition before it can rank and truncate, so a diff touching
     several maximally hot symbols could still drive DB cost proportional
     to those symbols' real total caller counts. A `LATERAL` join instead
     runs one indexed, `LIMIT`-bounded subquery per symbol name —
     `SELECT ... FROM unnest(:symbol_names) AS s(name) CROSS JOIN LATERAL
     (SELECT node.file_path, node.symbol_name FROM index_graph_edge edge
     JOIN index_graph_node node ON edge.source_node_id = node.id AND
     node.repo_id = :repo_id AND node.pr_number = 0 WHERE
     edge.target_symbol_name = s.name ORDER BY node.file_path,
     node.symbol_name LIMIT :limit_per_symbol) hit` — letting Postgres use
     the existing `ix_index_graph_edge_target_symbol_name` index and stop
     scanning each symbol's matches once `limit_per_symbol` is reached,
     bounding actual computation, not just result size. The `ORDER BY
     node.file_path, node.symbol_name` tie-break is explicit and
     deterministic (round three found the window-function draft's
     unspecified `ORDER BY` would have made the truncation test
     non-deterministic), matching Phase 1's own "dropped deterministically"
     language for its ceiling. **The join condition remains the
     security-critical part, not just the performance-critical part:**
     `index_graph_edge` has no `repo_id` column of its own — isolation
     depends entirely on `node.repo_id`/`node.pr_number = 0` being part of
     the `LATERAL` subquery's own join condition, never an after-the-fact
     filter, because a scoping bug here would forward another org's — or
     another PR's untrusted, fork-authored — content into the requesting
     job's context bundle.
  2. **Content resolution, batched across every distinct `(file_path,
     symbol_name)` pair returned by stage 1 in one query, using row-value
     matching, not independent per-column `IN` lists** — round three found
     the round-two draft's "batched... in one query" description was
     underspecified in a way that invites a real correctness bug: filtering
     `file_path IN (...)` and `symbol_name IN (...)` as two independent
     lists is a cross product (any file matched against any symbol in the
     two lists), which could resolve a caller's citation to the *wrong*
     file's content — a correctness bug feeding directly into the citation
     accuracy Assumption 1 and US6 both depend on. The query instead binds
     the pairs as a single parameter and matches them as a unit:
     `WHERE (file_path, symbol_name) = ANY(:pairs)` (a Postgres row-value
     array), or equivalently a `VALUES (...) AS v(file_path, symbol_name)
     JOIN index_chunk ON ...` — never two separate `IN` clauses. **The pairs
     must be passed as genuine bound parameters, never string-built into
     the SQL text** (round eight named this explicitly) — `file_path`
     values originate from previously-indexed repository content that can
     contain arbitrary characters (quotes, backslashes), and this codebase
     has no existing local precedent for a composite/row-value array or
     multi-row `VALUES` literal to copy (every other adapter query in this
     codebase uses SQLAlchemy's ORM/Core query builder throughout), so this
     is genuinely new SQL-construction territory worth flagging rather than
     assuming is obviously safe. Filtered
     by `repo_id`, `pr_number = 0` as a plain `WHERE` condition — correct,
     but a structurally weaker isolation guarantee than stage 1's join
     (named explicitly, not left implicit, since round two found this
     distinction matters for what the isolation tests below need to
     prove).
- `ContextQueryError` (per Assumption 8) raised on a real Postgres failure
  at either stage, carrying an `operation` field (`"find_callers_edges"` /
  `"find_callers_content"`) to discriminate which stage failed.
- **Accepted, named cost profile, not a further-engineered fix** (round
  three): the `LATERAL`/`LIMIT` design bounds per-symbol computation to
  roughly `limit_per_symbol` indexed row reads each, times
  `max_changed_symbols_per_job` (50) symbols — a real, bounded worst case,
  but still a real multiplier over a typical single-symbol PR. Contained
  by Phase 5's single whole-step timeout (US7 degrades gracefully rather
  than failing if this proves slow), the same "no latency SLA is set,
  correctness within the ceilings is the goal" posture every prior phase
  in this codebase has already accepted — not a claim that worst-case cost
  equals typical-case cost.

**Tests:** Integration (`adapter/storage/test_context_query.py`, real
Postgres/pgvector via testcontainers) — a fixture graph with a real caller
edge returns it with content; **two distinct callers whose `(file_path,
symbol_name)` values overlap across columns but not as a row** (e.g. caller
A at `(fileX.ts, symbolY)` and caller B at `(fileY.ts, symbolX)`) each
resolve to their own, correct content — round four found the original test
list ("a fixture graph with a real caller edge") couldn't actually
distinguish the round-three row-value `= ANY(:pairs)` fix from the
independent-`IN`-lists cross-product bug it was fixing, since a single
caller can't exhibit a cross-product error; a symbol with zero edges returns empty; a
renamed symbol's pre-change name returns its real existing callers; a
symbol with more callers than `limit_per_symbol` is truncated to exactly
`limit_per_symbol` rows, deterministically by the stated `ORDER BY`
(proven by asserting the query's own row count and identity, not just
that *some* truncation happened); **four dedicated isolation cases, not
three** (round three found a third isolation dimension round two's tests
didn't cover, and round six split the single `pr_number` case into two —
stale "three, not two" wording from round three corrected here) — two
repos sharing an identical `target_symbol_name` (stage
1's join-based `repo_id` isolation), two repos sharing an identical
`(file_path, symbol_name)` pair in `index_chunk` (stage 2's `WHERE`-based
`repo_id` isolation), **and two dedicated `pr_number` cases, not a single
one reused across both stages** (a subsequent review found the original
description read as one `index_chunk`-shaped fixture that only clearly
exercises stage 2) — one repo with a colliding `target_symbol_name` where
the *caller node itself* (`index_graph_node`) exists only at `pr_number >
0`, proving stage 1's `LATERAL` join's own `node.pr_number = 0` condition
excludes it, and a separate one repo with a colliding `(file_path,
symbol_name)` pair in `index_chunk` present at both `pr_number = 0` and
`pr_number > 0`, proving stage 2's row never surfaces the PR-scoped
(potentially fork-authored, per `IndexChunkRow`'s own documented threat
model) content — a real, not cosmetic, isolation dimension distinct from
`repo_id`, since a `pr_number` filter bug at either stage would surface
untrusted PR-branch content as if it were trusted canonical code; a forced
Postgres failure at each stage proving `ContextQueryError` wrapping with
the correct `operation` value.

**Observability & rollback:** `PostgresGraphQuery`/`PostgresSimilarityQuery`
themselves never log — they translate driver failures into
`ContextQueryError` and raise, mirroring `adapter/storage/indexing_repository.py`'s
own established convention of zero `logger` calls in that adapter module
(round six named this explicitly: the Protocol methods carry no `job_id`
parameter, so a log line emitted from inside the adapter couldn't be
correlated to a job the way every other structured log in this plan is).
**Query failures log at `warning` with `job_id`, repo/symbol identity, and
the failing `operation` from `ContextBuilderStep.run()`, the caller that
already has `job_id` in scope** — the same caller-logs, adapter-translates
split `indexing/pipeline.py` already uses for `IndexingRepository`. Never
logging retrieved content itself. No schema change beyond Phase 1's
additive `line_range` column — rollback is a code revert.

**Acceptance:** US2 and US3's `Given/When/Then` pass as integration tests;
all four isolation tests (`repo_id` × 2 stages, `pr_number` × 2 stages) and
the `ContextQueryError`-wrapping tests pass.

## Phase 3 — Semantic similarity query (Unit C)

**Why next:** buildable and testable independently of Phases 1–2 using
fixture chunk content.

- In `review/context_builder.py`: `SimilarityQuery` Protocol, one method:
  `async def find_similar(self, *, repo_id: str, embedding: EmbeddingVector,
  exclude_file_path: str, limit: int) -> tuple[Chunk, ...]`.
- Extend `adapter/storage/context_query.py`: `PostgresSimilarityQuery`
  implementing `SimilarityQuery` via pgvector's `<=>` operator, scoped the
  same way as Phase 2: `... WHERE repo_id = :repo_id AND pr_number = 0 AND
  file_path != :exclude_file_path ORDER BY embedding <=> :vector LIMIT
  :limit` — a sequential scan + sort, no ANN index (Assumption 5). Raises
  `ContextQueryError(operation="find_similar", ...)` on a real Postgres
  failure.
- Query-side embedding: reuse `indexing.embedding.EmbeddingClient.embed()`
  via `asyncio.to_thread`, one batched call across every changed
  post-change chunk in the job (Assumption 6) — never a synthesized
  description, per the spec's decided query-embedding-source reasoning.
  **The *number* of similarity queries this issues is bounded by Phase 1's
  own `max_changed_chunks_per_job` ceiling** (Assumption 6's upstream fix —
  round two found the original claim that "Phase 4's per-item cap already
  bounds this" was incorrect, since Phase 4 runs *after* these queries
  execute; the actual bound now comes from capping the input at Phase 1,
  not the output at Phase 4). **Accepted, named cost profile, not a
  further-engineered fix (round three):** each of up to
  `max_changed_chunks_per_job` (50) similarity queries is its own
  sequential scan + sort over `index_chunk` (no ANN index, Assumption 5) —
  a real, bounded worst case of up to ~50 full scans per job, not
  proportionate to a typical single-file PR's actual cost. Contained by
  Phase 5's whole-step timeout (US7 degrades gracefully rather than
  failing) rather than reduced further — the same "no latency SLA is set"
  posture as Phase 2's own accepted cost profile, not a claim the two
  ceilings (50 chunks × sequential scan) were reconciled to a tighter
  bound.

**Tests:** Integration (`adapter/storage/test_context_query.py`, extended —
real Postgres/pgvector **and** the real local embedding model, no network) —
a fixture chunk finds a real similar chunk elsewhere in the repo; a chunk's
own file is correctly excluded; **two repos with similar/matching chunk
content prove one repo's similarity query never returns the other repo's
chunk** (round six added this — the Security review section's own claim
that "all three query paths" have dedicated `repo_id` isolation coverage
was false for this one: Phase 3's test list previously covered only the
`pr_number` dimension below, leaving this query path's `WHERE repo_id =
:repo_id` filter, a cross-tenant-exfiltration boundary, completely
untested); **a repo with a colliding chunk present at
both `pr_number = 0` and `pr_number > 0` proves the PR-scoped row is never
returned** (the same third isolation dimension Phase 2 tests, applied here
too); a forced Postgres failure proves `ContextQueryError` wrapping with
`operation="find_similar"`.

**Observability & rollback:** `PostgresSimilarityQuery` itself never logs,
matching Phase 2's own adapter-never-logs convention (see Phase 2's
Observability section). Query failures log at
`warning` with `job_id`, repo/file identity, and `ContextQueryError`'s own
`operation="find_similar"` field, from `ContextBuilderStep.run()`, the
caller that already has `job_id` in scope. Embedding-call failures
(`EmbeddingError`) log the same way but with `job_id`/repo/file identity
only — `EmbeddingError` has no `operation` field (round eight corrected an
earlier draft that implied it did); `detail` is `EmbeddingError`'s own
diagnostic field. No schema change — rollback is a code revert.

**Acceptance:** US4's `Given/When/Then` passes as an integration test; the
`repo_id` and `pr_number` isolation tests and the
`ContextQueryError`-wrapping test all pass.

## Phase 4 — Context assembly + budget enforcement (Unit D)

**Why next:** depends on Phases 1–3's *output shapes*, not their code.

- New `review/context_bundle.py`, pure and no-I/O (per ADR-0005 — **this
  phase's own deliverable includes updating that ADR's scope list for this
  case**): `ContextBundle` model (an ordered, ID-indexed sequence of items
  — each item's assigned integer ID is what Phase 6's citation rendering
  and the LLM's structured output reference) and
  `assemble_context_bundle(caller_hits: ..., similarity_hits: ...,
  diff_byte_size: int, max_total_bytes: int, *, job_id: str) -> ContextBundle`
  — `job_id` threaded through purely so its own drop-at-`warning` logs can
  be correlated to the originating job, mirroring
  `indexing/pipeline.py`'s `_apply_cost_ceilings(..., *, job_id: str)`
  signature exactly (a subsequent review found the original signature had
  no way to do this despite the phase's own "every drop is logged" claim).
- Budget enforcement, mirroring `indexing/pipeline.py`'s own
  `_apply_cost_ceilings` shape: cap at 10 caller edges per symbol and 5
  similarity matches per chunk first (per-item soft caps — now a second,
  independent layer on top of Assumption 6's upstream per-job ceilings,
  not the only layer), then truncate the combined set lowest-ranked-first
  until the total serialized byte size is at or under `min(diff_byte_size,
  llm_max_diff_bytes)`. **A single item whose own serialized size alone
  exceeds `max_total_bytes`** is dropped entirely, logged at `warning`.
  **`diff_byte_size = 0`** (a degenerate but reachable case — e.g. a
  pathological diff-fetch result) collapses the proportional budget to
  zero, correctly suppressing all context for that job rather than
  dividing by zero or erroring; named explicitly as its own test case,
  not left to be incidentally covered by "various diff sizes." Every drop
  is logged, never a failure.

**Tests:** Unit (`review/test_context_bundle.py` — per-item cap
enforcement, proportional-total truncation at various diff sizes, the
zero-hits-in empty-bundle case, the single-oversized-item-dropped-entirely
case, and the `diff_byte_size = 0` case, per ADR-0005). Per `testing.md`'s
mandatory story→integration-test rule, US9 itself (not just its pure
truncation logic) also gets integration coverage from Phase 5's end-to-end
step test — the same Unit+Integration split this plan already uses for
US1/US10/US6 (round four found the original draft left US9 Unit-only,
contradicting both `testing.md` and the spec's own test-level assignment).

**Observability & rollback:** N/A beyond what's already logged by the
truncation function — no schema, no I/O.

**Acceptance:** US9's `Given/When/Then` passes as a unit test across
several fixture sizes including both named edge cases, and as an
integration test in Phase 5 (a real end-to-end run whose combined
caller/similarity hits exceed the budget and are visibly truncated in the
posted `ContextBuilderOutput`); ADR-0005's scope list is updated for this
case.

## Phase 5 — Context Builder step + `llm_review` wiring (Unit E)

**Why next:** depends on Phases 1–4 all existing — the integration unit
that proves the spec's own goal end-to-end (US1, US5, US7, US8, US10).

- New `review/context_builder.py`: `ContextBuilderStep` (`name =
  "context_builder"`). Input: structurally accepts `StaticAnalysisOutput`
  or `PrefilterOutput` (see the conditional wiring below — its
  `depends_on` is not fixed). Output: `ContextBuilderOutput(JobIdentity)`
  carrying `diff`, `findings` (pass-through) plus a new `context_bundle:
  ContextBundle` field, **explicitly forwarding `base_sha=input.base_sha`**
  (Assumption 2's fourth site).
  - `run()` orchestrates Phases 1–4 in order.
  - **Timeout design (Assumption 7) — the single most important
    correctness property of this phase:** the *entire* internal
    orchestration is wrapped in **one**
    `asyncio.wait_for(..., timeout=context_builder_internal_timeout_seconds)`
    — never one `wait_for` per internal call. This value (default 25s) is
    strictly less than `context_builder_step_timeout_seconds` (default
    35s, the value the outer `StepRegistry` reads for every step), with
    the margin enforced by a `WorkerSettings`-level `@model_validator`
    (Assumption 7).
  - **Unconditional graceful degradation (US7):** every typed failure that
    can actually reach `run()`'s top level
    (`DiffContentUnavailableError`, `DiffContentMalformedError`,
    `RepoTreeFetchError`, `RepoTreeTruncatedError`,
    `RepoTreeContentFetchError`, `ContextQueryError`,
    `EmbeddingError`, and the internal `TimeoutError`) is caught inside
    `run()`. On any of these, `run()` logs at **`warning`** with an
    `outcome=degraded_error` field and the specific exception type, and
    returns `ContextBuilderOutput` with an empty `context_bundle`. It
    never re-raises. **`ChunkParseError` is deliberately not in this list**
    (round seven removed it — an earlier draft listed it here and Phase
    5's Tests section claimed to force it at this level, but Phase 1
    already states `ChunkParseError` "is caught per file, logged at
    `warning`, and that file contributes no symbols" fully inside
    `resolve_changed_symbols` itself; it can never propagate out to
    `run()`'s catch, so listing it here and testing it here would have
    exercised an unreachable code path instead of the real one).
  - A repo with no index yet, or zero hits found with no error, logs at
    **`info`** with `outcome=degraded_empty` — an expected case, not a
    failure signal. A fully successful run logs `outcome=ok`.
  - **No cross-job escalation/alerting logic is added in this plan** — an
    earlier draft added a per-repo, in-process consecutive-`degraded_error`
    counter that escalated a log line's level after repeated failures.
    Round four removed it: it wasn't asked for by
    `spec-context-builder.md`'s own Observability section (which scopes
    this plan to structured, per-job logs only, with no cross-job counter);
    it silently doesn't work under this platform's own supported
    production shape — `charts/review/values.yaml`'s
    `worker.replicaCount` lets the Worker scale horizontally, and a
    per-pod, per-process counter keyed by `repo_id` never accumulates a
    shared count across pods, so a repo failing on every single job could
    still never trip the threshold under ordinary multi-replica deployment,
    not just under the narrower "best-effort under concurrency" caveat the
    earlier draft named; and its stated rationale (that log-level
    escalation is "far more likely to be configured to surface" for
    self-hosted operators) is a claim about other companies' own
    infrastructure this platform has no visibility into and cannot verify,
    per [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md)'s own
    "no on-call burden for other companies' self-hosted deployments"
    framing. Per-job `outcome` logging above is judged sufficient for this
    plan's scope; a real cross-job alerting story is left for whenever a
    metrics/dashboard system exists, not invented ad hoc here. **A nonzero
    dropped-by-ceiling count in the per-run log below is itself the
    ceiling-proximity signal an operator needs** — it already reports the
    exact count dropped, not just a pass/fail, so no separate
    "approaching the ceiling" metric is needed on top of it.
- Extend `apps/worker/settings.py`: `context_builder_enabled: bool = True`,
  `context_builder_step_timeout_seconds: int = 35`,
  `context_builder_internal_timeout_seconds: int = 25`
  (round seven changed these from `float` to `int` — every existing
  `StepTimeouts` field and its `as_mapping() -> dict[str, int]` return type
  in `review/config.py` are `int`-typed, and `asyncio.wait_for(timeout=...)`
  accepts an `int` just as well; a `float` here would have been the only
  mismatched type in `StepTimeouts` and failed `mypy --strict`), plus the
  `@model_validator(mode="after")` enforcing
  `context_builder_step_timeout_seconds > context_builder_internal_timeout_seconds`
  directly on `WorkerSettings` (Assumption 7). Bootstrap config, read once
  at startup — **toggling the kill switch requires a Worker
  restart/redeploy, not a live config change**, the same operational shape
  as every other per-step enable flag in this codebase.
- **Extend `review/config.py`'s `StepEnablement` and `StepTimeouts` models
  (and their `as_mapping()` methods) with a `context_builder` field, and
  `apps/worker/main.py`'s `_build_step_enablement`/`_build_step_timeouts`
  to populate it from the new settings** — round two found this step was
  missing entirely from the original draft. Without it, `StepRegistry`'s
  `self._enabled.get(step.name, True)` would default a missing
  `"context_builder"` key to *enabled* regardless of the settings flag
  (the kill switch silently not working), and `self._timeouts[step.name]`
  (no `.get()`/default) would raise a raw `KeyError` the moment the step
  runs — crashing the job via an unhandled exception instead of the
  internal timeout ever getting a chance to fire, the exact "job marked
  FAILED" outcome Assumption 7 exists to prevent, just via a different
  failure mode.
- Extend `review/llm_review.py`'s `_LLMReviewInput` with
  `context_bundle: ContextBundle | None = None`. Extend
  `_build_user_message`/prompt construction to append a clearly-labeled
  "Related code in this repository" section from a non-empty
  `context_bundle`, distinct from the diff text and static-analysis
  findings — never merged into the diff text, per US5. An empty or absent
  bundle produces no such section, byte-for-byte identical to today's
  Phase 1 prompt. **The section is prefaced with an explicit instruction
  that it is inert reference data, never a set of instructions to follow**
  — a subsequent review's cheap defense-in-depth response to the "arbitrary
  existing repo content reaching the LLM prompt" risk named in the Security
  review section below. **This alone does not reach Phase 6 — a subsequent review
  found the original draft stopped at the prompt-building input and never
  forwarded the bundle any further, leaving Phase 6's citation mechanism
  with no data to work from:** `LLMReviewOutput` (not just
  `_LLMReviewInput`) also gains `context_bundle: ContextBundle | None =
  None`, explicitly forwarded at its construction site the same way this
  plan already threads `base_sha` through every Output model (Assumption
  2) — `LLMReviewOutput` is what actually reaches `FindingsOutputStep` as
  its input, and `_LLMReviewInput` alone never does.
- Composition root (`apps/worker/main.py`): construct one
  `PostgresGraphQuery`/`PostgresSimilarityQuery` instance using the same
  database session factory `IndexRepository` already uses; wire the same
  `EmbeddingClient` instance `_build_indexing_deps` already constructs into
  `review_registry`'s new step too. **Extend `_build_review_registry`'s
  existing conditional `depends_on` wiring to a complete, all-combinations
  rule** — round two found the original three-way extension left one
  reachable combination
  (`context_builder_enabled=True` and `static_analysis_can_produce_findings=False`,
  confirmed reachable: `review/config.py`'s `check_step_configuration_viable`
  explicitly treats this as a legitimate configuration) completely
  unspecified, which would crash `StepRegistry.__init__` or `_resolve_input`
  on every job in that configuration. Confirmed-correct complete rule,
  covering all four reachable combinations:
  - `context_builder`'s own `depends_on` = `"static_analysis"` if
    `static_analysis_can_produce_findings(enablement)` else `"prefilter"`
    — mirroring `llm_review`'s existing fallback rule, applied one hop
    earlier, whenever `context_builder_enabled` is `True`.
  - `llm_review`'s `depends_on` = `"context_builder"` if
    `context_builder_enabled` is `True` (regardless of what
    `context_builder` itself depends on); otherwise the existing two-way
    conditional (`"static_analysis"` if `static_analysis_can_produce_findings`
    else `"prefilter"`) is unchanged.
  - **`ContextBuilderStep` is unconditionally constructed and placed in
    `_build_review_registry`'s `steps=[...]` list, exactly like every
    other step, and is never conditionally omitted from registration**
    (round three corrected this — an earlier draft claimed it would be
    "never registered at all" when disabled, which contradicts this
    codebase's actual, confirmed mechanism: `apps/worker/main.py`'s real
    `_build_review_registry` unconditionally constructs every step
    regardless of enablement, and `review/pipeline.py`'s `StepRegistry`
    controls whether a registered step actually runs purely via the
    `enabled: Mapping[str, bool]` lookup — `self._enabled.get(step.name,
    True)` — never via the step's presence or absence in the list. This
    is also the only description consistent with this phase's own fix two
    bullets below: extending `StepEnablement`/`StepTimeouts` with a
    `context_builder` key only matters, and only makes the `KeyError`/
    silently-stays-enabled bugs reachable in the first place, if the step
    *is* unconditionally registered — a disabled-and-never-registered step
    would never reach either lookup at all). When `context_builder_enabled`
    is `False`, its `depends_on` value is irrelevant since
    `enabled["context_builder"] = False` means it's skipped at run time
    regardless — the rule above only needs to produce *some* valid,
    registered-step-name value (`"static_analysis"` or `"prefilter"`, per
    the same fallback logic) so `StepRegistry.__init__`'s dependency
    validation doesn't reject it as pointing at a truly absent step.
  `review/test_step_registry.py:90-113`'s existing fake `context_builder`
  step proves the framework supports inserting a new step at an arbitrary
  position with zero changes to unrelated steps' own code; it does not
  prove, and this plan doesn't claim it proves, that repointing
  `llm_review`'s existing conditional needs no new composition-root logic
  — that new logic (the complete rule above) is this phase's own work,
  tested directly.

**Tests:** Integration (`review/test_context_builder_step.py`, real
Postgres via testcontainers, fakes for `DiffFetcher`/`RepoTreeFetcher`) —
the full Phase 1→4 orchestration at two commits (US1, US10), a non-empty
bundle reaching `llm_review`'s completion request (US5, extending
`review/test_llm_review_step.py` with a recorded LiteLLM cassette), every
typed failure individually forced and each producing an empty bundle
logged at `warning` with `outcome=degraded_error` plus a completed (not
failed) job (US7) — including a case proving the internal timeout fires
before the outer step timeout ever could — a case proving Phase 4's budget
enforcement actually truncates the assembled bundle end-to-end when
combined caller/similarity hits exceed `max_total_bytes` (US9's own
integration coverage, not just Phase 4's unit-level truncation test), a
mixed in-scope/out-of-scope-language PR still getting context for its
resolvable symbols (US8), **two `base_sha = None` cases, not one** (round
six added the first; round eight added the second after finding the first
alone was insufficient) — a `base_sha = None` job with zero similarity
hits too, completing with `outcome=degraded_empty` rather than an uncaught
exception failing the job, and a `base_sha = None` job that *does* have
nonzero similarity hits, proving it logs `outcome=ok` **and**
`base_sha_missing=true` together, rather than the missing-blast-radius
condition disappearing into an indistinguishable `ok` — `context_builder_enabled = False` producing
byte-for-byte Phase 1 behavior, and **all four combinations** of
`context_builder_enabled` × `static_analysis_can_produce_findings` in
`apps/worker/test_main.py` — including the specific combination
(`context_builder_enabled=True`, `static_analysis_can_produce_findings=False`)
round two found unhandled, proving `context_builder` falls back to
depending on `"prefilter"` and the job completes successfully. A unit test
on `WorkerSettings` itself (`apps/worker/test_settings.py`, mirroring
`apps/webhook_api/test_settings.py`'s existing `pytest.raises(ValidationError)`
pattern for its own cross-field validator) proves
`context_builder_internal_timeout_seconds >= context_builder_step_timeout_seconds`
is rejected at construction — round four found this validator, "the single
most important correctness property of this phase" by the plan's own
words, had no test of its own anywhere in the original draft. **This
phase's own deliverable includes registering this case in ADR-0005's
"Scope, extended per phase" list** (round eight added this — every other
new pure-logic-unit-tested case in this plan, Phases 1/4/6, explicitly
registers itself there; this one was the sole exception). Separately, a
round-trip test in `review/test_llm_review_step.py` (extending its
existing LiteLLM-cassette coverage) proves `_parse_llm_findings` actually
converts the LLM response's JSON `context_ref_ids: list[int]` into
`Finding.context_ref_ids: tuple[int, ...]` on a real constructed `Finding`
— round eight added this after finding no test anywhere exercised this
conversion: `review/test_citation.py` calls `render_citation` with a
hand-built list directly, never via a parsed `Finding`, leaving the
actual `list`→`tuple` conversion this plan's own hashability fix depends
on completely untested.

**Observability & rollback:** Logs per run: `job_id`, PR identity, counts
of resolved symbols/edges found/matches found/dropped-by-ceiling (both the
new upstream and existing per-item ceilings), total context bytes,
duration, and the `outcome` field (`ok` / `degraded_error` at `warning` /
`degraded_empty` at `info`; see Phase 5's body above for why round four
removed the earlier draft's cross-job escalation logic). **On a successful
(`ok`) run, the log also includes the `file_path`/`symbol_name` identity of
every bundle item actually included** — a subsequent review found the
original draft logged only counts on the success path, which left the
Security review's own named residual ("arbitrary existing repo content
reaching the LLM prompt for the first time," a planted-content/
prompt-injection concern) with no way to reconstruct after the fact which
files were actually spliced into a given job's prompt. **Bounded, not
unbounded, log volume:** the per-item cap (10 edges/symbol × 50 symbols, 5
matches/chunk × 50 chunks — Phase 2/4's existing ceilings) means this is at
most ~750 identity lines per job, an upper bound already implied by
ceilings this plan established elsewhere for cost reasons, not a new,
undiscussed volume this addition introduces on its own. `file_path`/
`symbol_name` are already established as safe to log elsewhere in this
plan (Phase 2/3's query-failure logs use the identical fields, "never
logging retrieved content itself"), so this reuses that same boundary, not
a new one. On `degraded_error`, the log includes not just the exception
type but also `ContextQueryError`'s own `operation`/`detail` fields when
that's the exception involved — the one diagnostic signal Assumption 8
built `ContextQueryError` to carry, useless if the degradation log doesn't
surface it. **`ContextBuilderStep` also emits its own, new
`logger.warning("context_builder degraded", ...)` line on `degraded_error`
— a bespoke call inside `run()` itself, not a reuse of any existing
framework mechanism** (round six corrected this: an earlier draft claimed
this "reuses `review/pipeline.py`'s existing 'step isolated: failed'
log line," which is inaccurate — that exact line is emitted solely by
`StepRegistry._run_one` when `FailurePolicy.ISOLATE` catches a step that
*raises*, a mechanism `static_analysis.py`'s inner per-analyzer registry
uses and this macro step, under `FailurePolicy.FATAL`, does not; since
`ContextBuilderStep.run()` is designed by US7 to never raise, that
framework branch can never fire for it regardless of outcome, and the
framework's own generic "step completed" (info) line fires unconditionally
instead). Emitting a distinct, explicit `warning`-level line from inside
`run()` is still the right fix for the same underlying gap this was meant
to close — an operator scanning logs for warning-level step problems would
otherwise never notice `context_builder` degradations — it just needs to
be described accurately as new step-level logic, not as invoking existing
framework behavior. **Deliberate, minor
deviation from `spec-context-builder.md`'s "same shape as indexing's and
Phase 1's own per-step logs" instruction:** no existing per-job log in this
codebase uses a named `outcome` enum field (existing logs signal
success/degradation via level plus free text) — the plan keeps `outcome`
as a new, explicit field because Phase 5 has three meaningfully distinct
non-error outcomes to distinguish (`ok`/`degraded_error`/`degraded_empty`),
more than existing per-step logs need. A per-repo/per-day aggregate
dashboard is explicitly out of scope for this plan (no metrics/dashboard
system exists through Phase 2, per every prior spec's stance) — per-job
structured logging is this plan's whole observability story, matching
`spec-context-builder.md`'s own stated scope, not a claim of parity with a
real alerting system. Kill switch (`context_builder_enabled = False`,
requiring a Worker restart) is the operational rollback lever; the
existing `apps/worker/main.py` `_check_step_configuration` startup log
(which already logs `enablement.as_mapping()`) is what lets an operator
confirm at Worker boot whether the kill switch actually resolved to
enabled/disabled — no new logging needed for that. No schema change beyond
Phase 1's additive `line_range` columns, so no data cleanup is ever needed
regardless of switch state.

**Acceptance:** US1, US5, US7, US8, US9, and US10's `Given/When/Then` all
pass as integration tests; the timeout-margin test (both the runtime race
test and the new `WorkerSettings` unit test), the
`context_builder_enabled = False` regression case, and all four
conditional-wiring combinations pass; every existing
`review/test_llm_review_step.py` test for the bare-diff-only path still
passes unchanged.

## Phase 6 — Citation rendering in posted findings (Unit F)

**Why last:** depends on Phase 5's `ContextBundle` shape actually existing
and reaching a real posted finding. **This phase's own deliverables include
writing the plan's one E2E test** (named in the Traceability table below;
round two found no phase had explicitly committed to it) —
`tests/e2e/test_context_builder.py`, the full Docker Compose stack proving
a fixture repo's real cross-file caller produces a posted finding with a
real citation.

- Extend the LLM's forced structured-output schema (`llm_review.py`'s
  findings JSON contract) with an optional `context_ref_ids: list[int] |
  None = None` field per finding (the JSON schema itself; a plain `list` is
  the correct shape here, since this is deserialized straight from the
  LLM's JSON response, not stored on a frozen model), **capped at a small
  fixed length (e.g. `max_length=5`) in the schema itself** (round eight
  added this): the Security review's own named residual (planted/indexed
  content attempting to steer the LLM's output) means a manipulated
  completion could otherwise emit an unbounded number of fabricated IDs on
  one finding, each producing its own `warning`-level log line in
  `render_citation` — a log-volume/noise vector distinct from, and not
  covered by, Phase 5's own bounded-log-volume analysis (which only bounds
  the per-run bundle-item success log, not per-finding invalid-ID
  warnings). **`findings/models.py`'s
  shared `Finding` model gains the same field typed as `context_ref_ids:
  tuple[int, ...] | None = None` — a tuple, not a list** (a subsequent
  review found the original draft added this field only to the LLM's
  response schema, never to `Finding` itself — since `Finding` is frozen
  with no `extra="allow"`, `_parse_llm_findings`'s `Finding(...)`
  construction would silently have nowhere to put a parsed
  `context_ref_ids` value, and `FindingsOutputStep` would have no ref-ids to
  give `render_citation` for any finding, regardless of any other fix;
  round six further corrected the field's type on `Finding` specifically —
  a `list` is unhashable, and `frozen=True` on a pydantic model generates
  `__hash__` from field values, so a `list`-typed field would make
  constructing a `Finding` with a non-`None` `context_ref_ids` raise
  `TypeError` the moment anything hashes it — `findings/merge.py`'s own
  docstring already flags future dedupe/set-based logic over `Finding` as a
  concern, and `Assumption 5`'s own `Chunk.line_range` already chose a
  tuple over a list on the same reasoning). `_parse_llm_findings` converts
  the JSON schema's `list[int]` to a `tuple[int, ...]` when constructing
  `Finding`. Defaulting to `None` keeps every static-analysis-sourced
  `Finding` unaffected — only `_parse_llm_findings` ever sets it. **Round
  ten correction — this residual was later closed, not left accepted:**
  an earlier round of this plan named `findings/merge.py`'s "first
  occurrence wins" dedup keeping whichever `Finding` appears first in
  `LLMReviewOutput.findings` (static-analysis findings before LLM ones,
  per `review/llm_review.py`'s own ordering) as an accepted gap — an LLM
  finding that dedupes against a similar static-analysis finding on the
  same file/line would lose, silently discarding its `context_ref_ids`
  (and thus its citation) along with the rest of the finding. A later code
  review judged this narrow enough and safe enough to fix outright rather
  than continue accepting: `_dedupe` now carries `context_ref_ids` forward
  onto the kept (first-seen) `Finding` whenever the dropped duplicate
  carries it and the kept one doesn't ("first non-`None` value wins", not
  "last duplicate wins", so a kept finding's own real citation is never
  overwritten by a later duplicate's). `message`/`severity`/`source`
  precedence is unchanged — the kept finding still reads as the
  static-analysis finding's own text, just citing the same real,
  already-budget-truncated context a same-defect LLM finding would have.
- **`review/output.py`'s `FindingsOutputInput` gains
  `context_bundle: ContextBundle | None = None`**, forwarded from
  `LLMReviewOutput`'s own new field (added above) the same way every other
  field on `FindingsOutputInput` already mirrors `LLMReviewOutput` — without
  this, `FindingsOutputStep` has the per-finding `context_ref_ids` but
  nothing to resolve them against.
- New `review/citation.py`, pure and no-I/O (per ADR-0005 — **this phase's
  own deliverable includes updating that ADR's scope list for this
  case**): `render_citation(ref_ids: list[int], bundle: ContextBundle, *,
  job_id: str) -> str | None` — `job_id` (already available on
  `FindingsOutputInput`/`JobIdentity`, so this is a zero-cost addition, not
  new plumbing) is threaded through purely so an unknown-ID `warning` log
  can be correlated to the originating job (round six added this — the
  same gap already fixed for `assemble_context_bundle`/
  `resolve_changed_symbols`, missed here in an earlier draft) — looks up
  each ID against the bundle's own already-retrieved
  data and renders a citation from that data alone. **Renders file path,
  symbol name, and line range only — never the bundle item's underlying
  `Chunk.content`/source text.** This is a deliberate scope boundary, not
  an oversight (round four added it): `FindingsOutputStep` posts citation
  text *after* `findings.redact.TextRedactor` has already run, which today
  only ever runs upstream inside `review/llm_review.py`/
  `review/static_analysis.py` per its own docstring — a citation that
  echoed raw chunk content would bypass redaction entirely and could post
  an unrelated file's real source (potentially containing a secret nobody
  flagged for removal) verbatim into a PR comment, a materially worse and
  irreversible outcome versus the diff-only content Phase 1 already
  exposes. **A bundle item with `line_range = None`** (a chunk indexed
  before Assumption 5's migration backfilled it) **renders a citation
  without the line-range portion**, logged at `info`, never an error or a
  dropped citation. **An ID with no matching bundle entry is silently
  dropped from the citation, never a `KeyError`, but is logged at
  `warning`** (round two raised this from `info` to `warning` — a
  fabricated/stale ID is a qualitatively different signal than "found
  nothing," possibly indicating the LLM hallucinating references, and
  deserves the same visibility tier as a genuine degrade, not the same
  tier as an expected no-op).
- `review/output.py` (`FindingsOutputStep`) calls `render_citation` with
  each finding's own `context_ref_ids` and the step input's
  `context_bundle`, then, for any non-`None` result, **builds a new
  `Finding` via `finding.model_copy(update={"message": finding.message +
  "\n\n" + citation_text})` and passes that modified `Finding` — in place
  of the original — into the existing, unchanged
  `self._publisher.post_review(...)`/`post_check_run(...)` calls
  (the `FindingsPublisher` Protocol)**, not by mutating `finding.message`
  directly (`Finding` is `frozen=True`) and not by calling any
  adapter-internal function from `review/output.py` itself. **Round eight
  correction (the second and final version of this fix):** round seven's
  own fix said this happens "before calling `_finding_body(new_finding)`"
  — wrong on two counts, independently caught by four reviewers:
  `_finding_body` is a private, underscore-prefixed helper defined and used
  only inside `adapter/github/posting.py` (an adapter module), never
  imported into or callable from `review/output.py` (a domain module)
  today; `FindingsOutputStep` only ever hands `Finding` objects to the
  `FindingsPublisher` Protocol's `post_review`/`post_check_run` methods,
  which call `_finding_body` internally, later, inside the adapter. Naming
  `_finding_body` as something `review/output.py` calls directly would
  have required a domain→adapter import of a private symbol — the exact
  violation class round four already found and fixed once for `DiffFetcher`,
  recurring here undetected through three prior review rounds. The fix is
  simpler than either prior description: pass the already-`model_copy`'d
  `Finding` through the *existing* publisher call unchanged, and let
  `_finding_body`'s own `escape_markdown(finding.message)` — which already
  runs on every finding regardless of this feature — pick up the appended
  citation text automatically. **Ordering relative to `FindingsOutputStep`'s
  existing dry-run early return** (which logs findings and returns before
  `post_review`/`post_check_run` are ever called): the `model_copy` step
  must run *before* that dry-run branch, not only on the real-post path —
  round eight added this after a reviewer noted the module's own docstring
  requires dry-run output to reflect "the same... objects a real post would
  use," and a citation applied only on the post path would silently make
  dry-run diverge from what actually gets posted.

**Tests:** Unit (`review/test_citation.py` — a valid ID renders the right
text, an unknown ID renders nothing and logs at `warning`, an empty
`ref_ids` renders nothing, **a rendered citation for a real ID contains
only file path/symbol/line-range text, never the bundle item's underlying
`Chunk.content`** (round four added this — the metadata-only scope above
needs its own regression test, not just a design statement), and **a
bundle item with `line_range = None` still renders a citation, omitting
only the line-range portion** (a subsequent review added this — proving
the pre-migration/un-backfilled case degrades gracefully rather than
erroring or dropping the whole citation), per ADR-0005). Integration
(`review/test_findings_output_step.py`, extended — a posted finding with a
real citation via a recorded GitHub cassette, confirming the citation text
survives `escape_markdown` intact; a finding with no `context_ref_ids`
posts exactly as a Phase 1 finding would). E2E
(`tests/e2e/test_context_builder.py`, new — see above).

**Observability & rollback:** No new I/O beyond the existing posting call.
No schema change beyond Phase 1's additive `line_range` columns — rollback
is a code revert.

**Acceptance:** US6's both `Given/When/Then` scenarios pass as integration
tests; the invalid-ID-drops-safely-and-logs-at-warning case passes as a
unit test; the E2E test passes; ADR-0005's scope list is updated for this
case.

## Security review

One pass across every new input/trust boundary this plan as a whole
introduces:

- **PR base-commit content fetch for an arbitrary path set at an
  attacker-influenced commit** (Phase 1) — reuses the existing
  installation-token exchange and `RepoTreeFetcher`'s already-applied
  symlink/submodule mode guard and per-blob size ceiling. Same
  realistic-threat-actor framing as indexing's own security review: a
  fork-based PR's `opened`/`synchronize` event already lets an untrusted
  external contributor make the platform fetch and Tree-sitter-parse their
  own attacker-controlled code — this is a second fetch+parse of the same
  threat class, not a new one.
- **Cross-tenant and cross-PR data exfiltration via the blast-radius/
  similarity queries** (Phases 2–3) — two distinct isolation dimensions
  (`repo_id`, `pr_number`) across three query paths, six tests total, not
  one dimension tested once (round two found the original draft only
  proved and only described `repo_id` isolation for one of two stages;
  round three found a second dimension, `pr_number`, was missing entirely
  — wording in this section previously said "three... dimensions"
  inconsistently with its own later "both dimensions" summary, corrected
  here): the edge/node lookup's `repo_id` isolation is
  **join-condition-enforced** (`index_graph_edge` has no `repo_id` column
  at all, so `repo_id`/`pr_number` must be part of the `LATERAL` join to
  `index_graph_node`, not an after-the-fact filter); the caller-content
  and similarity lookups' `repo_id` isolation is a plain `WHERE repo_id =
  :repo_id` filter — correct, but structurally weaker, named as such
  rather than implied to carry the same join-level guarantee. **All three
  query paths also filter `pr_number = 0`**, which is a real isolation
  boundary in its own right, not cosmetic: `IndexChunkRow`'s own schema
  documents `pr_number > 0` rows as potentially fork-authored, untrusted
  content, isolated from the canonical index by key rather than rejected
  at write time — a `pr_number` filter bug at any of the three query paths
  could surface untrusted PR-branch content as if it were trusted canonical
  code. **Both dimensions are covered by a dedicated test at each of the
  three query paths — six isolation tests total, not three** (round six
  found the similarity query's own `repo_id` isolation was claimed here but
  never actually tested anywhere in Phase 3, despite being the identical
  cross-tenant-exfiltration shape as the caller-content stage's own
  dedicated test; fixed by adding it).
- **Resource exhaustion via per-job query cost** (Phases 2–3, Assumption
  6) — bounded at multiple independent points, though not eliminated
  (round two's "batch the calls" fix closed only round-trip *count*; round
  three found the initial "in-query `LIMIT`" fix closed transmitted *size*
  but not *computation* cost, and corrected it to a `LATERAL`-join design
  that lets Postgres use the existing index to stop scanning early per
  symbol): an upstream ceiling on how many symbols/chunks generate a query
  at all (`max_changed_symbols_per_job`/`max_changed_chunks_per_job`,
  applied before any query runs), a `LIMIT` enforced inside each per-symbol
  subquery (bounding both computation and transmission, not just the
  latter), and a single whole-step timeout bounding total wall-clock.
  **Named, accepted residual:** the worst case (a diff touching the
  maximum allowed number of maximally "hot" symbols/chunks) is still a
  real multiplier — on the order of 50× a typical single-file PR — over
  typical-case cost; this plan bounds that worst case to a fixed ceiling
  and lets it degrade gracefully on timeout (US7) rather than eliminating
  the multiplier itself, the same "no latency SLA, correctness within
  ceilings" posture every prior phase in this codebase has accepted.
- **Tree-sitter parsing of untrusted content, a second time per review** —
  identical risk profile to `spec-indexing.md`'s own named, accepted gap.
  Not a new risk category, a second call site exercising an already-accepted
  one; now correctly moved off the event loop via `asyncio.to_thread`
  (Phase 1), matching indexing's own precedent.
- **Arbitrary existing repo content reaching the LLM prompt for the first
  time** (Phases 5–6, added in round four) — until now, `llm_review.py`'s
  completion request is built only from the diff plus static-analysis
  JSON, both either the PR author's own change or a deterministic tool's
  output. This plan's context bundle splices in one-hop callers and
  semantically-similar chunks from files the PR author may never have
  touched or reviewed, including previously-merged (`pr_number = 0`)
  content — a new class of input reaching the prompt, not just a bigger
  diff. A comment or string planted in already-merged code could attempt
  to steer the reviewing LLM's output (e.g. suppressing an unrelated
  finding in the same completion) the next time it's pulled in as context.
  **Accepted, not fully solved — a subsequent review found the original
  framing overstated how bounded this is:** the forced structured JSON
  output schema, `escape_markdown` on render, and the existing
  `TextRedactor` pass over `message` bound *formatting* and *secret-leakage*
  risk, but none of the three actually bounds the specific example named
  above — a planted instruction attempting to steer the LLM's own judgment
  (e.g. suppressing an unrelated finding) is a judgment-manipulation risk,
  not a formatting one, and no control here touches it. **One cheap
  defense-in-depth mitigation is added as a result:** the "Related code in
  this repository" section's own label (added in Phase 5) is accompanied by
  an explicit system-prompt instruction that this section is inert
  reference data to inform review, never an instruction to follow — a
  standard, low-cost RAG-prompt mitigation that doesn't eliminate the risk
  but is worth doing given how cheap it is. This remains a genuinely new
  trust-boundary crossing worth naming explicitly, unlike every other
  crossing this section already covers, and worth revisiting if a stronger
  guarantee is ever needed.
- **`context_ref_ids`, an LLM-controlled field feeding into rendered PR
  comment content** (Phase 6) — the LLM only ever selects an integer ID
  from a platform-controlled, already-retrieved set; it never supplies
  free text that becomes citation content. An out-of-range or fabricated
  ID degrades to no citation, logged at `warning`, never a crash. **Round
  nine correction — the original claim here was wrong in a way a later
  code review caught:** an earlier draft had `render_citation`'s output
  appended directly into `Finding.message`, relying on
  `adapter/github/posting.py`'s existing `escape_markdown(finding.message)`
  call to sanitize it — true as far as it went (the embedded file
  path/symbol values genuinely did get escaped), but that same blanket
  pass also escaped `render_citation`'s own Markdown template characters
  (backticks, the bold `**Context:**` header), so every posted citation
  rendered as literal backslash-escaped text instead of the intended
  formatting. Fixed by giving `Finding` its own `citation_markdown` field
  (kept separate from `message`, never merged into it) and moving the
  escaping *into* `render_citation` itself — it now escapes each embedded
  file path/symbol name individually before building its own template
  around them, so the template survives while the untrusted values inside
  it are still sanitized exactly as before. `adapter/github/posting.py`
  appends `citation_markdown` unescaped, after its own
  `escape_markdown(finding.message)` pass, never re-escaping it.
  **`render_citation` is scoped to file path/symbol/line-range metadata
  only, never the bundle item's underlying `Chunk.content`** (round four
  added this constraint, with its own regression test in Phase 6) —
  without it, a citation could echo an unrelated file's real source text
  verbatim into a PR comment, entirely bypassing `TextRedactor` (which only
  ever runs upstream of `FindingsOutputStep`), a materially worse and
  irreversible secret-leakage vector the metadata-only scope closes rather
  than merely mitigates.
- **No new credential type, no new write capability, no new Postgres
  table.** Every new query reuses the existing database connection and
  read-only access to tables `spec-indexing.md` already created; the only
  new write surface (Phase 6) is additional text inside an already-existing
  GitHub Adapter posting call.

## Compatibility & rollout

- **No existing API/schema contract breaks — verified against the real
  code, not assumed:** `PullRequest`/`WebhookEvent`/`JobIdentity` all gain
  new *optional* fields. `_LLMReviewInput`/`LLMReviewOutput`/
  `FindingsOutputInput` each gain one new `Optional`-typed field defaulting
  to `None`; `findings.models.Finding` likewise gains `context_ref_ids:
  tuple[int, ...] | None = None` (a tuple, not a list — Assumption 5's own
  reasoning for `Chunk.line_range` applies identically here, since `Finding`
  is a frozen, hashable model), defaulting every existing (static-analysis)
  `Finding` construction site to unaffected. `StaticAnalysisOutput`'s own
  shape is untouched (only its construction call site gains one forwarded
  field, alongside three sibling call sites including Phase 5's new
  `ContextBuilderOutput`). **One schema change, not zero** — Assumption 5's
  nullable `start_line`/`end_line` migration on `index_chunk` alone (not
  `index_graph_node` — see Assumption 5's own correction) is purely
  additive (no existing column touched, no
  backfill required to deploy safely) and is the sole exception to this
  plan's otherwise no-migration posture.
- **Staged rollout via the kill switch (Phase 5):**
  `context_builder_enabled` defaults to `True` per the spec's own decided
  NFR — a pure, already-bounded read path with unconditional graceful
  degradation (US7) as its own built-in safety net. Toggling it requires a
  Worker restart (bootstrap config, read once at startup).
- **Dependency-failure behavior**, covering every external dependency this
  plan touches, each with a named typed exception and confirmed catch
  point (Phase 5): GitHub API (the domain-owned `DiffContentUnavailableError`/
  `DiffContentMalformedError` wrapping `ingestion.diff.DiffFetcher`'s
  adapter-level failures, plus
  `RepoTreeFetchError`/`RepoTreeTruncatedError`/`RepoTreeContentFetchError`),
  Postgres (`ContextQueryError`), the local embedding model
  (`EmbeddingError`), and the internal step timeout (`TimeoutError`) —
  every one degrades to an empty bundle inside `ContextBuilderStep.run()`;
  none propagates to fail the review job, and the framework's own
  enablement/timeout maps are correctly populated (Phase 5) so the kill
  switch and timeout margin are load-bearing, not merely declared in
  settings.
- **The change is fully undoable** via the kill switch or a plain code
  revert — with no data cleanup ever required, since this plan writes no
  new *rows* to Postgres at any phase (Assumption 5's migration adds
  nullable columns only, never populated by anything outside `indexing`'s
  own existing write path, and safe to leave in place unused on rollback).

## Traceability: PRD user story → test

| Story | Phase | Test path | Level |
| --- | --- | --- | --- |
| US1 | 1 (unit) + 5 (integration) | `review/test_context_symbols.py` + `review/test_context_builder_step.py` | Unit + Integration |
| US2 | 2 | `adapter/storage/test_context_query.py` | Integration |
| US3 | 2 | `adapter/storage/test_context_query.py` | Integration |
| US4 | 3 | `adapter/storage/test_context_query.py` | Integration |
| US5 | 5 | `review/test_llm_review_step.py` (extended) | Integration |
| US6 | 6 | `review/test_citation.py` (unit) + `review/test_findings_output_step.py` (integration) | Unit + Integration |
| US7 | 5 | `review/test_context_builder_step.py` | Integration |
| US8 | 5 | `review/test_context_builder_step.py` | Integration |
| US9 | 4 (unit) + 5 (integration) | `review/test_context_bundle.py` + `review/test_context_builder_step.py` | Unit + Integration |
| US10 | 1 (unit) + 5 (integration) | `review/test_context_symbols.py` + `review/test_context_builder_step.py` | Unit + Integration |
| NFR — proportional budget incl. `diff_byte_size = 0` | 4 | `review/test_context_bundle.py` | Unit |
| NFR — per-item ceilings + oversized-single-item drop | 4 | `review/test_context_bundle.py` | Unit |
| NFR — upstream `max_changed_symbols/chunks_per_job` ceiling | 1 | `review/test_context_symbols.py` | Unit |
| NFR — `base_sha` webhook wiring (`_build_webhook_event`/`_review_enqueue_payload`) | 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| NFR — `base_sha = None` degrades to `degraded_empty`, never an uncaught exception | 1, 5 | `review/test_context_builder_step.py` | Integration |
| NFR — `base_sha = None` with nonzero similarity hits logs `base_sha_missing=true` alongside `outcome=ok` | 1, 5 | `review/test_context_builder_step.py` | Integration |
| NFR — `DiffFetcher` adapter exception re-raised as domain-owned type | 1 | `adapter/github/test_diff.py` | Integration |
| NFR — citation renders without a line-range portion when `line_range = None` | 6 | `review/test_citation.py` | Unit |
| NFR — cross-tenant (`repo_id`) isolation, edge/node join stage | 2 | `adapter/storage/test_context_query.py` | Integration |
| NFR — cross-tenant (`repo_id`) isolation, content-lookup stage | 2 | `adapter/storage/test_context_query.py` | Integration |
| NFR — cross-tenant (`repo_id`) isolation, similarity-query stage | 3 | `adapter/storage/test_context_query.py` | Integration |
| NFR — cross-PR (`pr_number`) isolation, edge/node join stage | 2 | `adapter/storage/test_context_query.py` | Integration |
| NFR — cross-PR (`pr_number`) isolation, content-lookup stage | 2 | `adapter/storage/test_context_query.py` | Integration |
| NFR — cross-PR (`pr_number`) isolation, similarity-query stage | 3 | `adapter/storage/test_context_query.py` | Integration |
| NFR — in-query, per-symbol `LIMIT` via `LATERAL` join (bounds computation, not just result size) | 2 | `adapter/storage/test_context_query.py` | Integration |
| NFR — `ContextQueryError` wrapping (both query paths) | 2, 3 | `adapter/storage/test_context_query.py` | Integration |
| NFR — internal timeout fires before outer step timeout | 5 | `review/test_context_builder_step.py` | Integration |
| NFR — `context_builder_step_timeout_seconds > context_builder_internal_timeout_seconds` margin rejected at construction | 5 | `apps/worker/test_settings.py` | Unit |
| NFR — `ok`/`degraded_error`/`degraded_empty` outcome logging | 5 | `review/test_context_builder_step.py` | Integration |
| NFR — `context_builder_enabled` kill switch (incl. enablement-map wiring) | 5 | `review/test_context_builder_step.py` | Integration |
| NFR — all-combinations `_build_review_registry` conditional wiring | 5 | `apps/worker/test_main.py` | Integration |
| NFR — invalid/hallucinated `context_ref_ids` degrade safely + log at `warning` | 6 | `review/test_citation.py` | Unit |
| NFR — citation renders metadata only, never raw `Chunk.content` | 6 | `review/test_citation.py` | Unit |
| NFR — `context_ref_ids` round-trips `list[int]` (LLM JSON) → `tuple[int, ...]` (`Finding`) | 6 | `review/test_llm_review_step.py` | Integration |
| NFR — citation text survives `escape_markdown` | 6 | `review/test_findings_output_step.py` | Integration |
| NFR — row-value `(file_path, symbol_name)` matching, not cross-product `IN` lists | 2 | `adapter/storage/test_context_query.py` | Integration |
| NFR — existing `review/test_llm_review_step.py` regression | 5 | `review/test_llm_review_step.py` | Integration |
| All (happy path) | 6 | `tests/e2e/test_context_builder.py` | E2E |
