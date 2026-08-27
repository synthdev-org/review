# RAG Techniques — Reference Overview

This document is a technique-level reference for retrieval-augmented generation
(RAG) systems, covering how content gets indexed, stored, and retrieved. It's
meant to be consulted when designing or evaluating any part of this platform
that touches indexing/retrieval — most directly the Indexing job and Context
Builder described in [`c4-architecture.md`](c4-architecture.md) — and draws on
two concrete sources: [`docs/research/research-current-art.md`](../research/research-current-art.md)
(how AI code review vendors build RAG specifically for codebases) and a survey
of `~/projects/knowhow` (a general-purpose, multi-source RAG knowledge base),
cited inline as **[research]** and **[knowhow]** respectively. Where the two
diverge, that's noted explicitly — the differences are informative, not noise.

---

## 1. Chunking

How content gets split into retrievable units before embedding.

- **Naive splitting** (fixed character/line count) is the baseline everyone
  starts from and the first thing mature systems move away from — it cuts
  functions mid-statement or sentences mid-clause and measurably hurts
  retrieval quality.
- **Boundary-aware splitting** breaks on structural units instead: blank
  lines, headings, paragraphs for prose; statement/block boundaries for code.
  knowhow does this per content format (markdown/HTML on heading+blank-line
  boundaries, plaintext on blank lines, code on a line-pattern heuristic) —
  notably choosing a heuristic over a real per-language AST parser as a
  deliberate scope trade-off, not an oversight **[knowhow]**.
- **AST-aware / structural chunking** is the more rigorous version for code:
  parse into a real syntax tree (e.g. via Tree-sitter) and split at syntactic
  boundaries, re-attaching stripped context (imports, enclosing class) so each
  chunk stays self-contained and syntactically valid. Formalized academically
  as **cAST** (arXiv 2506.15655), which reports concrete retrieval gains
  (+4.3 Recall@5, +2.67 Pass@1) directly attributable to this over naive
  splitting. Greptile and Bito both build real AST-derived chunks/graphs this
  way **[research]**.
- **Token-budget packing.** Once split into structural units, units are
  typically re-packed toward a target token count with overlap, so a chunk is
  neither too small (loses context) nor too large (dilutes embedding
  relevance / wastes prompt budget). knowhow packs to ~512 tokens with a
  64-token overlap and reserves headroom for a prepended summary
  **[knowhow]**.
- **Context re-attachment / prefix fusion.** Chunks are often prefixed with a
  short LLM-generated summary or intent statement before embedding, so a
  chunk that reads as opaque in isolation ("return x + y") retrieves
  correctly against a conceptual query ("where do we calculate the discount")
  — this is knowhow's "Layer 1" enrichment **[knowhow]**, and CodeRabbit's
  and Bito's equivalent context-bundle assembly serves the same purpose
  **[research]**.

**When it matters less:** if the reviewing/retrieving agent can cheaply
re-explore the source itself (grep/glob/view a live repo checkout), chunking
quality matters less because bad retrieval is recoverable by further
exploration. It matters most when the index is the *only* way back to the
source (a stale snapshot, a document store with no live re-fetch path).

---

## 2. Embedding

- **Model choice is a trade-off between quality, cost, and control.**
  API-based embedding (OpenAI, Voyage, Cohere) is easiest to stand up and
  usually higher-quality, but couples the system to a third-party call on
  every index write and every query. Local embedding (e.g. Xenova/ONNX
  running `all-MiniLM-L6-v2`, as knowhow does **[knowhow]**) removes that
  dependency entirely at the cost of a smaller, weaker model and a fixed
  embedding dimension baked into the schema (knowhow pins `vector(384)` to
  its chosen model and documents that swapping models breaks the schema
  **[knowhow]**) — the same "any model, any endpoint, BYO key" principle this
  platform applies to its LLM Gateway (see `c4-architecture.md` design goal
  3) generalizes to embedding models too, though few surveyed tools actually
  make the embedding model swappable at runtime the way they make the chat
  model swappable.
- **Docstring/summary embedding over raw code.** Rather than embedding raw
  source text, some systems generate a natural-language description of a
  function/class/chunk and embed *that* — Greptile's approach, explicitly
  framed as going beyond "simple RAG" **[research]**. This trades indexing
  cost (an LLM call per unit) for retrieval quality (natural-language queries
  match natural-language descriptions better than they match code syntax).
- **Embedding isn't just chunks.** Mature systems embed multiple granularities
  from one document — the raw chunk, extracted atomic propositions/facts, and
  higher-level cluster/community summaries — all into the same vector space
  so a query can match at whichever granularity is most relevant (knowhow's
  Layers 2/3 **[knowhow]**; see §3 below).

