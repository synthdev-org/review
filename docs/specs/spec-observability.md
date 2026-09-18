# Observability & Telemetry PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-10

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 8 "observability hooks"
bullet into a concrete spec — but, per this PRD's own scoping (see
"Phases / Units" and "Related"), it does not actually depend on Phase 8's
placement in the roadmap and is scheduled independently of it. Built
against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and
[`docs/rules/testing.md`](../rules/testing.md) (verification tag `RQPTVE`);
requirements are stated as user stories with `Given/When/Then` acceptance
criteria per `testing.md`'s mandatory rule.

This spec **replaces** the "no metrics/dashboard system exists in this repo
... observability here is structured logs only" statement carried in
`spec-review-pipeline.md`, `spec-indexing.md`, and `spec-context-builder.md`
(each written before this PRD existed). Those three statements should be
updated to point here once this is accepted — flagged in Related, not
silently edited by this PRD.

## Problem & Goal

**End user: the platform operator** — the person on the customer's own
platform/SRE/security team who deployed this platform per
[ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md) and now has to
answer, without SSH-ing into a pod and reading JSON log lines: *is review
healthy, is it keeping up with PR volume, and how much of my BYO LLM spend
is it burning?* Today that operator has genuinely no way to answer any of
those three questions except `kubectl logs` / `docker compose logs` and
manual JSON-line reading — every pipeline stage already computes exactly
the numbers that would answer them (job counts, per-step duration, LLM
token usage, findings counts, verification outcomes — see the file:line
citations throughout Requirements below) and then discards every one of
them the moment the job finishes. ADR-0001 itself names this cost
explicitly: *"we lose direct visibility into production behavior across
customers... support and debugging depend more on what the customer can
report... than on us observing a fleet we run."* The operator is the only
one who can see it — and today, they can't either.

**Secondary end user: engineering leadership at the customer**, who need
LLM spend visibility to answer "is this review platform costing us more
than it's worth" — the same BYO-key-spend concern platform ADR 0003 and
this repo's own design goal 3 already flag as a real, customer-borne cost
review never gets to hide behind.

**Goal / success metric:** capability-based, matching this repo's existing
pattern (`spec-review-pipeline.md`'s own goal is likewise capability-, not
adoption-metric-based) — an operator who deploys this platform and points
`OTEL_EXPORTER_OTLP_ENDPOINT` at their own collector gets, with zero
additional code, a working Grafana dashboard rendering real pipeline
health, latency, LLM spend, and finding-quality data. No usage/adoption
metric is set by any project document; this is operator-facing
infrastructure, not a metered feature.

## Scope

### In scope

- **OpenTelemetry metrics and distributed traces**, exported via OTLP,
  covering:
  - Job queue / worker health (queue depth, wait time, duration, retries,
    failure rate).
  - Review pipeline stage duration and outcome, as one trace per review job
    with a child span per macro step.
  - LLM token usage (prompt/completion/total) by pipeline stage and model —
    raw counts only, no currency conversion (see Priority below).
  - Findings counts by severity × source, and counts dropped by the posted-
    findings ceiling.
  - Verification funnel (judged / rejected / unmatched).
  - Context Builder outcome (success / degraded-empty / degraded-error),
    item count, bytes, duration.
  - Prefilter funnel (total files → pattern-filtered → ceiling-dropped →
    final).
  - Webhook rejection counts by reason.
  - Indexing job health (files processed/skipped/failed, chunks/edges
    created, duration) as metrics **and** a trace.
  - A per-entrypoint liveness heartbeat (US14), independent of job
    activity — the signal that distinguishes "healthy, nothing to report"
    from "telemetry itself is broken."
- **A shipped Grafana dashboard** (JSON, committed to this repo), covering
  every metric above, distributed two ways: as a plain file for manual
  import, and as a Helm `ConfigMap` using Grafana's sidecar
  auto-discovery label convention so it appears automatically in a
  customer's existing Grafana if they use that convention.
- **A no-op default.** With no `OTEL_EXPORTER_OTLP_ENDPOINT` configured, no
  SDK/exporter is installed at all — zero overhead, zero connection
  attempts, zero log noise — matching the "optional, never a dependency"
  pattern [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)
  already established for optional external product-knowledge augmentation.
- **An optional local demo path**: a new `observability` Docker Compose
  profile bundling an OTel Collector, Prometheus, and Grafana (pre-loaded
  with the shipped dashboard) — opt-in via `docker compose --profile
  observability up`, leaving the default `docker compose up` exactly as
  Phase 0 left it (Webhook API + Worker + Postgres, no review capability
  assumed by this PRD either).
