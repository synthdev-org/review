# ADR: The OpenTelemetry API package may be imported directly in domain packages, held as module-level handles, with no new `typing.Protocol` telemetry adapter

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

[`docs/rules/architecture.md`](../rules/architecture.md)'s domain-package
rule requires infra imports to stay out of domain packages (no `fastapi`,
`sqlalchemy`, `httpx`, `litellm`, `subprocess`), behind an adapter and a
consumer-defined `typing.Protocol` when more than one implementation
exists or is clearly upcoming. The observability work
([`docs/specs/spec-observability.md`](../specs/spec-observability.md))
imports `opentelemetry.trace`/`opentelemetry.metrics` directly into
`review/pipeline.py`, `review/verification.py`, `review/context_builder.py`,
`review/review_generation.py`, `review/output.py`, `review/prefilter.py`,
and `indexing/pipeline.py` — all domain packages — each holding its
tracer/meter/counter/histogram handles as module-level names created at
import time (`tracer = trace.get_tracer(__name__)`,
`_enqueued_counter = meter.create_counter(...)`, etc., in these domain
files plus several adapter/entrypoint files:
`adapter/storage/queue.py`, `apps/worker/dispatch.py`,
`apps/webhook_api/webhook_routes.py`, `adapter/telemetry/heartbeat.py`).
This is a deliberate deviation from two separate rules — the domain-
package zero-infra-import rule, and the "wire concrete implementations
only at the composition root ... no module-level singletons or global
mutable state" rule — not an oversight; `architecture.md`'s own header
requires a new ADR for any deviation, not a one-off exception. This ADR
formalizes the reasoning `spec-observability.md`'s "Resolved decisions"
section already worked through, extended to explicitly cover the module-
level-handle shape, so it doesn't read as an unreconciled inconsistency
the next time a similar case arises, and so it satisfies the letter of
the ADR requirement rather than living only in a spec.

## Context

`architecture.md`'s domain-package rule exists to keep domain logic
swappable and infra-free: an adapter can be replaced (a different LLM
provider, a different git host) without touching the domain code that
consumes it via a stable `Protocol`. That rationale doesn't transfer
cleanly to OpenTelemetry's API package. This repo already has a working
precedent for exactly this shape: `logging` is imported directly in
domain modules today (e.g. `review/verification.py`,
`review/context_builder.py`) with no `Protocol` indirection, because a
logger call is a side effect the domain layer doesn't need to swap
implementations of — there is no second "logging provider" in view, and
the standard library's own logging façade already *is* the stable
interface.

OpenTelemetry's *API* package (as distinct from its SDK) is designed the
same way: a stable, vendor-neutral, no-op-by-default interface. Calling
`tracer.start_as_current_span(...)` or `counter.add(1)` against the
no-op implementation (the default until `init_telemetry` — the
composition-root-only function that constructs the real SDK/exporter —
registers a real one) is a side effect with nothing to swap; only the SDK
and exporter construction is real infra, and that is confined to
`adapter/telemetry/sdk.py` and wired only at the composition roots
(`apps/webhook_api/main.py`, `apps/worker/main.py`), per the composition-
root wiring rule. Introducing a bespoke `MetricsRecorder`/`Tracer`
`Protocol` here would duplicate what the OTel API package already *is* an
interface for — the same "no abstraction without a second real
implementation already in view" reasoning `architecture.md`'s own
simplicity section states.

The same reasoning covers the module-level singleton rule. Every file
above already writes `logger = logging.getLogger(__name__)` as a plain
module-level name (pre-existing, e.g. `review/citation.py`,
`review/context_symbols.py`, `review/intent_summary.py`, none of them
touched by the observability work) — this codebase has never treated a
handle to a stdlib-style global registry as the "concrete implementation"
the composition-root/constructor-injection rule targets. `logging.
getLogger`/`trace.get_tracer`/`metrics.get_meter` are the *same shape*: a
lookup into a process-global registry that returns a stable,
swap-nothing handle, not a constructed adapter instance carrying real
config (a DB connection string, an LLM endpoint URL) that a test or a
different environment would need to inject differently. The composition
root still controls the actual behavior entirely — by choosing whether
and how to call `init_telemetry`, exactly as it controls logging's real
behavior via `configure_logging`. A test that wants isolated telemetry
assertions swaps the *global provider* (`reset_otel_global_providers`,
`adapter/telemetry/conftest.py`) the same way a test that wants isolated
log assertions swaps a handler on the root logger — neither needs the
module-level handle itself to be constructor-injected.

`adapter/telemetry/sdk.py`'s own `_TELEMETRY_INITIALIZED_THIS_PROCESS`
module-level flag is a narrower case of the same exemption, scoped even
more tightly: it is adapter code, not domain code, and it exists purely
to track a fact about OTel's own already-global registration API
(`set_tracer_provider`/`set_meter_provider` are first-call-wins globals
by OTel's own design, not this codebase's choice) — the same shape as
needing a flag to know whether `logging.basicConfig()` was already
called. It carries no swappable business behavior of its own.

## Decision

