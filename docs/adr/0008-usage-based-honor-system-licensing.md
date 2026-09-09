# ADR: Usage-based, honor-system licensing — free under 20 private reviews/day per GitHub org, nothing technically gated

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

The platform is free to run at any scale on public repositories, and free on
private repositories up to 20 reviews/calendar-day (reset 04:00, not
midnight) **per connected GitHub org**, not per deployment. Above that
threshold, a paid license is required by the terms of use, but the software
enforces nothing — no feature gate, no hard stop, no blocked review. The only
technical behavior added is disclosure, on two surfaces: the admin UI shows
each connected org's day's private-repo review count against the free-tier
guideline, and once an org exceeds it, every review posted for that org that
day carries an added footer line on the PR itself, naming the overage and
that a license is required — the review's actual findings are posted in
full regardless. This is deliberately closer to a pure honor system
([ADR-0001](0001-self-hosted-kubernetes-deployment.md)'s "Option considered"
table, pattern 1) than to a technically-enforced license (Redis
Enterprise/NGINX Plus-style file or cert validation), because
[ADR-0001](0001-self-hosted-kubernetes-deployment.md) already committed to
zero vendor visibility into any deployment — there is no channel through
which company size, revenue, or seat count could be verified even if we
wanted to gate on them.

## Context

[ADR-0001](0001-self-hosted-kubernetes-deployment.md) settled *that* the
business model is licensing rather than metered hosting/inference, but
explicitly left the licensing mechanism itself out of scope ("packaging/pricing
work outside the scope of this ADR"). Two questions had to be resolved before
that gap could close:

1. **What is the free/paid threshold measured against?** Company size or
   revenue (the Docker Desktop precedent) is unverifiable by software that,
   per [ADR-0001](0001-self-hosted-kubernetes-deployment.md), never observes
   the customer's infrastructure and never proxies inference. A usage metric
   the deployment computes about itself — review volume — is the only kind of
   threshold this architecture can actually measure honestly.
2. **Is anything technically enforced?** Rejected outright. A self-hosted,
   BYO-LLM, no-telemetry product has no reliable way to distinguish a
   legitimate license-file bypass from a customer who genuinely lost their
   license file, and hard technical gating (à la Redis Enterprise) requires
   exactly the kind of local artifact-and-signature infrastructure that adds
   real engineering cost for a threat model (large enterprises quietly
   evading payment) that a contract and an honest usage dashboard address
   about as effectively for the audience this product targets.

Public repositories are excluded from the count entirely — open-source usage
should impose zero friction, matching CodeRabbit's own free tier (unlimited
public repos, uncapped) and consistent with this platform wanting broad
goodwill/adoption in OSS the same way.

The concrete threshold was pegged against CodeRabbit's published free-tier
limit — 4 PR reviews/hour on private repos, uncapped on public — as the most
directly comparable competitor data point. CodeRabbit's number is an
hourly-burst rate limit, not a daily allowance, so it does not translate 1:1
into a calendar-day honor-system figure: sustaining 4/hour across a working
day would suggest 30-40/day, but that overstates what CodeRabbit actually
lets a small team do in practice (their own framing: a 5-person team doing
1-2 PRs/dev/day comfortably fits under their limit). 20 reviews/day was
chosen as a number that fits that same small-team framing without adopting
CodeRabbit's hourly mechanism.

CodeRabbit's limit is two-dimensional (4 PRs/hour AND 200 files/hour) so that
one large PR can't be used to exhaust an otherwise generous review budget.
This platform's costing model does not depend on diff size the way an
inference-metered SaaS's does (LLM spend is the customer's own, per
[ADR-0001](0001-self-hosted-kubernetes-deployment.md)'s bring-your-own-LLM
decision) — a files/hour-equivalent cap was considered and rejected as
solving a cost problem this product doesn't have.