---

## 3. Beyond Flat Chunks: Hierarchical and Graph Structure

A pure flat list of embedded chunks is the simplest possible index and the
one every system starts with — but two structural additions recur across
mature implementations:

- **Hierarchical summarization (RAPTOR-style).** Cluster related chunks and
  generate a summary of the cluster; recursively summarize summaries into
  higher tiers. This lets a query that needs a broad, document-spanning
  answer retrieve a summary node instead of forcing the reader to assemble
  one from a dozen disconnected chunks. knowhow implements this as its
  "Layer 3" using GMM clustering + UMAP dimensionality reduction, storing
  summaries with an explicit `level` column in the same table shape as
  regular chunks **[knowhow]**.
- **Graph/relationship indexing**, run alongside — not instead of — vector
  search:
  - *Code call/import graphs*: Greptile persists a graph of call/import/usage
    edges and walks it outward from a diff to compute true "blast radius";
    CodeRabbit builds a fresh per-review "Codegraph" of symbol
    definitions/references/co-change history rather than a stale pre-embedded
    one **[research]**. This directly motivated this platform's own
    call/import graph in the Postgres index (`c4-architecture.md` Level 2).
  - *Entity/relationship knowledge graphs*: extract entities and typed
    relationships from content (e.g. via an LLM extraction pass) and persist
    them as first-class graph structure, separate from the chunk-level
    vectors. knowhow mirrors its relational `entities`/`links` tables into an
    Apache AGE property graph specifically for multi-hop traversal, and runs
    Leiden/Louvain community detection over it to find topic clusters
    **[knowhow]**. This is a materially different investment than a
    code-only call graph — it requires an LLM extraction step per document,
    not just static parsing.
  - The common thread: relational/vector storage answers "what's similar to
    this," while a graph answers "what's *connected* to this" — multi-hop
    questions (what depends on this function three levels out, what other
    documents does this entity appear in) are graph-shaped queries that
    vector similarity alone answers poorly.

**Trade-off to weigh:** a call/import graph can be built from pure static
parsing (cheap, deterministic, no LLM in the loop) — this platform's design
already does this. An entity/relationship knowledge graph over free-text
content requires LLM-based extraction (real cost per document, real
hallucination risk) to populate. Reach for a real graph engine (Apache AGE,
Neo4j) only once graph *queries* — not just graph *storage* — are a real
requirement; plain relational edge tables cover simple 1-hop lookups fine.

---

## 4. Hybrid Search and Reranking

- **Hybrid (vector + keyword) search.** Pure embedding similarity misses
  exact-match cases embeddings are bad at — identifiers, error codes, literal
  strings. Fusing vector similarity with classic full-text search (BM25-style
  ranking, or in Postgres a `tsvector`/GIN index) covers both. knowhow fuses
  a `tsvector` FTS column with vector similarity inside its retrieval query,
  with reciprocal-rank-fusion-shaped scoring implied by the fused result
  **[knowhow]**.
- **Reranking as a distinct second pass.** Retrieve a wider candidate set
  cheaply (overfetch — knowhow overfetches 5× **[knowhow]**), then rerank the
  top N with a more expensive, more accurate model — typically a
  cross-encoder (jointly scores query+document pairs, unlike a bi-encoder
  embedding model that scores them independently) run locally
  (knowhow: `Xenova/ms-marco-MiniLM-L-6-v2`, ONNX, on-CPU **[knowhow]**).
  This is the retrieval-side analogue of the verification pattern in §5 — a
  structurally separate, narrower-scoped pass catches what the cheap first
  pass got wrong, rather than trusting the first pass's own ranking.
- **Ranking policy beyond relevance.** Base relevance score isn't always the
  final sort key — knowhow multiplies it by a configurable policy weighting
  scope, trust tier, staleness/recency, and source region before reranking
  runs **[knowhow]**. Worth considering wherever "most relevant" and "most
  trustworthy" aren't the same thing (e.g. a stale but well-matched doc vs. a
  fresher, slightly-less-matched one).

---

## 5. Query-Time Techniques

- **HyDE (Hypothetical Document Embeddings).** Instead of embedding the raw
  query, have an LLM generate a hypothetical answer to the query first, then
  embed *that* and search with it — hypothetical answers tend to be closer in
  embedding space to real matching documents than short user queries are.
  knowhow runs this as an optional pre-retrieval step with a raw-query
  fallback on failure **[knowhow]**.
