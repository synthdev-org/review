# ADR: Product-knowledge MCP client — official `mcp` SDK in one adapter, operator-provisioned credentials with host binding, a network egress guard, and integer-major contract versioning

## Status

PROPOSED (drafted 2026-10-11 alongside
[`docs/specs/spec-product-knowledge.md`](../specs/spec-product-knowledge.md);
becomes ACCEPTED when the owner signs off on that spec). **Amended
2026-10-11 during Phase 4 implementation** (owner decisions, status
unchanged): the `mcp` v2 SDK's HTTP stack is `httpx2`, not `httpx`, and
Semgrep is moved to an isolated environment — see "Implementation findings"
below.

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

[ADR-0003](0003-knowhow-product-knowledge-augmentation.md) decided *that*
review calls an external, vendor-neutral product-knowledge contract over MCP.
It deliberately left four implementation-shaping questions open or implied
that have real architectural weight, because answering them wrongly is
expensive to reverse once third parties implement the contract:

1. Which MCP client implementation does the adapter use, given the MCP
   specification changed its handshake model on 2026-07-28?
2. Where does the provider *credential* live? ADR-0003 and `c4-architecture.md`
   say the per-org/repo "endpoint, credential, and display label" are day-two
   Postgres config; every other credential in this platform (GitHub App key,
   LLM key, database URL) is bootstrap config supplied as a Kubernetes `Secret`
   and `models.py` states credentials are "never modeled or written to any
   table".
3. What stops a customer-supplied endpoint URL from being an SSRF/credential
   exfiltration primitive?
