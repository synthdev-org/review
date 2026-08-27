# ADR: Review owns its own code indexing; knowhow augments via MCP with product knowledge

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Review keeps the indexing/RAG pipeline described in
[`c4-architecture.md`](../reference/c4-architecture.md): its own AST-aware
chunker, its own embeddings, its own call/import graph, kept incrementally
fresh on every push, stored in review's own Postgres/pgvector. Knowhow (a
separate, self-hostable multi-source RAG knowledge base — Confluence, Slack,
GitHub, tickets) is not review's indexing backbone. Instead, review's
Context Builder makes an additional, optional call to knowhow's MCP tools
(`knowhow_search`/`knowhow_fetch`/`knowhow_related`) — the same external
interface knowhow's own first-party clients already use — to pull in
**non-code product knowledge** and fuse it into the context bundle handed to
the LLM alongside review's own code-derived context. Review and knowhow stay
fully separate products: separate repos, separate deployables, separate
release cadences. Any integration happens over knowhow's network API, never
a shared database schema, and never an in-process library import (review is
Python per [ADR-0002](0002-implement-in-python.md); knowhow is TypeScript —
no in-process path exists between them regardless of preference).

## Context

Knowhow already builds a general-purpose, multi-source RAG pipeline well —
AST-ish chunking, local embeddings, hybrid vector+FTS search, an entity
graph, cross-encoder reranking, all on Postgres/pgvector/Apache AGE. The
question this ADR resolves: how much of that should review reuse versus
build itself, given the goal of selling review as a separate, optional
module alongside knowhow.

Using knowhow as review's *entire* indexing base was considered and
rejected. Four issues, all specific to code review's quality bar and
freshness needs, made that shape unworkable:

1. **Chunking ceiling.** Knowhow's code connector uses a documented
   line-pattern heuristic, not a real AST parser — a reasonable trade-off
   for general-purpose knowledge, but below the bar code review needs. The
   research survey (`research-current-art.md` §2.1) treats real AST-aware
   chunking as necessary for the field's highest-leverage technique, not
   optional polish.
2. **Graph shape mismatch.** Knowhow's graph is built for general
   entity/relationship navigation across docs, Slack, and tickets — not for
   the query code review depends on most: "what calls this function whose
   signature just changed" (the "blast radius" technique, §2.1). Generic
   graph traversal doesn't substitute for a review-purpose-built call/import
   graph.
3. **Freshness/SLA mismatch.** A PR review needs the index current as of the
   exact commit under review, ideally within seconds of push — a materially
   tighter SLA than general knowledge sync cadence.
4. **Roadmap dependency.** Code-indexing fidelity would become subject to
   knowhow's own priorities (breadth across knowledge sources), which
   review's team doesn't independently control.

None of these four issues apply to the *non-code* knowledge knowhow already
covers well — Confluence pages, Slack threads, tickets. A design doc being a
day stale is tolerable in a way a stale code index isn't; review has no
code-review-specific reason to want different chunking for a Confluence page
than knowhow's general-purpose approach already gives it; and review has no
reason to build its own Confluence/Slack connectors when knowhow's already
exist. Splitting responsibility along this line — code indexing stays with
review, non-code product knowledge comes from knowhow — captures the reuse
win for the part of the problem that was never review's differentiator,
without capping review's core capability on someone else's roadmap.

## Options considered

| Option | Code-review quality control | Freshness fit | Reuses knowhow | Runtime dependency |
| --- | --- | --- | --- | --- |
| **Knowhow as full indexing base** | Weak — capped by knowhow's general-purpose chunking/graph choices, outside review's control | Mismatched — general knowledge sync cadence, not per-push SLA | Maximal | Hard — review non-functional without knowhow |
| **Fully self-contained** (no knowhow integration) | Strong — review controls its entire quality bar | Strong — review's own index is inherently fresh | None — duplicates Confluence/Slack/ticket connectors from scratch | None |
| **Split responsibility** (review owns code, knowhow augments non-code) | Strong for code (review's own pipeline); appropriate for non-code (knowhow's existing connectors) | Code stays per-push fresh; non-code inherits knowhow's cadence, which fits that content type | Partial — exactly the slice that was never review's differentiator | Soft — review fully functions without knowhow, richer with it | **Accept** |

