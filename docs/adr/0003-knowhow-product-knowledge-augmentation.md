# ADR: Review owns its own code indexing; external product-knowledge providers augment via review's own MCP contract, knowhow first

## Status

ACCEPTED

**Amended 2026-09-17**: generalized from a knowhow-specific integration to a
review-owned, vendor-neutral MCP contract that any RAG/product-knowledge
provider can implement. This amends the original decision in place rather
than superseding it with a new ADR number — nothing has shipped against the
original knowhow-specific framing (Phase 7 in
[`roadmap.md`](../roadmap.md) hasn't started; no code references knowhow's
tool names), so there is no shipped behavior this amendment invalidates. The
substance of the original decision (review owns code indexing outright,
external augmentation is optional and non-code-scoped, results stay
distinctly labeled) is unchanged; what changes is that the integration
contract is now review's own, not knowhow's.

**Amended 2026-10-11**: the open questions below are resolved by
[`docs/specs/spec-product-knowledge.md`](../specs/spec-product-knowledge.md)
and [ADR-0013](0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md),
again in place (nothing has shipped against this contract). Three substantive
changes to the text above, none altering the decision's direction:

- **The contract gains fields it was missing for deterministic citations:**
  every result item carries a required `title`, and search/related items carry
  an optional `updated_at`; `url` and `score` are optional-nullable. A fourth
  tool, `product_knowledge_contract`, is how a provider declares the contract
  version(s) it implements (the "negotiated at connection time" mechanism).
  `product_knowledge_related` is optional for providers.
- **The credential is held by reference.** The per-org/repo day-two config
  stores the endpoint, display name, and a credential *reference*; the token
  itself is bootstrap config (a Kubernetes `Secret`, `existingSecret`-
  compatible) bound to specific hosts, consistent with design goal 7.
- **The "works out of the box" claim for knowhow is not yet true:** knowhow
  currently exposes `knowhow_search`/`knowhow_fetch`/`knowhow_related` with
  different arguments and encoding. It becomes true when knowhow registers the
  contract tools; the conformance suite is the acceptance check.

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Review keeps the indexing/RAG pipeline described in
[`c4-architecture.md`](../reference/c4-architecture.md): its own AST-aware
chunker, its own embeddings, its own call/import graph, kept incrementally
fresh on every push, stored in review's own Postgres/pgvector. External
product-knowledge sources (Confluence, Slack, tickets, and similar
non-code content) are not review's indexing backbone. Instead, review's
Context Builder makes an additional, optional MCP client call against
**review's own vendor-neutral tool contract** — `product_knowledge_search` /
`product_knowledge_fetch` / `product_knowledge_related` (see "The contract"
below) — to pull in non-code product knowledge and fuse it into the context
bundle handed to the LLM alongside review's own code-derived context. Any
MCP server that implements this contract can be configured as the provider
for a given org/repo; review's Context Builder code has no knowledge of, or
dependency on, which product sits behind the endpoint.

**knowhow** (a separate, self-hostable multi-source RAG knowledge base —
Confluence, Slack, GitHub, tickets) is the contract's reference and first
implementation, and remains the only implementation that exists today. It
gets no special-cased code path: it is configured the same way any future
provider would be, by pointing review's per-org/repo config at an MCP
endpoint that speaks the contract. Review and knowhow stay fully separate
products regardless: separate repos, separate deployables, separate release
cadences. Any integration happens over the configured MCP endpoint, never a
shared database schema, and never an in-process library import (review is
Python per [ADR-0002](0002-implement-in-python.md); knowhow is TypeScript —
no in-process path exists between them regardless of preference, and
wouldn't be desirable even if it did, since it would defeat the
provider-agnostic point of this decision).

## Context

