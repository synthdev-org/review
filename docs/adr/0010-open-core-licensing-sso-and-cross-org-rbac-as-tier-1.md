# ADR: Open-core licensing — SSO/OIDC admin identity federation and cross-org RBAC as Tier 1, via a future `review-enterprise` plugin

## Status

PROPOSED

Drafted at the project owner's request, following the admin-stats and
cross-product-identity exploration that surfaced the gap this ADR closes.
Unlike this repo's other ADRs (each recording a design already settled
against working code), this one commits to a commercial tiering line ahead
of any of the capabilities it gates existing — flip to ACCEPTED once
reviewed, don't treat it as settled by the act of being written.

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

`platform/docs/adr/0003-open-core-licensing-and-distribution-model.md`
(amended 2026-09-10 to formally cover review) establishes the platform-wide
open-core model — free and fully usable for solo devs/small teams, monetized
via a two-tier gate — and states it's written at the platform level
precisely so each product evaluates its own capabilities against the same
criteria, not just knowhow's original SSO example. This ADR does that
evaluation for review and finds two capabilities that meet the Tier 1 bar
(correlates with company size; high incentive to bypass; the logic would run
locally/server-side with no structural dependency, so a bare runtime check
is trivially patched out):

- **SSO/OIDC identity federation for the admin UI** — the direct analogue of
  knowhow's and kungu's Tier 1 identity-federation case.
- **Cross-org visibility and any RBAC richer than a fixed default** — the
  capability [ADR-0004](0004-multi-org-tenancy.md) already names as "an
  explicit admin grant, not the default," analogous to kungu's multi-team
  governance Tier 1 case.

Both are extracted into a single private plugin, `review-enterprise`,
mirroring `knowhow-enterprise`/`kungu-enterprise`'s in-process-plugin shape
— never a network service. The identity-federation half reuses platform
ADR-0005's canonical reference pattern verbatim (review is Python/FastAPI,
same as kungu, so kungu's exact port shape transfers with no new design).
The cross-org-governance half has no existing pattern to reuse and is left
deliberately underdesigned here, blocked on
[ADR-0004](0004-multi-org-tenancy.md)'s own still-open RBAC design question
— this ADR commits to the *tier*, not the *shape*.

None of this is buildable yet: review has no admin UI (roadmap Phase 5, not
started), no `user` table, no login/session code anywhere in the repo, and
no multi-org model (roadmap Phase 6, not started). This ADR is written now,
ahead of all of that code, for the same reason kungu's own Tier 1 ADRs were
written before kungu had any code — designing the extension-port boundary
while a module is still greenfield is materially cheaper than knowhow's
retrofit.

## Context

Two review capabilities were evaluated against platform ADR 0003's Tier 1
criteria:

- **SSO/OIDC (+ optionally SCIM) for the admin UI.** review's admin UI
  (roadmap Phase 5, committed to the MVP line) will be the first surface in
  this repo that needs any human login at all — today, every authenticated
  path is machine-to-machine (webhook HMAC verification, GitHub App RS256
  JWT → installation token exchange; see `adapter/github/auth.py`). A large
  customer's IT policy requiring SSO before any tool touches source code is
  the same hard bypass incentive knowhow's and kungu's Tier 1 evaluations
  already established; the validation logic (SAML/OIDC assertion checking)
  is local with no server-side dependency, so a bare runtime check is not a
  real barrier here either.
- **Cross-org visibility / configurable RBAC beyond a fixed default.**
  [ADR-0004](0004-multi-org-tenancy.md) already decided that org joins
  repo/team as a scoping key modeling "one customer's internal structure,"
  that default visibility stays within an org, and that cross-org
  visibility is "an explicit admin grant, not the default" — but left exact
  RBAC design as an open question, both in that ADR's own Consequences and
  in [`c4-architecture.md`](../reference/c4-architecture.md)'s "Open
  questions." The capability itself meets the same Tier 1 bar as SSO: a
  solo dev or small team runs a single GitHub org, so gating doesn't block
  real free use, and — per ADR-0004's own framing of org count as
  proportional to "one customer's internal structure" — a larger customer
  runs more internal orgs, giving the incentive to bypass this the same
  company-size correlation SSO has.

