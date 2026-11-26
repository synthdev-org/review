# Phase 2 — Context Builder PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-05

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 2 bullet ("Context Builder
queries the index during review (blast-radius lookups for changed signatures,
not just the changed hunk)... Review job upgraded to consume built context
instead of the bare diff from Phase 1") into a concrete spec. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification tag
`RQPTVE`); requirements are stated as user stories with `Given/When/Then`
acceptance criteria per `testing.md`'s mandatory rule.

**Companion to [`spec-indexing.md`](spec-indexing.md), not a duplicate of it.**
That spec built the index this spec reads: `index_chunk` (AST-derived chunks
with 384-dim embeddings), `index_graph_node`/`index_graph_edge` (the
call/import graph), and the per-repo `last_indexed_commit_sha` cursor — all
already migrated, all currently write-only. As `spec-indexing.md` itself
states: *"The Context Builder that queries this index during review, and the
review pipeline's upgrade from bare diff to built context, are the *other*
Phase 2 capability, specified separately once this indexing spec's schema and
query shape actually exist to build against."* This is that spec. It adds no
new schema — every requirement below is a **read path** against tables
`spec-indexing.md` already created, plus the pipeline wiring to use what gets
read.

Also builds on [`spec-pipeline-step-framework.md`](spec-pipeline-step-framework.md)
(the execution model, unchanged by this spec — its own test suite already
proves a step named `context_builder` can be inserted between `prefilter` and
`output` with zero changes to any other step) and extends
[`spec-review-pipeline.md`](spec-review-pipeline.md)'s `llm_review` step,
whose Phase 1 input (`_LLMReviewInput`: bare diff + static-analysis findings,
`review/llm_review.py:145-161`) this spec widens to also carry retrieved
context.

## Problem & Goal

**End user: the PR author.** Per Phase 1's own honest framing
(`spec-review-pipeline.md`), the platform already posts findings — but
`llm_review.py`'s prompt today is built from exactly two inputs: the bare
diff and static-analysis findings (`review/llm_review.py:216-222`). A PR that
changes a function's behavior gets reviewed as if that function existed in
isolation: nothing tells the reviewing LLM (or, transitively, the PR author)
that three other files call it, or that the repo already has an established
pattern for the kind of change being made elsewhere. [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)
names the concrete requirement this spec exists to satisfy: a PR review needs
to reason about blast radius, not just the changed hunk.

**Goal / success metric, capability-based (no fabricated adoption metric,
matching Phase 1 and Phase 2's indexing spec's own stance):** for a repo with
a current index, a PR review's context includes (a) known existing callers of
every function/method/class the PR modifies or removes, and (b) existing code
elsewhere in the repo that's semantically similar to what the PR adds or
changes — both actually reaching the LLM Gateway completion call, not merely
computed and discarded — within the cost ceilings this spec sets, and without
ever preventing a PR from getting a review at all when that context can't be
produced.

**Secondary end user: the platform operator**, whose Postgres/pgvector
instance now serves read queries at review time, not just index-time writes —
this spec's non-functional requirements bound that added load.

## Scope

### In scope

- **Diff-to-symbol resolution.** For every changed file in scope
  (`.ts`/`.tsx`, `.php`, `.go` — the same three languages `spec-indexing.md`
  scoped indexing to; a file in any other language contributes nothing to
  this step, the same "excluded entirely, not partially handled" posture as
  indexing's own US9), resolve each changed diff hunk to the enclosing
  function/method/class/interface symbol it falls inside, using the same
  syntactic-boundary logic the indexing chunker already applies to that
  language. A hunk that doesn't resolve to a single enclosing symbol (an
  import-only change, a top-level constant, a hunk spanning more than one
  declaration) contributes no symbol — not an error, just nothing to look up
  for that hunk.