- **A guard against dashboard/metric name drift**: an automated check that
  fails if the shipped dashboard JSON references a metric name that
  doesn't exist in the instrumented code, so the two can't silently drift
  apart after a rename.

### Out of scope (deferred)

- **A dashboard screen inside review's own Admin UI.** Explicitly decided
  against for this PRD — see the architecture-fork decision recorded in
  "Resolved decisions" below. Would require new Postgres persistence
  (`review_run`/`finding`/token-usage tables, none of which exist today)
  and Phase 5's Admin UI (not built) — a materially larger, separate piece
  of work if it's ever wanted.
- **SSO/RBAC-scoped dashboard access** —
  [`ADR-0008`](../adr/0008-open-core-licensing-sso-and-cross-org-rbac-as-tier-1.md)'s
  Tier 1 territory, and not a dependency of this PRD: the shipped dashboard
  lives in the customer's *own* Grafana, which has its own access control
  entirely outside this platform's boundary.
- **OTEL logs**, or replacing the existing structured JSON logger
  (`apps/*/logging_config.py`). That logger already works; nothing in this
  codebase suggests it's inadequate, and reworking it is a separate
  decision this PRD doesn't need to make.
- **Cost-in-currency estimation.** No price-per-token table exists or is
  designed anywhere in this codebase; v1 exports raw token counts only
  (see "Resolved decisions").
- **Org/repo/team-sliced historical drill-down / audit-trail queries**
  ("show me every rejected finding for repo X last month"). OTEL metrics
  give aggregate, time-series data via the customer's own Prometheus
  retention — not ad-hoc row-level historical queries. That granularity
  needs Postgres persistence, out of scope per the above.
- **A vendor-hosted or bundled-by-default observability backend.** review
  never runs Grafana/Prometheus on the customer's behalf in production —
  only exports data to whatever the customer already runs, or the
  Compose-only local demo profile above.
- **Auto-instrumentation of FastAPI/SQLAlchemy/httpx** (e.g.
  `opentelemetry-instrumentation-fastapi`). Would add spans review doesn't
  control the shape of and hasn't validated for secret-safety (see the
  Privacy/Compliance NFR below) — only the explicit, hand-placed spans and
  metrics listed in Requirements are in scope for v1.

### Priority

- **Must have:** OTel SDK/exporter wiring with a true no-op default, plus
  the US14 heartbeat (Unit A) — the heartbeat is Must, not Should, because
  without it the dashboard cannot distinguish "healthy, nothing to report"
  from "telemetry itself is broken," which defeats the Problem & Goal's
  own stated aim of not falling back to log-reading; review-pipeline
  instrumentation — stage duration/outcome, one trace per job, LLM token
  counts, findings counts, verification funnel, context builder outcome,
  prefilter funnel (Unit B); worker/queue metrics and webhook rejection
  counters (Unit C); the shipped Grafana dashboard plus the dashboard/
  metric drift guard (Unit E); the Compose `observability` profile and its
  E2E proof that emitted telemetry actually reaches the dashboard (Unit
  F) — without Unit F, nothing in this PRD is verifiably true rather than
  aspirational JSON.
- **Should have:** indexing job metrics and trace (Unit D) — real value,
  but a customer with a healthy index and an unhealthy review pipeline
  still gets the platform's primary value signal without it.
- **Could have:** nothing currently deferred to Could within this PRD's own
  scope — the items that would otherwise be Could (cost-in-currency,
  Admin-UI screen, historical drill-down) are cut to Out of scope entirely
  rather than Could, since each needs real additional design work this PRD
  doesn't do (see Out of scope).

## Requirements

Each `US*` cites the file:line where the underlying data is already
computed today (and currently discarded), per this repo's existing
convention of grounding requirements against real code rather than
aspirational design.

- **US1 — Queue/worker health.** As the platform operator, I want job
  queue depth, wait time, duration, and retry/failure counts exported as
  metrics, so I can tell whether review is keeping up with PR volume
  without reading worker logs.
  *Given* jobs are enqueued, claimed, and completed via
  `adapter/storage/queue.py`'s `enqueue`/`claim_next`/`mark_done`/
  `mark_failed` and `apps/worker/dispatch.py`'s dispatch loop, *when* the
  worker processes them, *then* an OTel counter/gauge/histogram set
  reflects queue depth by status/type, enqueue-to-claim latency, job
  duration by type, and retry counts.