- **Query classification / routing.** Classify the query shape (e.g.
  symbol-like/exact-match vs. open-ended/conversational) before deciding the
  retrieval strategy, rather than running every query through the same
  pipeline — knowhow's "Plan" stage does this as step one of its retrieval
  pipeline **[knowhow]**.
- **Agentic, on-demand exploration** as an alternative (or complement) to a
  pre-built index: give the retrieving agent tools (`grep`, `glob`, `view`,
  or their code-graph equivalents) and let it decide what to look at, rather
  than relying entirely on a pre-computed index's judgment of what's
  relevant. GitHub Copilot Code Review and CodeRabbit's investigation phase
  both do this; GitHub reports it cut review cost ~20% with no quality loss
  by narrowing from the diff outward rather than embedding-searching blind
  **[research]**. Qodo's own reversal (documented in their engineering blog,
  literally titled "we took most of it out") is the sharpest data point here:
  they built a full CST/AST RAG system, then removed most of it once
  larger-context agentic exploration matched the same benchmark scores more
  cheaply — keeping RAG only for what exploration structurally *can't*
  recover (institutional memory of past decisions) **[research]**. The
  general principle: **index only what exploration can't cheaply
  rediscover; explore everything else.**
- **Graph neighbor expansion at query time.** After initial retrieval,
  expand into directly-connected graph nodes (related entities, calling
  functions) as a distinct pipeline stage, rather than requiring the vector
  search alone to surface everything relevant — knowhow's "Relate" stage
  **[knowhow]**.
- **Pagination without re-computation.** Carrying the computed query
  vector (and HyDE result) inside an opaque pagination cursor avoids
  re-embedding or re-running HyDE on every subsequent page of the same
  query — a small but real cost optimization **[knowhow]**.

---

## 6. Incremental Indexing

Full reindex-on-every-change doesn't scale past a small corpus. The recurring
pattern for keeping an index live cheaply:

- **Upsert-keyed content identity.** Key stored documents on a stable
  `(source, source_id)` pair (or per-repo commit-scoped identity for code)
  and upsert on conflict, rather than reindexing everything on every push.
  knowhow keys on `(tenant_id, source, source_id)` with
  `ON CONFLICT ... DO UPDATE` **[knowhow]**.
- **Per-document delta rebuild.** On a detected change, tear down and rebuild
  only that document's own derived artifacts (chunks, embeddings,
  propositions, graph edges) — not the whole index **[knowhow]**.
- **Per-source sync cursors.** Each ingestion source tracks its own
  last-synced position/cursor and does delta fetch against the upstream
  system, rather than re-listing everything every cycle **[knowhow]**.
- **Deletion is the hard part, not addition.** Detecting *new or changed*
  content is comparatively easy; detecting that something was *removed*
  upstream requires a full listing to diff against. knowhow's "sweep-diff"
  backstop only performs deletions when every source container's full listing
  succeeded that cycle — any partial/errored/quarantined fetch suppresses all
  deletions for that cycle, favoring stale-but-present data over incorrectly
  deleting live data **[knowhow]**. This is the kind of design detail that's
  easy to skip and expensive to get wrong later.
- **Cache re-review state, not just the index.** A related but distinct
  incremental pattern from the code-review domain: Cloudflare's pipeline
  hands the model its own prior review comments and their resolution status
  on re-review, so a second pass doesn't re-litigate already-resolved
  findings — conceptually the same "don't redo settled work" instinct applied
  to review output rather than the index itself **[research]**.

---

## 7. Persistence Architecture

Two credible shapes recur, and the choice is mostly about operational
surface area, not raw technical capability:

- **One database, many roles.** Put vectors, chunk text, relational metadata,
  full-text index, graph edges, and even the job queue in a single Postgres
  instance (pgvector for vectors, native FTS, and — if a real graph engine is
  wanted — an extension like Apache AGE). This is what both knowhow
  **[knowhow]** and this platform's own architecture
  (`c4-architecture.md` Level 2) do, for the same reason: one fewer
  stateful service for an operator to run, back up, and secure. The
  trade-off is coupling — a single instance's scaling/availability
  characteristics now gate everything, not just OLTP-style metadata.
- **Dedicated vector store, separate from the metadata store.** A purpose-built
  vector database (LanceDB, Chroma, a managed vector service) alongside a
  conventional metadata store. CodeRabbit's Learnings feature uses LanceDB
  specifically for its semantic retrieval layer while other pipeline state
  lives elsewhere **[research]**. This buys purpose-built ANN performance
  and independent scaling, at the cost of a second stateful system and
  cross-store consistency to manage.