- **Blast-radius graph query (decided — Must-have).** For every resolved
  symbol that existed before this PR (modified, renamed, or removed — see
  the pre-change-name bullet below), query `index_graph_edge`/
  `index_graph_node` for every edge whose `target_symbol_name` matches that
  symbol, **scoped to the default-branch graph only (`pr_number = 0`)** —
  never the PR's own indexed delta rows. **Permanent design decision, not a
  provisional gap:** blast radius exists to surface impact the PR author
  *can't already see* — code elsewhere in the repo they aren't looking at.
  A new call introduced by a different file within the same PR is, by
  definition, something the PR author already has in front of them in their
  own diff; querying only `pr_number = 0` costs nothing the feature is
  actually for, while avoiding real coupling between this spec's query-time
  logic and indexing's write-time PR-delta storage semantics — the two
  stay independently understandable, per `architecture.md`'s decoupling
  rules, rather than the Context Builder needing to know how to merge two
  divergent graph views. **One-hop only, decided permanently:** this spec
  does not walk the graph transitively past direct callers/importers, for
  two independent reasons, not just caution: (1)
  [`research-current-art.md`](../research/research-current-art.md) §2.3
  documents Sourcery's own finding that simply enlarging the context given
  to one LLM call has "diminishing and even *negative* returns on
  accuracy" — a transitive walk grows the candidate set fastest exactly
  where relevance falls off fastest, since a caller-of-a-caller is
  meaningfully less likely to be impacted than a direct caller; (2) real
  call graphs contain cycles (direct and mutual recursion) a one-hop
  reverse lookup structurally cannot loop on, while a transitive walk would
  need its own cycle-detection/termination logic — a real
  correctness/reliability surface this spec avoids entirely by staying at
  one hop, consistent with `architecture.md`'s "no interface/abstraction
  without a second real implementation already in view" simplicity rule. A
  deeper walk, if ever justified by real usage data, is new, explicitly-
  scoped work — not a parameter tweak to this spec.
- **Pre-change symbol identity for renamed/removed symbols (decided).** A
  symbol renamed or removed by the PR is looked up under its **pre-change**
  name (as it appears in the default-branch graph) — the name existing
  callers actually reference today — never the new name the same diff might
  introduce, which by definition has no existing callers yet.
- **Caller content resolution.** Each blast-radius edge's source
  `(file_path, symbol_name)` is resolved back to its actual chunk content
  via `index_chunk` (also scoped to `pr_number = 0`) — the caller's real
  source text, not just its name, up to the per-item/total budget in
  Non-Functional Requirements.
- **Semantic similarity query (decided — Must-have, same priority as blast
  radius, not a stretch goal).** For each changed chunk's post-change
  content, embed it with the platform's existing local embedding model
  (`indexing.embedding.EmbeddingClient`, already implemented, reused
  unchanged — same model, same 384 dimensions) and perform a
  vector-similarity search against `index_chunk`, scoped to `pr_number = 0`
  and excluding the chunk's own file, returning the top-ranked matches up to
  the per-chunk/total budget below. **Query embedding source, decided:** the
  query vector always comes from the changed chunk's own post-change source
  text, never a synthesized natural-language description of the change (a
  HyDE-style approach). Two decisive reasons, not a preference: first,
  [ADR-0007](../adr/0007-llm-free-indexing-deferred-rag-enrichment.md)
  already decided `index_chunk`'s stored vectors embed raw code, not an
  LLM-generated docstring (the alternative Greptile documents, per
  `research-current-art.md` §2.1) — embedding a synthesized description as
  the query would search in a different representational space than what
  was actually indexed, degrading match quality independent of cost;
  embedding raw code as the query is the only choice that stays consistent
  with what the index actually stores. Second, synthesizing a description
  first would require a generative LLM call this spec has no other reason
  to make, reintroducing exactly the per-query inference cost ADR-0007
  exists to keep out of the indexing/retrieval path — this spec's own
  retrieval stays as LLM-free as indexing itself, a Postgres query plus one
  local-embedding call, never a hosted completion call.
- **Context assembly + budget enforcement.** Blast-radius results and
  similarity results are deduplicated and packaged into one context bundle
  per review job, capped by the ceilings in Non-Functional Requirements;
  anything beyond the cap is dropped and logged, never a job failure —
  mirroring indexing's own non-fatal cost-ceiling drop pattern.
- **`llm_review` step upgrade.** The completion request sent to the LLM
  Gateway includes the bare diff, static-analysis findings, and the context
  bundle's contents as clearly distinguishable sections in the prompt —
  never silently merged into the diff text as if the retrieved code were
  part of the PR's own changes.
- **Deterministic, platform-rendered citations (decided — Must-have).** When
  a posted finding draws on one or more context-bundle items, the citation's
  file path, symbol name, and line range must always match data the context
  builder actually retrieved — never text an LLM composed describing what it
  was shown. The LLM may indicate *which* retrieved item(s) informed a
  finding; it never gets to author the citation's factual content itself,
  since an LLM-authored citation carries the same hallucination risk as any
  other unverified LLM claim about the codebase.