Knowhow already builds a general-purpose, multi-source RAG pipeline well —
AST-ish chunking, local embeddings, hybrid vector+FTS search, an entity
graph, cross-encoder reranking, all on Postgres/pgvector/Apache AGE. The
original question this ADR resolved: how much of that should review reuse
versus build itself, given the goal of selling review as a separate,
optional module alongside knowhow. That question, and its answer (below),
are unchanged by this amendment.

A second question surfaced once Phase 6/7 planning made clear this would
ship as real, customer-facing config, not just an internal integration:
**should the thing a customer configures be "a knowhow endpoint," or "a
product-knowledge provider endpoint that happens to default to knowhow"?**
The former bakes a specific vendor's product into review's config schema,
job payloads, and Context Builder code — a customer who runs a different
internal RAG system, or a competitor's product, would have no path to the
same augmentation feature short of that system pretending to be knowhow.
The latter costs almost nothing extra: MCP is already the transport
(decided below, unchanged), so the only new work is defining review's tool
names and payload shapes as review's own contract instead of adopting
knowhow's, and labeling results by the configured provider's name rather
than hardcoding "knowhow" in citation text. Given review is being built and
sold as a genuine, standalone competitor to CodeRabbit/Qodo/Greptile (not a
knowhow add-on — see `AGENTS.md`), locking its one optional-augmentation
feature to a single named vendor was an unforced product constraint, not a
technical necessity.

This is not new abstraction for its own sake — per `architecture.md`'s
simplicity rule ("no interface, abstraction, or generic without a second
real implementation already in view"), the bar this amendment clears is
that **the contract itself**, not a second Python implementation, is the
abstraction boundary. MCP already made the transport swappable; this
amendment only removes the one place review's own naming coupled that
transport-level swappability to a specific vendor's tool names. No new
Python interface, adapter class, or plugin registry is introduced — see
"Decision" below.

The four issues that ruled out knowhow (or any RAG system) as review's
*entire* indexing base are unchanged and still apply to *any* provider, not
just knowhow:

1. **Chunking ceiling.** A general-purpose RAG system's code connector
   optimizes for broad applicability, not the AST-aware chunking fidelity
   code review's quality bar needs (research survey `research-current-art.md`
   §2.1).
2. **Graph shape mismatch.** A general-purpose entity/relationship graph
   doesn't substitute for a review-purpose-built call/import graph answering
   "what calls this function whose signature just changed" (§2.1).
3. **Freshness/SLA mismatch.** A PR review needs the index current as of the
   exact commit under review, within seconds of push — tighter than any
   general knowledge sync cadence.
4. **Roadmap dependency.** Code-indexing fidelity would become subject to
   an external product's own priorities, which review's team doesn't
   independently control — true of knowhow specifically, and of any other
   provider a customer might point review at instead.

None of these four issues apply to the *non-code* knowledge a general RAG
system already covers well — Confluence pages, Slack threads, tickets.
Splitting responsibility along this line — code indexing stays with review,
non-code product knowledge comes from an external, swappable provider —
captures the reuse win for the part of the problem that was never review's
differentiator, without capping review's core capability on any one
provider's roadmap, knowhow's included.

## Options considered

| Option | Code-review quality control | Freshness fit | Vendor lock-in | Runtime dependency |
| --- | --- | --- | --- | --- |
| **External provider as full indexing base** | Weak — capped by that provider's general-purpose chunking/graph choices, outside review's control | Mismatched — general knowledge sync cadence, not per-push SLA | N/A (rejected on quality grounds regardless of vendor) | Hard — review non-functional without it |
| **Fully self-contained** (no external augmentation) | Strong — review controls its entire quality bar | Strong — review's own index is inherently fresh | None | None — but duplicates Confluence/Slack/ticket connectors from scratch |
| **Split responsibility, knowhow-specific contract** (original 2026 decision) | Strong for code; appropriate for non-code | Code stays per-push fresh; non-code inherits provider cadence | High — config schema, job payloads, and citation text all name knowhow specifically; a different provider has no supported path in | Soft — review functions without it, richer with it |
| **Split responsibility, review-owned vendor-neutral contract** (this decision) | Strong for code; appropriate for non-code | Same as above | None — any MCP server implementing review's contract plugs in; knowhow is the reference implementation, not a special case | Soft — review functions without it, richer with it — **Accept** |