Both were weighed against leaving cross-org RBAC ungated or Tier-2-only,
which is rejected below for the same reason platform ADR 0003 itself moved
SSO out of an original all-Tier-2 draft: where the underlying logic is
local/server-side with no structural dependency, a runtime check alone is a
weak deterrent, and the extraction cost is cheap to pay now, before either
capability has any code to retrofit.

Unlike knowhow (retrofitting Tier 1 out of code already tangled through
`packages/accounts`/`packages/admin`) and more like kungu (greenfield for
both its Tier 1 capabilities), review is greenfield for both of these too —
more so than kungu, in fact: kungu at least had a written PRD and its own
accounts/team concepts sketched before its Tier 1 ADRs were written; review
has neither an admin UI nor a multi-org model designed at all yet. The one
genuine difference from kungu: kungu's second Tier 1 capability (team/project
governance) had no unresolved upstream design question blocking it. review's
does — ADR-0004's RBAC design is explicitly still open. This ADR commits to
gating cross-org RBAC as Tier 1; it does not, and cannot yet, design the
governance port's shape.

review's admin-stats surface (LLM token spend, queue/index health,
verification/merge funnel counts, findings-by-severity) was evaluated
separately in prior discussion and does **not** meet this bar — it matches
platform ADR 0003's own Tier 2 example ("audit logging and usage/cost
governance dashboards") and knowhow's actual shipped precedent
(`knowhow/packages/llm-usage`, `packages/admin/screens/llm-usage`, both
open). This ADR doesn't re-litigate that; it's stated here only to draw the
line clearly against the two capabilities that *do* meet the Tier 1 bar.

## Options considered

| Option | Bypass incentive vs. company size | Structural barrier needed? | Blocks real free-tier use? | Verdict |
| --- | --- | --- | --- | --- |
| Gate neither (both stay open, Tier 2 legal-terms-only) | High for both — SSO satisfies a hard IT policy; unlicensed cross-org visibility scales for free with customer size | Yes for both — local/server-side logic, trivially patched if left as a bare runtime `if` | No | Reject — leaves the two capabilities most correlated with "this customer should pay" behind the weakest deterrent |
| SSO Tier 1, cross-org RBAC Tier 2 | Cross-org RBAC's incentive judged materially weaker than SSO | Only for SSO | No | Reject — ADR-0004 frames org count as proportional to customer size; a bigger customer's incentive to bypass free cross-org visibility isn't meaningfully weaker than its incentive to bypass SSO |
| Both Tier 1, one `review-enterprise` plugin (chosen) | High for both | Yes for both | No — solo dev/small team runs one org, no external IdP, one fixed role, zero license required | **Accept** |
| Both Tier 2 only, no plugin, legal terms as sole deterrent | Weak deterrent for logic with no structural dependency | No | No | Reject — same reasoning platform ADR 0003 itself used to move its own original all-Tier-2 SSO draft into Tier 1 |

## Decision

1. **Two Tier 1 capabilities, both extracted into a private plugin, never
   shipped in the open review repo:**
   - SSO/OIDC identity federation for the admin UI. SCIM provisioning is
     explicitly **not** committed here — see Open questions.
   - Cross-org visibility grants and any RBAC richer than the free tier's
     fixed default, per [ADR-0004](0004-multi-org-tenancy.md)'s "explicit
     admin grant, not the default" framing.
2. **The free tier ships a basic, fixed role split inside the admin UI once
   it's built** — not gated, not part of the plugin — mirroring knowhow's
   and kungu's own open binary role precedent, so a single org can separate
   "can approve Memory entries / change routing" from "can only view" with
   zero license required. Exact shape is undesigned until Phase 5 actually
   scopes the admin UI; this ADR commits only to "a basic split ships free."