- **Graceful degradation, unconditionally (decided).** No index for the
  repo yet, a graph/chunk/embedding query erroring, or a query exceeding its
  timeout all produce an **empty context bundle** — logged with the specific
  condition — and the pipeline proceeds to `llm_review` exactly as it would
  with a non-empty bundle. A Context Builder problem never fails the job and
  never costs the PR author the review they'd otherwise get. This directly
  satisfies `spec-indexing.md`'s own requirement that "no index data for
  this repo yet" be treated as a normal, non-error state.
- **Per-step configuration**, reusing the pipeline step framework's already-
  proven mechanism unchanged: an enable/disable flag and a timeout, following
  the exact naming convention `apps/worker/settings.py` already establishes
  for `prefilter_enabled`/`static_analysis_enabled`/`llm_review_enabled` and
  their `*_step_timeout_seconds` counterparts.

### Out of scope (deferred to a later spec or roadmap phase)

- **The external product-knowledge MCP augmentation call** against review's
  own vendor-neutral contract (`product_knowledge_search`/
  `product_knowledge_fetch`/`product_knowledge_related`; knowhow is the
  contract's reference implementation — see
  [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)). Per
  the roadmap, this is Phase 7, entirely separate from Phase 2's own
  code-derived context — this spec's Context Builder produces only
  code-derived context; the provider call-out is a distinct, later
  addition to the same step. **Now specified in
  [`spec-product-knowledge.md`](spec-product-knowledge.md).**
- **Memory / dismissed-finding suppression / learned rules.** Phase 4. This
  spec's context bundle contains only code retrieved from the index, never
  anything from the (not-yet-built) Memory store.
- **A structurally separate verification pass** over context-aware findings.
  Phase 3. This spec only changes what `llm_review` is given to reason with;
  it does not add a second, verifying LLM call.
- **Multi-hop / transitive blast radius** (walking the graph more than one
  edge out from a changed symbol). Decided permanently against, not a
  placeholder — one-hop direct callers/importers only (see Scope → In
  scope's reasoning: relevance falls off faster than a transitive walk
  grows the candidate set, and one hop structurally avoids call-graph
  cycles). A deeper walk, if ever justified by real usage data, is new,
  explicitly-scoped work, not an extension of this spec.
- **Overlaying the PR's own indexed delta (`pr_number != 0`) onto the
  blast-radius query.** Decided permanently against, not a placeholder —
  default-branch-only (see Scope → In scope's reasoning: the gap this
  leaves — a new caller introduced by a different file in the same PR — is
  something the PR author already has in front of them in their own diff,
  so closing it would add real query-time/write-time coupling for a case
  that already costs the PR author nothing to notice themselves).
- **Precise per-language symbol resolution** (real Go module-graph
  resolution, Composer autoload resolution, tsconfig-aware TypeScript
  resolution). Inherited unchanged from `spec-indexing.md`'s own decision —
  this spec's blast-radius query is exactly as precise (or imprecise) as the
  name-based heuristic graph it queries, and does not add its own resolution
  layer on top.
- **Query result caching across jobs.** Each review job's context-builder
  queries run fresh against the current index; no cross-job cache is
  introduced. A fixed sane default budget (see Non-Functional Requirements)
  keeps per-job cost bounded without one.
- **A customer-configurable/BYO embedding endpoint** for the query-side
  embedding call. Inherited unchanged from `spec-indexing.md` — the bundled
  local model is the only option, for both indexing and this spec's query
  embedding, so the two stay dimensionally compatible without a runtime
  check.
- **Languages beyond TypeScript, PHP, Go.** Inherited unchanged from
  `spec-indexing.md` — a fourth language is new scope for both specs
  together, not a gap in this one alone.

### Priority

- **Must have:** diff-to-symbol resolution; the blast-radius graph query
  (one-hop, default-branch-scoped) and its caller-content resolution; the
  semantic similarity query; context assembly with budget enforcement;
  `llm_review`'s consumption of the context bundle; deterministic
  platform-rendered citations; unconditional graceful degradation. These
  collectively *are* this spec's goal — a context-aware review that never
  regresses Phase 1's "a PR always gets a review" guarantee — and aren't
  divisible further without failing it, the same reasoning `spec-indexing.md`
  applied to its own must-have set. Both retrieval techniques (blast radius
  and semantic similarity) are Must, not one Must and one Should — this was
  a deliberate scope decision, not a default: earlier RAG survey material
  might suggest starting narrower, but the decision here is that both ship
  together.