- **US2 — Pipeline stage duration and outcome, as a trace.** As the
  platform operator, I want one distributed trace per review job with a
  child span per macro pipeline step, so I can see exactly which stage
  made a slow review slow.
  *Given* `review/pipeline.py`'s `StepRegistry` already computes
  `duration_ms` per step (lines 382–408) and a `StepOutcome` per step
  (`"success"`/`"disabled"`/`"failed"`/`"timeout"`, lines 130–147), *when*
  a review job runs, *then* one trace exists with a root span for the job
  and one child span per macro step, each carrying that step's duration
  and outcome as span attributes.
- **US3 — LLM token usage.** As engineering leadership, I want prompt/
  completion/total token counts exported per pipeline stage and model, so
  I can see what review's BYO-key LLM spend actually looks like.
  *Given* `adapter/llm/litellm_client.py` already normalizes a
  `TokenUsage` (prompt/completion/total, lines 158–170) on every
  completion call and discards it, *when* a completion call succeeds,
  *then* an OTel counter records those three values tagged by pipeline
  stage and model name.
- **US4 — Findings counts.** As the platform operator, I want findings
  counted by severity × source, plus how many were dropped by the posted-
  findings ceiling, so I can see whether static analysis or the LLM is
  driving finding volume.
  *Given* `findings/merge.py`'s `MergeResult` already computes `findings`
  (with `severity`/`source` per item) and `dropped_by_ceiling_count`,
  *when* a review job's findings are merged, *then* counters reflect
  findings by severity × source and the dropped-by-ceiling count.
- **US5 — Verification funnel.** As the platform operator, I want judged/
  rejected/unmatched counts from the verification step exported, so I can
  see the kill rate the platform's own design goal 6 claims as its
  differentiator.
  *Given* `review/verification.py` already logs `judged_count`,
  `rejected_count`, `unmatched_count` (lines 361–368) in one log line and
  nowhere else, *when* the verification step runs, *then* the same three
  counts are exported as OTel counters.
- **US6 — Context Builder outcome.** As the platform operator, I want to
  see how often the Context Builder degrades (empty or errored) versus
  contributes real context, plus item count/bytes/duration, so I can tell
  whether RAG retrieval is actually working for a given repo.
  *Given* `review/context_builder.py` already computes `outcome`
  (`"success"`/`"degraded_empty"`/`"degraded_error"`),
  `context_bundle_item_count`, `context_bundle_total_bytes`, and
  `duration_ms` (lines ~515–540), *when* the Context Builder step runs,
  *then* those four values are exported as OTel metrics tagged by outcome.
- **US7 — Prefilter funnel.** As the platform operator, I want the
  prefilter's file-count funnel (total → pattern-filtered →
  ceiling-dropped → final) exported, so I can tell whether a large PR is
  being silently truncated before static analysis or the LLM ever see it.
  *Given* `review/prefilter.py` already computes `total_files`,
  `passed_pattern_filter`, `dropped_by_materialization_ceiling`,
  `dropped_by_aggregate_bytes_ceiling`, `final_file_count` (lines
  210–219), *when* the prefilter step runs, *then* those five counts are
  exported as OTel metrics.
- **US8 — Webhook rejection counts.** As the platform operator, I want
  webhook rejections counted by reason, so I can tell a misconfigured
  GitHub App secret (signature failures) apart from a rate-limited
  attacker apart from oversized payloads without grepping logs.
  *Given* `apps/webhook_api/webhook_routes.py` already logs "webhook
  rejected" with a `reason` (`rate_limited`/`body_too_large`/an invalid-
  signature reason/`malformed_json`/`invalid_payload_shape`, lines
  501–530), *when* a webhook is rejected, *then* a counter increments
  tagged by that same reason.
- **US9 — Indexing job health (Should-have).** As the platform operator, I
  want indexing job metrics and a trace (files processed/skipped/failed,
  chunks/edges created, duration), so I can tell whether a repo's index is
  healthy independent of review pipeline health.
  *Given* `indexing/pipeline.py` already logs these fields per
  `spec-indexing.md`'s Observability section, *when* an indexing job runs,
  *then* the same fields are exported as OTel metrics and one trace with a
  span per indexing stage.
