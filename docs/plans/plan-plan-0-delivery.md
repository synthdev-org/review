# Plan: Phase 0 — Scaffolding

**Status: Proposed.** Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 0
into buildable, independently-testable steps. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`), plus [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md)
(self-hosted Kubernetes/Helm + Docker Compose) and
[ADR-0002](../adr/0002-implement-in-python.md) (Python/FastAPI/SQLAlchemy/
LiteLLM stack). No project-specific `docs/plans/` format convention exists
yet — this file establishes one using the generic phase/tests/acceptance
shape described below; later plans in this repo should follow the same
shape until a dedicated convention doc says otherwise.

This repo is currently empty of code (only `docs/`, `AGENTS.md`, `CLAUDE.md`
exist) — every phase below is new work, not an extension of anything
existing. There is nothing yet to reuse.

## Scope

**In scope** — every bullet the roadmap lists under Phase 0:
- Repo structure: domain packages (`ingestion`, `indexing`, `review`,
  `llmgateway`, `findings`, `memory`, `tenancy`), adapter packages
  (`adapter/github`, `adapter/analyzers`, `adapter/storage`, `adapter/llm`),
  two entrypoints (`apps/webhook_api`, `apps/worker`).
- CI: `ruff` + strict type checker gate, unit test run on every push.
- Postgres schema baseline (Alembic): org/repo/team tables, job queue
  (`SKIP LOCKED`-style), bootstrap config tables.
- LLM Gateway skeleton: LiteLLM wired in, one provider configured
  end-to-end.
- GitHub App skeleton: webhook signature verification, installation
  resolution, App manifest/callback.
- Docker Compose stack for local dev (Webhook API + Worker +
  Postgres/pgvector, LLM Gateway pointed at local LM Studio) — the same
  images Kubernetes runs in production.
- Helm chart skeleton: `ConfigMap`/`Secret` rendering, `existingSecret`
  support, `postgresql.enabled` toggle.

**Out of scope** — explicitly deferred to later roadmap phases, not
rejected:
- Any actual review capability (bare-diff review, LLM-generated findings,
  posted PR comments/check-runs) — Phase 1 ("Walking skeleton").
- Real indexing/RAG, verification pipeline, Memory, admin UI, multi-org
  tenancy, knowhow augmentation, production-grade Helm hardening — Phases
  2–8, per the roadmap.
- Static analysis runner integration — explicitly a Phase 1 item in the
  roadmap ("ships before RAG sophistication"), not Phase 0.

**On `testing.md`'s PRD/spec → user story → integration test traceability
requirement:** this plan sequences infrastructure scaffolding, not
user-facing behavior — there are no `As a/I want/so that` user stories at
this phase, so no traceability table applies here. The first roadmap phase
with real user stories is Phase 1 ("Walking skeleton"), whose own plan
should carry that table.

## Assumptions this plan makes (flagged, not decided)

Neither of these is fixed by any doc in `docs/` — both are ordinary,
reversible implementer choices, not architecture decisions, so this plan
makes a call rather than blocking on it:

- **Python dependency/build tool: `uv`.** Single lockfile, fast installs,
  and good Docker layer-caching behavior for a multi-stage build. Poetry or
  pip-tools would work equally well; nothing in `architecture.md` or the
  ADRs mandates one over another. Easy to swap later — it only affects
  `pyproject.toml`/lockfile mechanics, not any package boundary.
- **CI platform: GitHub Actions.** The repo's remote is
  `github.com/mystery-digital/review` and the product itself is a GitHub
  App — GitHub Actions is the path of least friction (no separate CI
  account/credentials to provision) and is what `roadmap.md`'s CI bullet
  most naturally targets. Swapping to another CI platform later only
  touches `.github/workflows/`, not application code.

## Phase 1 — Repo & package scaffold, dev tooling, CI

**Why first:** every later phase writes code into packages this phase
creates; CI must exist before any phase can be merged with an enforced
quality gate rather than a manual check.

- `pyproject.toml` (uv-managed), Python version pin, `ruff` config (lint +
  format) and strict `mypy` (or `pyright`) config per
  [`architecture.md`](../rules/architecture.md)'s "Idiomatic Python"
  section.
- Empty (but importable) domain packages: `ingestion/`, `indexing/`,
  `review/`, `llmgateway/`, `findings/`, `memory/`, `tenancy/` — each with
  an `__init__.py` and a one-line docstring naming its bounded context, no
  infra imports per the architecture rule.
- Empty adapter packages: `adapter/github/`, `adapter/analyzers/`,
  `adapter/storage/`, `adapter/llm/`.
- Two entrypoint skeletons: `apps/webhook_api/main.py` (FastAPI app object,
  no routes yet beyond `/healthz`), `apps/worker/main.py` (process
  skeleton, no job dispatch yet). Config parsed once per entrypoint via
  `pydantic-settings`, per the architecture rule — even a near-empty
  settings model at this stage, so later phases extend it rather than
  introducing it.
- `.github/workflows/ci.yml`: `ruff check` + `ruff format --check` +
  `mypy --strict` (or `pyright --strict`) as one gated job, `pytest`
  (unit-only — no `-m integration`) as another, both running on every push
  per [`testing.md`](../rules/testing.md)'s CI-gating section. This
  workflow gains further jobs in later phases as this plan introduces
  higher test tiers — a `pytest -m integration` job on every PR (Phase 2,
  the first phase with an integration test) and an E2E job on merge to
  `main` (Phase 5) — matching `testing.md`'s per-tier gating rule rather
  than trying to stand up all three tiers before there's anything to test
  at them.
- A shared logging setup (stdlib `logging` configured for structured/JSON
  output, level driven by each entrypoint's settings) — configured
  independently at each entrypoint's own composition root
  (`apps/webhook_api/main.py`, `apps/worker/main.py`), not factored into a
  new `shared`/`common`/`utils` package, per `architecture.md`'s "no
  common/shared/utils dumps" rule. Later phases (3, 4, 5) add log call
  sites at meaningful events; none of them stand up a logging framework of
  their own — this is the one place it's configured.

**Tests:** Unit only at this stage — there is no behavior yet beyond
config parsing. A `test_settings.py` per entrypoint asserting the
pydantic-settings model rejects missing required fields and accepts a
minimal valid `.env`-shaped input. A unit test on the shared logging setup
asserts a log call produces structured (JSON, not bare-string) output and
that the level actually follows the settings-provided log-level value.
This is also the phase that proves the CI gate itself works: a
deliberately failing lint/type/test case in the PR that introduces
`ci.yml`, confirmed to fail the check, then fixed.

**Observability & rollback:** N/A at this stage — no deployed component
yet, nothing to roll back at runtime. Rollback of this phase itself is a
plain revert (no schema, no running service).

**Acceptance:** `ruff check .`, `mypy --strict .` (or `pyright`), and
`pytest` (including the settings and logging unit tests above) all pass
locally and in a fresh GitHub Actions run against a no-op commit; `python
-c "import ingestion, indexing, review, llmgateway, findings, memory,
tenancy"` succeeds with zero infra imports pulled in (verified by the type
checker seeing no `fastapi`/`sqlalchemy`/`httpx`/
`litellm` symbols inside domain package source).