- **Should have:** none identified beyond the Must set — unlike
  `spec-indexing.md`'s own Should-have (the full-tree-diff fallback), this
  spec has no comparably-scoped "materially reduces risk but the platform
  still functions without it" capability; the graceful-degradation
  requirement already covers the risk that would otherwise motivate one.
- **Could have:** a per-repo/per-org override of the context budget ceilings
  (vs. one fixed platform-wide default). A fixed sane default (see
  Non-Functional Requirements) still satisfies the goal — same reasoning
  `spec-indexing.md` applied to its own per-repo cost-ceiling override.

## Requirements

Stated as user stories per `testing.md`'s mandatory format.

**US1 — Changed symbols are identified from the diff**
As a platform operator, I want the platform to know which functions/methods/
classes a PR actually touches, so that blast-radius and similarity lookups
have a concrete symbol to search from instead of guessing from raw diff text.
- Given a PR's diff touching one or more `.ts`/`.tsx`, `.php`, or `.go`
  files, When the context builder step runs, Then each changed hunk is
  mapped to its enclosing function/method/class/interface symbol name using
  the same syntactic-boundary logic the indexing chunker already applies,
  and a hunk that doesn't fall inside exactly one such boundary is recorded
  as having no resolved symbol rather than raising an error.

**US2 — Blast radius surfaces existing callers of a changed symbol**
As a PR author, I want to see what else in the repo calls a function I'm
changing, so that I understand the impact of my change beyond the lines I
touched.
- Given a resolved changed symbol from US1 that existed before this PR, When
  the context builder queries the default-branch graph (`pr_number = 0`),
  Then every graph edge whose `target_symbol_name` matches that symbol's
  pre-change name is returned as a one-hop caller/importer with its own
  source file path and symbol name; a symbol with zero matching edges (newly
  added by this PR, or genuinely uncalled) yields an empty result, not an
  error.