- **US10 — Zero-config no-op.** As the platform operator, I want the
  platform to run with exactly today's behavior (zero errors, zero
  overhead, zero log noise) when I haven't configured an OTLP endpoint, so
  adopting this feature is never a prerequisite for using the platform at
  all.
  *Given* `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, *when* either
  entrypoint (`apps/webhook_api/main.py`, `apps/worker/main.py`) starts,
  *then* no OTel SDK/exporter is constructed at all — no background
  export thread, no connection attempts, no warning logs.
- **US11 — Working dashboard.** As the platform operator, I want a
  committed Grafana dashboard that renders real data the moment I point it
  at my collector, so I don't have to hand-build panels from a metric-name
  list.
  *Given* the metrics from US1–US9 exist, *when* I import the shipped
  dashboard JSON into Grafana pointed at a Prometheus scraping my OTel
  Collector, *then* every panel resolves to real data — proven by Unit F's
  E2E scenario, not asserted by inspection.
- **US12 — Local demo.** As a developer evaluating or building on this
  platform, I want to see the dashboard working locally without standing
  up my own observability stack first, so I can verify the feature (and
  demo it) without external dependencies.
  *Given* `docker compose --profile observability up`, *when* a review job
  runs against the stack, *then* the bundled Grafana (pre-loaded with the
  shipped dashboard) shows real data for that job **within 2 minutes** —
  a concrete bound, not "eventually": the OTel SDK's default periodic
  metric export interval is 60s and the bundled Prometheus's scrape
  interval is configured to 15s (see Unit F), so 2 minutes gives one full
  export-then-scrape cycle plus margin, not an arbitrary number.
- **US14 — Telemetry-pipeline-health signal.** As the platform operator, I
  want a way to tell "the platform is healthy and just has nothing to
  report" apart from "my OTLP endpoint/collector is broken and I'm getting
  silent data loss," from the dashboard itself — not by falling back to
  `kubectl logs`, which is exactly the workflow this PRD exists to
  eliminate.
  *Given* both entrypoints (`apps/webhook_api/main.py`,
  `apps/worker/main.py`) emit a periodic liveness gauge/counter
  (independent of any job activity — a heartbeat, not a work-derived
  metric) once OTel is configured, *when* the process is running normally,
  *then* the heartbeat increments/updates **every 30 seconds** — a
  concrete interval, not "fixed" left unstated; chosen so the panel's own
  staleness threshold (2× the interval = 60s, per the UX section below)
  fires well inside US12's already-committed 2-minute dashboard bound —
  regardless of review/indexing job volume; *when* the OTLP endpoint
  becomes
  unreachable, *then* the heartbeat continues to be recorded in-process
  (per the Reliability requirement — recording is never blocked by export
  failure) but stops reaching the collector, so the dashboard's heartbeat
  panel visibly goes stale while every other panel would otherwise look
  merely quiet. This is the mechanism that resolves the ambiguity between
  "no data because nothing happened" and "no data because telemetry is
  broken."
- **US13 — Dashboard/metric drift guard.** As the platform operator (via
  this platform's own CI, on my behalf), I want a build-time check that
  fails if the shipped dashboard references a metric name the code no
  longer emits, so a future rename can't silently break the dashboard I'm
  relying on.
  *Given* the dashboard JSON's panel queries reference specific metric
  names, *when* CI runs, *then* every referenced metric name is checked
  against the set of metric names the instrumented code actually
  registers, and the build fails on any mismatch.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Performance:** instrumentation must never add synchronous, blocking
  I/O to the pipeline hot path. OTel's own batched, async span/metric
  processors (`BatchSpanProcessor` / periodic metric export) are used
  throughout — a step's `run()` call records into an in-process buffer and
  returns immediately; the network export happens on a background worker.
  **Concrete bound:** in-process recording (the only part on the hot path —
  export itself is async and off it) must add no more than **1ms** per
  call, measured by a benchmark test comparing an instrumented step against
  its own uninstrumented baseline — small relative to every existing
  pipeline step's own measured duration (already logged in the tens-to-
  thousands of ms range per `review/pipeline.py`'s own `duration_ms`), so
  this is a real, checkable ceiling rather than a restatement of
  "negligible."
- **Security:** if the configured OTLP endpoint requires auth (a bearer
  token, mTLS client cert), that credential is supplied via Kubernetes
  `Secret`/env var at the composition root, per `architecture.md`'s config
  rule — never hardcoded, never read ad hoc from domain code.
- **Reliability:** exporter unavailability (connection refused, DNS
  failure, timeout) must never fail, slow, or retry-block a review or
  indexing job. This is the same "optional, never a dependency" contract
  [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)
  established for external product-knowledge augmentation — OTel's own
  exporter failure handling
  (fire-and-forget, drop-and-log) is relied on rather than reimplemented.
  US14's heartbeat is what makes this failure mode detectable rather than
  just safely ignored.
- **Scale:** no explicit throughput target is set by any roadmap phase —
  consistent with `spec-pipeline-step-framework.md`'s own "no explicit
  throughput/latency target is set by any roadmap phase" position.
  **Cardinality is bounded and enumerable, not just constrained in kind:**
  every label is drawn from a small, closed set — pipeline stage name (6:
  prefilter/static-analysis/context-builder/review-generation/
  verification/findings-output), outcome (4: success/disabled/failed/
  timeout), severity (5), source (3: gitleaks/semgrep/llm), plus
  operator-scoped values (org/repo IDs — bounded by how many repos one
  deployment actually watches; model name — bounded by how many models an
  operator actually configures, typically 1–3 per
  `model_routing_config`). No label is ever unbounded free-text (a
  finding's `message`, a diff's content, a raw error string), which would
  also violate the Privacy/Compliance requirement below independent of the
  cardinality concern.

### Privacy / Compliance

- **No source code, diff content, secret material, or finding free text
  ever becomes a metric/span attribute value.** `findings/redact.py`'s
  `TextRedactor` exists specifically because a `Finding.message` can carry
  quoted source/secret material — this PRD's instrumentation only ever
  emits counts, durations, IDs, severities, sources, and outcomes, never
  the content those objects wrap. This is a hard requirement, verified by
  Unit B/C/D's own tests asserting the attribute set on every emitted
  metric/span is a fixed, reviewed allowlist — not by manual review alone.
- Org/repo identifiers are included as attributes (needed for US1–US9 to
  be useful per-repo) — this is the same tenancy-scoping data already
  visible in every structured log line today (`job_id`, `org_id`,
  `repo_id`, `repo_full_name`), not new exposure.

### Observability

(This PRD *is* the observability capability — this subsection covers how
the team would know if the feature itself is broken, matching every other
spec's convention.)

- Exporter-side failures (connection refused, malformed OTLP payload
  rejected by the collector) are logged once via the existing structured
  JSON logger (`apps/*/logging_config.py`), rate-limited so a persistently
  unreachable collector doesn't spam logs — never silently swallowed (per
  `architecture.md`'s "never swallow or log-and-continue silently" rule,
  applied here as "always log, never propagate into pipeline logic").
- US13's drift guard is itself the mechanism that catches this PRD's most
  likely failure mode (a metric renamed in code, dashboard left stale).

### Rollout & Release Safety

- **No database migration required.** Every metric/trace source already
  exists as in-memory data (see the file:line citations in Requirements);
  nothing new is persisted to Postgres.
- **No-op default (US10) is the backwards-compatibility guarantee** — an
  existing deployment that upgrades without setting
  `OTEL_EXPORTER_OTLP_ENDPOINT` sees zero behavior change.
- **New dependencies:** `opentelemetry-api`, `opentelemetry-sdk`, and an
  OTLP exporter package (gRPC or HTTP variant — a build-time choice, not a
  product decision this PRD makes) are added to `pyproject.toml`'s main
  dependencies. No auto-instrumentation packages (see Out of scope).
- **Compose profile is additive-only** — the default `docker compose up`
  service list and behavior is unchanged; `observability` is a named,
  opt-in profile per Compose's own profile mechanism.
- **Helm chart:** the dashboard ConfigMap is new, additive template
  content; no existing template is changed.

## UX & Design

No UI is designed or owned by this platform for this feature — the
"dashboard" is the customer's own Grafana instance, rendering a JSON this
repo ships and commits to keeping in sync with the metrics it describes
(US13). There is no design-system dependency (this repo has none yet — no
Admin UI exists) and no accessibility/responsive obligation on review's
part; Grafana owns that surface entirely.

What *is* a real product decision, owned here: the shipped dashboard's
panel grouping and primary-vs-drill-down hierarchy, so an operator's first
glance answers "is it healthy" before anything else:

- **Top row (at-a-glance health):** the US14 heartbeat panel (webhook API
  and worker each shown separately, "last seen" timestamp), queue depth,
  job failure rate, webhook rejection rate — the numbers that answer "is
  something wrong right now," with the heartbeat panel specifically
  answering the prior question "is anything wrong with telemetry itself."
- **Second row (pipeline performance):** per-stage duration
  (US2's trace data rolled into a latency histogram panel), LLM token
  usage by stage/model (US3).
- **Third row (quality/value signal):** findings by severity × source
  (US4), verification kill rate (US5), Context Builder contribution rate
  (US6) — the numbers that answer "is review actually finding real
  things and verifying them," design goal 6's own claim.
- **Fourth row (Should-have, if Unit D ships):** indexing job health
  (US9).

**States:**
- **Empty/first-use:** before any review job has run, every panel except
  the heartbeat shows "no data" (Grafana's own default), not an error — no
  special handling needed since this is Prometheus's/Grafana's native
  behavior for an unqueried time range. The heartbeat panel is what makes
  this distinguishable from the error state below: it still shows a recent
  "last seen" timestamp, because it updates independent of job activity.
- **Error/degraded (US14):** if the OTLP endpoint is unreachable, every
  other panel looks identical to the empty/first-use state (Grafana simply
  has no new data either way) — this is exactly the ambiguity that would
  otherwise force the operator back to log-reading. The heartbeat panel's
  "last seen" timestamp is the one signal that visibly goes stale
  (Grafana's own threshold-based panel coloring — red past 60s, 2× US14's
  30-second heartbeat interval), telling the operator "telemetry is
  broken," distinct from "nothing has happened yet."

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author, `R`/`A` on every existing
  ADR's RACI table; no separate design/eng/QA/legal stakeholders exist for
  this project at this stage.
- **Acceptance:** every `US*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios); `mypy --strict`/`ruff`
  clean per `architecture.md`; US13's drift guard passes in CI.

## Phases / Units

- **Unit A — OTel SDK/exporter wiring at both composition roots, plus the
  US14 heartbeat.** (`apps/webhook_api/main.py`, `apps/worker/main.py`,
  per `architecture.md`'s "wire concrete implementations only at the
  composition root" rule.) Depends on: nothing new. Risk: low-medium —
  first infra dependency of this specific shape in the repo, though the
  OTel API itself is treated the same way `logging` already is in domain
  code (imported directly, no new `typing.Protocol` layer — see "Resolved
  decisions" for the reasoning) so the blast radius is contained to
  composition-root wiring plus call sites. The heartbeat is scoped here,
  not to Unit B/C, since it's a property of the process being alive, not
  of any pipeline stage. **Acceptance:** US10 and US14's `Given/When/Then`
  both pass.
- **Unit B — Review pipeline instrumentation.** (`review/pipeline.py` and
  its steps: prefilter, static-analysis, context-builder,
  review-generation, verification, findings-output.) Depends on: Unit A.
  Risk: low — every value instrumented is already computed in existing
  code; this is exporting, not deriving. **Acceptance:** US2–US7's
  `Given/When/Then` all pass.
- **Unit C — Worker/queue + webhook instrumentation.**
  (`apps/worker/dispatch.py`, `adapter/storage/queue.py`,
  `apps/webhook_api/webhook_routes.py`.) Depends on: Unit A. Risk: low.
  **Acceptance:** US1 and US8's `Given/When/Then` pass.
- **Unit D — Indexing job instrumentation (Should-have).**
  (`indexing/pipeline.py`.) Depends on: Unit A. Risk: low — mirrors Unit
  B's shape exactly. **Acceptance:** US9's `Given/When/Then` passes.
- **Unit E — Shipped Grafana dashboard + Helm ConfigMap + drift guard.**
  Depends on: Units B, C (needs final metric names); D's metrics included
  if shipped in time, otherwise a follow-up panel addition. Risk:
  low-medium — JSON authoring is mechanical, but US13's drift guard is
  what actually keeps this correct over time; without it this unit is
  unverified boilerplate. **Acceptance, split deliberately because US11
  is not provable within this unit's own scope:** Unit E is *code
  complete* when the dashboard JSON + Helm ConfigMap exist and US13's
  `Given/When/Then` (the drift guard) passes in CI — that's fully
  checkable here. US11's `Given/When/Then` ("every panel resolves to real
  data") is **not** part of Unit E's own done-condition; it can only be
  proven once Unit F exists, per US11's own text. A planning agent should
  treat Unit E as shippable/mergeable on the drift-guard criterion alone,
  and treat US11 as owned by Unit F, not by Unit E.
- **Unit F — Compose `observability` profile + E2E proof.** Depends on:
  Units A–E (needs everything wired to prove end-to-end). Risk: medium —
  first time this repo's Compose stack includes non-product containers
  (collector/Prometheus/Grafana), and the E2E test must assert against a
  real Grafana HTTP API returning real panel data, not just "containers
  started." **Acceptance:** US11 and US12's `Given/When/Then` both pass —
  Unit F, not Unit E, is what actually closes US11 (see Unit E's note
  above); US12 is this unit's own original scope.

## Testing / Key Scenarios

Per `testing.md`'s pyramid. Unit-level assertions against emitted metrics/
spans use OTel's own in-memory SDK test components
(`InMemorySpanExporter`/in-memory metric reader) — genuinely no I/O, per
`testing.md`'s Unit-tier rule, despite domain code importing the OTel API
directly (see "Resolved decisions").

| Story | Given/When/Then (summary) | Test path | Level | Unit |
| --- | --- | --- | --- | --- |
| US10 | No `OTEL_EXPORTER_OTLP_ENDPOINT` set → no SDK/exporter constructed, zero log noise | `apps/webhook_api/test_main.py`, `apps/worker/test_main.py` (extended) | Unit | A |
| US14 (heartbeat emits) | Heartbeat gauge/counter updates every 30 seconds independent of job activity | `apps/webhook_api/test_main.py`, `apps/worker/test_main.py` (extended) | Unit (in-memory OTel reader, simulated clock) | A |
| US14 (survives exporter failure — the PRD's own named highest-risk failure mode) | OTLP endpoint unreachable (connection refused) → heartbeat and every other in-process metric/span keep recording normally; no exception propagates into pipeline/dispatch logic; one rate-limited warning is logged | new `adapter/telemetry` (or equivalent) integration test, plus `apps/worker/test_dispatch.py` (extended) for the "job still completes" half | Integration (real OTLP receiver killed mid-test, per `testing.md`'s adapter-integration convention) | A |
| US1 | A job's full lifecycle (enqueue → claim → done/failed) produces the expected queue metrics | `adapter/storage/test_queue_skip_locked.py` / `apps/worker/test_dispatch.py` (extended) | Unit (in-memory OTel reader) | C |
| US2 | A review job produces one trace with a child span per macro step, correct duration/outcome attributes | `review/test_review_job.py` (extended) | Unit (in-memory span exporter) | B |
| US3 | A completion call's `TokenUsage` is recorded as a counter tagged by stage/model | `review/test_review_generation_step.py` (extended) | Unit | B |
| US4 | `MergeResult`'s findings and dropped-by-ceiling count are recorded | `findings/test_merge.py` (extended) | Unit | B |
| US5 | Verification's judged/rejected/unmatched counts are recorded | `review/test_verification_step.py` (extended) | Unit | B |
| US6 | Context Builder's outcome/item-count/bytes/duration are recorded per outcome | `review/test_context_builder_step.py` (extended) | Unit | B |
| US7 | Prefilter's five-count funnel is recorded | `review/test_prefilter_step.py` (extended) | Unit | B |
| US8 | Each webhook-rejection reason increments its own counter | `apps/webhook_api/test_webhook_routes.py` (extended) | Unit | C |
| US9 | An indexing job produces the expected metrics and one trace | `indexing/test_pipeline.py` (extended) | Unit | D |
| US1–US9 (exporter integration, happy path) | Metrics/spans recorded in-process actually reach a real (local, ephemeral) OTLP collector over the network | new `adapter/telemetry` (or equivalent) integration test | Integration (real OTLP receiver, per `testing.md`'s adapter-integration convention) | A |
| US13 | CI fails if the dashboard JSON references a metric name absent from the instrumented code's registered metric set | new script + CI step (e.g. `scripts/check-dashboard-metrics.py`) | Unit (static check, no I/O) | E |
| US11, US12 | `docker compose --profile observability up`; a synthetic review job runs; the bundled Grafana's HTTP API returns real data for every dashboard panel within 2 minutes (see US12's own bound) | `tests/e2e/test_observability.py` (new) | E2E | F |
| US14 (dashboard-visible, E2E) | Same Compose stack; the OTel Collector container is killed mid-test; the heartbeat panel's Grafana API response shows a stale "last seen" past the panel's own staleness threshold while a new review job's other panels still show no new data — proving the two states are actually distinguishable in the rendered dashboard, not just in the metric stream | `tests/e2e/test_observability.py` (same file, extended) | E2E | F |

## Resolved decisions (formerly open assumptions)

- **Dashboard shape: OTEL export + a shipped Grafana dashboard, not a
  screen inside review's own Admin UI, and not both.** Weighed against
  building a Postgres-backed Admin UI stats screen (would need new
  `review_run`/`finding`/token-usage persistence, none of which exists,
  plus Phase 5's not-yet-built Admin UI and
  [ADR-0008](../adr/0008-open-core-licensing-sso-and-cross-org-rbac-as-tier-1.md)'s
  local-login prerequisite) and against doing both (materially larger
  scope for marginal v1 benefit). Decided: OTEL-only for v1, matching
  ADR-0001's "customer forwards metrics if they want them" self-hosted
  framing and the "two deployable components, not five" architecture
  rule — zero new deployable component, zero new Postgres schema, zero
  dependency on Phase 5 or ADR-0008.
- **OTEL signal scope: metrics + distributed traces, not logs.** The
  existing structured JSON logger stays as-is; OTEL logs would mean
  reworking or dual-running it for no stated need.
- **LLM cost metric: raw token counts, not cost-in-currency.** No price
  table exists anywhere in this codebase, and cost-in-currency is
  meaningless for a locally-hosted/free model — deferred as real,
  undesigned scope rather than guessed at.
- **Local demo: an optional Compose profile, not BYO-only.** Chosen so the
  feature is actually demoable/verifiable (Unit F's E2E test) without
  requiring the customer's own observability stack to exist first, while
  leaving the default `docker compose up` behavior (Phase 0's exit
  criteria) unchanged.
- **Roadmap position: scheduled now, not deferred to Phase 8.** This
  PRD's actual prerequisite is the review/indexing pipelines that already
  exist (Phase 1/2/3, done) — not Memory (Phase 4) or Admin UI (Phase 5),
  once the Admin-UI-screen option above was ruled out. `docs/roadmap.md`'s
  Phase 8 "observability hooks" bullet should be updated to reference this
  spec directly once accepted, rather than left as a vague forward
  reference — flagged as a follow-up edit, not performed by this PRD.
- **Domain code imports the OTel API directly — no new `typing.Protocol`
  telemetry adapter.** `architecture.md`'s domain-package rule normally
  requires infra imports to stay out of domain packages behind a
  Protocol, but this repo already has a working precedent for exactly this
  kind of cross-cutting, no-op-by-default concern: `logging` is imported
  directly in domain modules today (e.g. `review/verification.py`,
  `review/context_builder.py`) with no Protocol indirection, because a
  logger call is a side effect the domain layer doesn't need to swap
  implementations of. OTel's *API* package (as opposed to its SDK) is
  designed the same way — a stable, vendor-neutral, no-op-by-default
  interface; only the SDK + exporter construction (real infra) is confined
  to the composition root (Unit A). Introducing a bespoke
  `MetricsRecorder`/`Tracer` Protocol here would duplicate what the OTel
  API package already *is* an interface for, the same "no abstraction
  without a second real implementation already in view" reasoning
  `architecture.md`'s simplicity section already states.

## Related

- [`docs/roadmap.md`](../roadmap.md) — Phase 8's "observability hooks"
  bullet, which this spec sequences and should come to reference directly
  (follow-up edit, not performed here); also the "phases are
  dependency-ordered, not strictly sequential" framing this PRD's
  roadmap-position decision relies on.
- [`docs/rules/architecture.md`](../rules/architecture.md) — the
  composition-root wiring rule (Unit A), the domain-package infra-import
  rule this PRD's "Resolved decisions" reasons about via the `logging`
  precedent, and the "no abstraction without a second real implementation"
  simplicity rule.
- [`docs/rules/testing.md`](../rules/testing.md) — the pyramid this PRD's
  Testing section maps onto, including the adapter-integration convention
  Unit A's exporter test follows.
- [`docs/adr/0001-self-hosted-kubernetes-deployment.md`](../adr/0001-self-hosted-kubernetes-deployment.md)
  — "no shared logs/metrics unless the customer opts to forward them,"
  the framing this PRD's OTEL-export-only decision is built on.
- [`docs/adr/0003-knowhow-product-knowledge-augmentation.md`](../adr/0003-knowhow-product-knowledge-augmentation.md)
  — the "optional, never a dependency" pattern US10's no-op default and
  this PRD's Reliability NFR both reuse.
- [`docs/adr/0008-open-core-licensing-sso-and-cross-org-rbac-as-tier-1.md`](../adr/0008-open-core-licensing-sso-and-cross-org-rbac-as-tier-1.md)
  (PROPOSED) — confirms admin stats/dashboards are Tier 2 (open, this
  PRD), while SSO/RBAC stay a separate, out-of-scope Tier 1 concern; also
  the source of the local-login prerequisite that made the Admin-UI-screen
  alternative unattractive for v1.
- [`docs/specs/spec-review-pipeline.md`](spec-review-pipeline.md),
  [`docs/specs/spec-indexing.md`](spec-indexing.md),
  [`docs/specs/spec-context-builder.md`](spec-context-builder.md) — each
  carries a "no metrics/dashboard system exists" statement this PRD
  supersedes; each should be updated to reference this spec once accepted
  (follow-up edit, not performed here).
- [`docs/specs/spec-verification-pipeline.md`](spec-verification-pipeline.md)
  — source of US5's verification funnel fields and design goal 6's
  verification-as-differentiator claim this PRD's dashboard surfaces.
- [`findings/redact.py`](../../findings/redact.py) — why finding message
  text can never become a metric/span attribute (Privacy/Compliance NFR).