## Phase 2 — Postgres schema baseline (Alembic)

**Why here:** the job queue, org/repo/team tables, and config-table
scaffolding are a shared dependency of the Docker Compose stack (Phase 5)
and every later roadmap phase's persistence needs — building it once,
early, avoids every later phase re-deriving its own ad hoc schema. Phase
4's GitHub App work does *not* depend on this phase — its installation
resolution is payload parsing only, not a DB lookup (see Phase 4) — so the
two phases are independently parallelizable.

- `adapter/storage/` package: SQLAlchemy models + Alembic migration
  environment, wired to async SQLAlchemy per the architecture rule ("I/O is
  async"). The job queue's claim query uses SQLAlchemy's native
  `with_for_update(skip_locked=True)` rather than hand-rolled raw SQL —
  the ORM/Core primitive ADR-0002 already put in the stack for exactly
  this claim pattern.
- Baseline tables: `org`, `repo`, `team` (tenancy scaffolding — full
  RBAC/scoping is Phase 6 (roadmap) — not this plan's own Phase 6, which is
  the Helm chart skeleton below — but the tables exist now so the job
  queue and config tables below have somewhere to point their foreign
  keys), a job
  queue table shaped for `SKIP LOCKED`-style claiming (per the C4 doc's Job
  Queue container), and config-table scaffolding for **day-two platform
  config only** (model routing, per-org/repo/team setting placeholders).
  Per the C4 doc's "Configuration & Secrets" split, GitHub App and LLM
  *credentials* are bootstrap config — read once via `pydantic-settings`,
  sourced from Helm `Secret`/`existingSecret` in production or `.env`
  locally — and are never written to these tables; Phase 4 below is
  designed around that split.
- A startup check confirming the `pgvector` extension is present — the C4
  doc requires this to fail fast rather than surface an obscure query
  failure later. Phase 5's Docker Compose health checks call this check as
  part of what "healthy" means for the stack, giving it a real consumer
  within Phase 0's own scope.
- CI: extend `.github/workflows/ci.yml` with a `pytest -m integration` job
  against a `testcontainers`-managed Postgres, gated on pull requests, per
  `testing.md`'s CI-gating rule — this is the first integration-tier test
  in the plan, so it's the natural phase to add the job. Because Phases 3
  and 4 are independently parallelizable with this phase and each adds its
  own integration test, this job must merge no later than the first of
  Phase 3/4 — whichever of the three lands first should include it, rather
  than leaving another phase's integration test temporarily ungated on PRs.

**Tests:** Integration (per `testing.md`'s middle tier): a
`testcontainers-python`-backed real Postgres, running `alembic upgrade
head` from empty and asserting the resulting schema has the expected
tables/columns, then `alembic downgrade base` to prove the migration is
reversible. A `SKIP LOCKED` claim test: two concurrent claimers against a
fixture-seeded queue table never both claim the same row. A negative test
confirms the pgvector-presence check raises a specific, typed exception
(not a raw driver error) against a fresh Postgres instance without the
extension installed. Unit tests for any pure Python (e.g.
queue-row-to-pydantic-model mapping) live in `adapter/storage/`, marked
non-integration.

**Observability & rollback:** Every migration must have a working
`downgrade()` — the acceptance criterion below enforces this directly. No
running service yet at this phase, so no live rollback/runbook is needed;
the "rollback" here is Alembic's own downgrade path, which the test above
already exercises.

**Acceptance:** `alembic upgrade head` and `alembic downgrade base` both
succeed against a fresh `testcontainers` Postgres/pgvector instance in CI
(added to the integration test job); the `SKIP LOCKED` concurrent-claim
test passes (two concurrent claimers never claim the same row); the
pgvector-presence check returns a clear, typed error (not a raw driver
exception) when the extension is missing.

## Phase 3 — LLM Gateway skeleton

**Why here:** independent of the schema and GitHub App work (no shared
code with Phases 2 or 4), so it can be built in parallel with either, but
comes before Phase 5 (Docker Compose) since the compose stack's exit
criteria depends on the gateway being callable.

- `adapter/llm/` package: LiteLLM wired in behind a small
  `typing.Protocol` (e.g. `LLMClient.complete(...)`) defined in
  `llmgateway/` per the architecture rule ("interfaces defined at the
  consumer"), so `llmgateway/` itself has zero LiteLLM/`httpx` import.
- One provider configured end-to-end for local development: LM Studio via
  its OpenAI-compatible endpoint (`http://host.docker.internal:1234`, per
  the C4 doc's Level 3 diagram) — this is deliberately the *local* provider
  first, since Phase 0's exit criteria is a Docker Compose boot, not a
  production LLM call. Every call through the `LLMClient` Protocol
  (including the startup connectivity check below) carries an explicit
  timeout, per `architecture.md`'s "every I/O call takes a timeout and
  respects cancellation" rule.
- Bootstrap config extended (from Phase 1's near-empty settings model) with
  the LLM endpoint/credential fields the C4 doc's "Configuration & Secrets"
  section describes.

**Tests:** Integration: a recorded-cassette or live-mode (flagged, not
default per `testing.md`) call through the LiteLLM router to a real LM
Studio instance, asserting a completion comes back in the expected shape.
A negative integration test asserts the adapter surfaces a specific, typed
error (not a raw `httpx`/LiteLLM exception) when LM Studio is unreachable
or returns a malformed response — the first external-I/O failure mode in
the plan, called out explicitly since `testing.md` names LLM timeout as a
failure mode that matters. Unit: the `LLMClient` Protocol's fake
implementation is exercised by any Phase 0 code that depends on it (none
yet — this is groundwork for Phase 1) plus a unit test that config
validation rejects a malformed endpoint URL.

**Observability & rollback:** Structured logging around the LiteLLM call
boundary (request initiated, latency, provider/model used, error class) —
this is the seam every later pipeline stage's cost/latency visibility
depends on, so establishing the logging shape now avoids retrofitting it
once Phase 1–3 (roadmap numbering) pipeline stages start calling through
it. A startup connectivity check (same fail-fast pattern as Phase 2's
pgvector check) makes one low-cost call through the configured LLM
endpoint at Worker startup, logs a clear error, and **exits the Worker
process non-zero** if unreachable — fail-fast, matching Phase 2's pgvector
check, rather than continuing to run in a silently-broken state. A
negative test confirms this exit behavior. No user-facing rollback concept
yet beyond redeploying with a corrected endpoint config.

**Acceptance:** A local `docker compose run` (or equivalent standalone
invocation) against a running LM Studio instance returns a real completion
through the `LLMClient` Protocol; both the happy-path and the negative
(LM-Studio-unreachable and malformed-response) integration tests pass in
CI against recorded cassettes (no live network dependency in the default
CI run, per `testing.md`'s LLM-determinism rule); the Worker process exits
non-zero when the startup connectivity check fails.

## Phase 4 — GitHub App skeleton

**Why here:** depends on Phase 1's `apps/webhook_api` entrypoint and
`adapter/github/` package existing. It does not depend on Phase 2 or Phase
3 — the manifest/callback route hands credentials to the operator rather
than persisting them (see below), so it never touches Phase 2's tables,
and installation-ID resolution here is payload parsing only, not a DB
lookup. That keeps this phase genuinely parallelizable with Phase 3, both
finishing before Phase 5 needs them wired together.

- `adapter/github/` package: webhook signature verification and the
  GitHub App manifest flow (the redirect/callback GitHub's App-creation UI
  uses). Signature verification uses stdlib `hmac.compare_digest` for a
  constant-time comparison against the computed HMAC-SHA256 digest — a
  plain `==` comparison is a timing-attack vector that lets an attacker
  recover a valid signature byte-by-byte.
- `apps/webhook_api` gains its first real route: a webhook receiver that
  verifies the signature and returns 401/200 accordingly, with a request
  body size limit and basic rate limiting in front of it — this is the
  platform's first unauthenticated, externally-reachable endpoint, so
  resource exhaustion (oversized or high-frequency POSTs arriving before
  signature verification even runs) is in scope for this phase, not
  deferred to Phase 8 hardening. The rate limit is per-instance (in-process
  counter) — adequate for Phase 0's single-Webhook-API-pod Docker Compose
  target; a shared/distributed limiter for multi-replica production
  deployments is a Phase 8 (roadmap) hardening concern, noted here so it
  isn't mistaken for already solved. Installation-ID resolution at this
  phase is payload parsing only — no DB lookup, no persisted mapping — and
  the route intentionally does *not* enqueue a real review job yet (that's
  Phase 1 of the roadmap); Phase 0's job here is proving the trust
  boundary, not the pipeline.
- The App manifest/callback route exchanges GitHub's temporary code for
  the App ID, private key, and webhook secret (via an explicitly
  timeout-bound HTTP call, per `architecture.md`'s I/O rule), then returns
  them to the operator to place into their own bootstrap config (the local
  `.env` for Docker Compose, or the Helm `Secret`/`existingSecret` for
  production) — it does not write them into Postgres, matching the C4
  doc's "Configuration & Secrets" split (GitHub App credentials are
  bootstrap config, and Postgres is reserved for day-two platform config
  only; see Phase 2). CSRF
  protection uses the standard OAuth "state" pattern: the initiation route
  generates a random state value, HMAC-signs it with a timestamp using a
  dedicated setup secret, and sets it as a short-TTL (e.g. 10-minute),
  `HttpOnly`, `SameSite=Lax` cookie in the redirect to GitHub (`Lax`, not
  `Strict`, because the cookie must survive GitHub's top-level redirect
  back to the callback route); the callback route validates
  the returned `state` query parameter against the signed cookie value and
  its expiry using the same `hmac.compare_digest` pattern as webhook
  verification. This needs no persistence (consistent with the "no
  Postgres touch" design above) — "replay" is bounded by the TTL and by the
  value being single-use within one operator's browser session, the same
  guarantee a cookie-bound OAuth state token provides in the standard
  pattern this mirrors. The manifest-initiation and callback routes are
  both gated behind an explicit `SETUP_MODE` config flag, off by default —
  a one-time install-time flow the operator enables only for the duration
  of App setup (and is expected to run from a trusted network reach, e.g.
  a port-forward or VPN, while enabled — Phase 0 doesn't add
  network-level scoping of its own, just the flag), not a standing
  production endpoint any caller could otherwise trigger to provision
  attacker-supplied App credentials.

**Tests:** Unit: signature verification rejects a tampered payload, a
missing signature header, and a signature computed with the wrong secret
(exercised directly against the `hmac`/`hashlib`-based verification
function, no HTTP layer involved); accepts a validly-signed payload.
Integration (driven through the actual `apps/webhook_api` route via
FastAPI's in-process `TestClient`, which opens no real network socket but
does exercise routing/request-parsing beyond the pure verification
function above): the same three failure cases plus the happy path
returning 200/401; a malformed-JSON or missing-installation-ID payload
that still carries a valid signature returns a clean 4xx with a typed
error rather than an unhandled 500; an oversized body and a
rate-limit-exceeding burst are both rejected before signature verification
runs. The manifest/callback flow is exercised against a stubbed GitHub
OAuth-style callback (recorded fixture/`respx`, per `testing.md`), covering
both the happy path (valid, unexpired, cookie-matching state, returns
credentials to the operator) and the negative path (a missing state
parameter, a state that doesn't match the signed cookie, and an expired
state are each rejected before any GitHub API call is made). E2E is out of
scope here — a real GitHub App creation flow requires a real GitHub org and
is exercised manually during Phase 0's own acceptance, not automated in
CI.

**Observability & rollback:** Every rejected webhook (bad signature,
missing header, oversized body, rate-limited) is logged at `warning` with
the rejection reason (never the payload body, which may carry sensitive
repo/org data) — this is the platform's first externally-reachable
endpoint, so audit-grade logging of rejections starts here, not
retrofitted after an incident. The manifest/callback flow gets the same
treatment: rejected state/nonce attempts and successful credential
exchanges are both logged (the latter without ever logging the private
key/secret values themselves) — a sensitive one-time action deserves an
audit trail as much as the higher-volume webhook route does. No rollback
concept beyond redeploying a prior image or turning `SETUP_MODE` back off;
this route holds no persisted state of its own.

**Acceptance:** All of Phase 4's Tests pass in CI: a synthetic webhook
payload signed with a known test secret is accepted (200) and one signed
with the wrong secret, a missing signature header, an oversized body, or a
rate-limit-exceeding burst are each rejected before enqueue logic would
run; a validly-signed but malformed/incomplete payload returns a clean 4xx
rather than a 500. The manifest/callback route, driven manually against a
real (non-production) GitHub App during this phase's own verification with
`SETUP_MODE` enabled, successfully returns App credentials for the
operator to place into local `.env`; a missing, mismatched, or expired
state parameter is each rejected without calling GitHub's API.

## Phase 5 — Docker Compose stack

**Why here:** this is the integration point — it wires Phases 1–4 together
into the thing the roadmap's Phase 0 exit criteria actually names. It
cannot start meaningfully before those four exist, since there would be
nothing running to compose.

- `docker-compose.yml`: `webhook_api`, `worker`, `postgres` (with
  `pgvector` extension pre-installed in the image) services, `worker`
  configured to point its `LLMClient` at `host.docker.internal:1234` (LM
  Studio) per the C4 doc.
- `.env.example` documenting every bootstrap config value Phases 2–4
  introduced (Postgres DSN, GitHub App credentials, LLM endpoint) — per
  the C4 doc's note that Compose reads local config from a plain `.env`
  file, with a developer free to source it from their own tool.
- Compose-level health checks so `docker compose up` genuinely blocks until
  all three services are ready (Postgres accepting connections, Alembic
  migrations applied and Phase 2's pgvector-presence check passing, Webhook
  API's `/healthz` returning 200), not just "containers started." If
  `alembic upgrade head` fails, the affected service exits non-zero with
  the migration error visible in `docker compose logs`, rather than
  looping silently or reporting a misleading "started" state.
- `.github/workflows/e2e.yml` (or an added job in `ci.yml`): runs the
  Compose E2E test below on merge to `main` (and optionally nightly), per
  `testing.md`'s CI-gating rule — E2E is the one test tier not yet wired
  into CI by an earlier phase.

**Tests:** E2E (per `testing.md`'s top tier, first exercised here): `docker
compose up`, then assert `apps/webhook_api`'s `/healthz` returns 200,
`apps/worker` is running without crash-looping, and `alembic upgrade head`
has been applied to the compose Postgres instance. This is the literal
acceptance test for the roadmap's Phase 0 exit criteria, so it belongs in
`tests/e2e/` per `testing.md`'s convention even though "review capability"
doesn't exist yet to exercise end-to-end. A second E2E case forces a
migration failure (e.g. an invalid `DATABASE_URL`) and asserts the failure
is visible in `docker compose logs` rather than a silent hang.

**Observability & rollback:** `docker compose logs` for all three services
must show structured (not bare-print) startup logs, since this is the
first point a developer or the CI E2E job diagnoses a boot failure from
logs alone. Rollback is `docker compose down` — no persistent volume
retained by default in the dev compose file, so "rollback" here is just
"start clean," which is the desired dev-loop behavior, not a gap.

**Acceptance:** `docker compose up` from a clean checkout (no pre-existing
volumes) reaches a state where all three services report healthy and no
service has restarted/crash-looped, matching the roadmap's Phase 0 exit
criteria verbatim: "a Webhook API + Worker + Postgres that boots cleanly
with no review capability yet." The migration-failure E2E case also
passes: given an invalid `DATABASE_URL`, the affected service exits
non-zero and the error is visible in `docker compose logs` rather than a
silent hang.

## Phase 6 — Helm chart skeleton

**Why last:** explicitly not required for the roadmap's Phase 0 exit
criteria (which only names Docker Compose) and the C4 doc says it
"doesn't need to be production-grade yet, needs to exist" — sequenced last
because it's the one Phase 0 deliverable that isn't a dependency of
anything else in this plan, so it doesn't block Phase 1 (roadmap) work
from starting once Phase 5 lands.

- Chart skeleton (`Chart.yaml`, `values.yaml`, templates) rendering the
  same two deployables (`webhook-api` Deployment, `worker` Deployment) plus
  a `postgresql.enabled` toggle: `true` bundles a Postgres+pgvector
  subchart for quick trials, `false` takes externally-managed connection
  details, per the C4 doc. `values.yaml`'s default is `true` (fast-eval
  path); a separate `values-production.yaml` profile sets it to `false`,
  matching the C4 doc's Level 2 note that the bundled subchart "default[s]
  off in the production values profile."
- `ConfigMap` (non-secret bootstrap values) and `Secret` rendering for
  GitHub App credentials, LLM endpoint credentials, and DB connection
  secrets — sourced from `values.yaml` by default.
- `existingSecret` name/key support for every credential field, per the C4
  doc's secret-backend-agnostic design goal — this is the one Phase 0 Helm
  requirement with real design weight (get the field shape wrong here and
  every customer secret-backend integration breaks later), so it's called
  out explicitly rather than left implicit in "chart skeleton."

**Tests:** `helm lint` and `helm template` (rendering with the default
`values.yaml`, again with `values-production.yaml` layered on top, and
again with `postgresql.enabled: false` plus an `existingSecret` reference)
as a CI job — this is the Helm-equivalent of the pyramid's unit tier: no
real cluster involved, just template correctness. The
`values-production.yaml` render is asserted to produce
`postgresql.enabled: false`, confirming the production-default requirement
above rather than leaving it unverified. No integration or E2E tier for
the chart in Phase 0 (a real `helm install` against a test cluster is
reasonable for Phase 8's "production hardening," not here).

**Observability & rollback:** N/A for a chart skeleton with no real
workload logic yet beyond what Phase 5's images already log — health
probes wired to real endpoints are explicitly deferred to Phase 8 per the
roadmap. Rollback is `helm uninstall`/`helm rollback`, both available for
free once the chart exists; nothing this phase adds needs a bespoke
rollback procedure.

**Acceptance:** `helm template` succeeds and produces a `Secret` manifest
sourced from `values.yaml` in one run and from a referenced
`existingSecret` (no literal credential in the rendered output) in
another; rendering with `values-production.yaml` layered on top produces
`postgresql.enabled: false`; `helm lint` reports no errors.

## Security review

Phase 0's real attack surface is narrow but real — it's the first phase
with an externally-reachable endpoint and the first phase handling
credentials, so it gets a pass here rather than being folded thinly into
each phase above:

- **Webhook signature verification (Phase 4) is the platform's first
  trust boundary.** Any request without a valid HMAC signature against the
  configured webhook secret must be rejected before any other processing
  runs, using `hmac.compare_digest` for a constant-time comparison (a plain
  `==` comparison is a timing-attack vector) — verified directly by Phase
  4's unit tests. Failure mode if this regresses: an attacker can forge PR
  events, and once Phase 1 (roadmap) adds real job enqueueing, forge
  reviews or trigger arbitrary indexing against a repo they don't control.
- **Resource exhaustion on the webhook receiver.** As the platform's first
  unauthenticated, externally-reachable endpoint, it needs a request body
  size limit and basic rate limiting in front of signature verification
  itself (HMAC computation requires reading the full body) — otherwise an
  unauthenticated caller could send arbitrarily large or high-frequency
  POSTs to exhaust the Webhook API pod's memory/CPU. Phase 4 adds both.
- **Credential handling.** GitHub App private key, webhook secret, and any
  LLM API key are bootstrap config (`pydantic-settings`) and must never
  appear in logs — `SecretStr` (or equivalent) field types prevent
  accidental `repr()`/log-line inclusion. Phase 4's manifest flow returns
  freshly-created credentials to the operator rather than persisting them
  anywhere itself, so there is no second, DB-resident copy of the private
  key to separately secure.
- **Manifest-flow gating (Phase 4).** The manifest-initiation and callback
  routes are both gated behind an explicit `SETUP_MODE` flag, off by
  default, precisely because there is no admin authn/authz surface yet
  (that's Phase 5 of the roadmap) — without this gate, any network caller
  who can reach `webhook_api` could initiate and complete the App creation
  flow themselves and receive a real App's credentials. The state/nonce
  check on the callback additionally stops the callback leg being forged
  or replayed independently of who initiated the flow.
- **Job queue claiming (Phase 2) has no injection surface** beyond normal
  SQLAlchemy parameterized queries (`with_for_update(skip_locked=True)`,
  not raw SQL) — called out explicitly to confirm it was considered, not
  because a gap was found.
- **No authn/authz on admin/config endpoints yet**, and none is needed by
  anything Phase 0 ships beyond the `SETUP_MODE`-gated manifest flow above
  — full admin-API RBAC is tracked at its existing roadmap phase (5), not
  silently assumed to already exist here.
- **Webhook replay.** Not addressed in Phase 0 — no job enqueueing exists
  yet for a replay to affect — but flagged forward: Phase 1 (roadmap), which
  reuses this phase's verification code unmodified to enqueue real jobs,
  should dedupe on GitHub's `X-GitHub-Delivery` header before this becomes
  a real vector.
- **Secrets never committed.** `.env.example` (Phase 5) must contain only
  placeholder values; a `.gitignore` entry for `.env` itself is part of
  Phase 1's scaffold, not an afterthought.

## Compatibility & rollout

Greenfield repo — there is no existing API, schema, or customer
installation to preserve compatibility with. The compatibility concern
here runs forward, not backward:

- **The Alembic migration baseline (Phase 2) is the schema-versioning
  contract every later roadmap phase builds on.** Every phase from here
  forward adds migrations, never edits Phase 0's baseline migration in
  place — get the `downgrade()` discipline right now (enforced by Phase
  2's acceptance test) so it's a established habit, not a retrofit, once
  Phase 4 (roadmap)'s Memory schema and Phase 6 (roadmap)'s tenancy schema
  land on top of it.
- **The Helm chart's `values.yaml` shape (Phase 6) is a customer-facing
  contract the moment anyone installs it**, even as a skeleton. Since
  Phase 8 ("Production hardening," per the roadmap) is explicitly where
  chart maturity work happens, document this chart's `values.yaml` as
  pre-1.0/unstable (e.g. a comment at the top of `values.yaml` and a note
  in whatever README covers local setup) so a breaking values-schema
  change before Phase 8 doesn't read as an unannounced compatibility
  break to an early adopter.
- **Dependency-failure behavior for Phase 0's two live routes.** Neither
  the webhook receiver nor the manifest/callback route touches Postgres at
  request time — installation resolution is payload parsing only, and
  manifest credentials are handed to the operator, not persisted (see
  Phase 4). The manifest/callback route does call GitHub's API to exchange
  the temporary code; if that call fails or times out, the route returns a
  clear, typed error to the operator rather than hanging — a setup-time-only
  failure with no production blast radius, since it only ever runs with
  `SETUP_MODE` enabled. The only genuine dependency-being-up concerns in
  Phase 0 are LM Studio (dev-only, Phase 3, already covered by its own
  fail-fast connectivity check) and Postgres (a hard dependency by design
  per ADR-0001, exercised at boot by Phase 5's compose health checks, not
  by either live route above). Graceful degradation under partial outage
  for real request paths is a Phase 1+ (roadmap) concern once the review
  pipeline itself exists.
