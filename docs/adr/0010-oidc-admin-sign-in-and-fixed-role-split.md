# ADR: Admin sign-in — local login plus OIDC behind a swappable provider Protocol (Authentik first), and a fixed Admin/Viewer role split

## Status

ACCEPTED 2026-11-25. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (tag `WKPYNQ`) and
[`docs/rules/testing.md`](../rules/testing.md) (tag `RQPTVE`).

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

The Admin UI needs human sign-in. Every other authenticated path in this
repo is machine-to-machine (webhook HMAC verification, GitHub App RS256 JWT
to installation-token exchange; see `adapter/github/auth.py`). This ADR
fixes three things:

- **Local username/password login** is the baseline. It always works, with
  no external service, no license file and no identity provider.
- **OIDC single sign-on** is an additional way to sign in. It sits behind
  a consumer-defined `typing.Protocol` (`iam.oidc.OIDCProvider`) with two
  implementations in this repo: `GenericOIDCProvider` (any standards-
  compliant issuer, via OpenID Connect discovery) and `AuthentikProvider`
  (derives Authentik's issuer URL from a base URL and application slug).
  OIDC is off by default.
- **A fixed Admin/Viewer role split**, enforced server-side. There is no
  per-org or cross-org role customisation.

Everything here ships in this repository. There is no plugin, no separate
distribution and no license gate on any of it.

## Context

A large customer's IT policy often requires single sign-on before any tool
touches source code, so local passwords alone are not enough. Authentik is
the first identity provider the project owner runs, but customers will bring
others (Keycloak, Entra ID, Okta, Google). OIDC is the common protocol, and
the differences between providers are small (issuer URL shape, default
claims), so the right seam is a narrow Protocol rather than a hard-coded
provider.

[`architecture.md`](../rules/architecture.md) requires an interface to have
a second real implementation in view before it exists. `GenericOIDCProvider`
and `AuthentikProvider` meet that on day one, and further providers are one
new class plus one settings value.

## Options considered

| Option | Verdict |
| --- | --- |
| Local login only | Reject — fails the common "SSO before any tool touches source" policy |
| OIDC hard-coded to Authentik | Reject — the second provider would force a route rewrite |
| OIDC via a third-party library (Authlib) | Reject — a full client framework for one flow; `pyjwt[crypto]` and `httpx` are already dependencies |
| **Protocol + generic OIDC adapter + thin Authentik subclass (chosen)** | **Accept** |
| SAML | Out of scope; no evidenced need |

## Decision

1. **Local authentication is core and never depends on anything else.** It
   works with OIDC disabled, with no license file and with no identity
   provider deployed. OIDC is layered on top, never a replacement.
2. **The extension seam is `iam.oidc.OIDCProvider`**, a two-method Protocol
   defined at the consumer (`apps/admin_api/oidc_routes.py`):
   `authorization_url(login, redirect_uri)` and
   `complete_login(code, login, redirect_uri)`, which returns an
   `OIDCIdentity` (subject, username) or raises `OIDCError`. Its
   implementations live in `adapter/auth/oidc.py` and are wired at the
   composition root (`apps/admin_api/main.py`) from settings. The routes
   never name a vendor.
3. **Users are pre-provisioned.** A successful OIDC login signs in the
   existing, active `admin_user` whose `username` equals the configured ID
   token claim (default `preferred_username`). An unknown or inactive
   identity is rejected; no user is created and no role is inferred from IdP
   claims. Anyone who can set that claim at the IdP to a local username can
   sign in as that user, so operators must treat the IdP as trusted for
   username assignment. Binding to the immutable `sub` claim is the
   stronger design; it needs a schema change and is a follow-up.
4. **Protocol hygiene.** Authorization code flow with PKCE (S256), `state`
   and `nonce` checks, ID token signature validated against the issuer's
   JWKS with RS256 or ES256 only (never `none`, never a symmetric
   algorithm), and `iss`, `aud` and `exp` required and checked. The
   discovery document's `issuer` must equal the configured issuer. Every
   HTTP call has a timeout. Every failure sends the browser to
   `/login?error=sso_failed` with one generic message; the real reason goes
   to logs and the audit trail only.
5. **Sessions are the same sessions.** An OIDC login issues the same opaque,
   hash-looked-up session and CSRF cookies as a local login, through one
   shared function, so idle expiry, revocation and CSRF behave identically.
6. **Fixed role split.** Two roles, `admin` and `viewer`: viewers have
   read-only parity on every GET route; every mutating route requires
   `admin`, enforced server-side. Role configuration beyond this, and
   cross-org visibility, are not designed here; see
   [ADR-0004](0004-multi-org-tenancy.md)'s open RBAC question.
7. **SCIM and SAML are out of scope.** No user-provisioning-at-scale need is
   evidenced.

## Consequences

**Easier / positive:**
- A new provider is one adapter class and one settings value, with no route
  or frontend change.
- No new dependency: `pyjwt[crypto]` and `httpx` are already in the tree.
- Local login keeps working during an IdP outage, so operators cannot lock
  themselves out.

**Harder / trade-offs:**
- Username matching (decision 3) is weaker than `sub` binding and depends on
  the IdP's username hygiene.
- Admins must create a local user before that person can sign in through
  OIDC; there is no just-in-time provisioning.
- The provider's discovery document and JWKS are cached per process; a key
  rotation is picked up on the next unknown `kid`, not on a timer.
- A successful OIDC login does not revoke that user's other sessions, and
  deactivating a user does not log them out of the IdP.

## Open questions

- Whether to bind users to the OIDC `sub` claim (needs a migration and an
  admin flow to link existing users).
- Whether to map an IdP group claim to the Admin/Viewer role.
- The shape of any RBAC richer than the fixed split, blocked on
  [ADR-0004](0004-multi-org-tenancy.md).

## Related

- [`0002-implement-in-python.md`](0002-implement-in-python.md) — why the
  seam is a `typing.Protocol`.
- [`0004-multi-org-tenancy.md`](0004-multi-org-tenancy.md) — the org-scoping
  model and the open RBAC question.
- [`docs/specs/spec-admin-gui.md`](../specs/spec-admin-gui.md) — US-A1
  (local login), US-A5 (role enforcement) and US-A24 (OIDC sign-in).
