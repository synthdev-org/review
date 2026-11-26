# Pipeline Step Framework PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-09-02

Companion to [`spec-review-pipeline.md`](spec-review-pipeline.md), which
specifies Phase 1's concrete pipeline steps and the end-to-end review loop.
This document specifies the underlying execution model those steps (and every
future roadmap phase's steps — Phase 2's context builder, Phase 3's
verification pass, etc.) run on. Split into its own file because it is a
distinct, independently testable concern: the framework's correctness (can
you add a step without touching another? do independent steps really run
concurrently? does a disabled step really not execute?) can and should be
proven without reference to which concrete steps exist.

Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`). Per `testing.md`'s mandatory rule, requirements
below are stated as user stories (`As a/I want/so that`) with `Given/When/Then`
acceptance criteria, not prose requirements.

## Problem & Goal

Today (post Phase 0 scaffold), `review/__init__.py` is an empty package with
only a docstring — there is no pipeline execution engine at all. Without a
step-based framework, every future stage this platform will ever add (Phase
2's context builder, Phase 3's verification pass, a team's own custom check)
would require hand-editing the review job's orchestration code directly,
which:

- risks regressing already-working, already-tested steps every time a new
  one is added — directly contradicts `architecture.md`'s "new adapter/step
  = zero changes to existing code; if it requires one, the boundary is
  wrong" rule, and
- blocks a platform operator (the customer's platform/security team, per the
  C4 doc's "Platform admin" persona) from adding, removing, or disabling a
  check specific to their stack without waiting on a full pipeline release.

This directly benefits two end users:

- **The platform operator**, who can add a new analyzer or future pipeline
  stage as an isolated unit of work, and can turn off a step that's noisy,
  slow, or not applicable to their stack — without touching or risking any
  other step.
- **The PR author** (the ultimate consumer of what this pipeline produces,
  per `spec-review-pipeline.md`), who benefits transitively: independent
  steps running concurrently means a faster review, and an operator's
  ability to disable a bad-signal step means less noise in what gets
  posted on their PR.

**Goal:** a pipeline execution model where (1) steps are discrete,
independently implemented units: (2) steps that don't depend on each other's
output run concurrently; (3) a new step can be added without modifying any
existing step's code; (4) any step can be independently enabled or disabled.
Success is capability-based, not a usage metric — there are no users of this
platform yet to measure. Acceptance is: a new fake analyzer step can be
registered with zero diff to any existing step (proven mechanically by a
test, not just code review); two independent steps demonstrably overlap in
wall-clock execution time; a disabled step demonstrably never executes.

## Scope

### In scope

- A domain-defined `Step` `typing.Protocol` (per `architecture.md`'s
  "interfaces are `typing.Protocol`s ... defined at the consumer" rule),
  narrow (`run(input, context) -> output`; `input`/`output` are pydantic
  models specific to each step; `context` is one shared, step-agnostic
  pydantic model — `job_id`, a `dry_run` flag, and nothing step-specific —
  passed identically to every step). `context` exists so cross-cutting
  concerns (per-job log correlation via `job_id`; suppressing a step's own
  external side effect via `dry_run`, resolved in
  `spec-review-pipeline.md`'s Rollout & Release Safety section) are
  available to *any* step without widening the Protocol per concern or
  special-casing one step's implementation — a future side-effecting step
  reads the same `context.dry_run` field, it doesn't get a new parameter.
  Used at **two nested levels**:
  1. The **macro pipeline**: the ordered sequence of review-job stages —
     pre-filter, static-analysis, llm-review, findings-merge-and-output (per
     `spec-review-pipeline.md`). Mostly sequential today (each stage
     consumes the prior stage's output), but expressed as steps over a
     registry so a future stage (e.g. Phase 2's context builder, inserted
     between static-analysis and llm-review) is a new registry entry, not a
     rewrite of the stages around it.
  2. **Sub-steps within the static-analysis macro step**: each configured
     analyzer (Gitleaks, Semgrep — see `spec-review-pipeline.md`) is its
     own sub-step, with no dependency on any other analyzer's output, so
     they are the framework's first real case of concurrent execution.
- A step registry + runner (living in `review/`, the domain package that
  orchestrates the pipeline per `architecture.md`'s package-structure rule)
  that: resolves each step's declared dependencies to build an execution
  order, and runs any set of steps whose dependencies are already satisfied
  concurrently rather than one at a time.
- Concurrent execution via `asyncio` (not multiprocessing/threads) — per
  `architecture.md`'s "I/O is async" rule and
  [ADR-0002](../adr/0002-implement-in-python.md)'s reasoning that this
  workload (subprocess exec for analyzers, HTTP calls to the LLM Gateway and
  GitHub) is I/O-bound, so the GIL is not a constraining factor.
- Per-step enable/disable via bootstrap configuration (`pydantic-settings`,
  env var/Helm value per step), read once at Worker startup — decided by the
  C4 doc's bootstrap-vs-day-two config split: DB-backed, admin-UI-driven
  per-org/repo/team step configuration is explicit day-two config that
  arrives with Phase 4 (Memory) and Phase 5 (Admin UI); nothing in Phase 1
  builds toward it yet (`adapter/storage/models.py`'s `OrgRepoTeamSetting`
  table already exists as scaffolding for that later phase, with no
  reader/writer logic — this PRD does not add any).
- Failure isolation: a sub-step's failure is caught, logged, and excluded
  from that step's merged output, without stopping the pipeline; a macro
  step's failure is fatal to the whole job (see Requirements).
- A startup validation check (same fail-fast pattern as the existing
  `adapter/storage/pgvector_check.py` and
  `llmgateway/startup_check.py`) that refuses to start the Worker if the
  enabled-step configuration cannot produce a usable pipeline.

### Out of scope (explicitly deferred)

- **DB-backed / admin-UI-driven step configuration.** Per-org/repo/team
  variance in which steps run is Phase 5/6 territory (admin UI, multi-org
  tenancy) — Phase 1's step configuration is deployment-wide only, matching
  a single Helm/Compose deployment having one configuration.
- **Dynamic/hot-reload reconfiguration.** Bootstrap config is read once at
  process start per `architecture.md`; changing which steps are enabled
  requires a Worker restart, identical to every other bootstrap setting
  today (LLM endpoint, log level, etc.).
- **A generic third-party plugin-loading mechanism** (e.g. discovering step
  packages via Python entry points/external wheels). Steps ship in-repo;
  nothing in the roadmap or this conversation asks for externally-loaded
  plugins, and speculative support for it would violate `architecture.md`'s
  "no interface/abstraction without a second real implementation already in
  view" simplicity rule.
- **Data-driven/configurable step ordering or dependency graphs.** Which
  steps exist and how they depend on each other is fixed code (a registry
  built at composition-root time in `apps/worker/main.py`), not something
  an operator edits; only on/off is configurable.

### Priority

All four capabilities below are explicit, stated product requirements from
this conversation — none is a "should/could" cut candidate:

- **Must:** `Step` Protocol + registry + dependency-respecting execution
  order; concurrent execution of independent steps; per-step on/off
  bootstrap config; failure isolation (sub-step vs. macro-step semantics);
  startup validation of the enabled-step configuration.

## Requirements

Stated as user stories per `testing.md`'s mandatory format. Personas: the
**platform operator** (deploys and configures the platform) and, where a
story's outcome is only observable through it, the **PR author** (see
`spec-review-pipeline.md` for PR-author-facing stories in detail — this
document only restates the ones that originate in the framework itself).

**US-F1 — Steps are discrete, isolated units**
As a platform operator, I want each pipeline stage to be its own
independently implemented unit with no access to another stage's internals,
so that I can trust that changing or adding one step cannot silently break
another.
- Given the `review` package's step modules, When a boundary check runs (a
  test asserting no step module imports another step module directly —
  only the shared `Step` Protocol and the pydantic models passed between
  them), Then it passes for every registered step.
- Given two steps with a declared data dependency (one consumes the
  other's typed output), When either step's internal implementation
  changes without changing its declared input/output pydantic models, Then
  the other step's own tests still pass unmodified — proven by keeping
  each step's tests double-blind to the others' internals (fakes at the
  `Step` Protocol boundary, per `testing.md`'s "fakes over mocks"
  convention).

**US-F2 — Independent steps run concurrently**
As a PR author, I want independent checks on my PR to run at the same time
rather than one after another, so that I get my review faster.
- Given two or more enabled analyzer sub-steps within static-analysis, none
  of which depends on another's output, When the static-analysis step runs,
  Then their execution overlaps in wall-clock time rather than running
  strictly sequentially.

**US-F3 — New steps add without touching existing ones**
As a platform operator, I want to add a new analyzer (or, in a later
roadmap phase, a whole new pipeline stage) by writing new code and
registering it, so that I never have to re-test or risk regressing a step
that already works.
- Given a new analyzer sub-step implementation and one new registry entry,
  When the static-analysis step runs, Then the new analyzer's findings
  appear in the merged output, with zero modifications to any existing
  analyzer's code or to the static-analysis step's own orchestration code.
- Given a hypothetical new macro step (e.g. a future context-builder step)
  and one new registry entry declaring its position/dependency, When the
  pipeline runs, Then it executes in the declared position, with zero
  modifications to any existing macro step's code.

**US-F4 — Any step can be turned off**
As a platform operator, I want to disable a specific step without removing
its code, so that I can turn off a noisy or inapplicable check for my
deployment.
- Given a step's bootstrap config flag is set to disabled, When the
  pipeline runs, Then that step is never invoked at all (not invoked and
  then filtered) — its absence is observable as "never ran", not "ran
  and produced nothing".
- Given a non-essential (sub-)step is disabled, When the pipeline runs,
  Then the remaining enabled steps still run and the job still completes.

**US-F5 — Analyzer failures don't take down the review**
As a PR author, I want one broken analyzer to never prevent me from getting
review feedback at all, so that a single flaky tool doesn't silently zero
out my review.
- Given an enabled analyzer sub-step raises an unhandled exception or
  exceeds its timeout, When the static-analysis step runs, Then the failure
  is logged, that analyzer's findings are excluded from the merged output,
  and the remaining enabled analyzers' findings still appear.

**US-F6 — Core-step failures fail the job cleanly**
As a PR author, I want a failure in the review-generation or posting step to
never show up as a partial or garbled review, so that I can trust what does
get posted.
- Given the llm-review or findings-merge-and-output macro step raises an
  unhandled exception or exceeds its timeout, When the pipeline runs, Then
  the job is marked `FAILED` and no *content*-producing downstream step
  executes — no findings are merged, no PR review comments are posted.
  Findings-merge-and-output's own terminal step still runs in this case
  specifically to post a failure-conclusion check-run, so the failure is
  visible on the PR rather than silent — this is the
  one deliberate exception to "no downstream step executes," not a
  contradiction of it: it posts a failure signal, never a partial or
  garbled review.

**US-F7 — Invalid step configuration is caught at startup, not at review time**
As a platform operator, I want to be told immediately if my step
configuration can never produce a review, so that I don't discover it only
when the first real PR silently gets nothing.
- Given a bootstrap configuration where every step capable of producing
  findings is disabled (e.g. zero analyzers enabled and llm-review
  disabled), When the Worker starts, Then it exits non-zero with a clear
  error before claiming any job — the same fail-fast pattern as the
  existing pgvector and LLM-reachability startup checks.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Concurrency mechanism:** `asyncio`-based, per `architecture.md`; no
  process/thread pool is introduced for this — the workload is I/O-bound
  (subprocess exec, HTTP), per ADR-0002.
- **Timeouts:** every step's `run` call takes an explicit timeout and
  respects cancellation, per `architecture.md`'s "every I/O call takes a
  timeout" rule — a hung analyzer or LLM call cannot hang the whole job
  indefinitely. Each step declares its own timeout with a default of 30s,
  mirroring the existing `WorkerSettings.llm_timeout_seconds` default
  (`apps/worker/settings.py`) rather than inventing an unrelated number;
  overridable per step via the same bootstrap-config mechanism as its
  on/off flag.
- **Untrusted input:** analyzer sub-steps execute against PR-supplied diff
  content, which is untrusted. Beyond the per-step timeout above, analyzer
  subprocesses inherit the Worker container's own CPU/memory limits (the
  Helm chart's/Compose's existing resource-limit mechanism, matured
  further in Phase 8) rather than this framework inventing a second,
  parallel sandboxing layer — a pathological diff can make one analyzer
  slow (caught by the timeout) but cannot exceed what the container itself
  is allowed to consume. The framework never executes PR-supplied content
  as code itself — analyzers only ever *read* the diff/files as data.
- **No new credential types.** The framework introduces no new secrets;
  step configuration is boolean/enum bootstrap config, not credentials.
- **Reliability:** isolated sub-step failures (US-F5) and fatal macro-step
  failures (US-F6) are the framework's whole reliability contract for
  Phase 1 — there is no job-level retry-with-backoff or circuit-breaker
  requirement yet; a `FAILED` job simply stays `FAILED` (the existing
  job_queue schema already models this status; automatic re-enqueue of a
  failed job is not specified by any roadmap phase and is left as a future
  concern, not invented here). This is separate from call-level retry of
  individual GitHub API requests within one step's own execution, which the
  GitHub client provides (bounded, idempotency-scoped) to absorb routine
  transient 5xx/rate-limit responses without failing an otherwise-healthy job outright — the job-level contract above
  is unaffected: a job whose GitHub calls exhaust that retry budget still
  ends up `FAILED`, exactly as this bullet describes.
- **Scale:** no explicit throughput/latency target is set by any roadmap
  document for Phase 1 — the exit criteria is pipeline correctness, not
  speed. Establishing a latency SLA is a natural fit for Phase 8
  (production hardening) once there's a real deployment to measure; stating
  one here would be inventing a number no document or stakeholder has
  given.

### Privacy / Compliance

N/A beyond what ADR-0001/ADR-0002 already establish — the framework
introduces no new data handling; it only orchestrates calls to adapters
(GitHub, LLM Gateway, analyzers) that already exist or are specified in
`spec-review-pipeline.md`.

### Observability

- Every step logs its own start, completion, and failure (including which
  steps were skipped because disabled) via the existing structured-logging
  setup (`apps/worker/logging_config.py`) — extended with per-step context
  (`step_name`, `duration_ms`, `status`) plus `job_id` read from the shared
  `context` parameter every step already receives (see Scope's `Step`
  Protocol bullet), not threaded through some other side channel.
- No metrics/dashboarding system exists in this repo yet and none is
  specified by any roadmap phase up to and including Phase 1 — observability
  here is scoped to structured logs only, not a fabricated metrics
  requirement.

### Rollout & Release Safety

- The framework itself has no user-visible rollout risk (nothing consumes
  it yet outside this repo) — rollout-safety concerns that matter (e.g.
  dry-run mode for first deployment) belong to `spec-review-pipeline.md`,
  since they're about the concrete steps' effects (posting real PR
  comments), not the framework's execution model.
- Backwards compatibility: since this is wholly new code (no existing
  pipeline to migrate from — Phase 0 left `review/` empty), there is no
  migration path to design.

## UX & Design

N/A — this is a pure execution-engine/orchestration concern with no
directly observable surface of its own (its effects are only visible
through the concrete steps specified in `spec-review-pipeline.md`, which
covers the PR-comment/check-run UX).

## Stakeholders & Sign-off / Acceptance Criteria

- **Owner:** Andy Beak — sole engineer/author on this project, `R`/`A` on
  every existing ADR's RACI table. No separate design/eng/QA/legal
  stakeholders exist for this project at this stage.
- **Acceptance:** every `US-F*` story's `Given/When/Then` is proven by an
  automated test (see Testing / Key Scenarios below); `mypy --strict`/
  `ruff` clean per `architecture.md`; the framework has zero references to
  which concrete steps exist (i.e. `review`'s registry/runner code imports
  no analyzer- or LLM-specific logic) — enforced by code review against
  `architecture.md`'s package-boundary rule at merge time.

## Phases / Units

Single unit — this PRD is itself one buildable increment (Unit C in
`spec-review-pipeline.md`'s Phases / Units section), a prerequisite for
every concrete step that PRD specifies.

- **Risk:** medium. Unlike most of Phase 0/1's scaffolding, this is genuine
  new architectural synthesis — no existing pattern in the codebase to
  directly copy (the closest precedent, `adapter/storage/queue.py`'s
  `SKIP LOCKED` claiming, solves a different problem: concurrent *workers*
  claiming jobs, not concurrent *steps* within one job). Mitigated by
  keeping the `Step` Protocol intentionally narrow (1–3 methods, per
  `architecture.md`) and proving the concurrency/isolation/extensibility
  claims with fakes (`testing.md`'s "fakes over mocks" convention) rather
  than only asserting them via code structure.
- **Dependency:** none — pure domain/orchestration code, buildable and
  fully unit-testable before any concrete step (pre-filter, analyzers,
  llm-review, findings-merge) exists, using fake steps.

## Testing / Key Scenarios

Per `testing.md`'s pyramid: this PRD's scenarios are all pure orchestration
logic over fakes at the `Step` Protocol boundary — no real I/O, adapters,
network, or database — so every scenario below is a **unit** test living in
`review/` (co-located `test_*.py` per `testing.md`'s conventions).

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-F1 | No step module imports another step module directly (import-boundary check over the `Step` Protocol only) | `review/test_step_boundaries.py` | Unit |
| US-F1 | A fake downstream step's own tests pass unmodified when a fake upstream step's internals change but its output model doesn't | `review/test_step_registry.py` | Unit |
| US-F2 | Two fake steps with artificial delays and no mutual dependency run with overlapping wall-clock time | `review/test_step_registry.py` | Unit |
| US-F3 (sub-step) | Registering a new fake analyzer sub-step with zero diff to existing analyzer steps surfaces its result in merged output | `review/test_static_analysis_step.py` | Unit |
| US-F3 (macro step) | Registering a new fake macro step at a declared position runs there with zero diff to existing macro steps | `review/test_step_registry.py` | Unit |
| US-F4 | A step disabled via config flag is never invoked (asserted via a fake step recording whether `run` was called) | `review/test_step_registry.py` | Unit |
| US-F4 | Disabling a non-essential sub-step still lets the job complete using the remaining enabled steps | `review/test_static_analysis_step.py` | Unit |
| US-F5 | A raising fake analyzer sub-step is isolated; its failure is logged and excluded, other analyzers' findings still appear | `review/test_static_analysis_step.py` | Unit |
| US-F6 | A raising fake llm-review (macro) step marks the run failed and prevents findings-merge-and-output from being invoked | `review/test_step_registry.py` | Unit |
| US-F7 | Worker startup with an all-disabled step configuration exits non-zero with a clear error | `apps/worker/test_settings.py` (extended) or `apps/worker/test_main.py` | Unit |