2a. **Local authentication is core product, never gated, never routed
   through the identity-federation port or plugin — full stop.** This
   doesn't exist yet in any form (no `user` table, no login/session code
   anywhere in this repo today). When Phase 5 builds the admin UI, its own
   local login mechanism must work completely standalone: zero license,
   zero `review-enterprise` plugin installed, zero `identity-broker`
   deployed (`platform/docs/adr/0005-cross-product-identity-broker.md`).
   Identity federation is an optional, additional way to authenticate,
   layered on top of local login, never a replacement for it — the same
   guarantee kungu's `docs/adr/0002-open-core-licensing.md` item 2a and
   platform ADR-0005 item 3 both require of every product. Until Phase 5
   ships this, this ADR's Tier 1 gate has nothing to gate.
3. **Both capabilities ship from one private repo, `review-enterprise`**,
   loaded by review's open core in-process (never a network service, per
   platform ADR 0003 item 3's amendment) — two narrow, separately-versioned
   extension ports, one for identity federation, one for cross-org
   governance, never a single combined interface.
4. **The identity-federation port reuses platform ADR-0005's normative
   reference pattern verbatim — no new design.** A factory returning a
   list of adapters: `build_identity_federation_adapters(config) ->
   list[IdentityFederationAdapter]`, returning zero, one, or two entries
   (customer's own external IdP, `identity-broker`, both) depending on
   config; an empty list is a normal, valid, unlicensed/unconfigured state.
   Since review is Python/FastAPI
   ([ADR-0002](0002-implement-in-python.md)), this is the exact shape
   `kungu/docs/adr/0003-kungu-enterprise-plugin-mechanism.md` already
   worked out — platform ADR-0005 explicitly names that ADR as the
   canonical source "not to be re-derived per product," and this decision
   follows that instruction rather than inventing a second shape.
5. **The cross-org-governance port is new and review-specific — its shape
   is deliberately not designed by this ADR.** Unlike identity federation,
   there is no existing pattern to reuse: kungu's analogous
   `TeamGovernanceAdapter` is modeled around kungu's own team/project
   entities, not review's org/repo/team scoping model. Designing this port
   is blocked on [ADR-0004](0004-multi-org-tenancy.md)'s own open RBAC
   question resolving first — this ADR fixes only that the eventual
   capability is Tier 1, gated the same way as identity federation.
6. **License validation follows platform ADR 0003 item 2 exactly.** review
   embeds its own local JWT validator (same shape as knowhow's
   `packages/licensing` — offline Ed25519 signature verification only, no
   private signing key present), checking for `"review"` in the license's
   `products` claim. No separate signing infrastructure, no review-specific
   issuer. This does not exist in review today — confirmed no licensing
   code anywhere in this repo — and is new work this ADR schedules, not a
   retrofit.
7. **Default behavior with no license present leaves the full MVP core
   product usable** — every capability through roadmap Phases 0–5 (review,
   findings, static analysis, verification, Memory), single-org scope, a
   basic fixed role, no SSO — never the "disable everything" failure
   platform ADR 0003 flagged as knowhow's original blocking bug. review
   must not inherit that default; it must be built correctly from the first
   version of the gate.
8. **Sequencing — this ADR fixes the tiering decision, not a schedule.**
   None of items 2–7 are buildable before roadmap Phase 5 (admin UI) exists
   at all; item 5's org-governance port additionally can't be designed
   before Phase 6 (multi-org tenancy) lands and ADR-0004's RBAC question
   resolves. `docs/roadmap.md` and
   [`c4-architecture.md`](../reference/c4-architecture.md)'s own "Open
   questions" continue to own *when* SSO/OIDC ships relative to Phase 5;
   this ADR only fixes *that* it's Tier 1 once it does.

## Consequences

**Easier / positive:**
* The extension-port design cost is paid once, now, while both the admin UI
  and the multi-org model are still fully greenfield — the same advantage
  kungu's own Tier 1 ADRs banked, arguably cleaner here since review has no
  legacy accounts/admin code to untangle at all (knowhow's actual position).
* The identity-federation port costs zero new design — a direct, sanctioned
  reuse of platform ADR-0005's canonical pattern.
* Confirms and formalizes review's admin-stats surface (LLM spend,
  queue/index health, verification funnel, findings-by-severity) as Tier 2 —
  ships in the open admin UI, matching knowhow's actual `packages/llm-usage`
  precedent, with no further decision needed there.
* Leaves review positioned to join platform ADR-0005's cross-product
  identity broker cleanly once the admin UI and this port both exist,
  closing the loop that ADR's 2026-09-10 amendment left open.

**Harder / trade-offs:**
* Commits to a Tier boundary well ahead of the admin UI, the license
  validator, and ADR-0004's RBAC design all existing — real risk the
  boundary needs revision once each is actually built, the same humility
  ADR-0004 and `c4-architecture.md` already show by leaving exact RBAC
  design "Open" rather than guessed at.
* The org-governance port has no reusable pattern (unlike identity
  federation) — genuine, undiscounted design work remains, and it's blocked
  on ADR-0004's own unresolved question, not solved here.
* Every part of Decision items 3–7 is unbuilt today: no `review-enterprise`
  skeleton, no license validator, no admin UI, no `user` table. This ADR
  fixes the target, not the implementation — review is further from having
  any of this built than knowhow (working code to retrofit) or kungu (a
  written PRD plus two enterprise ADRs already in place).
* SCIM provisioning is explicitly out of scope here — no user-provisioning-
  at-scale need is evidenced (review doesn't have a single admin user
  today); committing to it now would be scope beyond what
  [`architecture.md`](../rules/architecture.md)'s "no abstraction without a
  second real implementation already in view" rule would justify for a
  whole gated capability, not just a code abstraction.
* Marked PROPOSED, not ACCEPTED — this is the one ADR in this repo that
  commits to a commercial tiering line rather than a technical design
  already checked against working code, and it should be read and
  confirmed before being treated as settled.

## Open questions

* Exact shape of the cross-org-governance extension port (method
  signatures, what "configurable RBAC" concretely means for review) —
  blocked on [ADR-0004](0004-multi-org-tenancy.md)'s own still-open RBAC
  design question, not decided here.
* Whether SCIM provisioning is ever added to the identity-federation Tier 1
  scope — deferred; no evidenced need today.
* Exact free-tier fixed role split (e.g. contributor vs. approver) —
  undesigned until Phase 5 actually scopes the admin UI.
* Whether `review-enterprise` is the right eventual repo name, or should be
  reserved until closer to Phase 5/6 — cosmetic, not blocking this decision.

## Related

* [`platform/docs/adr/0003-open-core-licensing-and-distribution-model.md`](../../../platform/docs/adr/0003-open-core-licensing-and-distribution-model.md)
  — the platform-wide open-core model and Tier 1/Tier 2 criteria this ADR
  applies to review specifically; amended 2026-09-10 to formally cover
  review.
* [`platform/docs/adr/0005-cross-product-identity-broker.md`](../../../platform/docs/adr/0005-cross-product-identity-broker.md)
  — the canonical identity-federation port reference pattern item 4 reuses
  verbatim, and the cross-product SSO broker this ADR positions review to
  join later; amended 2026-09-10 to add review as an in-scope, unimplemented
  product.
* `kungu/docs/adr/0002-open-core-licensing.md` and `kungu/docs/adr/
  0003-kungu-enterprise-plugin-mechanism.md` — the direct template for this
  ADR's structure and, for identity federation specifically, its exact
  Python port shape.
* [`0004-multi-org-tenancy.md`](0004-multi-org-tenancy.md) — defines the
  org-scoping model the cross-org-governance Tier 1 candidate builds on,
  and owns the still-open RBAC design question this ADR depends on but
  doesn't resolve.
* [`0002-implement-in-python.md`](0002-implement-in-python.md) — why this
  ADR's identity-federation port reuses kungu's Python shape rather than
  knowhow's TypeScript one.
* [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md)
  "Open questions" — SSO/OIDC admin UI timing and exact RBAC design, both
  still owned there, not by this ADR.
* [`docs/roadmap.md`](../roadmap.md) — Phase 5 (Admin UI, MVP line) and
  Phase 6 (multi-org tenancy), the two prerequisites item 8 sequences
  against.