## Decision

1. **Review's own indexing pipeline** — AST-based chunking, embeddings, and
   a call/import graph, built and kept incrementally fresh by the Indexing
   job type, stored in review's own Postgres/pgvector — is review's primary,
   always-available source of context. It never depends on knowhow being
   present or reachable.
2. **Knowhow is an optional augmentation**, wired into the Context Builder
   step of the review-job pipeline as an additional MCP client call
   (`knowhow_search`/`knowhow_fetch`/`knowhow_related`), scoped to non-code
   product knowledge — Confluence, Slack, tickets, and any other source
   knowhow connects. A knowhow endpoint + credential, configured per
   org/repo alongside the rest of review's day-two config, determines
   whether this call happens at all; its absence is a normal, fully
   supported deployment state.
3. **Context fusion**: results from review's own code-derived context and
   knowhow's product-knowledge results are assembled into one context bundle
   for the review-generation LLM call as distinct, clearly labeled sources
   (e.g. "from the codebase" vs. "from product knowledge") rather than
   merged/reranked into one undifferentiated list — the two carry different
   trust/freshness characteristics, and both the LLM and any human reading a
   finding's citation should be able to tell them apart.

## Consequences

**Easier / positive:**
* Code-review quality stays fully in review's control — own chunker, own
  graph, own freshness SLA — with no dependency on knowhow's roadmap for
  review's core capability.
* Review has no hard runtime dependency on knowhow. It ships and functions
  standalone; the "optional module" framing describes the augmentation
  feature itself, not review's basic operation.
* Still captures a real, appropriately-scoped reuse win: no need to build
  Confluence/Slack/ticket connectors or cross-source retrieval.
* Licensing/upsell story is literal: knowhow connectivity is a config toggle
  that unlocks a genuinely additive capability, a natural "buy knowhow too
  and reviews get better" pitch.
* The self-hosted trust/simplicity story (one product, one Postgres, two
  container images) is preserved for customers who only run review; it only
  extends to a second product's footprint for customers who opt into the
  augmentation.

**Harder / trade-offs:**
* Review keeps the full engineering cost of its own indexing pipeline
  (chunker, embedder, graph, incremental reindex) — no reuse savings apply
  to the code-indexing slice, only to the non-code augmentation.
* Two heterogeneous retrieval results (review's own ranked candidates,
  knowhow's MCP search results) need a real fusion/presentation design in
  the Context Builder, and must stay distinguishable per the Decision above
  rather than being silently blended.
* Knowhow-sourced content carries its own freshness cadence and its own
  indexing-time enrichment (knowhow's LLM calls generating
  summaries/propositions/entities) that review doesn't control or
  independently verify — the citation-labeling requirement is a mitigation,
  not a full fix.
* Knowhow's MCP tool contract is an inter-product API review depends on for
  the augmentation feature — its versioning/deprecation guarantees should be
  confirmed before shipping this as a supported feature.
* Tenancy mapping between review's org/repo/team model and knowhow's tenant
  model is a real integration detail: how review's config resolves the
  correct knowhow tenant for a given org.

## Open questions

* Exact UX/prompt design for presenting dual-sourced context to the
  review-generation LLM call, and how citations back to knowhow-sourced
  content should render in a posted PR comment.
* Whether review's per-team Memory store (learnings, dismissed-finding
  filtering) stays entirely review-owned, or also feeds knowhow's
  `fact_context` layer so other knowhow surfaces benefit from it too.
* Knowhow's MCP contract versioning/deprecation policy.

## Related

* [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) —
  review's indexing pipeline as designed, reaffirmed here unchanged; needs a
  small addition documenting the optional knowhow augmentation call in the
  Context Builder step.
* [`docs/adr/0001-self-hosted-kubernetes-deployment.md`](0001-self-hosted-kubernetes-deployment.md)
  — the self-hosted trust/simplicity story this decision preserves for
  review-only deployments.
* [`docs/adr/0002-implement-in-python.md`](0002-implement-in-python.md) —
  the language boundary that rules out in-process reuse of knowhow's code.
* [`docs/research/research-current-art.md`](../research/research-current-art.md)
  §2.1 — the AST-chunking and call-graph "blast radius" findings that
  motivated keeping code indexing under review's own control.
