# ADR: Multi-org tenancy, scoped to one customer's internal structure

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

One Review deployment can connect more than one GitHub org. The intended
shape is a single enterprise customer running several internal GitHub orgs —
business units, internal teams, an "org per department" pattern many
enterprises already have — not unrelated companies sharing one deployment.
Org becomes a scoping key alongside repo and team in the tenancy hierarchy
(org → repo → team → findings/memory), the same way team already scopes
Memory and config in [`c4-architecture.md`](../reference/c4-architecture.md).
It is explicitly **not** a hard multi-tenant security boundary: no per-org
encryption-key separation, no assumption that different orgs on the same
deployment are mutually distrusting parties. [ADR-0001](0001-self-hosted-kubernetes-deployment.md)
already establishes the real trust boundary as the whole deployment (one
customer, their own infrastructure) — this decision extends what "org" means
inside that boundary, it doesn't add a new boundary.

## Context

The C4 architecture document left this open: "whether one deployment supports
multiple GitHub orgs under one customer install, or is strictly one org per
deployment." Two materially different shapes were implicit in that question
and needed disambiguating before schema/RBAC work could start:

1. **Multi-tenant SaaS-grade isolation** — orgs on one deployment treated as
   if they belong to different companies, requiring the kind of hard
   isolation guarantees (per-tenant encryption boundaries, strict
   cross-tenant leakage prevention as a security-critical invariant) a
   vendor-hosted platform serving unrelated customers would need.
2. **Internal multi-org grouping** — orgs on one deployment all belong to the
   same customer, who simply happens to run their GitHub estate as multiple
   orgs internally. The isolation need here is normal RBAC (a team shouldn't
   see another team's findings by default), not tenant-security isolation.

The confirmed requirement is (2). Single-org-per-deployment was the
simplest option but would force a customer running several internal GitHub
orgs to stand up a separate Review installation — separate Postgres,
separate Memory, separate admin surface — per org, fragmenting a company's
own cross-team learnings for no security reason, and contradicting the "one
deployment serves a whole enterprise" framing already in the C4 doc's design
goals.

## Options considered

| Option | Data model complexity | Isolation guarantees required | Fits self-hosted model | Verdict |
| --- | --- | --- | --- | --- |
| **Single org per deployment** | Simplest — no org concept above repo | None needed | Forces artificial fragmentation: N installs for one customer with N internal GitHub orgs | Reject — contradicts "one deployment serves a whole enterprise" |
| **Multi-org, multi-tenant-SaaS-grade isolation** | Highest — per-org encryption boundaries, strict leakage-prevention as a security invariant, tenant-aware everything | Full tenant isolation, as if orgs are different companies | Over-engineered — ADR-0001 already makes the deployment itself the trust boundary; this adds a second boundary the actual requirement doesn't call for | Reject — solves a threat model (mutually distrusting tenants) that doesn't apply here |
| **Multi-org as internal grouping** (org is a scoping key, not a tenant boundary) | Moderate — org joins repo/team in the existing scoping hierarchy, ordinary RBAC | Normal RBAC (team-level visibility control), no tenant-security isolation | Yes — matches one customer, one deployment, one Postgres, internal structure reflected as scope, not as separate tenants | **Accept** |

## Decision

Extend the tenancy hierarchy to org → repo → team → findings/memory, where
"org" maps 1:1 to a connected GitHub org (one deployment may hold multiple
GitHub App installations, one per org). Org is a scoping and configuration
key — findings, Memory entries, and per-team rules can be scoped at the org
level the same way they're already scoped at the team level — not a
multi-tenant security boundary. RBAC can and by default should grant
cross-org visibility to platform admins (e.g. a security/platform team
overseeing all of a company's internal orgs), which is the normal and
expected case here, unlike a real multi-tenant SaaS where that would be a
serious leak.

## Consequences

**Easier / positive:**

* One deployment per enterprise customer regardless of how many internal
  GitHub orgs they run — no artificial per-org install fragmentation, and no
  duplicated Postgres/Memory/admin surface for what is organizationally one
  customer.
* Simpler schema and RBAC than multi-tenant-SaaS-grade isolation would
  require — no per-org encryption boundary, no tenant-isolation test surface
  to build and maintain.
* Memory can genuinely serve its "team-shared learning" purpose across a
  company's own internal teams/orgs when an admin wants that visibility — a
  legitimate feature here, where it would be an actively wrong default for a
  real multi-tenant SaaS.

**Harder / trade-offs:**

* RBAC still needs a real org-scoping layer — a team in org A should not see
  org B's findings/Memory *by default* even though orgs aren't a hard
  security tenant boundary. This needs deliberate design so "not a tenant
  boundary" doesn't collapse into "no boundary at all." Left as an open
  question in [`c4-architecture.md`](../reference/c4-architecture.md).
* This model explicitly does **not** support a true multi-tenant use case —
  e.g. an MSP/consultancy running one Review deployment on behalf of several
  separate client companies. If that need arises, it requires revisiting
  this decision (most likely reintroducing a harder tenant boundary, or
  requiring separate deployments per client as today), not something this
  ADR's data model accommodates as-is.

## Related

* [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) —
  design goal 5 (tenancy) and the Level 2 Memory/config scoping this decision
  extends.
* [`docs/adr/0001-self-hosted-kubernetes-deployment.md`](0001-self-hosted-kubernetes-deployment.md)
  — establishes the deployment itself, not a sub-boundary within it, as the
  real trust/isolation boundary this decision relies on.