- **Local/embedded vs. networked.** Distinct from the above: whether storage
  is embedded-in-process (SQLite, embedded LanceDB — no network hop, but
  single-writer/single-node by nature) or a networked service the app
  connects to over the wire (Postgres, a hosted vector DB — supports
  multiple app instances, but is a service to run and secure). knowhow is
  fully in the second camp — a self-managed but networked Postgres container,
  not an embedded file store **[knowhow]** — matching this platform's own
  choice for the same multi-instance-worker reason.
- **Multi-tenancy at the storage layer.** Where one deployment serves many
  tenants/orgs (this platform's design goal 5), row-level security scoped by
  a `tenant_id` set per connection (`SET LOCAL app.tenant_id`) is a proven,
  low-overhead pattern for enforcing isolation inside a shared schema rather
  than provisioning a schema/database per tenant **[knowhow]**.
- **Caching is usually thinner than expected.** Despite the machinery
  elsewhere, neither surveyed system leans on an external cache (Redis, etc.)
  for the index itself. knowhow's only cache is a small in-process memoizing
  cache for reranker scores, plus the pagination-cursor trick in §5
  **[knowhow]** — a reminder that Postgres query performance plus a
  reasonably-sized index often makes a dedicated cache layer unnecessary
  until proven otherwise.

---

## 8. Verification and Feedback Loops

Not indexing/persistence per se, but the layer that determines whether the
retrieved-and-generated output is trustworthy — included here because it
shapes what gets *fed back* into the index/memory over time.

- **Self-critique doesn't work.** The single sharpest, most consistently
  reproduced finding across vendor engineering blogs: asking the same model
  (even in a second call) to rate or reconsider its own output is close to
  random. Greptile tried an LLM self-rating filter for comment quality and
  found it added latency for no better-than-random signal; the fix that
  actually worked was non-generative — embedding similarity against a
  database of past comments developers had already dismissed **[research]**.
  Sourcery reached the same conclusion independently via a *structurally
  separate* second LLM call screening against a fixed rubric, not the same
  call re-examining itself **[research]**. This is already encoded as design
  goal 6 in `c4-architecture.md`.
- **Feedback becomes retrievable memory, not just a suppression flag.**
  Thumbs up/down, dismissed suggestions, and accepted fixes get folded back
  into the system as retrievable natural-language memory (CodeRabbit's
  "Learnings"), auto-synthesized rules (Bito generates a new persisted rule
  per dismissed suggestion class), or a similarity filter over past feedback
  (Greptile) — so retrieval/review quality on a given corpus improves over
  successive queries instead of resetting to the same baseline every time
  **[research]**. This is the same shape as this platform's own Memory store
  (`c4-architecture.md` Level 2).

---

## 9. Summary Table

| Concern | Naive baseline | Mature pattern | Cost of the upgrade |
|---|---|---|---|
| Chunking | Fixed char/line split | Structural/AST-aware split + token packing | Parser investment per language/format |
| Embedding | One fixed API model | Pluggable local or API model, docstring/summary embedding | Schema coupling to embedding dimension |
| Structure | Flat chunk list | Hierarchical summaries + graph edges | LLM extraction cost, second storage shape |
| Search | Vector-only | Vector + FTS hybrid + reranker pass | Extra index (GIN) + extra model call |
| Query | Embed raw query | HyDE, query classification, agentic exploration | Extra LLM call(s) per query |
| Index freshness | Full rebuild | Upsert-keyed delta reindex + sync cursors | Careful deletion-safety logic |
| Storage | Ad hoc / in-memory | Single Postgres (or vector-DB-plus-metadata-store) | Operational surface area of a real DB |
| Trust | Model self-rates its own output | Structurally separate verification pass or statistical filter | A second, narrower-scoped call or a trained filter |

---

## Sources

- [`docs/research/research-current-art.md`](../research/research-current-art.md)
  — full survey of AI code review vendor RAG/indexing techniques, with
  per-vendor citations.
- `~/projects/knowhow` — a general-purpose, multi-source RAG knowledge base
  (Confluence/Slack/GitHub/webhook connectors), explored directly for its
  indexing pipeline (`packages/indexing/`), embedding adapter
  (`packages/embedding/src/adapters/xenova-embedder.ts`), reranking
  (`packages/reranking/`), and Postgres/pgvector/Apache AGE schema
  (`docs/specs/spec-indexing.md`).
- [`c4-architecture.md`](c4-architecture.md) — this platform's own indexing
  design, which several sections above reference directly as the applied
  version of these techniques.
