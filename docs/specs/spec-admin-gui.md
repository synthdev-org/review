# Phase 5 — Admin UI PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-20

## Problem & Goal

Today, every capability the platform delivers through Phase 4 (review,
static analysis, verification, Memory) is invisible and unconfigurable to
the person operating it. A customer's admin cannot see what the platform is
doing (queue health, LLM spend, how many findings it's producing), cannot
curate what it's learning (Memory entries sit behind a shared static Bearer
token with no per-person identity — `docs/specs/spec-memory.md`'s own
stated stopgap, "targeted at Phase 5's admin UI" to fix), cannot configure
which repos/teams it watches or how, and cannot control which model serves
which pipeline stage without editing environment variables and restarting
a container. `docs/reference/c4-architecture.md`'s roadmap line is explicit
that Phase 5 is "confirmed in scope for the MVP release, not a fast-follow"
for exactly this reason: a working review loop nobody can see, curate, or
configure is not yet a product an enterprise admin can operate with
confidence.

The end user of this PRD is the **platform admin** — the person at a
self-hosted customer responsible for running review day to day (not the
developers whose PRs get reviewed, who never touch this surface). What they
cannot do today that this PRD fixes:

- **See** whether the platform is healthy and what it's costing them (queue
  depth, LLM token spend, findings volume) without reading container logs.
- **Curate** what the platform has learned (approve/reject/delete Memory
  entries) as themselves, not through a shared secret every admin on the
  team uses interchangeably with no audit trail of who did what.
- **Configure** org/repo/team-level settings and per-stage model routing
  without a deploy.

**Goal:** ship a real, standalone-deployable admin interface — with its own
local login, so no admin needs a license or a second product installed to
use it — that makes the platform's own Phase 0–4 capability visible,
curatable, and configurable. Success is measured by roadmap Phase 5's own
exit bar: a pilot customer's admin can configure a repo, review and approve
a pending Memory entry, and change which model serves review generation —
all without touching an environment variable or a `kubectl` command.

## Scope

### In scope

- Local username/password login — real per-user identity, replacing the
  Memory admin API's shared static Bearer token entirely.
- A basic, free, fixed two-role split (**Admin** / **Viewer**) — no
  per-org customization, no cross-org RBAC — per
  [ADR-0010](../adr/0010-oidc-admin-sign-in-and-fixed-role-split.md)
  decision 6.
- OIDC single sign-on (Authentik first, any OIDC provider via the
  `OIDCProvider` Protocol), US-A24.
- Org/repo/team configuration screens, reading and writing the
  already-scaffolded `OrgRepoTeamSetting` table (`adapter/storage/models.py`
  — "a later roadmap phase adds the admin API... that consult this table").
- The Memory approve/reject/delete/list screen, migrated onto real
  per-user auth and role enforcement, plus bulk approve/reject on that
  same queue (US-A23, Could-priority — see Scope > Priority).
- Model routing configuration, reading and writing the already-scaffolded
  `ModelRoutingConfig` table, **plus** the Worker's pipeline-step read-path
  that actually consults it (today every LLM-calling stage uses one flat
  `settings.llm_model` value — this PRD is what makes per-stage routing
  real, not just a UI wrapper around a table nothing reads).
- An admin-stats dashboard: LLM token spend, queue/index health,
  verification/merge funnel counts, findings-by-severity — open and ungated.
- **Deployment as a genuinely standalone, separately deployable service** —
  its own container image, Kubernetes `Deployment`/`Service`, and Helm
  values — not folded into the Webhook API's existing process. See
  "Architecture deviation," below.
- A Helm `Ingress` template (the chart has none today), so there is one
  coherent, chart-managed way to reach both the Admin UI and the existing
  webhook receiver in a production Kubernetes deployment.