**US3 — Blast radius includes the calling code itself, not just a pointer**
As a PR author, I want to see the actual code of a function that calls what
I'm changing, not just its name, so that I can judge the impact without
leaving the PR to go look it up myself.
- Given one or more blast-radius edges from US2, When the context builder
  resolves each caller's chunk content from the default-branch (`pr_number =
  0`) chunk table by `(file_path, symbol_name)`, Then the caller's actual
  source text is included in the context bundle, up to the per-caller and
  total budget in Non-Functional Requirements.

**US4 — Semantically similar existing code is surfaced alongside the diff**
As a PR author, I want the review to consider how similar code is already
written elsewhere in this repo, so that I get feedback grounded in this
codebase's own conventions, not just generic advice.
- Given a changed chunk's post-change content, When the context builder
  embeds that content with the platform's existing local embedding model and
  performs a vector-similarity search against the default-branch (`pr_number
  = 0`) chunk table, excluding the chunk's own file, Then the top-ranked
  matches are included in the context bundle, up to the per-chunk and total
  budget in Non-Functional Requirements.

**US5 — Review generation consumes built context instead of the bare diff
alone**
As a PR author, I want the platform's review of my PR to actually use the
blast-radius and similar-code context, so that Phase 1's "changed hunk in
isolation" limitation is actually fixed, not just computed and discarded.
- Given a context bundle produced by US1–US4 (possibly empty), When the
  `llm_review` step runs, Then the completion request sent to the LLM
  Gateway includes the bare diff, the static-analysis findings, and the
  context bundle's contents as clearly distinguishable sections — never
  merged into the diff text as if the retrieved code were part of the PR's
  own changes.

**US6 — Findings that draw on retrieved context cite it**
As a PR author, I want a finding that references other code in my repo to
point me at exactly what it's talking about, so that I can verify the claim
myself instead of taking the platform's word for it.
- Given a posted finding whose generation drew on one or more context-bundle
  items, When the finding is assembled for posting, Then the platform
  appends a citation listing the specific file path(s) and symbol
  name(s)/line range(s) the context bundle actually retrieved — the citation
  content always matches the platform's own retrieved data, never free text
  the LLM itself composed describing what it was shown.
- Given a posted finding whose generation drew on no context-bundle item,
  When the finding is assembled for posting, Then it renders exactly as a
  Phase 1 finding would — no citation section, no "nothing found"
  disclaimer.

**US7 — Context builder never blocks or fails a review**
As a PR author, I want my review to complete even if blast-radius/similarity
lookup can't run — no index exists yet, a query errors, or a query times
out — so that a context-builder problem never costs me the review I'd
otherwise get.
- Given the repo has no index yet, or a graph/chunk/embedding query raises an
  error, or a query exceeds its configured timeout, When the context builder
  step runs, Then it produces an empty context bundle, logs the specific
  condition, and the pipeline proceeds to `llm_review` exactly as it would
  with a non-empty bundle — never marking the job `FAILED`.

**US8 — Out-of-scope files and unresolvable symbols degrade the same way**
As a platform operator, I want a PR touching an unsupported language or an
unresolvable hunk to still get whatever context IS available, so that one
file in a mixed-language PR doesn't zero out context for the rest.
- Given a PR whose changed files include a mix of in-scope (TS/PHP/Go) and
  out-of-scope-language files, or include a hunk US1 couldn't resolve to a
  symbol, When the context builder step runs, Then blast-radius and
  similarity lookups still run for every resolved in-scope symbol, and the
  unresolved/out-of-scope portions contribute nothing to the bundle rather
  than blocking the rest.

**US9 — Context builder respects its own cost/latency budget**
As a platform operator, I want context retrieval to stay bounded regardless
of how many callers or matches exist, so that one heavily-called function in
a large repo can't blow up review latency or prompt size.
- Given a changed symbol with more blast-radius edges or similarity matches
  than the configured per-symbol/per-chunk ceiling, When the context builder
  assembles the bundle, Then only the top-N results (by edge count /
  similarity score) are included up to that ceiling, the excess is dropped
  and logged at `warning`, and the overall context bundle never exceeds the
  current job's diff-proportional total byte budget (see Non-Functional
  Requirements).

**US10 — Renamed or removed symbols use their pre-change identity for blast
radius**
As a PR author renaming or removing a function, I want blast radius computed
against the name existing callers actually use today, so that I still see
real impact instead of a query that trivially returns nothing because it
searched under the new (or now-absent) name.
- Given a diff that renames or removes a previously-existing function/
  method/class, When the context builder resolves that symbol for US2's
  blast-radius query, Then it queries using the symbol's pre-change name (as
  it appears in the default-branch graph), never any new name introduced by
  the same diff.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- Every new I/O call this spec introduces (Postgres graph/chunk queries, the
  query-side embedding inference call) takes an explicit timeout, per
  `architecture.md` — a `context_builder_step_timeout_seconds` setting,
  mirroring the existing per-step timeout fields in `apps/worker/settings.py`
  (`prefilter_step_timeout_seconds`, `static_analysis_step_timeout_seconds`,
  `llm_review_step_timeout_seconds`), defaulting to the same 30s baseline.
- **Context budget ceilings (decided).** Two layers, not one flat number:
  - **Total bundle size is proportional to the diff being reviewed, not a
    fixed constant:** the context bundle's byte budget for a given review
    job equals that job's own bare-diff byte size, capped at the same
    absolute ceiling `llm_max_diff_bytes` (200,000 bytes) already applies to
    the diff itself. This is a deliberate reuse of an existing, already-
    decided constant rather than a new, independently-tuned number — good
    for UX (a small PR isn't overwhelmed by a disproportionate wall of
    retrieved context relative to what it actually changed; a large PR gets
    proportionally more room, up to the same sane ceiling already governing
    diff size) and for decoupled architecture (no second cost knob that can
    drift out of sync with the first). This mirrors, and is grounded in,
    CodeRabbit's own publicly documented target — [`research-current-art.md`](../research/research-current-art.md)
    §3 states CodeRabbit "targets roughly a 1:1 ratio of code to surrounding
    context in its prompts" — and is reinforced by Sourcery's finding (§2.3,
    cited above) that simply maximizing context volume has diminishing and
    even negative returns, arguing against an unbounded or generously-large
    fixed ceiling.
  - **Per-item soft caps prevent one changed symbol from consuming the
    whole proportional budget:** up to 10 caller edges per changed symbol,
    and up to 5 similarity matches per changed chunk — ensuring a PR
    touching several symbols gets *some* context for each rather than one
    heavily-called function exhausting the total budget alone. Excess
    beyond either layer is dropped lowest-ranked-first (fewest edges /
    lowest similarity score), logged at `warning`, never a job failure,
    mirroring `spec-indexing.md`'s own non-fatal cost-ceiling drop pattern.
    These two per-item numbers remain starting defaults tuned during
    implementation — the *shape* of the budget (proportional total,
    per-item floor) is the decided part; the exact per-item counts are a
    reasonable starting point, not load-bearing to the design.
- **Security:** no new credential type — the graph/chunk queries reuse the
  existing Postgres connection, and the query-side embedding call reuses the
  existing local ONNX model with no network call. This step only *reads*
  already-indexed, already-trusted-boundary data; it introduces no new
  execution of PR-supplied content.
- **No latency SLA is set**, matching every prior phase's own stance —
  correctness within the ceilings above is the goal; a real target belongs
  to Phase 8 once a real deployment exists to measure against.
- **Reliability:** US7 is this spec's entire reliability contract — no
  retry-with-backoff or circuit-breaker requirement beyond what already
  exists at other steps; a context-builder query failure degrades to an
  empty bundle rather than retrying.
- **Scale:** query cost is bounded by the ceilings above per job, and by the
  index's own size, which `spec-indexing.md`'s cost ceilings already bound;
  this spec introduces no new scale concern beyond the read-query load those
  ceilings imply.

### Privacy / Compliance

*(True of the code-derived retrieval specified here. Phase 7
([`spec-product-knowledge.md`](spec-product-knowledge.md)) adds an optional,
operator-configured provider call that does send the PR title and linked
issue keys to the configured endpoint — see that spec's Privacy / Compliance
section.)*

No new data leaves the deployment on the retrieval side — blast-radius and
similarity queries run entirely against the customer's own Postgres, and the
query-side embedding call is the same local, no-network model
`spec-indexing.md` already uses. The retrieved context *does* get sent to the
customer's configured LLM endpoint as part of the `llm_review` completion
request — this is not a new trust boundary: Phase 1 already sends the bare
diff to that same configured, customer-consented-to endpoint (BYO-key, per
`c4-architecture.md` design goal 3); this spec only widens what code content
travels through that already-accepted path.

### Observability

- Every context-builder run logs (extending the existing structured-logging
  setup, same shape as indexing's and Phase 1's own per-step logs):
  `job_id`, PR identity, count of changed symbols resolved, count of
  blast-radius edges found (and how many were dropped by the ceiling), count
  of similarity matches found (and dropped), total context bytes included,
  query duration, and whether/why it degraded to an empty bundle.
- No metrics/dashboard system exists in this repo or is specified by any
  roadmap phase through Phase 2 — observability here is structured logs
  only, consistent with `spec-indexing.md`'s and `spec-review-pipeline.md`'s
  own stance.

### Rollout & Release Safety

- **One additive, nullable migration, not zero.** An earlier draft of this
  section said "no new Alembic migration"; that is no longer accurate:
  `index_chunk` gains nullable
  `start_line`/`end_line` integer columns, needed because US6's own
  "line range" citation requirement has no other source for it —
  `byte_range` alone can't be converted to a line number without fetching a
  caller/similar-chunk's *entire* file just to count newlines, an extra
  per-item I/O cost this spec's own budget ceilings don't otherwise pay
  for. The migration is purely additive (no existing column touched),
  nullable (no backfill required — a chunk indexed before this migration
  simply has `line_range = None`, and Phase 6's citation rendering renders
  without the line-range portion in that case, never an error), and scoped
  to `index_chunk` only (`index_graph_node` never reads a line range) — a narrow exception that still
  meaningfully lowers this spec's own rollout risk relative to
  `spec-indexing.md`'s, just not to literally zero schema change.
- **Kill switch (decided):** a `context_builder_enabled` bootstrap flag,
  default `true`, using the exact per-step enable mechanism
  `spec-pipeline-step-framework.md`'s US-F4 already proved (alongside the
  existing `prefilter_enabled`/`static_analysis_enabled`/`llm_review_enabled`
  fields). When disabled, `llm_review`'s `depends_on` reverts to
  `static_analysis` (its Phase 1 default) and review proceeds exactly as it
  does today, bare-diff only — the operational safety valve if a bad query
  pattern or the embedding model starts consuming Worker or Postgres
  capacity, without needing a redeploy.
- **Backwards compatibility:** `llm_review`'s input model gains new optional
  context fields; a deployment with `context_builder_enabled=false` (or one
  running this build against a repo with no index yet, per US7) sees zero
  behavior change from Phase 1 today.

## UX & Design

No new web UI (Admin UI is Phase 5) — but, like `spec-review-pipeline.md`,
posted GitHub PR comments are a real, directly user-facing surface, and this
spec changes their content for the first time since Phase 1.

- **Primary flow:** PR author opens/pushes a PR → (unchanged trigger/timing
  from Phase 1) → some posted findings now include a citation section
  pointing at specific existing files/symbols the finding actually drew on.
- **Empty-context state:** no blast-radius edges or similarity matches
  found → the finding renders exactly as a Phase 1 finding would, no
  citation section and no "nothing found" disclaimer (US6) — consistent
  with the existing baseline experience, not a new "is this broken?"
  signal.
- **Degraded state:** any context-builder failure (US7) is invisible to the
  PR author — the review still completes using the bare diff, with no
  visible error, since this is a deliberately non-fatal condition, distinct
  from Phase 1's US8 (a genuine review failure, which *does* surface
  visibly).
- **Content correctness:** citation text is deterministically rendered by
  the platform from its own retrieved data (US6), never LLM-authored — a
  citation can be wrong only in *what* the LLM chose to cite, never in
  *how* that citation is described, since the platform controls the
  description entirely.
- **Performance as UX:** no quantified perceived-performance target is set
  (see Non-Functional Requirements — deliberately not fabricated, matching
  Phase 1's and `spec-indexing.md`'s own stance), but the *shape* of the
  experience is specified: context retrieval happens entirely before
  `llm_review` sends its one completion call (US5), so the PR author still
  sees one coherent posted review appear, per `spec-review-pipeline.md`'s
  existing batched-review UX — this spec never turns that into a
  multi-stage or trickling experience, regardless of how many blast-radius/
  similarity queries run underneath it.
- **Accessibility/responsiveness:** inherited entirely from GitHub's own PR
  UI, same as `spec-review-pipeline.md` — nothing new to design.
- **Consistency with a design system:** N/A — no component library
  involved; this remains API-driven content, not rendered UI.

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author, `R`/`A` on every existing
  ADR's RACI table; no separate design/eng/QA/legal stakeholders exist for
  this project at this stage.
- **Acceptance:** every `US*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios); `mypy --strict`/`ruff` clean
  per `architecture.md`; the goal statement's budget ceilings are exercised
  by at least one test fixture large enough to trigger them.