Domain packages may import `opentelemetry.trace` and `opentelemetry.metrics`
(the API package) directly, with no `typing.Protocol` indirection, on the
same basis `logging` is already imported directly today: a stable,
no-op-by-default, vendor-neutral interface with no second real
implementation in view. This does **not** extend to the OTel SDK or any
exporter package (`opentelemetry.sdk.*`, `opentelemetry.exporter.*`) —
those remain real infra, confined to `adapter/telemetry/sdk.py` and
constructed only at the composition roots, unchanged from the ordinary
rule.

The resulting `tracer`/`meter`/counter/histogram handles (e.g. `tracer =
trace.get_tracer(__name__)`, `_enqueued_counter =
meter.create_counter(...)`) may be held as plain module-level names,
created once at import time, on the same basis `logger =
logging.getLogger(__name__)` already is throughout this codebase — a
lookup into a process-global registry, not a constructor-injected
concrete implementation, so the composition-root/"no module-level
singletons or global mutable state" rule doesn't apply to them.
`adapter/telemetry/sdk.py`'s own `_TELEMETRY_INITIALIZED_THIS_PROCESS`
module-level flag is covered under the same exemption, narrower still:
adapter-only bookkeeping over OTel's own inherently-global registration
API, carrying no swappable business behavior.

**Scope:** `review/pipeline.py`, `review/verification.py`,
`review/context_builder.py`, `review/review_generation.py`,
`review/output.py`, `review/prefilter.py`, `indexing/pipeline.py` — every
domain call site `spec-observability.md`'s Units B and D instrument —
plus the module-level tracer/meter/counter/histogram handles in
`adapter/storage/queue.py`, `apps/worker/dispatch.py`,
`apps/webhook_api/webhook_routes.py`, `adapter/telemetry/heartbeat.py`,
and the `_TELEMETRY_INITIALIZED_THIS_PROCESS` flag in
`adapter/telemetry/sdk.py`. Extended per future unit as new instrumented
call sites land, not re-litigated per case, the same way ADR-0006
extends its own scope list.

*Extension (product knowledge):* the
product-knowledge provider branch in `review/context_builder.py` (its
`review.product_knowledge.*` instruments and span, alongside
`review/product_knowledge.py`'s wire models/pure logic those instruments
describe) and the module-level counter in
`adapter/product_knowledge/provider_resolver.py`, both following the same
no-op-until-a-composition-root-registers-a-provider rule.

*Extension (citation-quality and config-write metrics):* the module-level
`review.citation.ref_total` counter in `review/citation.py`, the
`review.citation.findings_total` counter in `review/output.py`, and the
`review.config.write_total` counter in `apps/admin_api/config_routes.py`,
under the same no-op-until-registered rule.

## Consequences

**Easier / positive:**

- No bespoke telemetry `Protocol` to define, wire, and keep in sync with
  the real OTel API's own surface — domain code calls the same
  `opentelemetry.trace`/`opentelemetry.metrics` functions any Python
  OTel documentation or example already uses.
- Unit tests assert against OTel's own in-memory SDK test components
  (`InMemorySpanExporter`, an in-memory metric reader) with genuinely no
  I/O, per `testing.md`'s Unit-tier rule — there is no fake/mock of a
  bespoke Protocol to maintain either.

**Harder / trade-offs:**

- A future implementer has to correctly judge "is this the no-op-by-default
  API package, or the real SDK/exporter" before reaching for this
  exemption — usually obvious from the import path
  (`opentelemetry.trace`/`opentelemetry.metrics` vs.
  `opentelemetry.sdk.*`/`opentelemetry.exporter.*`), but not enforced by
  a lint rule. Mitigated the same way ADR-0006 mitigates its own
  equivalent risk: this ADR's scope list is explicit, and a genuinely new
  case (e.g. an OTel SDK import appearing in a domain package) should read
  as a bug, not a silently-assumed extension of this ADR.
- A reader unfamiliar with this ADR could reasonably ask "why does
  `review/pipeline.py` import `opentelemetry` directly when
  `architecture.md` says domain packages have zero infra imports" or "why
  is there a module-level singleton here when the rule says no module-
  level singletons" — this ADR is the answer to both; call sites don't
  repeat the full reasoning inline beyond a short pointer.

## Related

- [`docs/rules/architecture.md`](../rules/architecture.md) — the two
  rules this ADR formally deviates from (verification tag `WKPYNQ`): the
  domain-package zero-infra-import rule, and the "no module-level
  singletons or global mutable state" composition-root wiring rule — plus
  the "no abstraction without a second real implementation" simplicity
  rule this decision leans on.
- [`docs/specs/spec-observability.md`](../specs/spec-observability.md) —
  the PRD whose "Resolved decisions" section first worked through this
  reasoning; this ADR formalizes it rather than restating it from
  scratch.
- [`docs/adr/0006-protocol-defined-at-data-model.md`](0006-protocol-defined-at-data-model.md) —
  the sibling ADR this one's shape (a narrow, explicitly-scoped exemption
  from a stated architecture rule, extended per unit rather than
  re-derived per case) is modeled on.
- `adapter/telemetry/sdk.py` — owns the real SDK/exporter construction
  (`init_telemetry`) this ADR's scope explicitly excludes.