4. How is the contract versioned and deprecated (ADR-0003's open question)?

**Decisions:** (1) use the official `mcp` Python SDK (v2 line), confined to one
adapter module behind a consumer-defined Protocol, in its default dual-era
negotiation mode; (2) store the endpoint, display name, and a credential
**reference** in Postgres day-two config, and hold the credential itself in
bootstrap config (a Kubernetes `Secret`, `existingSecret`-compatible) with a
mandatory per-credential host binding; (3) apply a connection-time egress guard
(fixed deny-list always, operator allow-list optionally, private ranges allowed
by default because in-cluster providers are legitimately private, redirects
never followed, resolved address pinned); (4) version the contract with
integer majors, additive-only evolution inside a major, support for the
current and previous major, and a 12-month minimum deprecation window.

## Context

### The MCP protocol changed under us

The MCP specification's latest revision, `2026-07-28`, removes the
`initialize` handshake and protocol-level sessions: every request is
self-contained, carries its protocol version and client info in `_meta` (and
`MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers on HTTP), and a
mandatory `server/discover` method replaces version negotiation. Revisions
`2025-11-25` and earlier ("legacy") still use `initialize`, optional
`Mcp-Session-Id`, and a GET SSE stream. A client that wants to talk to both
detects the era by attempting a modern request first and falling back on a
non-modern `400`. knowhow's MCP server today is built on the TypeScript SDK
`^1.25` — a legacy-era server. Any provider a customer already runs will be a
mix of eras for the foreseeable future. A review client that speaks only one
era silently excludes half the ecosystem.

### The official SDK now handles both eras

The official Python SDK `mcp` reached its v2 line on 2026-07-28 (2.3.0 as of
2026-10-02), and its `Client` negotiates across eras by default
(`mode="auto"`: one `server/discover` probe at the newest version, falling back
to the classic `initialize` handshake), accepts a caller-supplied
`httpx2.AsyncClient` (see "Implementation findings": the v2 SDK depends on
`httpx2`, not `httpx`) for headers, auth and timeouts, and returns tool results
with `structured_content` / `is_error` (tool failures are results, not
exceptions; protocol failures raise `MCPError`).

### Credentials: three existing documents disagree

- `c4-architecture.md` ("Configuration & Secrets") and ADR-0003 list the
  provider "endpoint, credential, and display label" as day-two config in
  Postgres via the Admin API.
- `adapter/storage/models.py`'s module docstring, `c4-architecture.md` design
  goal 7 (secret-backend-agnostic, `existingSecret` for every credential), and
  the shipped Helm chart (`charts/review/templates/secret.yaml`) treat
  credentials as bootstrap-only. The LLM *endpoint* is likewise deploy-time and
  deliberately not admin-editable (`spec-admin-gui.md`: "Per-stage LLM
  endpoint/provider override" out of scope).
- A Postgres-stored token would need an at-rest encryption key — a new secret
  to bootstrap, rotate, and back up — and ADR-0004 explicitly provides no
  per-org encryption boundary.

### Why egress policy is not optional

The endpoint URL is typed into an Admin UI by a *platform admin*, who is
not necessarily the cluster operator. Combined with a stored bearer token,
an unconstrained URL field is (a) an SSRF primitive into the cluster network
and cloud metadata service, and (b) a way to send a legitimately provisioned
provider token to a host the operator never intended. Self-hosted
in-cluster providers (knowhow at `http://knowhow.<ns>.svc`) are legitimately
on private addresses, so "public addresses only" — the usual SSRF answer — is
not available.

## Options considered

### MCP client implementation

| Option | Dual-era support | Maintenance cost | Fit with `architecture.md` | Verdict |
| --- | --- | --- | --- | --- |
| **Official `mcp` SDK v2 (`>=2.3,<3`)**, used only inside `adapter/product_knowledge/` | Built in (`mode="auto"`) | Low — SDK owns era detection, header mirroring (`Mcp-Method`, `Mcp-Name`, `x-mcp-header`), SSE response parsing, `UnsupportedProtocolVersionError` retry | Async; takes our `httpx2.AsyncClient`; domain stays infra-free because the Protocol lives in `review/` | **Accept** |
| Hand-rolled `httpx` JSON-RPC client | Must implement both eras and the SSE/JSON response duality ourselves | High, and recurring — the protocol just changed shape and will again; every revision is our bug | Fits (httpx already a dependency) | Reject — re-implements a moving standard to avoid one dependency |
| `mcp` SDK v1.x (`1.30`) | Legacy era only; would need replacement soon | Medium | Fits | Reject — pins us to the era the spec just left |

### Credential storage

| Option | Matches existing credential handling | New secret/key to manage | Admin self-service | Verdict |
| --- | --- | --- | --- | --- |
| Token stored in Postgres (app-level encryption) | No — first credential in any table | Yes (encryption key, rotation, backup) | Full | Reject — contradicts design goal 7 and `models.py`; ADR-0004 gives no per-org key boundary |
| Endpoint **and** token fully bootstrap (LLM precedent) | Yes | No | None — every provider change needs a redeploy | Reject — defeats ADR-0003's "configured per org/repo" and is needless for the non-secret fields |
| **Endpoint/display name/credential *reference* in Postgres; token + host binding in bootstrap `Secret`** | Yes — token handling identical to `llm.apiKey`; day-two config stays day-two | No | Endpoint, label, scope, enable/disable, test-connection | **Accept** |

### Egress policy

| Option | Allows in-cluster providers | Blocks cloud metadata / link-local | Operator control | Verdict |
| --- | --- | --- | --- | --- |
| No guard | Yes | No | None | Reject — SSRF primitive behind an Admin session |
| Public addresses only | No | Yes | None | Reject — rules out knowhow's primary deployment shape |
| **Fixed always-deny set + optional operator host allow-list + private ranges allowed by default (operator can switch off)** | Yes | Yes | Allow-list and private-range switch | **Accept** |

## Decision

1. **Client library.** `adapter/product_knowledge/` (one generic adapter, no
   per-provider class) implements the consumer-defined
   `review.product_knowledge.ProductKnowledgeProvider` Protocol using the
   official `mcp` SDK `Client` in `mode="auto"`, over a caller-supplied
   `httpx2.AsyncClient`. No other module imports `mcp`. SDK-level changes are
   absorbed in that module and detected by adapter integration tests that run a
   real fake MCP server in **both** protocol eras.

2. **Credentials.**
   - Postgres day-two config (a registered `OrgRepoTeamSetting` key,
     `product_knowledge_provider`) holds `enabled`, `display_name`,
     `endpoint_url`, and `credential_ref` (a name, or null for an
     unauthenticated in-network provider). Scope precedence is the existing
     platform < org < repo resolution.
   - The token itself is bootstrap config: a single `SecretStr` JSON map
     `{"<credential_ref>": {"token": "...", "allowed_hosts": ["host", ...]}}`
     read by `pydantic-settings` at the Worker and Admin API composition
     roots and rendered by Helm with the existing `value` / `existingSecret`
     convention. `allowed_hosts` is **mandatory and non-empty**: a token is
     only ever sent to an endpoint whose host is in its own list, so an Admin
     session cannot redirect a provisioned token to a host the operator never
     bound it to.
   - Tokens never appear in Postgres, API responses, audit records, logs,
     spans, or metrics. This amends the credential wording in ADR-0003 and
     `c4-architecture.md`.

3. **Egress guard** (applied in the adapter's `httpx2` transport, on every
   connection, after DNS resolution; the resolved address is the one
   connected to, so DNS rebinding cannot swap it):
   - Always denied: loopback, link-local (including `169.254.169.254` and
     IPv6 equivalents), unspecified, multicast, and IPv4-mapped forms of
     these. **Amended 2026-10-11 (a tightening — "private
     ranges allowed" does not require allowing metadata endpoints):** also
     denied are the cloud metadata addresses that sit inside the allowed
     private/special ranges — AWS IMDS IPv6 `fd00:ec2::254`, Alibaba
     `100.100.100.200`, Azure wireserver `168.63.129.16`, OCI
     `192.0.0.192` — plus NAT64 `64:ff9b::/96`, 6to4 and IPv4-compatible
     IPv6 forms; non-canonical literals (`2130706433`, `0x7f.1`, IPv6 zone
     ids) are normalized through the stdlib `ipaddress` module after DNS
     resolution, never matched as URL strings. Schemes other than
     `http`/`https` are rejected. Redirects are never followed.
   - The HTTP client is built with `trust_env=False`: honoring
     `HTTP(S)_PROXY`/`ALL_PROXY` would let a proxy resolve and connect,
     silently bypassing resolve-and-pin and host binding. The bearer token is
     attached per request in the transport only after the host check, never
     as a client default header.
   - Hosts are canonicalised (lowercase, IDNA/punycode, trailing dot
     stripped, explicit port handling, IP-literal handling) by **one**
     function shared by the write-time binding check, the job-time check and
     the transport; `*.suffix` allow-list entries never match the bare
     suffix or a lookalike (`evilexample.com`).
   - Because the SDK follows same-origin redirects itself (it does not
     consult the client's `follow_redirects`), the guard transport refuses
     every `3xx` itself; it also forces `Accept-Encoding: identity` and
     refuses any `Content-Encoding`, since the 256 KiB response cap counts
     wire bytes and a compressed body would slip past it.
   - Cross-job fan-out is bounded: a process-level per-endpoint concurrency
     limit applies, and a `rate_limited` provider response triggers a short
     in-process cool-down for that endpoint.
   - Private ranges (RFC 1918, ULA, CGNAT) are allowed by default
     (`product_knowledge_allow_private_networks=true`) — in-cluster providers
     are legitimately private. An operator may set it `false`, and/or set a
     non-empty `product_knowledge_endpoint_allowlist` (exact hosts or
     `*.suffix` patterns), outside which every endpoint is blocked.
   - A bearer token is never sent over plaintext `http` to a non-private
     address.
   - Documented as defense in depth *under* an operator-applied Kubernetes
     `NetworkPolicy`, not a replacement for one.

4. **Contract versioning and deprecation** (closes ADR-0003's open
   question; normative detail in the spec):
   - A contract version is an integer major written as a string (`"1"`).
     Within a major, evolution is additive only: new optional response
     fields, new optional tools, new open-string values. Consumers ignore
     unknown fields; review only sends request arguments a provider's own
     `tools/list` `inputSchema` declares. Anything else is a new major.
   - A provider declares the majors it implements through a mandatory
     zero-argument tool, `product_knowledge_contract`, returning
     `{"supported_versions": ["1"]}`. A dedicated tool is used rather than
     server-info or capabilities because it is era-independent and
     implementable with any MCP server SDK; MCP itself says `serverInfo` must
     not drive client behavior.
   - Review supports the current and the previous major (N, N-1). It selects
     the highest major both sides support. No overlap means "no provider
     configured" — never a failed review.
   - A major is marked deprecated when its successor ships, and is not removed
     for at least 12 months after that release (the floor MCP's own feature
     lifecycle policy uses), announced in release notes, with a warning in the
     Admin UI test-connection result and a `deprecated_contract_version` log
     field/metric label in the meantime.

## Consequences

**Easier / positive:**

- Both MCP eras work with no per-era code of ours; a customer's existing
  legacy-era MCP server and a new modern-era one are configured identically.
- Credential handling is identical to every other credential the platform
  holds: `existingSecret`, any secret backend, no new at-rest key, nothing
  secret in the database dump or the Admin API.
- An Admin session cannot turn the integration into a token-exfiltration or
  cloud-metadata SSRF primitive, and in-cluster providers still work with zero
  extra config.
- Third-party provider implementers get a small, stable, explicitly
  versioned target and a runnable conformance suite.

**Harder / trade-offs:**

- A new third-party runtime dependency (`mcp` and its transitive set, which
  includes Starlette-family packages already present via FastAPI). Mitigated
  by confinement to one adapter and the Protocol boundary; the SDK's v2 line
  is two months old at the time of writing, so version pinning
  (`>=2.3,<3`) and the two-era integration tests are load-bearing.
- Adding a *new credential* (a new token or a new provider host) requires an
  operator action (update the Secret, restart/roll Worker and Admin API) —
  an Admin cannot self-serve a new secret. Switching an existing credential's
  endpoint within its bound hosts, enabling/disabling, and relabeling stay
  fully self-service.
- The Admin API and the Worker both need the credential map (the Admin API
  runs the test-connection probe), so the same `Secret` key is mounted in
  both Deployments.
- OAuth 2.1 interactive flows (the MCP authorization spec's default) are not
  supported: review is a headless client acting on its own behalf and accepts
  a pre-provisioned bearer token only. A client-credentials flow is deferred
  until a provider requires it.
- Integer-major versioning plus an always-additive rule means a provider can
  never rely on a *new* field being sent to it by an old review; contract
  authors carry that discipline.

## Implementation findings (Phase 4, 2026-10-11)

Two facts surfaced when the decision was first built; neither reverses it.

1. **`mcp` 2.x uses `httpx2`, not `httpx`.** `mcp>=2.3` depends on `httpx2`
   (a fork with its own `httpx2.AsyncClient` / `httpx2.AsyncBaseTransport`).
   The guard transport therefore subclasses `httpx2.AsyncBaseTransport`, and
   the client handed to the SDK is an `httpx2.AsyncClient`. The project's
   own `httpx` dependency (GitHub adapter, LiteLLM) is unaffected; the two
   libraries coexist, and `httpx2` stays confined to
   `adapter/product_knowledge/` (plus the test-only fake provider). One
   knock-on: Starlette's `TestClient` prefers `httpx2` whenever it is
   installed, so existing `TestClient`-based tests now receive `httpx2`
   responses (annotation-only change in one test module).
2. **Semgrep is isolated from the main dependency group.** Semgrep hard-pins
   `mcp` 1.x and imports `mcp.server.fastmcp`, so it cannot share a
   resolution with `mcp>=2.3,<3` (the rejected "SDK v1.x" row above would
   have been the only way to co-install). Semgrep is only ever executed as a
   `semgrep` subprocess (`adapter/analyzers/semgrep.py`), so it now lives in
   its own locked environment, `tools/semgrep/` (own `pyproject.toml` +
   `uv.lock`), installed to `/opt/semgrep` in the Docker `semgrep-tool`
   stage and symlinked onto `PATH` in the `dev` and `runtime` stages; CI
   installs it with `uv sync --project tools/semgrep --locked` and puts the
   `semgrep` entry point on `PATH`. The adapter's invocation (`semgrep` on
   `PATH`) and its pinned ruleset are unchanged.

## Security review hardening (2026-10-12)

Refinements from the security review; none reverses the decision.

1. **Binding may carry a port.** `allowed_hosts` and allow-list entries accept
   an optional `:port`, matched exactly against the endpoint's port (scheme
   default when omitted); entries without a port keep host-only matching
   (documented as weaker, `host:port` recommended).
2. **Unauthenticated providers are not exempt.** With no `credential_ref` the
   endpoint must match the operator allow-list or some credential's
   `allowed_hosts` at write time, test-connection, job time and in the
   transport (same rule everywhere).
3. **Gate keys.** The per-endpoint concurrency bound is keyed by origin
   (`scheme://host:port`, no path); the `rate_limited` cool-down by
   `(origin, credential_ref)`, so one tenant's credential being throttled does
   not starve another's. The negotiation cache stays keyed by URL + ref.
4. **Analyzer subprocesses** do not inherit the credential-map environment
   variable.

## Open questions

None that cannot be resolved by the spec. (Whether to add an OAuth
client-credentials token exchange is a deferral with a named trigger, not an
open question — see the spec's Out of scope.)

## Related

- [`docs/specs/spec-product-knowledge.md`](../specs/spec-product-knowledge.md)
  — the PRD this ADR supports; carries the wire contract and policy detail.
- [ADR-0003](0003-knowhow-product-knowledge-augmentation.md) — the governing
  decision; this ADR closes its versioning and credential-placement
  questions.
- [ADR-0004](0004-multi-org-tenancy.md) — org is a scoping key, not a
  security boundary, which is why per-org encryption was not introduced.
- [ADR-0011](0011-otel-api-imported-directly-in-domain-packages.md) and
  [ADR-0012](0012-telemetry-recording-failures-log-and-continue.md) — how
  provider-call telemetry is recorded.
- [`docs/rules/architecture.md`](../rules/architecture.md) — Protocol at
  consumer, adapters import infra, composition-root wiring.
- MCP specification `2026-07-28` (versioning, discovery, Streamable HTTP,
  authorization) and its feature-lifecycle policy; official Python SDK v2 —
  URLs in the spec's "Research basis".
