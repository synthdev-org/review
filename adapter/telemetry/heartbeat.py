"""US14's per-entrypoint liveness heartbeat
(docs/specs/spec-observability.md) — the signal that lets the shipped
dashboard tell "healthy, nothing to report" apart from "telemetry itself
is broken."

Imports only the OTel *API* (``opentelemetry.metrics``), the same
``meter = metrics.get_meter(__name__)`` style Units B/C/D use in domain
code — this module happens to live in ``adapter/telemetry`` (Unit A's own
package) because the heartbeat is a property of the process being alive,
not of any pipeline stage (see the spec's Unit A section), not because it
needs SDK access itself: ``run_heartbeat_loop`` below only ever calls
``.add(1)`` on a counter obtained through the API, exactly like any other
metric call site in this codebase. When no ``MeterProvider`` has been
registered (``adapter.telemetry.sdk.init_telemetry`` never called, or
called with ``otlp_endpoint=None``), that ``.add(1)`` call is OTel's own
no-op — this coroutine is still safe to run, it just records nowhere.

Only ever started as a background task by a composition root, and only
when telemetry was actually initialized (``otlp_exporter_otlp_endpoint``
is set) — see ``apps/webhook_api/main.py``'s lifespan and
``apps/worker/main.py``'s ``main()`` for the two call sites.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import metrics

from adapter.telemetry.sdk import suppress_telemetry_errors

logger = logging.getLogger(__name__)

meter = metrics.get_meter(__name__)

# Catalog name, verbatim (docs/specs/spec-observability.md's METRIC & SPAN
# CATALOG, US14) — no extra attributes, per that same catalog entry.
_heartbeat_counter = meter.create_counter(
    "review.process.heartbeat_total",
    description="Periodic liveness heartbeat, independent of job activity.",
)

# US14's own concrete interval — chosen so the dashboard panel's own
# staleness threshold (2x this = 60s) fires well inside US12's 2-minute
# dashboard bound. Not private (`_`-prefixed) purely for readability at the
# call site below; the real override seam for tests is
# `run_heartbeat_loop`'s own `interval_seconds` parameter, not this
# module-level default being monkeypatched.
HEARTBEAT_INTERVAL_SECONDS = 30.0


async def run_heartbeat_loop(*, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Loops forever: sleep ``interval_seconds``, then record one
    heartbeat. Runs independent of job activity — no queue/pipeline state
    is read here, by design (US14's own "regardless of review/indexing job
    volume" requirement).

    Designed to be run as an ``asyncio.Task`` a composition root cancels on
    shutdown (see both entrypoints' own wiring) — cancellation during the
    ``asyncio.sleep`` is the normal, expected exit path, not an error
    (re-raised as plain ``CancelledError``, never swallowed here).

    Every other exception, from any statement in the loop body (not just
    the ``.add(1)`` call), is caught and logged rather than propagated:
    both composition roots run this coroutine concurrently with the real
    dispatch/webhook-serving loop, which would otherwise let a single
    unexpected heartbeat failure tear down the entire process — killing
    real job/request handling over a purely cosmetic liveness signal.

    Reuses ``adapter.telemetry.sdk.suppress_telemetry_errors`` (this
    module already lives in ``adapter/telemetry``, so there's no
    domain/adapter boundary reason to hand-roll a second copy of that same
    log-and-continue policy) — ``asyncio.CancelledError`` is a
    ``BaseException``, not an ``Exception``, so it still propagates
    through unchanged without a separate ``except`` clause.

    Wraps ``asyncio.sleep`` too, not just ``.add(1)`` — a deliberate,
    named exception to ADR-0010's usual "wrapped block is the recording
    call only" convention, since this loop's only job is being a liveness
    signal that must never take the process down with it; see
    ``docs/adr/0010-telemetry-recording-failures-log-and-continue.md``'s
    own scope section for the full reasoning.

    Records once immediately, before the first sleep, rather than only
    after each interval — without this, the dashboard's heartbeat panel
    (2x-interval staleness threshold) looks stale for up to
    ``interval_seconds`` plus the OTel exporter's own periodic export
    interval after every process start/restart, which is exactly the
    "telemetry itself is broken" false signal US14 exists to rule out.
    """
    with suppress_telemetry_errors(logger, "heartbeat loop iteration failed"):
        _heartbeat_counter.add(1)
    while True:
        with suppress_telemetry_errors(logger, "heartbeat loop iteration failed"):
            await asyncio.sleep(interval_seconds)
            _heartbeat_counter.add(1)


@asynccontextmanager
async def heartbeat_task_scope(telemetry_enabled: bool) -> AsyncIterator[None]:
    """Starts ``run_heartbeat_loop`` as a background task for the lifetime
    of the wrapped block, and cancels it on the way out — the exact
    create/cancel/suppress sequence both composition roots
    (``apps/worker/main.py``'s ``_amain``, ``apps/webhook_api/main.py``'s
    ``_build_lifespan``) need around their own dispatch-loop/``yield``,
    factored out here so the two entrypoints can't silently diverge on it.

    A no-op (never starts a task) when ``telemetry_enabled`` is ``False``
    (US10) — matches ``run_heartbeat_loop`` only ever being started when
    telemetry was actually initialized.

    Uses a plain ``asyncio.create_task``, not ``asyncio.gather`` (whose
    default ``return_exceptions=False`` does NOT cancel the sibling
    awaitable when one of them raises — it would leave the heartbeat task
    running orphaned) — cancelled here on any exit (normal, or the wrapped
    block raising) so a real shutdown always cancels the heartbeat task
    too. ``run_heartbeat_loop`` can never itself be the failure that tears
    this down (see its own docstring), so no ``asyncio.TaskGroup``/
    ``ExceptionGroup`` unwrapping is needed.
    """
    heartbeat_task: asyncio.Task[None] | None = None
    if telemetry_enabled:
        heartbeat_task = asyncio.create_task(run_heartbeat_loop())
    try:
        yield
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