## Decision

1. **Review's own indexing pipeline** — AST-based chunking, embeddings, and
   a call/import graph, built and kept incrementally fresh by the Indexing
   job type, stored in review's own Postgres/pgvector — is review's primary,
   always-available source of context. It never depends on any external
   provider being present or reachable. (Unchanged from the original
   decision.)
2. **External product-knowledge augmentation is optional**, wired into the
   Context Builder step of the review-job pipeline as an additional MCP
   client call against **review's own tool contract** (see "The contract"
   below), scoped to non-code product knowledge — Confluence, Slack,
   tickets, and any other source a configured provider connects. A
   provider endpoint + credential + display label, configured per org/repo
   alongside the rest of review's day-two config, determines whether this
   call happens at all and which provider serves it; its absence is a
   normal, fully supported deployment state.
3. **The contract** is a small, versioned MCP tool spec review defines and
   owns, independent of any one provider's own API:
   - `product_knowledge_search(query, top_k) -> [{id, snippet, source_type,
     url, score}]`
   - `product_knowledge_fetch(id) -> {content, source_type, url,
     updated_at}`
   - `product_knowledge_related(id, top_k) -> [{id, snippet, source_type,
     url, score}]`
   - Contract version is negotiated at connection time (an MCP server
     declares the contract version(s) it implements); review's Context
     Builder refuses to call a provider whose declared version it doesn't
     recognize, and treats that the same as "no provider configured" (see
     Consequence below) rather than failing the review.
   - This is a wire-level contract only — no Python interface or adapter
     class per provider is introduced. One generic MCP client, config-driven
     by endpoint/credential, is sufficient (the same shape as the LLM
     Gateway: one LiteLLM-backed client, many providers reachable through
     it via config, not one adapter class per LLM vendor).
4. **knowhow implements this contract as its reference implementation.**
   knowhow's MCP server exposes `product_knowledge_search` /
   `product_knowledge_fetch` / `product_knowledge_related` (internally
   whatever knowhow's own search/fetch/related primitives already are,
   adapted to this shape) so that pointing review at a knowhow deployment
   works out of the box. Nothing about knowhow is privileged in review's
   code, config schema, or job payloads beyond being the provider the
   reference implementation and default docs/examples use.
5. **Context fusion**: results from review's own code-derived context and
   the configured provider's product-knowledge results are assembled into
   one context bundle for the review-generation LLM call as distinct,
   clearly labeled sources — labeled by the provider's configured display
   name (e.g. "from the codebase" vs. "from product knowledge (knowhow)"),
   never merged/reranked into one undifferentiated list. (Unchanged in
   substance from the original decision; only the label is now
   provider-driven rather than hardcoded.)

## Consequences

**Easier / positive:**

- Code-review quality stays fully in review's control — own chunker, own
  graph, own freshness SLA — with no dependency on any external provider's
  roadmap for review's core capability.
- Review has no hard runtime dependency on any product-knowledge provider.
  It ships and functions standalone; the "optional module" framing describes
  the augmentation feature itself, not review's basic operation.
- Still captures a real, appropriately-scoped reuse win: no need to build
  Confluence/Slack/ticket connectors or cross-source retrieval — for
  whichever provider a customer already runs.
- No vendor lock-in on the one optional-augmentation feature: a customer
  running a different internal RAG system, or a future competing product,
  has a real, supported path to the same capability by implementing one
  small MCP contract — not a reason to fork review or wait on a
  knowhow-specific integration.
