# ADR: Self-hosted deployment via Kubernetes (production) and Docker Compose (development)

## Status

ACCEPTED

Amended 2026-09-20: the "two container images" packaging in the Options
table and Consequences below is updated to three — `docs/specs/spec-admin-gui.md`
(Phase 5) makes the Admin UI its own standalone deployable entrypoint
(`apps/admin_api`), not a screen served from the Webhook API. This is the
"real requirement" `docs/rules/architecture.md`'s "no separate admin-UI
service until forced" line already anticipated — an enterprise deployment
requirement for the Admin UI to scale, restart, and fail independently of
the review pipeline. Updated in place rather than superseded by a new ADR,
since the underlying decision (self-hosted, customer-run infrastructure)
is unchanged; only the image count it commits to has grown.

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

The platform ships as a product the customer installs and runs in their own
infrastructure — an official Helm chart for production Kubernetes deployments,
and a Docker Compose file running the same container images for local
development — rather than as a service we host and sell metered access to.
This reverses an earlier "zero infrastructure" design (no server, no database,
invoked as a local git hook or CI step) once it became clear that design
couldn't deliver the two most differentiating techniques identified in
[`research-current-art.md`](../research/research-current-art.md): real
indexing/RAG (§2.1) and persistent, team-shared memory (§2.4). Both require
durable, queryable, shared state that a stateless CLI cannot hold.

## Context

An earlier iteration of this project aimed to avoid all infrastructure: a
single CLI binary invoked as a git hook locally and as a CI job step, with no
database (state read live from the Git host's API) and no server (no webhook
receiver, nothing always-on). That direction was driven by a much smaller goal
— stop paying for review-as-a-service on one project — and every design
decision optimized for zero ongoing cost and zero ops burden on the project's
sole maintainer.

The goal has since changed: build an actual competitor to CodeRabbit, Qodo,
and Greptile, enterprise-ready, not a hacky personal script. Two requirements
fell directly out of that reframing that the zero-infrastructure design cannot
satisfy:

1. **Real indexing/RAG.** The research is explicit that a bare diff is
   "necessary but grossly insufficient" (§2.1) and that every credible
   competitor builds a structural/semantic index over the repo. An index that
   is actually useful for retrieval — embeddings, a call/import graph, kept
   incrementally up to date — needs a place to live that survives between
   runs and isn't rebuilt from scratch on every invocation.
2. **Persistent, team-shared memory.** §2.4 identifies per-repo learnings
   (CodeRabbit) and auto-inferred team rules (Greptile, Bito) as some of the
   most differentiating techniques in the field. Two options were considered
   and rejected before reaching this decision: committing memory as files
   into the reviewed repository (rejected — it would force every teammate who
   doesn't use the tool to carry files they don't need, and git doesn't
   support the similarity-search queries the technique actually depends on),
   and a per-developer local index with no sharing mechanism (rejected — it
   solves storage but not the "combine teammates' experiences" requirement
   that's the whole point of the technique).

Both requirements need the same thing: a persistent store that isn't inside
the reviewed repo and isn't hosted by us. The only architecture that provides
that without reintroducing "we host your code's review data" is a service the
customer runs themselves.

## Options considered

| Option | Customer trust story | Ops burden | Delivers indexing/RAG + shared memory | Verdict |
| --- | --- | --- | --- | --- |
| **Zero-infrastructure CLI** (prior design) | Excellent — nothing to trust, nothing runs continuously | None | No — no durable, shared, queryable store exists in this design | Reject — cannot deliver the two requirements driving this decision |
| **Vendor-hosted SaaS** (we run it, multi-tenant) | Weak for an enterprise sale — customer's proprietary code and review history sits in infrastructure we operate, same trust posture as CodeRabbit/Qodo today | High — we own uptime, backups, security, compliance (SOC2 etc.), multi-tenant isolation, for every customer at once | Yes | Reject for now — the stronger enterprise trust story is "your code never leaves your infrastructure"; revisit as an *additional*, not exclusive, offering once the self-hosted core is proven |
| **Self-hosted only** — customer runs Kubernetes (prod, via Helm) and Docker Compose (dev, same images) | Strong — customer's own infrastructure, own data, own network boundary | Shifts to the customer, but bounded: three container images (Webhook API, Worker, Admin UI, per the 2026-09-20 amendment), one stateful dependency (Postgres) | Yes — a real, persistent, customer-owned database is exactly what both requirements need | **Accept** |

## Decision

Ship the platform as container images with two supported deployment paths:
Kubernetes via an official Helm chart for production, and Docker Compose
(the same images) for local development. The customer's own infrastructure
hosts everything, including the Postgres/pgvector instance that carries the
index, findings history, and shared team memory. We hold no customer code,
review data, or credentials.

A vendor-hosted SaaS offering is explicitly deferred, not ruled out — it may
be added later as an alternative deployment option once the self-hosted
product is proven, but it is not a dependency of this decision.

## Consequences

**Easier / positive:**
* Strongest available trust story for an enterprise sale: the customer's
  proprietary code and review history never leave infrastructure they
  control — no data-processing agreement, no "which region is our code
  processed in" question to answer.
* Unlocks real indexing/RAG and persistent team-shared memory (the reasons
  this decision was made) without either polluting the reviewed repo or
  requiring us to operate shared infrastructure across customers.
* We carry no hosting, uptime, or multi-tenant-isolation burden — no on-call
  rotation for other companies' review pipelines.
* Monetization becomes licensing rather than metered inference/hosting,
  which composes cleanly with the bring-your-own-LLM-key decision
  ([ADR-0002](0002-implement-in-python.md) and the C4 doc's LLM Gateway) —
  we never touch the customer's LLM spend either.

**Harder / trade-offs:**
* The customer must run and operate Postgres, the Webhook API, the
  Worker, and (per the 2026-09-20 amendment) the Admin UI themselves —
  Helm chart quality (upgrades, health probes, resource defaults,
  observability hooks) directly determines how painful that is, and is
  now a first-class product surface, not an afterthought.
* We lose direct visibility into production behavior across customers
  (no shared logs/metrics unless the customer opts to forward them) —
  support and debugging depend more on what the customer can report and
  on strong local reproducibility (Docker Compose mirroring the same
  images) than on us observing a fleet we run.
* No usage-based revenue capture the way a hosted SaaS provides — the
  business model shifts to licensing, which changes packaging/pricing
  work outside the scope of this ADR.
* Heterogeneous customer Kubernetes environments (versions, ingress
  controllers, security policies) are a real support-surface risk that a
  single vendor-hosted environment wouldn't have.

## Related

* [`docs/specs/spec-admin-gui.md`](../specs/spec-admin-gui.md) — Phase 5's
  Admin UI PRD, the source of the 2026-09-20 amendment (third deployable
  image, standalone from the Webhook API).
* [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) — the
  full architecture this decision shapes.
* [`docs/adr/0002-implement-in-python.md`](0002-implement-in-python.md) —
  language decision, made after and dependent on this one.
* [`docs/research/research-current-art.md`](../research/research-current-art.md)
  — research into how existing tools achieve thorough review, which
  identified the indexing/RAG and shared-memory techniques driving this
  reversal.
