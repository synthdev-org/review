# ADR: Indexing stays LLM-free; richer RAG enrichment deferred to an optional phase

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Phase 2's indexing job ([`roadmap.md`](../roadmap.md)) makes **zero
generative LLM calls**. It embeds raw AST-derived chunks directly with a
bi-encoder embedding model and builds the call/import graph by pure static
parsing — no docstring/summary generation, no RAPTOR-style hierarchical
summarization, no LLM-based entity/relationship graph extraction. Those
richer techniques, all surveyed in
[`rag-techniques.md`](../reference/rag-techniques.md) §§2–3, are real,
deliberate scope but explicitly deferred: they land later, if at all, as an
**optional** enrichment capability that a customer opts into, never as a
cost or latency tax every indexed push pays by default. This mirrors the
"optional augmentation, never a dependency" shape design goal 8 already
established for knowhow ([ADR-0003](0003-knowhow-product-knowledge-augmentation.md)),
applied here to indexing-time enrichment rather than non-code product
knowledge.

## Context

`rag-techniques.md` surveys a spectrum of indexing-time techniques, several
of which trade indexing cost for retrieval quality by adding an LLM call per
unit indexed:

- **Docstring/summary embedding** — generate a natural-language description
  of each chunk and embed that instead of (or alongside) the raw chunk
  (Greptile's approach): one LLM call per chunk.
- **RAPTOR-style hierarchical summarization** — cluster chunks and
  recursively summarize clusters into higher tiers: multiple LLM calls per
  document, repeated as the corpus changes.
- **Entity/relationship knowledge graph extraction** — an LLM extraction
  pass per document to populate a graph beyond the statically-parsed
  call/import graph.

Two things make the cost of these different here than in a general-purpose
RAG system like knowhow:

1. **BYO-LLM means the customer pays for every indexing-time call, not us.**
   Design goal 3 ([`c4-architecture.md`](../reference/c4-architecture.md))
   commits the platform to never proxying or subsidizing inference — every
   token is billed against the customer's own configured endpoint. An
   indexing-time LLM call isn't an internal cost trade-off we absorb; it's
   inference spend added directly to the customer's bill, multiplied across
   every chunk, in every file, on every push, for the lifetime of the
   deployment.
2. **Indexing runs far more often than review does.** A review job runs once
   per PR event; an indexing job runs on every push to keep the index fresh
   (roadmap Phase 2). Per-chunk LLM enrichment costs get paid at push
   frequency, not PR frequency — the more expensive trigger, not the
   cheaper one.

Against that, `rag-techniques.md` §5 already names the mitigating pattern:
Qodo's own reversal ("we took most of it out") found that agentic,
query-time exploration matched benchmark scores that a full CST/AST RAG
system achieved more expensively, keeping heavier retrieval machinery only
for what exploration structurally can't recover. The general principle it
states — **index only what exploration can't cheaply rediscover; explore
everything else** — argues directly against building indexing-time LLM
enrichment into Phase 2's core pipeline before it's proven necessary.

## Options considered

| Option | Indexing-time LLM cost | Retrieval quality (day one) | Fits BYO-LLM cost story | Verdict |
| --- | --- | --- | --- | --- |
| **Full enrichment in Phase 2** (docstring embedding + hierarchical summarization + entity extraction, all mandatory) | Highest — LLM calls per chunk, per cluster, per document, on every push | Best out of the box | Worst — bills the customer for enrichment before any customer has asked for it | Reject — cost/latency committed before demand is proven |
| **LLM-free core, enrichment deferred and optional** (this decision) | Zero in the mandatory path; non-zero only if/when a customer opts into a future enrichment capability | Adequate baseline (raw-chunk embeddings + static call graph); improvable later via query-time techniques (HyDE, agentic exploration) without redesigning storage | Strong — matches "optional augmentation, never a dependency" already proven for knowhow | **Accept** |
| **No enrichment ever, not even optional** | Zero, permanently | Capped at baseline forever | Strong | Reject — forecloses a real, requested capability (customer's own RAG call-out) instead of just sequencing it later |

## Decision

1. **Phase 2's indexing job is LLM-free.** It performs AST-aware chunking,
   embeds chunks with a bi-encoder embedding model (a model call, not a
   generative one), and builds the call/import graph via static parsing
   only. No chunk-level summary generation, no hierarchical
   summarization/clustering, no LLM-based entity/relationship extraction
   ships as part of Phase 2 or as a default-on behavior of any later phase.
2. **Richer RAG enrichment is deferred, not designed away**, and moves to
   the roadmap's "Deferred, not scheduled" section. When it lands, it lands
   as an **optional phase** a customer explicitly configures — a
   separately-invoked capability, not a change to the mandatory indexing
   path's cost or latency profile. This follows the same shape as knowhow's
   augmentation (ADR-0003): present when configured, a fully supported no-op
   when it isn't.
3. **Retrieval-quality gaps left by an LLM-free index are addressed at query
   time first, not by adding indexing-time LLM cost.** If raw-chunk
   embeddings prove insufficient, the first move is a query-time technique
   (HyDE, agentic exploration, graph-neighbor expansion — `rag-techniques.md`
   §5) that pays its cost only on an actual review, not on every push,
   before revisiting whether indexing-time enrichment is justified.

## Consequences

**Easier / positive:**
- Every indexed push costs the customer zero LLM inference spend —
  predictable, low operating cost, directly serving the "fast and cheap to
  run" requirement this decision is scoped to.
- Indexing latency stays low and independent of LLM endpoint
  latency/availability — a slow or rate-limited LLM endpoint can't stall the
  freshness SLA the indexing job commits to (ADR-0003's "freshness/SLA
  mismatch" reasoning applies here too).
- Smaller Phase 2 surface area to build and test: no summary-generation
  pipeline, no clustering job, no entity-extraction pass, no LLM-response
  parsing/validation on the indexing path.
- Consistent with design goal 8's already-accepted "optional, never a
  dependency" shape — this decision applies the same pattern to a second
  concern (indexing enrichment) rather than establishing a new one.

**Harder / trade-offs:**
- Retrieval quality on natural-language queries starts at the baseline raw
  embeddings provide, not the higher ceiling summary/hierarchical embeddings
  would give — acceptable per this decision, but a real, measurable gap
  until/unless query-time mitigations or the deferred enrichment phase land.
- Defers, rather than answers, exactly what the "optional phase where we can
  call out to a RAG" looks like — its interface, whether it's a first-party
  capability or an external call-out, and how its results fuse with review's
  own context bundle are all open (see below), same category of open
  question ADR-0003 left for knowhow's context-fusion UX.
- Requires discipline to hold the line in later phases: "just add a
  docstring-summary call, it's only one more LLM call" is exactly the
  incremental pressure this ADR exists to pre-empt.

## Open questions

- Exact shape of the future optional enrichment phase: a first-party
  capability inside review, an external call-out (RAG-as-a-service style),
  or both — not decided here, only that it's deferred and optional.
- What signal actually justifies turning it on for a given customer/repo
  (measured retrieval quality gap vs. a blanket "more is better" default) —
  needs its own evaluation before this phase gets scheduled.
- How a future enrichment phase's results would fuse with review's own
  code-derived context bundle, and whether that follows the same
  distinct-labeled-source pattern ADR-0003 established for knowhow.

## Related

- [`docs/roadmap.md`](../roadmap.md) — Phase 2's indexing bullets, scoped by
  this decision; the deferred enrichment phase added to "Deferred, not
  scheduled."
- [`docs/reference/rag-techniques.md`](../reference/rag-techniques.md) §§2–3,
  §5 — the techniques this decision defers, and the query-time-first
  principle motivating the deferral.
- [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) —
  design goal 3 (BYO-LLM, customer bears inference cost) and design goal 8
  (optional augmentation, never a dependency), both directly load-bearing
  for this decision.
- [`docs/adr/0003-knowhow-product-knowledge-augmentation.md`](0003-knowhow-product-knowledge-augmentation.md)
  — the precedent for "optional, never a dependency" this decision applies
  to a second concern.