- A `docker-compose.yml` service for local dev, using the same shared
  application image the other two entrypoints already build from
  (`Dockerfile`'s `runtime` stage, entrypoint selected via `command:`).

### Out of scope (deferred to a later spec, ADR, or roadmap phase)

- **Cross-org RBAC.** Blocked on ADR-0004's open RBAC design question.
  OIDC single sign-on is in scope (US-A24, ADR-0010) as an additional
  login method; local login is never replaced.
- **Per-org RBAC / cross-org visibility restriction.** Roadmap Phase 6
  (multi-org tenancy) hasn't landed; this MVP's Admin/Viewer roles are
  platform-wide, not org-scoped — every Admin/Viewer sees every connected
  org's data. [ADR-0004](../adr/0004-multi-org-tenancy.md)'s "platform
  admin sees all orgs" default is effectively this MVP's only mode.
- **SCIM / bulk user provisioning.** No evidenced need (ADR-0010).
- **Per-stage LLM endpoint/provider override.** `ModelRoutingConfig` only
  stores `model_name` per stage/org; the LLM Gateway endpoint stays a
  single, deployment-wide, deploy-time value, matching the schema already
  scaffolded for this feature — not a gap this PRD introduces.
- **Password-reset via email.** No SMTP dependency exists or is introduced
  by this PRD (per `architecture.md`'s "no abstraction without a second
  real implementation already in view"); an Admin can reset another local
  user's password directly, which is sufficient for a single-org admin
  team and avoids a new external dependency.
- **A design system.** This is the first frontend surface in this repo;
  no component library exists to reuse or extend. This PRD sets baseline
  UX expectations (below) but does not choose a frontend framework —
  that's an implementation decision for the delivery plan, not a product
  requirement.

### Priority

**Must** (roadmap Phase 5's own stated bar plus the explicit product call to
ship this release complete — a pilot customer needs all of these to operate
the platform at all):
- Local login, bootstrap admin, Admin/Viewer roles (US-A1–A7)
- Memory approve/reject/delete/list, migrated to real auth (US-A8–A11)
- Org/repo/team configuration (US-A12–A13)
- Model routing configuration, including the Worker read-path (US-A14–A16)
- Admin-stats dashboard: LLM spend, queue/index health, funnel counts,
  findings-by-severity (US-A17) — no longer treated as cuttable; ships in
  this same release.
- Standalone deployability: Helm `Deployment`/`Service`/`Ingress`,
  docker-compose service (US-A20–A22)

**Could** (nice-to-have, no roadmap or ADR commitment forces it):
- Bulk approve/reject on the Memory queue (US-A23).

## Requirements

Stated as user stories per `testing.md`'s mandatory format.

### Auth & user management

**US-A1 — Local login works standalone, with zero license or plugin**
As a platform admin, I want to log in with a username and password, so
that I can access the Admin UI without installing any additional product
or license — even before considering SSO.
- Given a provisioned local admin account, When the admin submits correct
  credentials to the login form, Then the platform issues a session
  credential and grants access to the Admin UI, with no dependency on a
  license file or an OIDC provider
  ([ADR-0010](../adr/0010-oidc-admin-sign-in-and-fixed-role-split.md) decision 1).
- Given incorrect credentials, When submitted, Then the login is rejected
  with no session issued, and the failure reveals nothing about whether
  the username exists.

**US-A2 — Sessions expire and can be revoked**
As a platform admin, I want my session to expire after inactivity and to
be able to log out explicitly, so that a forgotten, unattended browser tab
doesn't leave the Admin UI open indefinitely.
- Given an active session, When its configured lifetime elapses with no
  activity, Then the next request is rejected and the admin must log in
  again.
- Given an active session, When the admin clicks "log out," Then that
  session is revoked server-side immediately (not merely cleared
  client-side) and cannot be reused even if the token is replayed.

**US-A3 — A fresh deployment bootstraps its first admin, never with a
default credential**
As an operator standing up a new deployment, I want an initial admin
account provisioned from credentials I supply, so that I can log in for
the first time without the platform shipping a default username/password
any attacker could guess.
- Given a fresh deployment with no admin users, When it starts with an
  operator-supplied bootstrap username/password (Helm value or environment
  variable, backed by a Kubernetes `Secret`), Then exactly one Admin-role
  account is created on first startup.
- Given a deployment that already has at least one admin user, When it
  restarts, Then the bootstrap step is a no-op — it never re-creates or
  resets an existing account.

**US-A4 — An admin manages other local admin users**
As a platform admin, I want to create and deactivate other local admin
accounts and assign each a role, so that my team can share access without
everyone sharing one login.
- Given an authenticated Admin, When they create a new local user with a
  username, initial password, and a role (Admin or Viewer), Then that user
  can log in and is scoped to that role's permissions.
- Given an authenticated Admin, When they deactivate another user, Then
  that user's existing sessions are revoked and they can no longer log in,
  though their historical actions (e.g. past Memory approvals) remain
  attributed to them for audit purposes.

**US-A5 — Viewer role is enforced server-side, not just hidden in the UI**
As a platform admin who assigns a colleague the Viewer role, I want them
genuinely unable to change anything, so that "view-only" is a real
guarantee, not a UI convention a direct API call bypasses.
- Given a Viewer-role session, When it calls any mutating endpoint
  (approve/reject/delete a Memory entry, change org/repo/team config,
  change model routing, manage users), Then the request is rejected (403)
  regardless of what the UI does or doesn't render.
- Given a Viewer-role session, When it calls any read endpoint this PRD
  adds, Then it succeeds identically to an Admin-role session — Viewer is
  read-only, not reduced-visibility.

**US-A6 — Failed logins are rate-limited**
As a platform admin, I want repeated failed login attempts throttled, so
that the login form isn't a practical brute-force target.
- Given repeated failed login attempts for the same username within a
  short window, When the configured attempt ceiling is exceeded, Then
  further attempts for that username are rejected for a cooldown period,
  independent of whether the credentials would otherwise have been
  correct.

**US-A24 — An admin signs in through an OIDC identity provider**
As a platform admin whose organisation runs Authentik (or another OIDC
provider), I want to sign in through it, so that I use my company
identity instead of a second password. Local login keeps working
([ADR-0010](../adr/0010-oidc-admin-sign-in-and-fixed-role-split.md)).
- Given OIDC is configured and an active local user whose username equals
  the ID token's username claim, When the user completes the provider's
  login and returns with a valid code, Then a normal session is issued and
  the audit log records `login_succeeded` with method `oidc`.
- Given a valid provider login for an identity with no local user, or a
  deactivated user, When the callback is processed, Then no session is
  issued, no user is created, and the browser lands on `/login` with a
  generic error.
- Given a callback with a wrong or missing `state`, or an ID token with a
  bad signature, wrong issuer, wrong audience, expired, or wrong `nonce`,
  When the callback is processed, Then it is rejected with no session.
- Given OIDC is not configured, When the login screen loads, Then no SSO
  button is shown and `/api/v1/auth/oidc/login` returns 404.

**US-A7 — The Memory admin API moves off the shared static token**
As a platform admin, I want every Memory approve/reject/delete action
attributed to the person who did it, so that curation has a real audit
trail instead of "someone with the shared key did this."
- Given the Admin UI now issues real per-user sessions, When any Memory
  admin endpoint (`GET /admin/memory-entries`, `POST .../approve`,
  `POST .../reject`, `DELETE .../{id}`) is called, Then it authenticates
  via the caller's session, not the `admin_api_key` static Bearer token
  `docs/specs/spec-memory.md` shipped as an explicit stopgap — and the
  action is attributed to that user in the entry's audit trail.
- Given a request carrying the old static Bearer token and no valid
  session, When it reaches any Memory admin endpoint, Then it is rejected
  — the static-token path is fully retired, not left as a silent
  alternate credential.

### Memory review

**US-A8 — List Memory entries**
As a platform admin or viewer, I want to see pending, approved, and
rejected Memory entries filtered by status/org/repo/team, so that I can
find what needs review.
- Given entries exist across multiple statuses, When I request the list
  filtered by `status=pending`, Then only pending entries are returned,
  paginated (existing `limit`/`offset` contract:
  `docs/specs/spec-memory.md`'s Admin API section), with the total count.

**US-A9 — Approve a Memory entry**
As a platform admin, I want to approve a pending entry with an explicit
classification, so that it starts influencing future reviews as
`docs/specs/spec-memory.md`'s US-M4 already defines.
- Given a pending entry, When an Admin approves it with an explicit
  `entry_type`, Then the existing approve behavior (US-M4) executes
  exactly as spec'd, now attributed to that Admin's identity.

**US-A10 — Reject a Memory entry**
As a platform admin, I want to reject an entry I disagree with, so that
it's excluded from future learning.
- Given a pending or approved entry, When an Admin rejects it, Then the
  existing reject behavior (US-M5) executes exactly as spec'd, attributed
  to that Admin.

**US-A11 — Delete a Memory entry**
As a platform admin, I want to permanently remove an entry, with a
confirmation step, so that curation mistakes are correctable but not
accidental.
- Given an entry, When an Admin deletes it with explicit confirmation,
  Then the existing delete behavior (US-M5) executes exactly as spec'd,
  attributed to that Admin.

**US-A23 — Bulk approve/reject on the Memory queue (Could)**
As a platform admin triaging a large batch of similar pending entries, I
want to approve or reject several at once, so that curating a big backlog
doesn't require one click per entry.
- Given multiple pending entries selected on the queue screen, When an
  Admin bulk-approves them with one explicit `entry_type` applied to all
  selected, Then each selected entry executes the existing US-A9/US-M4
  approve behavior individually (including its own embedding computation
  where applicable), attributed to that Admin, and any single entry's
  failure (e.g. already-deleted) doesn't roll back or block the others.
- Given multiple pending or approved entries selected, When an Admin
  bulk-rejects them, Then each executes the existing US-A10/US-M5 reject
  behavior individually, same partial-failure handling as bulk-approve.

### Org/repo/team configuration

**US-A12 — View configured orgs/repos/teams**
As a platform admin, I want to see every org/repo/team the platform knows
about and their current settings, so that I understand what's configured
without querying the database directly.
- Given at least one org/repo/team exists, When I open the configuration
  screen, Then I see the org → repo → team hierarchy and, for each scope,
  its current `OrgRepoTeamSetting` key/value entries (empty scopes show
  clearly as "using platform defaults," not as an error).

**US-A13 — Update a setting at a chosen scope**
As a platform admin, I want to set a configuration value at the
platform, org, repo, or team level, so that I can tune behavior without a
deploy.
- Given a scope (platform-wide, one org, one repo, or one team) and a
  known setting key, When an Admin sets its value, Then a row is
  written/updated in `OrgRepoTeamSetting` at that exact scope (`repo_id`/
  `team_id` NULL narrows up to org-wide or platform-wide, per that table's
  existing column semantics), and a narrower scope's value overrides a
  broader one wherever that setting is read.
- Given an invalid value for a known setting key, When an Admin submits
  it, Then the write is rejected (422) with a clear validation error —
  never silently coerced or stored malformed.

### Model routing

**US-A14 — View current model routing**
As a platform admin, I want to see which model serves each LLM-calling
pipeline stage (`intent_summary`, `review_generation`, `verification` —
the three stages that call the LLM Gateway today), so that I know what's
actually running before I change it.
- Given `ModelRoutingConfig` rows exist for some stages/orgs, When I open
  the model routing screen, Then I see, per stage, the effective model
  (an org-specific row if one exists, else the platform-wide row if one
  exists, else today's static `settings.llm_model` default) and which of
  the three sources it came from.

**US-A15 — Update model routing**
As a platform admin, I want to change which model serves a given stage,
scoped platform-wide or to one org, so that I can route, e.g., a cheaper
model to `intent_summary` and a stronger one to `verification`.
- Given a stage and an optional org scope, When an Admin sets a model
  name, Then a `ModelRoutingConfig` row is written/updated (unique per
  `org_id`+`stage`, per that table's existing constraint).

**US-A16 — The Worker actually reads routing config, not just the UI**
As a platform admin, I want a model routing change to take effect on the
next job, so that the configuration screen isn't cosmetic.
- Given a `ModelRoutingConfig` row exists for a stage (and, where
  applicable, an org), When the Worker dispatches a job touching that
  stage for that org, Then it requests the configured model — falling
  back through org-specific → platform-wide → the existing static
  default — with no restart required for the change to take effect.

### Admin-stats dashboard

**US-A17 — Operational and cost visibility**
As a platform admin, I want to see LLM token spend, queue/index health,
verification/merge funnel counts, and findings-by-severity, so that I can
judge whether the platform is healthy and what it's costing me without
reading logs.
- Given the platform has processed at least one job, When I open the
  dashboard, Then I see current queue depth, recent LLM token usage,
  verification funnel counts (judged/rejected/unmatched, per
  `docs/specs/spec-observability.md`'s US5 counters), and a
  findings-by-severity breakdown, each labeled with the time window it
  covers.

### Deployment

**US-A20 — The Admin UI deploys as its own service**
As a platform operator, I want to deploy, scale, and restart the Admin UI
independently of the Webhook API and Worker, so that an Admin UI issue
never affects the review pipeline's availability and vice versa.
- Given a Helm install with the Admin UI enabled, When it completes, Then
  a distinct `Deployment` and `Service` exist for the Admin UI, separate
  from the existing `webhook-api` and `worker` resources, built from the
  same shared application image via its own `command:`/entrypoint.
- Given `docker compose up` locally, When the stack starts, Then a
  distinct `admin_api` service is running alongside `webhook_api` and
  `worker`, reachable on its own port.

**US-A21 — One coherent way to reach the platform in Kubernetes**
As a platform operator, I want a single, chart-managed `Ingress` covering
both the Admin UI and the GitHub webhook receiver, so that I don't have to
hand-write external exposure for a chart that ships with none today.
- Given Ingress is enabled in the Helm values, When the chart is
  installed, Then an `Ingress` resource routes external traffic to both
  the Admin UI and Webhook API services, with TLS configurable via either
  an `existingSecret` reference or cert-manager annotations (matching the
  chart's existing `existingSecret` convention for other secrets).

**US-A22 — Admin UI has production-appropriate health/resource defaults**
As a platform operator, I want the Admin UI's Kubernetes resources to have
sensible liveness/readiness probes and resource requests/limits, so that
it behaves like a first-class production workload, not an afterthought.
- Given the Admin UI Deployment, When Kubernetes evaluates its probes,
  Then liveness/readiness endpoints exist and are wired, matching the
  pattern the Webhook API's own Deployment template already establishes.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Performance:** Admin UI CRUD-style operations (approve/reject a Memory
  entry, save a config/routing change) complete with a server-side p95
  under 500ms, excluding operations already carrying their own documented
  latency (e.g. `docs/specs/spec-memory.md`'s embedding-compute path on
  approve). *(Assumption pending validation — no prior NFR document sets
  an Admin UI latency target; this mirrors typical internal-tool
  expectations.)*
- **Security:**
  - Passwords are stored using a modern adaptive hash (e.g. Argon2id or
    bcrypt) — never reversible, never logged, in cleartext or hashed form.
  - Session credentials are high-entropy, revocable server-side, and
    carried in a mechanism resistant to casual exfiltration (e.g. an
    `HttpOnly`, `Secure` cookie or an equivalently protected bearer token).
  - Role checks (US-A5) are enforced at the API layer on every mutating
    endpoint — never inferred from what the frontend chooses to render.
  - Every mutating admin action (approve/reject/delete, config change,
    routing change, user management) is attributed to the acting user in
    an audit-visible way, extending the attribution
    `docs/specs/spec-memory.md` already tracks per entry.
  - The bootstrap admin credential (US-A3) is supplied via a Kubernetes
    `Secret`/environment variable, never auto-generated and printed to
    logs — avoids leaking a real credential into log aggregation.
- **Reliability:** `apps/admin_api` is stateless — sessions and all admin
  data live in Postgres, not in-process memory — so it can run multiple
  replicas the same way `webhook_api` already does, with no
  sticky-session requirement. A transient Postgres outage returns a clean
  5xx to the caller (never a hang or a silently-accepted write that's lost)
  — the same "every I/O call takes a timeout" rule `architecture.md`
  already mandates repo-wide, not a new guarantee this PRD invents.
  *(Assumption pending validation: no formal uptime/error-rate SLA exists
  for any entrypoint in this repo today; this PRD does not introduce one
  for the Admin UI specifically, deferring to whatever operational SLA the
  customer's own deployment already targets.)*
- **Scale:** sized for a single-org-to-low-dozens-of-orgs self-hosted
  deployment (this MVP has no cross-org RBAC to make a larger admin team
  practical yet — see Scope) — at least 20 concurrent admin sessions and a
  Memory queue of 10,000+ entries browsable without the list screen
  degrading below the Performance target above. *(Assumption pending
  validation — no prior spec sizes an expected admin-team or Memory-queue
  volume; these numbers are a reasonable floor for the MVP's target
  customer size, not a measured requirement.)*

### Privacy / Compliance

- Memory entries may contain code snippets or PR discussion text; the
  Admin UI exposes them only to authenticated Admin/Viewer sessions, never
  to an unauthenticated caller or a third party — consistent with this
  platform's self-hosted "customer code and review data never leave their
  infrastructure" positioning (`AGENTS.md`).
- The admin-stats dashboard's metrics (US-A17) carry only the
  bounded-cardinality attributes `docs/specs/spec-observability.md`'s own
  privacy rule already established for the underlying counters (severity/
  source tags, never finding message text) — the dashboard adds no new
  free-text exposure beyond what those counters already record.

### Observability

- `apps/admin_api` is instrumented with OpenTelemetry the same way the
  other two entrypoints already are (`docs/specs/spec-observability.md`'s
  existing pattern) — traces, request metrics, and structured logs, not a
  silent third entrypoint.
- Every rejected auth attempt (bad credentials, rate-limited, expired
  session, role-denied) is logged at a level that lets an operator notice
  a real attack pattern, without logging the attempted password itself.

### Rollout & Release Safety

- This is greenfield: no live customer has ever used an Admin UI, so there
  is no live-migration or backwards-compatibility burden for the UI
  itself. The one real cutover is the Memory admin API's auth mechanism
  (US-A7) — since nothing has shipped to a customer yet, this is a direct
  replacement, not a dual-auth transition period.
- The Helm chart's new `Deployment`/`Service`/`Ingress` resources default
  to enabled but are individually toggleable (matching the chart's
  existing `postgresql.enabled`-style convention), so an operator who
  isn't ready to expose the Admin UI externally can disable Ingress alone
  without disabling the service.
- **Unit F's Worker read-path change carries its own kill-switch.** Since
  it touches a production-critical hot path (every LLM-calling job
  dispatch), it ships behind a `model_routing_config_enabled` flag
  (matching the exact enable/disable-flag convention `architecture.md`
  already establishes for every other pipeline-step toggle) — disabling it
  reverts to today's exact behavior, a single static `settings.llm_model`
  for every stage, with zero `ModelRoutingConfig` reads. Default is
  enabled; an operator who hits an unexpected routing issue in production
  can turn it off without a rollback deploy.
- **New tables are additive, not a migration of existing data.** The
  admin-user/session tables and the `ModelRoutingConfig`/
  `OrgRepoTeamSetting` reader/writer logic are all brand-new — no admin UI
  has ever shipped to a customer, so there is no existing row to backfill
  or existing behavior to preserve compatibility with. Each unit's schema
  change is a standard additive Alembic migration, following the same
  convention `docs/specs/spec-memory.md`'s own migrations already
  established.

## UX & Design

No design system or component library exists anywhere in this repo today —
this is the first user-facing frontend surface it ships. This PRD sets
baseline expectations; the specific frontend framework/library is an
implementation decision for the delivery plan, not fixed here.

- **Primary flow:** login → dashboard (stats) → org/repo/team
  config → Memory review queue → model routing. Each screen is reachable
  from a persistent navigation element; there is no forced linear wizard.
- **Edge cases:** first login with zero orgs/repos configured shows a
  clear "nothing configured yet" empty state, not a blank or erroring
  screen; an empty Memory queue states plainly that nothing is pending;
  a session expiring mid-action returns the user to login without losing
  unsaved form input where reasonably preservable; a request that times
  out or fails due to connectivity shows a clear "couldn't reach the
  server, retry" state, distinct from a validation error, never an
  indefinite spinner.
- **States:** every screen that calls the backend has an explicit
  loading, empty, error, and populated-success state — no screen silently
  shows nothing while a request is in flight or has failed. Every mutating
  action (approve/reject/delete, config save, routing change, user
  management) shows an explicit, unambiguous success confirmation distinct
  from the screen simply re-rendering, so the admin never has to guess
  whether their action took effect. No separate "undo" is required beyond
  the existing delete confirmation step (US-A11) — approve/reject
  decisions are already revisable by re-classifying the same entry, per
  `docs/specs/spec-memory.md`'s US-M5 ("reject and delete are as easy as
  approve, and classification stays revisable").
- **Performance as UX:** every primary-flow screen (login, dashboard,
  Memory queue, org/repo/team config, model routing) shows visible data or
  a completed action (not just chrome/loading state) within 2 seconds on a
  typical broadband connection against a warm backend — the dashboard and
  Memory queue are the highest-risk cases (aggregating OTel/Prometheus
  data and potentially large queues respectively), but the target applies
  platform-wide, not just to those two. *(Assumption pending validation —
  no prior spec sets a client-perceived load-time budget; this is a
  reasonable default for an internal admin tool, not a measured
  requirement.)*
- **Accessibility:** WCAG 2.1 AA baseline (keyboard navigable, sufficient
  contrast, form labels/errors programmatically associated) — a
  reasonable default for enterprise operator tooling absent a more
  specific customer requirement.
- **Responsive/device:** desktop-first, since this is an operational tool
  used by admins at a workstation, not a mobile-primary audience; it must
  remain usable (not broken) at tablet widths, but mobile-optimized layout
  is not a requirement.
- **Permission-denied UX:** a Viewer never sees a mutating control as
  clickable-but-failing — controls US-A5 would reject server-side are
  disabled/hidden in the UI too, so the server-side 403 is a defense
  Viewers should never actually encounter in normal use, not their only
  signal that an action isn't available.
- **Consistency:** since this is the first frontend surface, it
  establishes the baseline visual/interaction language any future
  review-owned UI surface would follow — no prior pattern to conform to.

## Stakeholders & Sign-off / Acceptance Criteria

| Role | Who | Responsibility |
| --- | --- | --- |
| Author / Engineering | Andy Beak | Drafts and implements this PRD, per this repo's existing RACI convention (every ADR lists the same R/A) |
| Security review | `/security-review` (this repo's own skill) | Reviews the new auth surface (login, session handling, role enforcement, bootstrap credential) before this ships |

**Acceptance:** a phase is not done until its `Given/When/Then` scenarios
pass as the integration tests the Testing table below names, per
`testing.md`'s traceability requirement.

## Architecture deviation (resolved)

`docs/rules/architecture.md` previously stated two things this PRD's
"standalone deployable service" decision (US-A20) directly conflicted
with: "Two deployable entrypoints share every package below `apps/`" (the
Webhook API and Worker), and "No separate admin-UI service... until a real
requirement forces one." Per that document's own header ("Deviation
requires a new ADR, not a one-off exception"), this was a hard prerequisite
before Unit B could start.

**Resolved 2026-09-20** by amending
[ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md) in place
(rather than superseding it with a new ADR — the underlying self-hosted
decision is unchanged, only the image count it commits to grew from two to
three) to record the Admin UI as a third, standalone deployable image.
`docs/rules/architecture.md` and `docs/reference/c4-architecture.md` are
updated to match: both now describe three deployable entrypoints
(Webhook API, Worker, `apps/admin_api`), and the "no separate admin-UI
service" line now names the Admin UI as the one exception already forced.
No further governance work blocks Unit B.

## Phases / Units

- **Unit A — Architecture governance: `apps/admin_api` as a third
  deployable entrypoint.** ✅ **Done (2026-09-20).** Depends on: nothing.
  Risk: was process-blocking, not code — resolved by amending ADR-0001 in
  place and updating `architecture.md`/`c4-architecture.md` to match (see
  "Architecture deviation," above). **Acceptance:** ADR-0001 records the
  three-image decision; `architecture.md`'s entrypoint list and
  "no separate admin-UI service" line both reflect it — met.
- **Unit B — `apps/admin_api` entrypoint skeleton + local auth (US-A1,
  US-A2, US-A3, US-A5, US-A6).** Depends on: Unit A. Risk: medium — the
  first human-identity/session code anywhere in this repo (today every
  authenticated path is machine-to-machine; see
  [ADR-0010](../adr/0010-oidc-admin-sign-in-and-fixed-role-split.md)). **Acceptance:** US-A1, US-A2, US-A3, US-A5, and US-A6's
  `Given/When/Then` all pass as integration tests against a real Postgres.
- **Unit C — User management (US-A4).** Depends on: Unit B. Risk: low —
  ordinary CRUD once auth/roles exist. **Acceptance:** US-A4's
  `Given/When/Then` passes as an integration test.
- **Unit D — Memory admin API migration (US-A7, US-A8–A11).** Depends on:
  Unit B. Risk: low — the underlying approve/reject/delete/list logic is
  already shipped and tested (`docs/specs/spec-memory.md`); this unit
  swaps its auth dependency and adds attribution, it doesn't re-implement
  the behavior. **Acceptance:** US-A7's `Given/When/Then` (old token
  rejected, new session accepted, attribution recorded) passes as an
  integration test; US-A8–A11 pass as regression-equivalent integration
  tests against the migrated routes.
- **Unit E — Org/repo/team configuration (US-A12, US-A13).** Depends on:
  Unit B. Risk: low-medium — first reader/writer logic against
  `OrgRepoTeamSetting`, which today is schema-only. **Acceptance:** US-A12
  and US-A13's `Given/When/Then` pass as integration tests, including the
  scope-precedence (platform < org < repo/team) behavior.
- **Unit F — Model routing: config + Worker read-path (US-A14, US-A15,
  US-A16).** Depends on: Unit B (config API) and touches `apps/worker`
  (read-path). Risk: medium — the read-path change affects a
  production-critical hot path (every LLM-calling job dispatch); needs a
  fallback chain (org → platform → static default) that never fails
  closed if `ModelRoutingConfig` is empty (today's exact behavior).
  **Acceptance:** US-A14 and US-A15's `Given/When/Then` pass as
  integration tests against the admin API; US-A16 passes as an integration
  test proving a routing change changes which model a real job requests,
  with no restart.
- **Unit G — Admin-stats dashboard (US-A17).** Depends on: Unit B. Risk:
  low-medium — read-only aggregation against existing OTel/Prometheus
  metrics and Postgres counts; no new write path. **Acceptance:** US-A17's
  `Given/When/Then` passes as an integration test asserting the dashboard
  endpoint returns current values for all four named metrics categories.
- **Unit I — Frontend application (all screens).** Depends on: Units B–G
  (can start against contract-mocked APIs once each unit's endpoint shape
  is fixed, per this repo's own "accept protocols" spirit applied to a
  frontend/backend boundary). Risk: medium — genuinely new frontend
  codebase with no prior pattern in this repo to follow.
  **Acceptance:** one E2E test (below) exercises the real built frontend
  against the real backend for the roadmap's own Phase 5 exit scenario.
- **Unit J — Helm chart + docker-compose (US-A20, US-A21, US-A22).**
  Depends on: Unit B (needs a real image to deploy), can develop the
  chart/compose shape in parallel using a stub. Risk: low-medium — the
  chart has no `Ingress` template today, so this is new chart surface, not
  an extension of an existing one. **Acceptance:** US-A20, US-A21, and
  US-A22's `Given/When/Then` pass — `helm template` renders a valid
  `Deployment`/`Service`/`Ingress` for the Admin UI, and
  `docker compose config` validates a working `admin_api` service.
- **Unit K — Bulk approve/reject (US-A23, Could).** Depends on: Unit D.
  Risk: low — no new domain behavior, just applying Unit D's existing
  approve/reject calls to a selected set with per-item, not all-or-nothing,
  failure handling. **Acceptance:** US-A23's `Given/When/Then` passes at
  Integration tier (`apps/admin_api/test_memory_routes.py`) and E2E tier
  (`tests/e2e/test_admin_ui.py`, against the real queue-screen bulk-select
  UI and the real built stack), including the partial-failure case (one
  selected entry already deleted, the rest still succeed).

## Testing / Key Scenarios

Per `testing.md`'s pyramid.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-A1 | Correct credentials issue a session with no license/plugin dependency; incorrect credentials are rejected without revealing username validity | `apps/admin_api/test_auth_routes.py` (new) | Integration |
| US-A2 | Idle session expires; explicit logout revokes server-side immediately | `apps/admin_api/test_auth_routes.py` | Integration |
| US-A3 | Fresh deployment bootstraps exactly one Admin account from operator-supplied credentials; re-running is a no-op | `apps/admin_api/test_main.py` (new) | Integration |
| US-A4 | Admin creates/deactivates other local users with a role; deactivated user's sessions are revoked | `apps/admin_api/test_user_routes.py` (new) | Integration |
| US-A5 | Viewer session rejected (403) on every mutating endpoint; identical read access to Admin | `apps/admin_api/test_role_enforcement.py` (new) | Integration |
| US-A6 | Repeated failed logins for one username are throttled after the configured ceiling | `apps/admin_api/test_auth_routes.py` | Integration |
| US-A7 | Old static Bearer token rejected on migrated Memory routes; new session accepted; action attributed to acting user | `apps/admin_api/test_memory_routes.py` (migrated from `apps/webhook_api/test_admin_routes.py`) | Integration |
| US-A8–A11 | List/approve/reject/delete behave identically to `docs/specs/spec-memory.md`'s US-M4/US-M5/US-M8, now under real session auth | `apps/admin_api/test_memory_routes.py` | Integration |
| US-A23 | Bulk-approve/reject applies the existing per-entry behavior to each selected entry; one entry's failure doesn't block the rest | `apps/admin_api/test_memory_routes.py` | Integration |
| US-A23 | Same scenario, against the real queue-screen bulk-select UI and the real built stack | `tests/e2e/test_admin_ui.py` | E2E |
| US-A12 | Config screen shows org → repo → team hierarchy with current `OrgRepoTeamSetting` values, empty scopes shown as "using defaults" | `apps/admin_api/test_config_routes.py` (new) | Integration |
| US-A13 | Setting a value at a scope writes/updates `OrgRepoTeamSetting`; invalid value rejected (422); narrower scope overrides broader at read time | `apps/admin_api/test_config_routes.py` | Integration |
| US-A14 | Model routing screen shows effective model per stage with its source (org/platform/static default) | `apps/admin_api/test_model_routing_routes.py` (new) | Integration |
| US-A15 | Setting a model for a stage/org writes/updates `ModelRoutingConfig` | `apps/admin_api/test_model_routing_routes.py` | Integration |
| US-A16 | A routing change changes which model a real Worker job requests, with no restart, following the org → platform → static fallback chain | `apps/worker/test_model_routing_integration.py` (new) | Integration |
| US-A17 | Dashboard endpoint returns current LLM spend, queue/index health, verification funnel counts, and findings-by-severity | `apps/admin_api/test_stats_routes.py` (new) | Integration |
| US-A20 | Helm install produces a distinct Admin UI `Deployment`/`Service`; `docker compose up` runs a distinct `admin_api` service | `charts/review/tests/` (new, matching existing chart test conventions) | Integration (Helm template rendering) |
| US-A21 | Chart-rendered `Ingress` routes to both Admin UI and Webhook API services, with TLS configurable via `existingSecret` or cert-manager annotations | `charts/review/tests/` | Integration (Helm template rendering) |
| US-A22 | Admin UI Deployment has liveness/readiness probes and resource requests/limits matching the Webhook API's own pattern | `charts/review/tests/` | Integration (Helm template rendering) |
| Phase 5 exit scenario | Full Docker Compose stack: an admin logs in, configures a repo, approves a pending Memory entry, and changes review-generation's model — all through the real UI against the real backend | `tests/e2e/test_admin_ui.py` (new) | E2E |

## Amendment 2026-10-11 — Phase 7 product-knowledge provider configuration

*Added by [`spec-product-knowledge.md`](spec-product-knowledge.md); the
stories above are unchanged. This block only records what that spec adds to
this one's surface.*

- **Scope → In scope, "Org/repo/team configuration screens" (US-A12/US-A13):**
  the closed `SETTING_KEY_REGISTRY` gains one key,
  `product_knowledge_provider` (`enabled`, `display_name`, `endpoint_url`,
  `credential_ref`, `allow_public_repos`, `issue_key_prefixes`). Written through the existing
  `PUT /api/v1/config/{scope}/{key}` (Admin-only, audit-logged, 422 on
  invalid, platform < org < repo precedence). Team scope is rejected for this
  key.
- **New routes** (Admin API): `GET /api/v1/product-knowledge/credentials`
  (credential reference names, never tokens; Viewer-readable via
  `require_session`; the bound hosts are returned to Admins only) and `POST /api/v1/product-knowledge/test-connection`
  (Admin-only via `require_admin_role`; audit-logged).
- **UX & Design → Config screen:** one new "Product knowledge" panel
  (fields, effective-config preview, Test connection, inline data-egress
  disclosure) at platform/org/repo scope; states and permission-denied
  behavior follow this spec's existing rules.
- **RBAC (US-A5):** unchanged rule applied — write and the side-effecting
  test probe are Admin-only; reads are Viewer-visible.
- **Non-Functional → Security:** a new bootstrap credential map is read by
  `apps/admin_api` as well as the Worker (test-connection); tokens never
  appear in any Admin API response or audit record.

## Open items requiring validation

These were resolved as **assumptions pending validation** during this
PRD's drafting (per the interview's own tagging convention), not as firm
product decisions — flagging them explicitly rather than treating them as
settled:

1. **Performance NFR (p95 < 500ms)**, the Scale floor (20 concurrent
   sessions / 10,000+ Memory entries), and the 2-second page-visible-data
   budget — no prior NFR document sets any of these for the Admin UI;
   this PRD's numbers are reasonable defaults for internal tooling, not
   derived from a stated requirement.

Two items previously open here are now resolved, not pending:
- ~~Whether the "Should"-priority admin-stats units ship in the same
  release as "Must" units~~ — resolved: all of Units A–J are Must-priority
  for this release (Scope > Priority, above); only Unit K (bulk actions)
  remains genuinely optional.
- ~~The Architecture deviation ADR's exact content~~ — resolved: ADR-0001
  was amended in place 2026-09-20 (see "Architecture deviation," above).
