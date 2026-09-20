# ADR: A telemetry-recording failure is logged and the wrapped block continues, never re-raised

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

[`docs/rules/architecture.md`](../rules/architecture.md) states: "Specific
exception types per domain concern; never a bare `except:` or `except
Exception: pass`... Never swallow or log-and-continue silently." The
observability work's `suppress_telemetry_errors` context manager
(`adapter/telemetry/sdk.py`, with bounded-context-local copies in
`review/_telemetry.py` and `indexing/_telemetry.py`, per
[ADR-0011](0011-otel-api-imported-directly-in-domain-packages.md)'s
domain/adapter import boundary) catches `except Exception:`, logs a
warning with `exc_info=True`, and does not re-raise — a "log-and-continue"
shape. This is a deliberate deviation, not an oversight;
`architecture.md`'s own header requires a new ADR for any deviation, not a
one-off exception. This ADR formalizes the reasoning
[`docs/specs/spec-observability.md`](../specs/spec-observability.md)'s
Reliability NFR already established, so it doesn't read as an
unreconciled inconsistency the next time a review pass encounters it.

## Context

`architecture.md`'s "never swallow or log-and-continue silently" rule
exists so a genuine failure is never lost — either it propagates to a
caller that can react to it, or it's at minimum visible. Read literally
against `except Exception: logger.warning(...)`, `suppress_telemetry_errors`
looks like exactly the forbidden shape. But the rule's purpose — a failure
must never vanish unnoticed — is satisfied here by a different mechanism
than re-raising: every call site wraps telemetry recording that happens
*after* the real operation it describes has already succeeded (a job
already enqueued and durably committed, a pipeline step that already
produced its real output, a claim already made). Re-raising from the
telemetry-recording code at that point would fail an operation that
already succeeded, purely because *observing* it failed — the exact
failure mode `spec-observability.md`'s Reliability NFR names as this
feature's own highest-risk scenario (US14: "survives exporter failure"):
telemetry must never affect the real outcome it's reporting on.

The rule's "never silently" clause is satisfied, not bypassed: every
failure is logged at `warning` level with `exc_info=True`, preserving the
full exception chain and traceback in the log record — visible to anyone
watching logs or alerting on warning-level telemetry-failure messages
(rate-limited via `_RateLimitedLogFilter` so a persistently unreachable
collector doesn't spam logs, per the same module). Nothing here matches
the rule's other named-forbidden shape (`except: pass`, discarding the
exception with no trace at all).

## Decision

`suppress_telemetry_errors` (and its bounded-context-local copies,
per ADR-0011's domain-import boundary) may catch `except Exception:`,
log at `warning` level with `exc_info=True`, and continue without
re-raising, when and only when the wrapped block **records** a
metric/span/trace for an operation that has already durably succeeded
before the wrapped block runs. This does not extend to any other
`except Exception:` use in this codebase — every other domain/adapter
exception path still follows the ordinary rule (specific exception types,
`raise ... from err`, no silent continuation) unchanged.

**Scope:** `adapter/telemetry/sdk.py::suppress_telemetry_errors`,
`review/_telemetry.py::suppress_telemetry_errors`,
`indexing/_telemetry.py::suppress_telemetry_errors`, and every call site
that uses one of these three (`adapter/storage/queue.py`,
`adapter/telemetry/heartbeat.py`, `apps/webhook_api/webhook_routes.py`,
`apps/worker/dispatch.py`, and the `review`/`indexing` pipeline
instrumentation sites). Extended per future instrumentation site as new
call sites land, not re-litigated per case, the same way ADR-0006 and
ADR-0011 extend their own scope lists.

**One named exception to "the wrapped block is the recording call
only":** `adapter/telemetry/heartbeat.py::run_heartbeat_loop` wraps its
entire loop body (`asyncio.sleep` plus the counter `.add(1)`), not just
the `.add(1)` call. This is deliberate, not a drift from the convention:
the heartbeat coroutine's *only* job, end to end, is to be a liveness
signal that must never take the process down with it — `.sleep()` failing
is exactly as irrelevant to "should this kill the process" as `.add(1)`
failing, since neither is real job/request-handling logic. Reasoned in
that module's own docstring; recorded here so it doesn't read as an
unreconciled exception the next time a review pass checks call sites
against this ADR's general convention.

## Consequences

**Easier / positive:**

- A telemetry-recording bug (bad attribute name, OTel API misuse, an
  unreachable collector) can never turn into a failed job, a failed
  webhook response, or a failed pipeline step — the property
  `spec-observability.md`'s Reliability NFR names as this feature's
  highest-risk failure mode to avoid.
- The "catch broadly, log loudly, never propagate" contract lives in one
  reviewable shape per bounded context instead of being re-derived by
  hand at each call site (the state this PRD found before factoring it
  out).

**Harder / trade-offs:**

- A bug inside the wrapped block that has nothing to do with telemetry
  recording (e.g. a typo that also happens to live inside a
  `with suppress_telemetry_errors(...):` block) would be masked the same
  way a genuine telemetry failure is. Mitigated by scope: the wrapped
  block at every call site is, by convention, the metric/span-recording
  call only — never real operation logic — so this risk is contained to
  reviewers checking that convention holds at new call sites, not
  eliminated structurally.
- A future reviewer unfamiliar with this ADR will, correctly, recognize
  the bare `except Exception:` as matching `architecture.md`'s forbidden
  shape on first read. This ADR is the answer; the context manager's own
  docstring points here rather than re-arguing the case inline.

## Related

- [`docs/rules/architecture.md`](../rules/architecture.md) — the rule this
  ADR formally deviates from (verification tag `WKPYNQ`).
- [`docs/specs/spec-observability.md`](../specs/spec-observability.md) —
  the PRD whose Reliability NFR ("telemetry recording must never affect
  the real outcome it's reporting on") this ADR formalizes.
- [`docs/adr/0011-otel-api-imported-directly-in-domain-packages.md`](0011-otel-api-imported-directly-in-domain-packages.md) —
  the sibling ADR explaining why this same shape exists as three
  bounded-context-local copies rather than one shared function.