- Licensing/upsell story is still literal and arguably stronger: knowhow
  connectivity remains a config toggle that unlocks a genuinely additive
  capability ("buy knowhow too and reviews get better"), but the pitch no
  longer depends on review having hardcoded a competitor out.
- The self-hosted trust/simplicity story (one product, one Postgres, two
  container images) is preserved for customers who only run review; it only
  extends to a second product's footprint for customers who opt into the
  augmentation, regardless of which product that is.

**Harder / trade-offs:**

- Review keeps the full engineering cost of its own indexing pipeline
  (chunker, embedder, graph, incremental reindex) — no reuse savings apply
  to the code-indexing slice, only to the non-code augmentation. (Unchanged.)
- Two heterogeneous retrieval results (review's own ranked candidates, the
  provider's MCP search results) need a real fusion/presentation design in
  the Context Builder, and must stay distinguishable per the Decision above
  rather than being silently blended. (Unchanged.)
- Review now owns defining and versioning its own contract — previously an
  open question was "confirm knowhow's MCP contract versioning/deprecation
  policy"; now review must set and maintain that policy itself, for a
  contract other parties (starting with knowhow, potentially others later)
  depend on. This is more upfront design work than adopting an existing
  API, in exchange for not being coupled to it.
- A third-party MCP server claiming to implement the contract might do so
  incorrectly or incompletely (e.g. `product_knowledge_related` returning
  unrelated results, or `source_type`/`url` fields populated inconsistently)
  with no way for review to verify conformance beyond runtime behavior —
  see "Open questions."
- Provider-sourced content carries its own freshness cadence and its own
  indexing-time enrichment that review doesn't control or independently
  verify — the citation-labeling requirement is a mitigation, not a full
  fix. (Unchanged from the original decision, now stated generically.)
- Tenancy mapping between review's org/repo/team model and a given
  provider's own tenant model is a real integration detail per provider,
  not solved once for knowhow specifically.

## Open questions

All four original open questions are resolved (2026-10-11):

- **Context-fusion/citation UX** — resolved in
  [`spec-product-knowledge.md`](../specs/spec-product-knowledge.md): a
  separate labeled prompt section and a separate bundle budget; citations
  rendered by the platform as `[title](url) — source_type · host` under a
  label naming the provider's display name.
- **Feeding Memory back to a provider** — decided **out of scope**: the
  contract is read-only and Memory stays review-owned and human-approved
  (design goal 9). Revisit only as a new optional tool under its own ADR.
- **Contract versioning/deprecation policy** — decided in
  [ADR-0013](0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md)
  decision 4: integer majors, additive-only within a major, current and
  previous major supported, 12-month minimum deprecation window.
- **Conformance harness** — decided **yes**, shipped with Phase 7 as a
  pytest-runnable suite plus an in-repo fake provider.

## Related

- [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) —
  review's indexing pipeline as designed, reaffirmed here unchanged; needs
  the optional augmentation call in the Context Builder step documented in
  provider-agnostic terms.
- [`docs/roadmap.md`](../roadmap.md) — Phase 7, which delivers this
  contract and its reference (knowhow) configuration.
- [`docs/specs/spec-product-knowledge.md`](../specs/spec-product-knowledge.md)
  and [ADR-0013](0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md)
  — the wire contract, configuration, security model, and client
  implementation decisions for Phase 7.
- [`docs/adr/0001-self-hosted-kubernetes-deployment.md`](0001-self-hosted-kubernetes-deployment.md)
  — the self-hosted trust/simplicity story this decision preserves for
  review-only deployments.
- [`docs/adr/0002-implement-in-python.md`](0002-implement-in-python.md) —
  the language boundary that rules out in-process reuse of any TypeScript
  provider's code, knowhow included.
- [`docs/research/research-current-art.md`](../research/research-current-art.md)
  §2.1 — the AST-chunking and call-graph "blast radius" findings that
  motivated keeping code indexing under review's own control regardless of
  which external provider, if any, is configured.
