# Plan: Phase 5 — Admin UI

**Status: Proposed.** Sequences [`docs/specs/spec-admin-gui.md`](../specs/spec-admin-gui.md)
— roadmap Phase 5 — into buildable, independently-testable phases, following
the same phase/tests/acceptance/traceability shape
`docs/plans/plan-deliver-memory.md` established as this repo's `docs/plans/`
convention (no `docs/plans/README.md` exists; that prior plan is the only
convention source). Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`).

`spec-admin-gui.md`'s own Unit A (architecture governance — amending
ADR-0001 to record `apps/admin_api` as a third deployable image) is already
**Done (2026-09-20)**. This plan sequences the remaining Units B–K as
Phases 1–10 below, 1:1, in the spec's own dependency order. It does not
re-litigate any of that spec's product/architecture calls — including
[ADR-0010](../adr/0010-open-core-licensing-sso-and-cross-org-rbac-as-tier-1.md)'s
free Admin/Viewer split, [ADR-0008](../adr/0008-usage-based-honor-system-licensing.md)'s
20/day-at-04:00 usage guideline, and [ADR-0004](../adr/0004-multi-org-tenancy.md)'s
"platform admin sees all orgs" MVP default — it only sequences the work and
resolves the *implementation-shape* questions the spec deliberately leaves
open. Those resolutions are recorded below as **Assumptions this plan
makes**, including two genuine technical corrections this plan's own
drafting surfaced by reading the real codebase rather than assuming (see
Assumptions 5 and 10) — not invented, checked.

This is additive work on real, running code (`apps/webhook_api`,
`apps/worker`, `adapter/storage`), not greenfield except for the frontend
and the `iam` package. Every phase below names the existing file/module/
function it extends or mirrors; every cross-file reference was checked
against the real file during drafting (`apps/webhook_api/main.py`,
`apps/webhook_api/settings.py`, `apps/webhook_api/admin_routes.py`,
`apps/worker/main.py`, `apps/worker/dispatch.py`, `apps/worker/settings.py`,
`adapter/storage/models.py`, `adapter/storage/unit_of_work.py`,
`adapter/storage/queue.py`, `adapter/storage/tenancy.py`,
`adapter/storage/conftest.py`, `adapter/storage/migrations/versions/0007_job_queue_job_type_widen.py`,
`adapter/telemetry/`, `review/llm_telemetry.py`, `review/verification.py`,
`review/output.py`, `charts/review/templates/deployment-webhook-api.yaml`,
`charts/review/templates/service-webhook-api.yaml`, `charts/review/values.yaml`,
`charts/review/test-render.sh`, `docker-compose.yml`, `Dockerfile`,
`pyproject.toml`).

## Scope

**In scope** — `spec-admin-gui.md`'s Units B–K, in full, sequenced 1:1 as
Phases 1–10 below: the `apps/admin_api` entrypoint with local login/session
auth, user management, the Memory admin API's migration off the shared
static token, org/repo/team configuration, model routing (config +
Worker read-path), the admin-stats dashboard, the free-tier usage counter
and disclosure, the React frontend, the Helm chart's new Deployment/
Service/Ingress plus a docker-compose service, and (Could-priority) bulk
Memory approve/reject.

**Out of scope** — the spec's own out-of-scope list applies unchanged:
SSO/OIDC and cross-org RBAC (both Tier 1, `review-enterprise`-gated), per-
org RBAC/visibility restriction, SCIM/bulk provisioning, per-stage LLM
*endpoint* override, the PR-footer usage-overage notice, password-reset via
email, and any design-system commitment beyond this PRD's baseline UX. Not
repeated in full here; see the spec's own Scope section.

## Assumptions this plan makes (flagged, not decided)

1. **A new `iam` bounded-context package** (`iam/models.py`: pydantic
   `User`, `Session`, `Role` enum; `iam/auth.py`: pure session-expiry and
   role-check logic; Protocols `UserRepository` and `SessionStore`, 1–3
   methods each, defined at their consumer — `apps/admin_api`) holds this
   plan's first human-identity domain logic. Unlike `memory` (Phase 4
   filled an already-scaffolded empty package), `iam` doesn't exist yet —
   this plan creates it, following the same "organize by bounded context,
   zero infra imports" rule `architecture.md` already states for every
   other domain package. `PasswordHasher` (Argon2) and the login rate
   limiter (Assumption 5) are plain **concrete classes, not Protocols** —
   no second implementation is in view for either, and this repo's own
   closest precedent for a comparable secret-verification concern
   (`apps/webhook_api/admin_routes.py:156-168`'s inline
   `hmac.compare_digest` check) uses no interface at all, per
   `architecture.md`'s "no interface... without a second real
   implementation already in view" rule.
2. **Org/repo/team setting reader/writer logic and the free-tier usage
   counter both live in the existing `tenancy` package**, not a new one —
   `tenancy` already models `Org`/`Repo`/`Team`, so `OrgRepoTeamSetting`'s
   scope-precedence logic (Phase 4) and the usage counter (Phase 7) are
   natural extensions, not new contexts. The usage counter's Protocols
   (`UsageCounterRecorder`/`UsageCounterReader`) are defined here, at the
   data model, per [ADR-0006](../adr/0006-protocol-defined-at-data-model.md)'s
   multi-entrypoint-consumer exemption — the Worker records, the Admin API
   reads, spanning two entrypoints, the same reasoning that already placed
   `MemorySignalRecorder` at the data model instead of one consumer.
   ADR-0006's own Scope section enumerates only two cases and states new
   ones are "extended per phase as new cases land," implying an ADR
   update per case — `memory/store.py`'s own `MemorySignalRecorder`
   already used this exemption without one, a pre-existing gap. This plan
   repeats that same shortcut rather than fixing it, which is a governance
   gap worth the user's attention, not something this plan resolves
   silently or claims to have closed.
3. **Model routing resolution is a concrete `FallbackModelRoutingResolver`
   class in `llmgateway`, not a Protocol** — only one implementation is in
   view (the org → platform → static-default chain), so per
   `architecture.md`'s "no interface without a second real implementation"
   rule this stays a plain class, mirroring `JobQueueRepository`'s own
   no-Protocol precedent. It is imported directly by **both**
   `apps/worker` (to select the model at dispatch time) and
   `apps/admin_api` (to compute the same effective-model-plus-source for
   `GET /api/v1/model-routing`'s US-A14 response) — one implementation
   shared by name across two entrypoints, rather than the Admin API
   re-deriving the same fallback chain independently, which would violate
   `architecture.md`'s "thin composition roots, not separate codebases
   with duplicated logic" rule. Its own Postgres read is wrapped with an
   explicit timeout and **fails open to the static default on error or
   timeout** — it must never hang or fail a job, the same "never fails
   closed if `ModelRoutingConfig` is empty" guarantee the spec already
   requires for the missing-row case, extended here to the
   unreachable-database case. Each LLM-calling step
   (`IntentSummaryStep`/`ReviewGenerationStep`/`VerificationStep`) receives
   the resolver via constructor injection at composition-root time and
   calls `resolver.resolve(stage, org_id)` itself inside its own `run()` —
   `apps/worker/dispatch.py` never reaches into an already-built step's
   internals to overwrite its `model` field, which would violate
   `architecture.md`'s "no stage reaching into another stage's internals"
   and "no... global mutable state" rules. **The "no Protocol" decision
   above is about the resolver's own swappability only — it does not
   excuse `llmgateway` from importing `adapter/storage`'s concrete
   `PostgresModelRoutingRepository` directly**, which would violate
   `architecture.md`'s unconditional "the domain never imports entrypoint
   or adapter code" rule (the same rule Phases 4, 6, and 7 each correctly
   respect via their own consumer-defined Protocol for an adapter-satisfied
   dependency). `llmgateway/routing.py` therefore also defines a narrow
   `ModelRoutingConfigReader` Protocol (`get(org_id, stage) ->
   ModelRoutingConfigRow | None`) that `FallbackModelRoutingResolver`
   depends on — this Protocol exists to preserve the domain/adapter
   dependency direction, not for swappability, and
   `PostgresModelRoutingRepository` implements it.
4. **The Prometheus-reading adapter extends this repo's one existing
   shared HTTP-client pattern, rather than standing alone.**
   `adapter/telemetry/prometheus_reader.py`'s `PrometheusMetricsReader`
   (implementing a `MetricsQuerier` Protocol defined at its consumer,
   `apps/admin_api`) reuses `adapter/github/http.py`'s shared httpx
   retry/backoff + redacted-logging wrapper — this repo's own "one shared
   implementation, not N independent copies" precedent for exactly the
   transient-failure shape (slow/unreachable endpoint) this reader also
   needs to handle — rather than hand-rolling a second one.
   **Query-building and derived-metric math (e.g. queue depth via
   `enqueued_total − claimed_total`) live in a small, separately
   unit-tested `apps/admin_api/stats_service.py` module, not inline in
   `stats_routes.py`.** Checking `apps/webhook_api/admin_routes.py` during
   drafting confirmed that file keeps its route handlers thin and
   delegates real logic to domain packages (`memory.fingerprint`,
   `indexing.embedding`) — it does not embed algorithmic logic in route
   modules. This dashboard logic doesn't need a whole new bounded-context
   package (no reusable domain rules, single consumer), but it does need a
   home outside the route handler itself to stay unit-testable and match
   that precedent.
5. **Login rate limiting is Postgres-backed via a small, dedicated
   `login_attempt` windowed-counter table** (`username`, `window_start`,
   `attempt_count`, atomic `INSERT ... ON CONFLICT DO UPDATE` increment) —
   **not** the existing in-process `InMemoryRateLimiter`, and **not** the
   general-purpose `admin_audit_log` table either. `apps/admin_api` must
   run multiple stateless replicas with no sticky-session requirement
   (Reliability NFR), so an in-memory limiter would undercount attempts
   spread across replicas — a real correction to the `InMemoryRateLimiter`
   precedent (webhook enqueue, `apps/webhook_api/settings.py:65-78`), which
   doesn't carry the same multi-replica correctness bar. Querying the
   general `admin_audit_log` table for this is a second problem this plan
   corrects up front: that table is unbounded, platform-wide, and has no
   index or retention policy for a login-attempt access pattern — coupling
   a security-critical, per-request hot path to an ever-growing generic
   log is a self-inflicted degradation vector under sustained attack. The
   dedicated counter table instead mirrors Phase 7's own
   `PrivateRepoReviewCount` atomic-upsert pattern — the same structural
   problem (bounded-window occurrence counting) solved the same proven way
   elsewhere in this plan, rather than a third, novel approach. A concrete
   `PostgresLoginAttemptRepository` class implements this, not a Protocol
   (Assumption 1).
6. **A single generic `admin_audit_log` table** (`actor_user_id`, `action`,
   `resource_type`, `resource_id`, `created_at`, `metadata JSONB`) records
   every mutating admin action (approve/reject/delete, config write,
   routing write, user management, bulk actions, login failures/successes)
   — rather than bespoke attribution columns bolted onto every affected
   table. `memory_entry` additionally gets one new nullable
   `approved_by_user_id` FK column (alongside its existing free-text
   `approved_by` string, `adapter/storage/models.py:542-543`) since
   US-A9's own Given/When/Then implies the entry itself, not just a side
   log, carries who approved it; reject/delete attribution for Memory
   entries, and every other unit's attribution, is carried by the audit log
   alone (`MemoryEntry` has no existing `rejected_by`/`deleted_by` columns
   to extend). This is a deliberate asymmetry, not an oversight: US-A9's
   own Given/When/Then is the one story phrased around the entry itself
   ("approves it... attributed to that Admin's identity"), while reject
   and delete have no equivalent entry-level field to extend today, and
   the audit log alone already satisfies both stories' actual acceptance
   criteria.
7. **Session credential is a high-entropy random opaque token, not a
   JWT** — this repo's one existing `pyjwt` dependency
   (`pyproject.toml:30`) is used for GitHub App RS256 auth, an unrelated
   concern, and a decodable JWT invites a revocation-check foot-gun a
   hash-lookup token avoids by construction. Delivered via an `HttpOnly`,
   `Secure`, `SameSite=Lax` cookie; the token itself is SHA-256-hashed
   before storage in a new `admin_session` table and compared via
   `hmac.compare_digest` — mirrors `admin_api_key`'s own "never store or
   compare the raw secret" pattern (`apps/webhook_api/admin_routes.py:156-168`).
   Idle expiry via a `last_seen_at` column updated on each authenticated
   request (sliding window, matching US-A2's "after inactivity" wording).
8. **Password hashing: `argon2-cffi` (Argon2id), a new dependency.**
   Verified nothing in `pyproject.toml` covers this today.
9. **Frontend: React + TypeScript + Vite** (confirmed with the user, since
   this is the first frontend surface in the repo with no existing
   convention to mirror). The built static bundle (`vite build` → `dist/`)
   is copied into the shared `runtime` Docker stage at image-build time and
   served by `apps/admin_api`'s own FastAPI app via `StaticFiles`, mounted
   under a catch-all route that excludes `/api/*` and `/healthz` — keeps
   this a single deployable process/image, consistent with US-A20's "own
   container image... via its own command/entrypoint" (no second nginx or
   static-hosting container introduced).
10. **Helm liveness/readiness probes have no existing pattern to mirror.**
    US-A22 asks for probes "matching the pattern the Webhook API's own
    Deployment template already establishes" — verified via
    `charts/review/templates/deployment-webhook-api.yaml` and
    `deployment-worker.yaml` that **neither has any probe today** (only
    docker-compose has a `/healthz`-based healthcheck). This plan adds
    fresh liveness/readiness probes to the Admin UI's Deployment only,
    reusing the existing `/healthz` convention already implemented in
    `webhook_api` and mirrored into `admin_api` (Phase 1) — and does
    **not** retroactively add probes to the other two Deployments, since
    that pre-existing gap is outside this PRD's scope, not a drive-by fix.
    A real correction to the spec's own wording, checked rather than
    assumed.
11. **Bootstrap admin credentials are required `AdminAPISettings` fields
    with no default** (`admin_bootstrap_username: str`,
    `admin_bootstrap_password: SecretStr`) — mirrors `admin_api_key`'s
    existing "no safe default, dangerous to silently default" pattern
    (`apps/webhook_api/settings.py:178-186`). Every environment, including
    post-bootstrap restarts, keeps the value set; the bootstrap routine is
    a no-op past the first run regardless of whether the value is present.
    **The bootstrap routine's check-then-insert is wrapped in a single
    transaction holding a fixed-key `pg_advisory_xact_lock`**, closing a
    real TOCTOU race this plan's drafting caught: since `apps/admin_api`
    runs multiple stateless replicas with no sticky-session requirement,
    concurrent replicas starting against a fresh database could otherwise
    each observe an empty `admin_user` table and race to insert. The
    advisory lock serializes the check across replicas, so only the first
    to acquire it inserts, and every later replica's own check correctly
    observes that row and no-ops — satisfying US-A3's "already has at
    least one admin user → no-op" guarantee regardless of username, which
    a narrower `ON CONFLICT (username) DO NOTHING` alone would not (a
    differently-named later admin wouldn't conflict on username and would
    wrongly re-run bootstrap). `admin_user.username` also gets its own
    `UniqueConstraint` regardless, as ordinary integrity protection for
    Phase 2's `POST /api/v1/users`.
12. **Unit G's exact metric-to-endpoint mapping uses the real OTel counters
    this plan verified exist** (`review.queue.enqueued_total`,
    `review.queue.claimed_total`, `review.queue.wait_seconds`,
    `review.queue.job_attempts`; `review.llm.tokens_total{stage,model,
    token_type}`; `review.verification.{judged,rejected,unmatched}_total`,
    unlabeled; `review.findings.count_total{severity,source}`,
    `review.findings.dropped_by_ceiling_total`). "Queue depth" is **not** a
    directly emitted gauge and must be derived
    (`enqueued_total − claimed_total`) via PromQL; no counter distinct from
    the shared queue/job metrics was found for a separate "index health"
    signal. Phase 6 confirms the exact PromQL query set against a real
    Prometheus instance before finalizing the dashboard response shape,
    rather than assuming it here — flagged, not invented, exactly like the
    Phase 4 (Memory) plan's own verified-API corrections.
13. **CSRF defense for the cookie-authenticated session is a required
    Phase 1 deliverable, not an implementation-time judgment call, and is
    session-bound, not a naive double-submit.** At login, alongside the
    `HttpOnly` session cookie (Assumption 7), the server sets a second,
    **non-`HttpOnly` but still `Secure`, `SameSite=Lax`** cookie holding
    an HMAC of the session id (keyed by a server-side secret) — the same
    "value the client hands back must match a server-held computation,
    compared via `hmac.compare_digest`" shape this repo already uses
    twice (`apps/webhook_api/admin_routes.py:156-168`'s bearer-token
    check; `adapter/github/manifest.py`'s `generate_setup_state`/
    `verify_setup_state` signed-cookie pair) — rather than a bare
    independent random value, which would remain vulnerable to
    cross-subdomain cookie injection ("cookie tossing") if this
    deployment ever shares a parent domain with a less-trusted
    subdomain. The client echoes this value back in a custom header
    (e.g. `X-CSRF-Token`) on every mutating request; `require_session`
    recomputes the expected HMAC from the request's own session id and
    compares via `hmac.compare_digest`. This closes the residual gap a
    bare double-submit token would leave, and — since the CSRF cookie
    itself now also carries `Secure` — closes the separate gap of a
    network attacker setting/overwriting a non-`Secure` cookie over a
    downgraded connection to defeat the check entirely. `SameSite=Lax`
    alone (Assumption 7) doesn't cover Chrome's ~2-minute "Lax+POST"
    grace window for freshly-set cookies, non-compliant clients, or a
    proxy/CDN that strips `SameSite` — this is why the check exists at
    all. Every mutating route added in Phases 2–7 and 10 checks this
    token via the same `require_admin_role`/`require_session` dependency
    chain (Phase 1), not a per-route reimplementation.
14. **Password policy, Argon2 cost parameters, and login-timing
    consistency are stated explicitly, not left to library defaults.**
    Bootstrap and admin-created passwords (US-A3, US-A4) require a stated
    minimum length (12 characters) enforced server-side before hashing;
    `Argon2PasswordHasher` (Assumption 1) uses explicit, documented cost
    parameters (OWASP's current baseline: 19 MiB memory, 2 iterations, 1
    degree of parallelism) rather than whatever `argon2-cffi`'s
    constructor defaults to. The login route also performs a dummy,
    constant-cost Argon2 verification against a fixed placeholder hash
    when the submitted username doesn't exist, so a nonexistent- vs.
    wrong-password response takes equivalent time — closing the
    username-enumeration timing side-channel US-A1's own "reveals nothing
    about whether the username exists" acceptance criterion requires,
    which a bare `if user is None: return 401` shortcut would otherwise
    leave open.

## Phase 1 — `apps/admin_api` entrypoint skeleton + local auth (Unit B: US-A1, US-A2, US-A3, US-A5, US-A6)

**Status: Done (2026-09-30).**

**Why first:** every later phase is either a route inside this entrypoint or
a caller that needs a session to authenticate with; nothing else in this
plan is testable end-to-end without it.

- New `iam` package (Assumption 1): `iam/models.py` (`User`, `Session`,
  `Role`), `iam/auth.py` (pure expiry/role-check logic, zero infra
  imports), Protocols `UserRepository`, `SessionStore`.
- `adapter/storage/models.py`: new `AdminUserRow` (`username` with a
  `UniqueConstraint`, per Assumption 11), `AdminSessionRow`,
  `AdminAuditLogRow` ORM classes (Assumption 6), and `LoginAttemptRow`
  (`username`, `window_start`, `attempt_count`, Assumption 5). New
  migration `0008_admin_iam_schema.py`, following
  `0007_job_queue_job_type_widen.py`'s exact shape (docstring,
  `revision`/`down_revision`, plain `op.*` calls, no ORM-model imports).
- `adapter/storage/iam.py`: `PostgresUserRepository`, `PostgresSessionStore`,
  `PostgresAuditLogRepository`, `PostgresLoginAttemptRepository`
  (concrete class, no Protocol, per Assumption 5) — constructor takes
  `session_factory`, methods delegate to `run_in_session` exactly like
  `JobQueueRepository` (`adapter/storage/queue.py:83-84,149`) and
  `PostgresTenancyStore`. `PostgresLoginAttemptRepository` also runs a
  periodic purge (rows older than a few `login_rate_limit_window_minutes`
  windows) on each write, bounding the table's growth against a flood of
  attempts against nonexistent usernames.
- `adapter/auth/argon2_hasher.py`: `Argon2PasswordHasher` (concrete class,
  explicit OWASP-baseline cost parameters, Assumptions 1/14).
- `apps/admin_api/settings.py`: `AdminAPISettings(OtelSettingsMixin)`,
  `env_prefix="REVIEW_ADMIN_API_"`, shared `database_url` alias (mirrors
  `apps/webhook_api/settings.py:52`), `session_cookie_secure: bool = True`,
  `session_idle_timeout_minutes`, `admin_bootstrap_username`,
  `admin_bootstrap_password: SecretStr` (Assumption 11),
  `login_rate_limit_attempts`/`login_rate_limit_window_minutes`,
  `min_password_length: int = 12` (Assumption 14), `admin_csrf_secret:
  SecretStr` (required, no default, mirroring `admin_api_key`'s own
  "no safe default" pattern — the key the CSRF HMAC in Assumption 13 is
  keyed by; a new, dedicated secret, not a reuse of any session-cookie
  material, since the session token itself is an opaque hash-lookup value
  with no signing key of its own).
- `apps/admin_api/auth_routes.py` (`create_auth_router(...)` factory,
  mirroring `manifest_routes.py:52-67`'s shape): `POST /api/v1/auth/login`
  (verifies via `Argon2PasswordHasher`, running a dummy constant-cost
  verification against a fixed placeholder hash when the username doesn't
  exist — Assumption 14 — then, on success, sets both the `HttpOnly`
  session cookie and a non-`HttpOnly` CSRF-token cookie, Assumption 13),
  `POST /api/v1/auth/logout`, `GET /api/v1/auth/session`.
- `apps/admin_api/dependencies.py`: `require_session` (also validates the
  `X-CSRF-Token` header against the CSRF cookie on any non-GET request,
  Assumption 13), `require_admin_role` — the US-A5 enforcement point every
  later phase's mutating routes reuse.
- `apps/admin_api/bootstrap.py`: idempotent bootstrap routine, its
  check-then-insert wrapped in one transaction holding a fixed-key
  `pg_advisory_xact_lock` (Assumption 11 — closes the multi-replica race).
- `apps/admin_api/main.py`: `create_app(settings)` composition root
  mirroring `apps/webhook_api/main.py:316-425`'s exact shape (builds
  engine/session_factory, constructs the `iam` repositories + Argon2 hasher
  + `PostgresLoginAttemptRepository` via constructor injection, runs
  bootstrap once at startup, registers `GET /healthz` like
  `main.py:328-331`, mounts `auth_routes`); module-level `settings =
  AdminAPISettings(); app = create_app(settings)` for uvicorn import,
  matching `main.py:428-431`.
- `docker-compose.yml`: new `admin_api` service block mirroring
  `webhook_api`'s (`docker-compose.yml:67-71`: `build.target: runtime`,
  `image: review-app:compose-local`, port mapping, `/healthz` healthcheck,
  `depends_on.migrate`), plus the new required env vars.

**Tests:** Unit tests for `iam/auth.py`'s pure logic (session-expiry math,
role-check) with fakes for the two Protocols. Integration
(`apps/admin_api/test_auth_routes.py`, `test_main.py`,
`test_role_enforcement.py`) against `migrated_engine`/`pgvector_container`
imported from `adapter/storage/conftest.py` exactly like
`apps/webhook_api/conftest.py` does, using `TestClient(create_app(settings))`
inside a `with` block per `apps/webhook_api/test_admin_routes.py:211-213`'s
documented reason (one anyio portal per pooled connection). Cover:
correct/incorrect login with no username-existence leak (including a
timing-equivalence assertion between the nonexistent-username and
wrong-password paths, per Assumption 14), idle expiry (backdating a
session's `last_seen_at` directly in the database rather than mocking the
clock, since `TestClient`'s shared portal makes time-mocking fragile),
explicit logout revocation + replay rejection, bootstrap idempotency
(fresh vs. already-has-admin), a **separate, non-`TestClient` concurrency
test** proving the advisory lock prevents duplicate admins — invoking
`apps/admin_api/bootstrap.py`'s routine directly via `asyncio.gather`
over two independently-created sessions/engines against the same
`migrated_engine` database, mirroring
`adapter/storage/test_queue_skip_locked.py`'s existing pattern for
forcing genuine concurrent contention, since `TestClient`'s own
`with`-block precedent is single-portal and sequential by design and
cannot exercise two overlapping bootstrap attempts on its own — an
aggregate `review.auth.login_failed_total` assertion alongside the
rate-limit ceiling case (asserting it reads/writes `login_attempt`, not
`admin_audit_log`), a mutating request missing or with a mismatched CSRF
header rejected, and a `require_admin_role`-gated dummy route rejecting a
Viewer session (403).

**Observability & rollback:** OTel instrumentation via `OtelSettingsMixin`,
matching the other two entrypoints. Every rejected auth attempt logged at
`WARNING` with `{reason, username}`, never the password. A second,
unlabeled counter (`review.auth.login_failed_total`, no username
attribute, to avoid unbounded cardinality) is incremented on every failed
login regardless of username — the per-username `login_attempt` table
throttles one attacker hammering one account, but is blind to a
low-and-slow campaign spread across many distinct usernames; this
aggregate counter gives operators a signal to alert on for that pattern,
which the per-username limiter alone cannot surface. Rollback: this is a
brand-new, independently deployable image (Unit A's ADR-0001 amendment) —
not running it (compose service commented out, or Helm `adminApi.enabled:
false` once Phase 9 exists) has zero effect on `webhook_api`/`worker` until
Phase 3 retires the static token.

**Acceptance:** US-A1, US-A2, US-A3, US-A5, US-A6's Given/When/Then all pass
as integration tests against a real Postgres.

## Phase 2 — User management (Unit C: US-A4)

**Why next:** ordinary CRUD once auth/roles exist; only depends on Phase
1's `iam` package and repositories.

- `apps/admin_api/user_routes.py` (`create_user_router(...)`):
  `POST /api/v1/users`, `GET /api/v1/users`,
  `PATCH /api/v1/users/{id}/deactivate` — gated by `require_admin_role`.
- Deactivation revokes every one of that user's `admin_session` rows
  (`PostgresSessionStore.revoke_all_for_user`). `admin_audit_log.actor_user_id`
  carries no `ON DELETE CASCADE` from `admin_user` — deactivation is a
  status flip, never a hard delete, so historical audit rows keep
  resolving to the deactivated user (per US-A4's second Given/When/Then).

**Tests:** `apps/admin_api/test_user_routes.py` — create user with a role,
deactivated user's existing session rejected on the next request,
deactivated user's past audit rows still resolve to their username.

**Observability & rollback:** user-management actions logged to
`admin_audit_log` (`action=user_created`/`user_deactivated`). No kill
switch needed — additive CRUD, no production hot path.

**Acceptance:** US-A4's Given/When/Then passes as an integration test.

## Phase 3 — Memory admin API migration (Unit D: US-A7, US-A8–A11)

**Why next:** the one real cutover in this plan (per the spec's own
Rollout & Release Safety framing) — doing it early means every later route
module copies this same auth-swap pattern instead of inventing its own.

- Move `apps/webhook_api/admin_routes.py`'s four route handlers
  (`admin_routes.py:172-281`: list/approve/reject/delete) into
  `apps/admin_api/memory_routes.py`, swapping
  `Depends(_require_admin_key)` for `Depends(require_session)` on the read
  route (Viewer parity, per US-A5's second clause) and
  `Depends(require_admin_role)` on approve/reject/delete.
- Each mutating handler writes an `admin_audit_log` row and, on approve,
  sets `memory_entry.approved_by_user_id` (Assumption 6) from the acting
  session's user id.
- Delete `apps/webhook_api/admin_routes.py` entirely, its registration in
  `apps/webhook_api/main.py:415-422`, and `admin_api_key` from
  `WebhookAPISettings` (`settings.py:186`) — a full retirement per
  US-A7's second Given/When/Then ("the static-token path is fully
  retired"), not a dual-auth transition (ruled out explicitly by the
  spec's Rollout & Release Safety section, since no live customer exists).
- **Remove the dead config surface a partial retirement would otherwise
  leave behind**, verified by grep to have no other consumer: the
  `REVIEW_WEBHOOK_API_ADMIN_API_KEY` wiring in
  `charts/review/templates/deployment-webhook-api.yaml`, the
  corresponding required (`:?`) env var in `docker-compose.yml`, and its
  entry in `.env.example`; **the existing top-level `adminApi.apiKey`
  field in `charts/review/values.yaml:157-159` and the conditional
  `admin-api-key` secret-data block in
  `charts/review/templates/secret.yaml:18,44-45`** (both orphaned once
  `deployment-webhook-api.yaml`'s reference to `.Values.adminApi.apiKey`
  is deleted); and `apps/webhook_api/main.py`'s
  `_build_embedding_client`/`_check_embedding_model_startup` plus
  `WebhookAPISettings.embedding_model_path` — both exist solely to serve
  the approve handler being deleted (confirmed via grep: no other
  consumer in `webhook_api`), so leaving them would make `webhook_api`
  keep loading and checksum-verifying the local ONNX embedding model at
  every startup for a capability it no longer has. Also delete
  `apps/webhook_api/test_main.py`'s two tests tied to that startup check
  (`test_create_app_succeeds_with_loadable_embedding_model`,
  `test_create_app_exits_non_zero_with_unloadable_embedding_model`,
  currently at `test_main.py:178-215`), which construct settings and
  assert log messages against the code being removed. **This empties the
  top-level `adminApi:` values.yaml key entirely** (Phase 3 removes its
  only field, `apiKey`) — Phase 9 then introduces a **fresh** `adminApi:`
  block for the real `apps/admin_api` service (replicaCount/image/
  service/resources/credentials) into that same now-empty key, so the two
  phases don't collide in shape; Phase 9's own bullet is written assuming
  Phase 3 has already run. This is core cleanup for a genuine full
  retirement, not drive-by scope creep — "fully retired, not left as a
  silent alternate credential" requires removing the credential's entire
  supporting surface, not just its route.

**Tests:** `apps/admin_api/test_memory_routes.py`, migrated wholesale from
`apps/webhook_api/test_admin_routes.py` (same `MemoryAdminRepository`/
`PostedCommentReader`/`EmbeddingClient` fakes and 401/422/400/404 cases),
plus new cases: the old route path no longer exists on `webhook_api` at
all (its OpenAPI schema no longer lists `/admin/memory-entries`), and
approve/reject attribute the acting session's user id.

**Observability & rollback:** full retirement, not a flagged toggle —
rollback is reverting this phase's commit, matching the spec's framing of
this as a one-way cutover on greenfield data (no live customer has used
this API yet).

**Acceptance:** US-A7, US-A8–A11's Given/When/Then pass as integration
tests against the migrated routes.

## Phase 4 — Org/repo/team configuration (Unit E: US-A12, US-A13)

**Why next:** first reader/writer logic against `OrgRepoTeamSetting`
(`adapter/storage/models.py:292-326`), today schema-only ("placeholder
only — no reader/writer logic exists yet"); depends only on Phase 1.

- `tenancy/settings.py` (Assumption 2): pure scope-precedence resolution
  (platform < org < repo/team) as a pure function over matching rows —
  unit-testable with no I/O — plus a `SettingRepository` Protocol.
- `adapter/storage/tenancy.py`: `PostgresSettingRepository` — get-by-scope,
  upsert against `uq_setting_scope_key`
  (`org_id`/`repo_id`/`team_id`/`key`), validated against a small closed
  registry of known setting keys (reject unknown keys or malformed values
  with 422, per US-A13's second Given/When/Then — never silently coerced).
- `apps/admin_api/config_routes.py`: `GET /api/v1/config` (org→repo→team
  tree, each scope's settings; empty scopes marked
  `using_platform_defaults: true`), `PUT /api/v1/config/{scope}/{key}`.

**Tests:** unit tests for the pure precedence-resolution function across
platform/org/repo/team combinations. Integration
(`apps/admin_api/test_config_routes.py`): write at each scope, read-time
override confirmed, invalid value → 422, empty-scope UI shape.

**Observability & rollback:** config writes logged to `admin_audit_log`.
No kill switch — nothing yet reads `OrgRepoTeamSetting` for runtime
pipeline behavior (this PRD only builds the admin round-trip), so there is
no production hot path to break.

**Acceptance:** US-A12, US-A13's Given/When/Then pass, including
scope-precedence.

## Phase 5 — Model routing: config + Worker read-path (Unit F: US-A14, US-A15, US-A16)

**Why next:** the plan's highest-risk phase — the only unit touching a
production-critical hot path (every LLM-calling job dispatch in
`apps/worker`) — sequenced with Phases 1–4 already proven so its kill
switch has real infrastructure (audit log, config-route precedent) to lean
on.

- `llmgateway/routing.py` (Assumption 3): a narrow `ModelRoutingConfigReader`
  Protocol (`get(org_id, stage) -> ModelRoutingConfigRow | None`) —
  preserving the domain/adapter dependency direction, not for
  swappability — plus concrete `FallbackModelRoutingResolver` class
  (`resolve(stage, org_id) -> str`, no Protocol of its own — only one
  implementation is in view) implementing the org → platform →
  static-default chain against that reader, with an explicit per-call
  timeout that **fails open to the static default on error or timeout**
  (never hangs or fails the calling job); unit-tested with a fake
  `ModelRoutingConfigReader`, including an error-injection case proving
  the fail-open behavior. Imported directly by both `apps/worker` and
  `apps/admin_api` (Assumption 3) so the two entrypoints share one
  fallback implementation rather than each computing it independently.
- `adapter/storage/llm_routing.py`: `PostgresModelRoutingRepository`,
  implementing `ModelRoutingConfigReader` (get by org+stage, upsert
  respecting `uq_model_routing_org_stage`
  (`adapter/storage/models.py:243`)).
- `apps/admin_api/model_routing_routes.py`: `GET /api/v1/model-routing`
  (effective model per stage + its source: org/platform/static, computed
  via the same `FallbackModelRoutingResolver`), `PUT /api/v1/model-routing/{stage}`.
- `apps/worker/settings.py`: new `model_routing_config_enabled: bool =
  True`, mirroring `memory_suppression_enabled`'s exact four-piece pattern
  verified in `apps/worker/settings.py:312-325` and
  `apps/worker/main.py:496-514,798-811,850-855` — field with rationale
  docstring, startup `WARNING` log when disabled, conditional construction
  at the composition root (`None` restores byte-for-byte today's
  behavior), never read directly by step code. **Toggling this flag
  itself still requires a `WorkerSettings` env-var change and a Worker
  restart, exactly like `memory_suppression_enabled` — it is not an
  instant runtime flip.** (US-A16's own "no restart required" guarantee
  is about a *routing config change* taking effect, not about this kill
  switch; see Compatibility & rollout, below, for the corrected claim.)
- `apps/worker/main.py`'s `_build_review_registry`: when enabled, inject
  the shared `FallbackModelRoutingResolver` instance into each
  LLM-calling step's constructor (`IntentSummaryStep`/
  `ReviewGenerationStep`/`VerificationStep`) alongside today's other
  `_LLMStepKwargs`, rather than passing a fixed `model` string. Each
  step's own `run()` calls `self._model_resolver.resolve(stage,
  job.payload.org_id)` at **dispatch time** — this is the one deliberate
  deviation from the existing "build kwargs once at startup" shape
  (`main.py:792-796`), required because US-A16 demands "no restart
  required" for a routing *change* to take effect, and it is done via
  constructor injection into the step, never by `dispatch.py` reaching
  into an already-built step's internals (Assumption 3).
- A new metric, `review.llm.model_routing_source_total{source}`
  (`source` ∈ `org`/`platform`/`static_default`/`fallback_on_error`),
  incremented on every `resolve()` call — gives operators a real signal
  for which tier is actually serving traffic, including the fail-open
  path, beyond a `DEBUG`-level log line that's typically disabled in
  production.

**Tests:** unit tests for `FallbackModelRoutingResolver`'s three-tier
fallback, **plus a fake-repository-raises case** asserting the resolver
falls open to the static default rather than propagating the error.
Integration: `apps/admin_api/test_model_routing_routes.py` (view/set,
unique-constraint upsert, and the effective-source field correct across
all three tiers). `apps/worker/test_model_routing_integration.py`: seed a
`ModelRoutingConfig` row, dispatch a real job against `migrated_engine`,
assert the fake `LLMClient` receives the configured model with no worker
restart; a second case with the flag disabled asserts the static default
is used even with a seeded row present (kill-switch proof); a third case
simulates the repository raising during `resolve()` and asserts the job
still completes using the static default, not a failed dispatch.

**Observability & rollback:** `model_routing_config_enabled=false`
reverts to today's exact single static-default behavior, zero
`ModelRoutingConfig` reads — but requires a Worker restart to flip (see
above; this is a corrected claim, not the "instant" rollback an earlier
draft of this plan asserted). A transient Postgres failure during
`resolve()` degrades to the static default rather than affecting job
success, and is visible via the `fallback_on_error` value of the new
`review.llm.model_routing_source_total` metric.

**Acceptance:** US-A14, US-A15 pass as integration tests against the admin
API; US-A16 passes proving a live routing *change* takes effect with no
restart.

## Phase 6 — Admin-stats dashboard (Unit G: US-A17)

**Why next:** read-only aggregation against already-emitted OTel/
Prometheus metrics and existing Postgres counts; no new write path, so
lower risk than Phase 5, sequenced here since Phase 7's usage counter
builds on the same "read an external metrics system" concern.

- `adapter/telemetry/prometheus_reader.py` (Assumption 4):
  `PrometheusMetricsReader` implementing `MetricsQuerier` (defined in
  `apps/admin_api`) — reuses `adapter/github/http.py`'s shared httpx
  retry/backoff + redacted-logging wrapper for calls to the Prometheus
  HTTP API's `/api/v1/query`, with typed pydantic response models (per
  `architecture.md`'s "validate every boundary" rule). New
  `AdminAPISettings.prometheus_base_url` field.
- `apps/admin_api/stats_service.py` (Assumption 4): the PromQL
  query-building and derived-metric math (queue depth via
  `review_queue_enqueued_total - review_queue_claimed_total` — Assumption
  12, no direct depth gauge exists; LLM token usage summed by
  `stage`/`model`/`token_type`; the three unlabeled verification-funnel
  counters; findings-by-severity grouped by `severity`) — a separately
  unit-tested module, not inline in the route handler.
- `apps/admin_api/stats_routes.py`: thin `GET /api/v1/stats` handler
  calling `stats_service`, each field labeled with its time window. If
  Prometheus is unreachable, returns a clean 5xx with a distinct error
  body, never a hang (Reliability NFR), logs at `ERROR`, and increments a
  new `review.stats.prometheus_unreachable_total` counter — matching this
  plan's other new signals (`review.auth.login_failed_total`,
  `review.llm.model_routing_source_total`,
  `review.usage_counter.increment_failed_total`) as a metric an operator
  can alert on, rather than relying on log-based alerting alone for this
  one signal.

**Tests:** unit tests for `stats_service`'s PromQL-query-building logic
against a fake `MetricsQuerier`. Integration
(`apps/admin_api/test_stats_routes.py`): stubbed Prometheus-shaped HTTP
responses asserting all four categories present and correctly windowed;
an unreachable-Prometheus case asserting the clean-5xx behavior and the
`ERROR`-level log.

**Observability & rollback:** this endpoint is itself observability
tooling with no side effects — no kill switch; a slow/unreachable
Prometheus degrades only this one dashboard endpoint, never the review
pipeline. The `ERROR`-level log on persistent unreachability (above) is
the proactive signal distinguishing "Prometheus had one slow request"
from "Prometheus has been down for an hour and nobody's opened the
dashboard to notice."

**Acceptance:** US-A17's Given/When/Then passes — the endpoint returns
current values for all four named metric categories.

## Phase 7 — Free-tier usage counter + disclosure (Unit H: US-A18, US-A19)

**Why next:** genuinely new infrastructure (nothing counts reviews per org
per day today) needing the Worker's job-completion path as its write
trigger — the plan's other production-hot-path touch besides Phase 5, but
lower risk since it's a pure increment with no behavior-changing read.
Depends on Phase 1 (Admin API) and Phase 6 (the dashboard surface it
appears alongside).

- `tenancy/usage.py` (Assumption 2): `UsageCounterRecorder`/
  `UsageCounterReader` Protocols.
- `adapter/storage/models.py`: new `PrivateRepoReviewCount` row
  (`installation_id`, `day` (04:00-anchored date), `count`), unique per
  `(installation_id, day)`; migration `0009_usage_counter.py`.
- `adapter/storage/tenancy.py`: `PostgresUsageCounterRepository` — atomic
  `INSERT ... ON CONFLICT DO UPDATE count = count + 1`; a pure `day_for(now)`
  helper computing the 04:00-anchored calendar day, unit-tested against
  boundary cases (03:59 / 04:00 / 04:01, per ADR-0008's Decision).
- `apps/worker/dispatch.py`: after a `REVIEW` job completes successfully,
  if the repo is private on GitHub (confirm the exact existing repo-privacy
  field at implementation time), call
  `UsageCounterRecorder.increment(installation_id, now)`; public repos are
  an unconditional no-op.
- `apps/admin_api/usage_routes.py`: `GET /api/v1/usage` — per-org
  current-day count, `over_threshold: bool` (count > 20), no functionality
  gated by it (informational only, per ADR-0008's Decision).

**Tests:** unit tests for `day_for`'s 04:00 boundary math. Integration:
`apps/worker/test_usage_counter.py` — completed private-repo job increments
the right row; public-repo job never touches the table; two jobs either
side of 04:00 land in different rows; **a fake `UsageCounterRecorder` that
raises on `increment()` — asserting the `REVIEW` job still completes
successfully and the failure metric below is incremented**, proving the
fail-soft behavior this phase documents is real, not just described.
`apps/admin_api/test_usage_routes.py` — seeded counts above/below 20
render correctly, with no other field altered by crossing the threshold.

**Observability & rollback:** an increment failure (e.g. transient
Postgres error) must not fail the otherwise-successful `REVIEW` job —
wrapped so the counter-write error is logged, not raised, **and a new
`review.usage_counter.increment_failed_total` counter is incremented** so
a systemic write-failure pattern (e.g. a degraded Postgres path silently
undercounting every org) is visible and alertable rather than only
discoverable by grepping logs. This is a deliberate, narrow exception to
`architecture.md`'s "never swallow a domain error" rule, justified because
ADR-0008 makes this metering side-effect explicitly lower-priority than
the review job it's attached to — called out here rather than silently
written into the code.

**Acceptance:** US-A19's Given/When/Then (counted/not-counted) and
US-A18's (threshold display) pass as integration tests.

## Phase 8 — Frontend application (Unit I: all screens)

**Why next:** the first frontend codebase in this repo, establishing the
baseline visual/interaction language every later screen follows —
sequenced after every backend phase whose contract it depends on for real
(as opposed to contract-mocked) end-to-end verification, per the spec's
own note that individual screens can start earlier against mocks.

- New `apps/admin_api/frontend/` (React + TypeScript + Vite, Assumption
  9): login, dashboard (stats + usage), org/repo/team config, Memory
  review queue, model routing, user management screens.
- Each screen implements the PRD's UX baseline verbatim: explicit loading/
  empty/error/populated-success states (never silently blank mid-request
  or on failure); explicit success confirmation distinct from a bare
  re-render on every mutating action; disabled/hidden (not
  clickable-but-failing) controls for a Viewer session; "nothing
  configured yet" empty states; WCAG 2.1 AA baseline (keyboard nav,
  contrast, associated form labels/errors); desktop-first but usable at
  tablet widths.
- Build step (`npm run build`) runs during the Docker image build, copying
  `dist/` into the shared `runtime` stage; `apps/admin_api/main.py` mounts
  it via `StaticFiles` under a catch-all excluding `/api/*` and
  `/healthz`.
- `apps/admin_api/frontend/` adds Vitest + React Testing Library (the
  standard pairing for a Vite project) as a dev dependency.

**Tests:** `testing.md` is silent on a frontend tier only because no
frontend exists yet in this repo — that silence isn't an exemption, and a
single E2E happy path plus manual verification would leave the PRD's own
substantial branching logic (per-screen loading/empty/error/success
states, Viewer-role control hiding, form validation, session-expiry
mid-action handling) with no automated, CI-gated coverage anywhere in
this plan, inverting the pyramid's "E2E stays few" shape rather than
following it. Each screen gets Vitest + React Testing Library
component-level tests covering its own loading/empty/error/success states
and, where applicable, Viewer-role control hiding and form validation —
the automated layer between "one E2E happy path" and "manual only." The
E2E test below remains the acceptance gate for the full real-backend
flow; these component tests are this plan's answer to the edge cases E2E
alone can't proportionately cover.

**Observability & rollback:** no runtime kill switch — a broken frontend
build fails the Docker build/CI, never reaches a running deployment;
rollback is redeploying the prior image tag.

**Acceptance:** the E2E test (Testing table, below) exercises the real
built frontend against the real backend for the full Phase 5 exit
scenario.

## Phase 9 — Helm chart + docker-compose (Unit J: US-A20, US-A21, US-A22)

**Why next:** needs a real `apps/admin_api` image and its real `/healthz`
surface to validate probes against; the chart/compose shape could be
drafted earlier against a stub per the spec, but placed here so this
phase's tests exercise the real thing, not a stand-in.

- `charts/review/templates/deployment-admin-api.yaml` and
  `service-admin-api.yaml`, mirroring `deployment-webhook-api.yaml`/
  `service-webhook-api.yaml` exactly (same `review.labels`/
  `selectorLabels` helpers, same credential-env-var helper extended with
  the new bootstrap/session/CSRF secrets — `admin_bootstrap_username`/
  `admin_bootstrap_password`, and `admin_csrf_secret` (Assumption 13)),
  **plus** fresh `livenessProbe`/
  `readinessProbe` blocks against `/healthz` (Assumption 10 — no existing
  probe pattern to mirror, added new here).
- `charts/review/templates/ingress.yaml` — brand new (verified no
  `Ingress` template exists in the chart today): routes to both
  `admin-api` and `webhook-api` Services, TLS via `existingSecret`
  reference or cert-manager annotations, mirroring the chart's existing
  `existingSecret` convention (`values.yaml`'s header).
- `charts/review/values.yaml`: populates the top-level `adminApi:` key
  (left empty by Phase 3's removal of its old `apiKey` field) with a
  fresh block mirroring `webhookApi:`'s shape
  (`replicaCount`/`image`/`service`/`resources`) plus its own credential
  fields; new top-level `ingress:` block
  (`enabled`, `className`, `hosts`, `tls`, and an optional
  `annotations: {}` passthrough), defaulting `enabled: true`,
  independently toggleable from `adminApi.enabled`. The values.yaml
  header documents an example nginx-ingress rate-limit annotation
  (e.g. `nginx.ingress.kubernetes.io/limit-rps`) an operator can set via
  `ingress.annotations` on the login path as a defense-in-depth,
  network-layer complement to Phase 1's per-username/aggregate
  application-layer throttling — documented as an available option, not
  a default this plan imposes.
- `docker-compose.yml`: finalize the `admin_api` service block drafted in
  Phase 1 with the real port/healthcheck now that `/healthz` exists.

**Tests:** extend `charts/review/test-render.sh` (the chart's one existing
test convention — grep-based `helm template` assertions; verified no
`helm unittest`/Python renderer exists to introduce instead) with new
assertions: Admin UI Deployment/Service render with probes and resources;
Ingress renders with both backend services and both TLS modes;
`docker compose config` validates the new service.

**Observability & rollback:** `adminApi.enabled: false` and
`ingress.enabled: false` are independently toggleable — an operator not
ready to expose the Admin UI externally can disable Ingress alone without
disabling the service itself.

**Acceptance:** US-A20, US-A21, US-A22's Given/When/Then pass — `helm
template` renders a valid Deployment/Service/Ingress, `docker compose
config` validates the new service.

## Phase 10 — Bulk approve/reject (Unit K, Could: US-A23)

**Why last:** the plan's only optional unit (no roadmap/ADR commitment
forces it); depends on Phase 3's migrated Memory routes; nothing later in
this plan depends on it, so it is safe to cut if time runs out.

- `apps/admin_api/memory_routes.py` gains
  `POST /api/v1/memory-entries/bulk-approve` and `.../bulk-reject`, each
  iterating Phase 3's existing per-entry approve/reject handlers
  individually (not a new bulk-specific domain method), collecting
  per-item success/failure into the response rather than rolling back the
  batch on one failure.

**Tests:** `apps/admin_api/test_memory_routes.py` — bulk-approve of N
entries with one pre-deleted id: N-1 succeed, one reports its own failure,
response shape distinguishes them.

**Observability & rollback:** no kill switch needed — additive UI-level
convenience over already-shipped, already-tested single-entry behavior;
disabling it is simply not rendering the bulk-select UI, zero backend
risk.

**Acceptance:** US-A23's Given/When/Then passes, including the
partial-failure case.

## Security review

- **Login endpoint** (unauthenticated caller, username+password): brute
  force against one username → the dedicated `login_attempt` Postgres
  counter (Phase 1, Assumption 5), decoupled from the general audit log to
  avoid an unbounded table becoming a degradation vector on this hot path
  itself; a distributed low-and-slow campaign across many usernames →
  the unlabeled `review.auth.login_failed_total` aggregate metric (Phase
  1) gives operators a signal the per-username counter can't; an
  external, network-layer volumetric limit is documented as an optional
  Ingress annotation (Phase 9); credential stuffing → generic failure
  message, no username-existence leak (US-A1's second clause); timing
  side-channel between a wrong-password and a nonexistent-username
  response → closed by a dummy constant-cost Argon2 verification on the
  nonexistent-username path (Assumption 14), not just Argon2's own
  constant-time verify within a single comparison; password strength →
  a stated minimum length plus explicit, documented Argon2 cost
  parameters rather than library defaults (Assumption 14). Two residual
  items the `login_attempt` design (Assumption 5) doesn't fully close on
  its own: it's keyed on raw, unauthenticated `username` input with no
  stated retention, so an attacker flooding attempts against many
  nonexistent usernames grows the table indefinitely — Phase 1 adds a
  periodic purge of rows older than a few rate-limit windows, the same
  bounded-retention shape any windowed counter needs regardless of table
  choice. And since the counter has no IP dimension, an attacker who
  knows a real admin's username can lock that admin out indefinitely by
  repeatedly failing their password — a real self-DoS risk given
  self-hosted deployments likely have very few admin accounts; the
  cooldown Phase 1 implements is time-bounded and auto-expiring (not an
  indefinite lock), which bounds this to a temporary denial rather than a
  permanent one, but a longer-term fix (e.g. an IP dimension, or an
  Admin-visible unlock) is not designed here and is left as a follow-on
  hardening item, not silently solved.
- **Session cookie & CSRF:** `HttpOnly`/`Secure`/`SameSite=Lax` blocks JS
  exfiltration and most CSRF vectors for cookie-carried auth, but not all
  of them (Chrome's ~2-minute "Lax+POST" grace window for freshly-set
  cookies, non-compliant clients, a proxy/CDN stripping `SameSite`) — this
  plan mandates a concrete, **session-bound** CSRF token as a required
  Phase 1 deliverable (Assumption 13): an HMAC of the session id, keyed
  by a new dedicated `admin_csrf_secret` setting, carried in its own
  `Secure`/`SameSite=Lax` (non-`HttpOnly`) cookie and echoed back via a
  custom header, checked on every mutating request by the same
  `require_session` dependency every later phase's routes already use —
  not a bare independent-random-value double-submit (which would remain
  open to cross-subdomain cookie-tossing) and not a mitigation choice
  left open at implementation time.
- **Bootstrap admin (Phase 1):** a check-then-insert bootstrap routine
  racing across multiple stateless replicas on a fresh deployment could
  otherwise create duplicate/ambiguous admin rows — closed by wrapping the
  check-and-insert in a single transaction holding a `pg_advisory_xact_lock`
  (Assumption 11), plus an explicit `UniqueConstraint` on `admin_user.username`
  as defense-in-depth for ordinary user creation (Phase 2).
- **Role enforcement:** every mutating endpoint added in Phases 2–7 and 10
  is gated by `require_admin_role`; every read endpoint by `require_session`
  only (Viewer parity) — enforced by a FastAPI dependency, never inferred
  from what the frontend renders (US-A5).
- **Static-token retirement (Phase 3):** the worst residual risk before
  this phase ships is the *old* token remaining valid alongside new
  sessions — closed by fully deleting the route and setting, not layering
  new auth on top of the old one.
- **Config/routing writes (Phases 4, 5):** parameterized ORM writes,
  validated pydantic request bodies, a closed setting-key registry
  rejecting unknown keys — worst case of a bad write is a misconfigured
  stage/setting (422 on invalid values, per NFR), not data corruption or
  injection.
- **Prometheus reader (Phase 6):** `apps/admin_api` makes outbound calls to
  an operator-configured, internal Prometheus base URL; no user-supplied
  input reaches that URL or its query string — all PromQL is
  server-constructed, no raw-query pass-through — closing off PromQL
  injection from an admin session.
- **Cross-tenant exposure:** explicitly and knowingly out of scope per
  ADR-0004's "platform admin sees all orgs" MVP default — every Admin/
  Viewer sees every connected org's data. This is a documented scope
  limitation carried forward from the spec, not an oversight of this plan,
  and should not be raised as a new finding by a later review pass.
- **Secrets:** bootstrap password, the new `admin_csrf_secret` (Assumption
  13 — the CSRF HMAC signing key), and the Prometheus base URL if it ever
  embeds credentials, are all `SecretStr`/Kubernetes-`Secret`-backed,
  never logged. The session token itself needs no signing key — it's an
  opaque, hash-lookup value with no server-verifiable claims of its own
  (Assumption 7) — but the CSRF token is a distinct, HMAC-based mechanism
  and does need one, which is why it gets its own dedicated setting
  rather than reusing session-cookie material.

## Compatibility & rollout

- The one true breaking change is US-A7's static-token retirement (Phase
  3) — a direct cutover, no dual-auth window, justified because no live
  customer exists yet (matches the spec's Rollout & Release Safety
  section verbatim).
- Every new table (Phases 1, 2, 7) and every newly-populated-but-
  previously-scaffolded table (Phases 4, 5) is additive; no existing row
  is migrated or backfilled.
- Phase 5's Worker read-path change is the one production-hot-path change
  with a real rollback lever: `model_routing_config_enabled`, default on.
  **This flag itself requires a `WorkerSettings` env-var change and a
  Worker restart to flip — an earlier draft of this plan incorrectly
  called it "instant"; the "no restart required" guarantee US-A16 asks
  for is about a *routing config change* (an admin editing a
  `ModelRoutingConfig` row) taking effect without a restart, which
  Phase 5's dispatch-time resolution correctly provides.** Separately,
  `FallbackModelRoutingResolver`'s own per-dispatch Postgres read is
  time-bounded and fails open to the static default on error or timeout,
  so a Postgres blip degrades model selection, never job success. **For
  the common incident (a bad routing *value*, not a broken routing
  *subsystem*), the fast fix is reverting the offending
  `ModelRoutingConfig` row via the existing no-restart `PUT
  /api/v1/model-routing/{stage}` (US-A16) — the restart-requiring flag is
  only needed to disable the subsystem entirely**, not the day-to-day
  lever an on-call operator reaches for first.
- **Migration rollback is untested beyond mirroring `0007`'s
  `upgrade()`/`downgrade()` shape.** `0008`'s `downgrade()` drops
  `admin_user`/`admin_session`/`admin_audit_log`/`login_attempt` (all
  sessions and audit history lost); `0009`'s drops the usage-counter
  table. Neither migration's `downgrade()` is expected to run in
  production once real rows exist — this plan doesn't specify an
  operational runbook for that case, and treats it the same way this
  repo's prior migrations already do (forward-only in practice, `alembic
  downgrade` reserved for pre-release development use).
- Phase 7's usage-counter write on the Worker's job-completion path fails
  soft (logged, not raised) so a metering bug can never turn into a
  review-pipeline outage.
- Helm/compose additions (Phase 9) are independently toggleable
  (`adminApi.enabled`, `ingress.enabled`), so a cautious operator can stage
  rollout: ship the image without exposing it externally, verify
  internally, then flip Ingress on.
- **Open deployment-topology item:** Phase 6 introduces `apps/admin_api`'s
  first dependency on reaching a Prometheus instance over the network.
  This plan assumes every self-hosted deployment topology this PRD targets
  can route from the Admin API's pod/container to the same Prometheus
  instance the OTel collector already feeds — a reasonable assumption
  given `docs/specs/spec-observability.md`'s existing pattern, but not
  independently confirmed against every possible customer network
  topology, and should be verified during Phase 6, not assumed silently.

## Testing / Key Scenarios (traceability)

Reproduces `docs/specs/spec-admin-gui.md`'s own Testing / Key Scenarios
table, mapped to this plan's phase numbers.

| Phase | Story | Test path | Level |
| --- | --- | --- | --- |
| 1 | US-A1 | `apps/admin_api/test_auth_routes.py` | Integration |
| 1 | US-A2 | `apps/admin_api/test_auth_routes.py` | Integration |
| 1 | US-A3 | `apps/admin_api/test_main.py` | Integration |
| 1 | US-A5 | `apps/admin_api/test_role_enforcement.py` | Integration |
| 1 | US-A6 | `apps/admin_api/test_auth_routes.py` | Integration |
| 2 | US-A4 | `apps/admin_api/test_user_routes.py` | Integration |
| 3 | US-A7 | `apps/admin_api/test_memory_routes.py` (migrated from `apps/webhook_api/test_admin_routes.py`) | Integration |
| 3 | US-A8–A11 | `apps/admin_api/test_memory_routes.py` | Integration |
| 4 | US-A12 | `apps/admin_api/test_config_routes.py` | Integration |
| 4 | US-A13 | `apps/admin_api/test_config_routes.py` | Integration |
| 5 | US-A14 | `apps/admin_api/test_model_routing_routes.py` | Integration |
| 5 | US-A15 | `apps/admin_api/test_model_routing_routes.py` | Integration |
| 5 | US-A16 | `apps/worker/test_model_routing_integration.py` | Integration |
| 6 | US-A17 | `apps/admin_api/test_stats_routes.py` | Integration |
| 7 | US-A18 | `apps/admin_api/test_usage_routes.py` | Integration |
| 7 | US-A19 | `apps/worker/test_usage_counter.py` | Integration |
| 8 | Phase 5 exit scenario | `tests/e2e/test_admin_ui.py` | E2E |
| 9 | US-A20 | `charts/review/test-render.sh` | Integration (Helm template rendering) |
| 9 | US-A21 | `charts/review/test-render.sh` | Integration (Helm template rendering) |
| 9 | US-A22 | `charts/review/test-render.sh` | Integration (Helm template rendering) |
| 10 | US-A23 | `apps/admin_api/test_memory_routes.py` | Integration |