## Phases / Units

No new schema and no new step-framework capability — this spec is entirely
new domain/adapter logic built on two already-proven foundations
(`spec-indexing.md`'s schema, `spec-pipeline-step-framework.md`'s
step-insertion model), which meaningfully lowers the risk profile relative to
either of those two specs.

- **Unit A — Diff-to-symbol resolution.** Depends on: nothing new
  (conceptually reuses the same AST-boundary logic `spec-indexing.md`'s
  chunker already implements). Risk: low-medium — the rename/removed-symbol
  and multi-symbol-hunk edge cases (US1, US10) need real per-language
  fixtures, not just one. **Acceptance:** US1, US10.
- **Unit B — Blast-radius graph query.** Depends on: nothing new — the
  schema's `target_symbol_name` index was already built anticipating exactly
  this reverse lookup (`spec-indexing.md`'s own US5 design). Risk: low.
  **Acceptance:** US2, US3.
- **Unit C — Semantic similarity query.** Depends on: nothing new; reuses
  the existing `EmbeddingClient` for the query-side embedding call. Risk:
  low-medium — the first vector-similarity (KNN) read against `index_chunk`
  in this codebase; prior code only ever wrote to it. **Acceptance:** US4.
- **Unit D — Context assembly + budget enforcement.** Depends on: Units A,
  B, C. Risk: low — bounded aggregation logic, mirrors `spec-indexing.md`'s
  own cost-ceiling pattern. **Acceptance:** US9.
- **Unit E — Context Builder step + `llm_review` wiring.** Depends on:
  Units A–D; extends `spec-pipeline-step-framework.md`'s proven
  step-insertion pattern (already demonstrated with a test-only fake
  `context_builder` step). Risk: low — the framework already proves this
  exact insertion point requires zero changes to any other step.
  **Acceptance:** US5, US7, US8.
- **Unit F — Citation rendering in posted findings.** Depends on: Unit E's
  context-bundle shape and Unit D's retrieved data; extends the
  findings-merge-and-output step and its structured-findings schema. Risk:
  medium — this is the unit that has to actually resist the
  hallucination-risk trap named in US6 (an LLM-authored citation), which
  needs deliberate structured-output schema design, not just prompt
  wording. **Acceptance:** US6.

## Testing / Key Scenarios

Per `testing.md`'s pyramid, with [ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md)'s
pure-no-I/O exemption applied where it genuinely fits (symbol resolution's
own AST-matching logic, and citation-text rendering from already-retrieved
data, both perform no I/O of their own).

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US1 | A `.go`/`.php`/`.ts` diff hunk resolves to its enclosing symbol; an import-only hunk resolves to none | `review/test_context_symbols.py` | Unit (ADR-0005 — pure AST-boundary matching, no I/O) |
| US2 | A modified symbol with existing callers in the default-branch graph returns those edges; an uncalled/new symbol returns none | `adapter/storage/test_context_query.py` | Integration (real Postgres/pgvector via testcontainers) |
| US3 | A returned blast-radius edge's caller content is resolved from `index_chunk` | `adapter/storage/test_context_query.py` | Integration |
| US4 | A changed chunk's embedding finds a real similar chunk elsewhere in the repo, excluding its own file | `adapter/storage/test_context_query.py` | Integration (real Postgres/pgvector + real local embedding model, no network) |
| US5 | A non-empty context bundle appears as a distinguishable section in the `llm_review` completion request | `review/test_llm_review_step.py` (extended) | Integration (recorded LiteLLM cassette) |
| US6 | A finding referencing a context-bundle item gets a platform-rendered citation; a finding referencing nothing gets none | `review/test_findings_output_step.py` (extended) | Unit for the rendering logic (ADR-0005) + Integration for end-to-end posting (recorded GitHub cassette) |
| US7 | No index yet, a forced query error, and a forced timeout all yield an empty bundle and a completed (not failed) job | `review/test_context_builder_step.py` | Integration (real Postgres) |
| US8 | A mixed in-scope/out-of-scope-language PR still gets context for its in-scope symbols | `review/test_context_builder_step.py` | Integration |
| US9 | A symbol with more edges/matches than the ceiling is truncated to the top-N, excess logged at `warning` | `review/test_context_builder_step.py` | Integration |
| US10 | A renamed function's blast radius is computed under its pre-change name, not its new name | `adapter/storage/test_context_query.py` | Integration |
| US1–US10 (happy path) | Full Docker Compose stack: a fixture repo with a real cross-file caller and a real similar-code pattern, PR modifies the called function, asserts the posted finding includes a citation referencing the real caller | `tests/e2e/test_context_builder.py` (new) | E2E |

## Design decisions closed during review

Four questions this spec initially left as open assumptions were resolved
during review, using `research-current-art.md`'s vendor evidence and this
project's own architecture/ADR precedent, and are now decided in place
above rather than listed as pending here:

1. **Context budget shape** — proportional to the reviewed diff's own size,
   capped at the existing `llm_max_diff_bytes` ceiling, not an independent
   fixed constant (see Non-Functional Requirements). Grounded in
   CodeRabbit's documented ~1:1 code-to-context ratio and Sourcery's
   documented finding that maximizing context volume has diminishing/
   negative returns. The per-item counts within that shape (10 edges/
   symbol, 5 matches/chunk) remain tunable starting defaults — the
   proportional-total/per-item-floor *shape* is the decided part.
2. **Query embedding source** — always the changed chunk's raw post-change
   content, never a synthesized description (see Scope → In scope). Decided
   on two grounds: representational consistency with what `index_chunk`
   actually stores (raw code, per ADR-0007), and avoiding a generative LLM
   call this spec has no other reason to introduce.
3. **One-hop-only blast radius** — decided permanently, not provisional (see
   Scope → In scope and Out of scope). Grounded in Sourcery's diminishing-
   returns finding plus a real correctness reason: one hop structurally
   cannot loop on call-graph cycles, while a transitive walk would need its
   own cycle-detection logic this spec avoids building.
4. **Default-branch-only graph scope** — decided permanently, not provisional
   (see Scope → In scope and Out of scope). The gap it leaves (a same-PR
   cross-file caller) is something the PR author already sees in their own
   diff, so closing it would trade real query/write coupling for a case
   that costs the user nothing to notice themselves.