A third question surfaced once [ADR-0004](0004-multi-org-tenancy.md)'s
multi-org tenancy was factored in: **what is the threshold scoped to — the
deployment as a whole, or each connected org individually?** A single
deployment can hold several internal GitHub orgs. Scoping the threshold to
the whole deployment would let usage from a busy org and a quiet org net out
against each other in a way that under-counts real usage from the busy org,
and — more importantly — a per-deployment threshold is trivially reset by
standing up a second deployment (another `helm install`, per
[ADR-0001](0001-self-hosted-kubernetes-deployment.md)'s "two container
images" packaging), which is far cheaper for a customer to do than to
restructure their actual GitHub org layout. Scoping per org, keyed by the
org's stable GitHub App installation ID rather than by which deployment
happens to be counting it, is both the more accurate attribution for the
multi-org-per-deployment case ADR-0004 describes and the more defensible
contractual unit — "licensed per connected GitHub org" survives a customer
moving that org between deployments in a way "licensed per deployment" does
not.

This does not fully close the evasion path — see Consequences.

A fourth question: **is the admin UI's dashboard the only disclosure surface,
or should the overage also appear where developers actually look — the PR
itself?** The admin UI only reaches whoever has admin access and chooses to
check it; the PR review is something every author already sees on every PR,
with no extra step. The GitHub Adapter already posts inline comments and
check-run status per review (per `c4-architecture.md`'s Level 2 diagram), so
reading the same per-org counter used for the admin dashboard and appending
one line when over threshold is a small addition to an existing write path,
not a new one. The trade-off is reach vs. tone: a PR-visible notice reaches
every collaborator with repo access, most of whom have no purchasing
authority and did nothing to cause the overage, which risks reading as
sales-pressure nagware layered onto a product whose differentiation is
developer experience. Kept to a single quiet footer line, not a repeated or
prominent element, this was judged worth it for the reach it buys — see
Decision.

## Options considered

| Option | Verifiable by the software | Matches no-telemetry architecture | Engineering cost | Verdict |
| --- | --- | --- | --- | --- |
| **Company size / revenue threshold** (Docker Desktop pattern) | No — deployment cannot observe headcount or revenue | Compatible in principle, but unenforceable and unverifiable either way | Low (pure legal terms) | Reject — threshold the software can never itself surface honestly to the customer |
| **Local signed license file, hard-gated features** (Redis Enterprise / NGINX Plus pattern) | Yes, for seat/node/expiry counts | Yes | High — key issuance, signature verification, renewal flow, degrade-gracefully behavior | Reject — solves a stricter enforcement need than this decision requires, and adds a distribution/support surface (lost keys, renewal friction) disproportionate to the honor-system goal |
| **Reviews/day threshold, scoped per deployment** | Yes, within one deployment | Yes | Low | Reject — undercounts a deployment with several busy orgs netting against quiet ones, and is trivially reset by standing up a second deployment for the same org |
| **Reviews/day threshold, scoped per connected GitHub org, disclosed but not enforced** (this decision) | Yes — the deployment already computes and stores every review it performs, and org is already a first-class scoping key | Yes — no new outbound calls, no artifact to issue or verify | Low — a counter keyed by org ID, a per-repo public/private check already available via the GitHub API, and a dashboard line already planned as part of the MVP admin UI | **Accept** |

## Decision

Count reviews performed against private repositories only (public repos never
count, unconditionally free). A review is one review event — no secondary
files/hour or diff-size dimension, since diff size does not affect this
platform's own costs the way it affects an inference-metered SaaS's.

The threshold is **20 private-repo reviews per calendar day, per connected
GitHub org**, where the day resets at **04:00** (not midnight), chosen to
avoid splitting a single workday's review activity across two counting
periods for teams working late. The counting key is the org's GitHub App
installation ID, not the deployment — a deployment holding multiple orgs
(per [ADR-0004](0004-multi-org-tenancy.md)) tracks each org's count
independently, and none of an org's usage is netted against another org's
headroom.

Nothing is technically gated at or above the threshold. Disclosure happens on
two surfaces, both read from the same per-org counter:

1. **Admin UI** (the same MVP-committed surface used for Memory
   approve/reject) shows each connected org's current day's private-repo
   review count. Once an org's count exceeds 20, the UI also shows a notice
   that *that org* is over the free-tier guideline and a paid license applies
   to it. Cross-org visibility follows the same platform-admin exception
   [ADR-0004](0004-multi-org-tenancy.md) already grants for Memory/findings —
   a platform admin can see every connected org's usage, an org-scoped team
   by default sees only its own.
2. **The review itself.** Once an org's count for the day exceeds 20, every
   subsequent review posted for that org — via the GitHub Adapter, alongside
   the existing inline comments and check-run status — carries one added
   footer line stating the org is over its free-tier daily limit and a
   license is required. This is additive only: the review's findings, inline
   comments, and check-run status are posted in full exactly as they would be
   under the threshold; nothing about the review's content or delivery
   changes.

Both surfaces are informational only — no review is blocked, delayed, or
degraded as a result of either.

## Consequences

**Easier / positive:**
* No license-file issuance, signature verification, renewal, or "lost key"
  support burden — the entire mechanism is a counter and a UI banner.
* Consistent with [ADR-0001](0001-self-hosted-kubernetes-deployment.md)'s
  trust story: no new outbound call, no new artifact the customer must
  install or protect, nothing added that could be mistaken for phone-home
  telemetry.
* The metric (reviews/day) is something the deployment can state to its own
  admin honestly and unconditionally, unlike company size or revenue, which
  it can never verify.
* Public-repo exclusion keeps OSS usage frictionless, matching the
  competitive baseline this was pegged against.
* Per-org attribution correctly reflects usage in the multi-org-per-deployment
  case [ADR-0004](0004-multi-org-tenancy.md) describes, rather than letting a
  quiet org's headroom mask a busy org's overage.
* The PR-footer channel reaches whoever is actually looking at the PR without
  requiring anyone to know the admin UI exists or check it — a meaningfully
  wider audience than the dashboard alone, using a write path (the GitHub
  Adapter's existing comment/check-run post) that already exists.

**Harder / trade-offs:**
* Enforcement is entirely contractual — a customer can exceed the threshold
  indefinitely with zero technical consequence. This is an accepted
  trade-off, not an oversight: the alternative (hard gating) was rejected
  above as disproportionate engineering cost for this architecture.
* **Per-org scoping does not close the multi-deployment evasion path, and
  nothing running purely inside the customer's own infrastructure can.** A
  deployment's counter only ever sees the reviews *it* processed; it has no
  way to know whether the same GitHub org is also connected to a second,
  quieter-looking Review deployment elsewhere, because
  [ADR-0001](0001-self-hosted-kubernetes-deployment.md) commits to zero
  vendor visibility and no telemetry — detecting this would require exactly
  the kind of phone-home call that ADR explicitly rules out as incompatible
  with the product's trust story. This is accepted, not solved: the license
  terms define the unit as "per connected GitHub org, regardless of how many
  Review deployments that org is connected to," so a customer who splits one
  org across several deployments to stay under 20/day on each is in breach of
  contract even though no software will ever detect it. Closing this
  technically would mean reversing ADR-0001, which is out of scope here.
* The chosen number (20/day) is a judgment call pegged loosely to a
  competitor's differently-shaped limit (hourly burst vs. daily honor
  threshold), not derived from this platform's own cost model, since BYO-LLM
  means there isn't a direct cost-per-review to size it against. It may need
  revisiting once real usage data exists.
* A team spanning multiple time zones may find a fixed 04:00 reset arbitrary
  relative to their own working hours; this was accepted as simpler than a
  configurable or rolling-window reset for a threshold that isn't
  enforced anyway.
* The PR-footer channel is visible to every collaborator with repo access —
  most of whom have no purchasing authority and did nothing to cause the
  overage — which risks reading as sales-pressure nagware on a product whose
  differentiation is developer experience. Mitigated, not eliminated, by
  keeping it to a single quiet line rather than a repeated or prominent
  element; if this proves to hurt developer sentiment in practice, the
  admin-UI-only channel from the prior version of this decision remains the
  fallback.

## Related

* [`docs/adr/0001-self-hosted-kubernetes-deployment.md`](0001-self-hosted-kubernetes-deployment.md)
  — establishes licensing (not metered hosting) as the business model and
  zero vendor visibility as a constraint this decision operates inside.
* [`docs/adr/0004-multi-org-tenancy.md`](0004-multi-org-tenancy.md) — the
  org/repo scoping this decision's per-repo public/private check relies on.
* [`docs/adr/0003-knowhow-product-knowledge-augmentation.md`](0003-knowhow-product-knowledge-augmentation.md)
  — prior precedent for licensing/upsell expressed as a config toggle rather
  than a technical gate.
* [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) —
  design goal 9's admin UI, the surface this decision's disclosure banner
  extends.
