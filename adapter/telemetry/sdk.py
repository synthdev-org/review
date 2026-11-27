"""OTel SDK/exporter construction for both composition roots
(``apps/webhook_api/main.py``, ``apps/worker/main.py``), per
docs/specs/spec-observability.md's Unit A.

``init_telemetry`` is the one function either composition root calls. Per US10
("Zero-config no-op"), passing ``otlp_endpoint=None`` constructs *nothing*
— no ``TracerProvider``, no ``MeterProvider``, no exporter, no background
export thread, no connection attempt, no warning log — a strict early
return, not "construct then immediately no-op". Every domain module's own
``trace.get_tracer(__name__)``/``metrics.get_meter(__name__)`` call then
keeps returning OTel's own no-op proxy implementations, exactly as if this
package didn't exist.

When ``otlp_endpoint`` is set, this constructs a real ``TracerProvider`` +
``MeterProvider``, each wired to an OTLP gRPC exporter, and registers them
globally via ``trace.set_tracer_provider``/``metrics.set_meter_provider``
so every already-written ``get_tracer(__name__)``/``get_meter(__name__)``
call site (Units B/C/D) picks them up automatically with zero further
wiring — that global-registration contract is the whole point of this
being infra-only, composition-root code.

Reliability (this PRD's own highest-risk failure mode): recording a
span/metric only ever appends to an in-process buffer
(``BatchSpanProcessor``/``PeriodicExportingMetricReader``, both OTel's own
batched/async implementations) and returns immediately — the network
export itself runs on a background thread this module never blocks on.
Both of those implementations already catch every exception a failed
export can raise internally and log it rather than propagate it (verified
against the installed ``opentelemetry-sdk``'s own
``BatchSpanProcessor``/``PeriodicExportingMetricReader`` source — see
``adapter/telemetry/test_telemetry_integration.py`` for the integration
test that proves this against a real, killed OTLP receiver rather than
trusting that reading alone). This module's own contribution on top of
that guarantee is purely observability: rate-limiting the resulting log
noise (see ``_install_rate_limited_exporter_failure_logging`` below) so a
persistently unreachable collector logs once per window, not once per
failed export attempt.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings

# Logger names OTel's own SDK/exporter code logs export failures under
# (module-level ``logger = logging.getLogger(__name__)`` in each, verified
# against the installed ``opentelemetry-sdk``/
# ``opentelemetry-exporter-otlp-proto-grpc`` source). These already
# propagate to the root logger this repo's own
# ``apps/*/logging_config.py::configure_logging`` configures — no
# additional handler wiring needed for the failures to reach the existing
# structured JSON log stream, only rate-limiting to keep them from
# spamming it.
_EXPORTER_FAILURE_LOGGER_NAMES = (
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk.metrics._internal.export",
    "opentelemetry.exporter.otlp.proto.grpc.exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
)

# Matches US14's own staleness-threshold reasoning (2x the 30s heartbeat
# interval = 60s) — not derived from it directly, just a convenient,
# already-PRD-reviewed order of magnitude for "how often is it acceptable
# to repeat an unreachable-collector warning."
_DEFAULT_EXPORTER_FAILURE_LOG_WINDOW_SECONDS = 60.0

# Bounds how long `shutdown_telemetry` will wait on a single provider's
# `force_flush`/`shutdown` pair (run on a plain daemon thread via
# `_run_in_daemon_thread`, not `asyncio.to_thread` — see that function's
# own docstring for why) before giving up on that provider — well under a
# typical Kubernetes pod's SIGTERM grace period, and short enough that an
# unreachable OTLP collector at shutdown can't stall process exit for
# anywhere near OTel's own ~30s per-call defaults.
_SHUTDOWN_TIMEOUT_SECONDS = 5.0

# Passed as the `timeout` kwarg to both OTLP exporters' constructors
# (`OTLPSpanExporter`/`OTLPMetricExporter`) so every individual export RPC
# — not just the shutdown-time `force_flush` — is bounded, rather than
# falling back to the OTel exporter's own ~10s default. Keeps a slow or
# unreachable collector from letting export attempts (issued from
# `BatchSpanProcessor`'s/`PeriodicExportingMetricReader`'s own background
# threads, never this module's callers) pile up indefinitely.
_EXPORT_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger(__name__)


class _RateLimitedLogFilter(logging.Filter):
    """Drops every log record on a given logger past the first one in each
    ``window_seconds`` window — this PRD's Observability NFR ("logged
    once ... rate-limited so a persistently unreachable collector doesn't
    spam logs"). One instance per logger (see
    ``_install_rate_limited_exporter_failure_logging`` below), each
    tracking its own window independently, since a real outage plausibly
    trips several of these loggers (span export, metric export, the
    shared gRPC channel) at once with distinct messages worth seeing once
    each, not collapsed into a single global counter.
    """

    def __init__(self, *, window_seconds: float) -> None:
        super().__init__()
        self._window_seconds = window_seconds
        self._last_emitted_monotonic: float | None = None
        # `BatchSpanProcessor` and `PeriodicExportingMetricReader` each run
        # their own background export thread and can both call `filter()`
        # on a shared logger (e.g. the common gRPC exporter/transport
        # logger) at effectively the same moment during a real outage —
        # without a lock, two threads can both read a stale
        # `_last_emitted_monotonic` before either writes the new one,
        # letting both records through in the same window and defeating
        # the "once per window" guarantee this class exists to provide.
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        with self._lock:
            if (
                self._last_emitted_monotonic is not None
                and now - self._last_emitted_monotonic < self._window_seconds
            ):
                return False
            self._last_emitted_monotonic = now
            return True


def _install_rate_limited_exporter_failure_logging(
    *, window_seconds: float = _DEFAULT_EXPORTER_FAILURE_LOG_WINDOW_SECONDS
) -> None:
    """Attaches one independent rate-limiting filter per logger in
    ``_EXPORTER_FAILURE_LOGGER_NAMES`` — never raises, never swallows the
    underlying record (only throttles repeats), and never touches this
    repo's own loggers (``apps.*``, domain package loggers) — scoped
    strictly to OTel's own internal logger names.

    Idempotent per logger: if a ``_RateLimitedLogFilter`` is already
    attached (``init_telemetry`` called more than once in the same
    process — e.g. a test session that builds multiple apps, or a future
    caller that re-inits telemetry), this skips that logger rather than
    stacking a second filter on it. Without this guard, filters
    accumulate indefinitely (never removed anywhere), each one an
    independent "first call after a window" gate — so the *newest* filter
    (with its own fresh, unstarted window) lets through log spam the
    *older* filter would already be suppressing, silently defeating the
    "logged once per window" anti-spam guarantee this exists to provide.
    """
    for logger_name in _EXPORTER_FAILURE_LOGGER_NAMES:
        logger = logging.getLogger(logger_name)
        if any(isinstance(f, _RateLimitedLogFilter) for f in logger.filters):
            continue
        logger.addFilter(_RateLimitedLogFilter(window_seconds=window_seconds))


@contextmanager
def suppress_telemetry_errors(
    logger: logging.Logger, message: str, **extra: object
) -> Iterator[None]:
    """Runs the wrapped block; any exception it raises is caught and logged
    as a rate-limited-adjacent warning (``exc_info=True``) rather than
    propagated.

    Every call site that records a span/metric after some real operation
    already succeeded (a job enqueued, a claim made, a pipeline step
    finished) previously hand-rolled this exact ``try: <record> except
    Exception: logger.warning(..., exc_info=True)`` shape independently —
    this PRD's own Reliability NFR (telemetry recording must never affect
    the real outcome it's reporting on) restated by hand at every one of
    those sites instead of enforced once. Factored out here so future
    instrumentation sites reuse this instead of re-deriving the same
    try/except, and so the "never propagate, always log with exc_info"
    contract can't drift between sites.

    This deliberately deviates from ``architecture.md``'s "never swallow
    or log-and-continue silently" rule — see
    ``docs/adr/0012-telemetry-recording-failures-log-and-continue.md`` for
    the formal reasoning and exact scope of the exemption.

    Args:
        logger: the caller's own module logger (``logging.getLogger(__name__)``)
            — this helper never owns or creates a logger itself, so log
            records still carry the calling module's name.
        message: the exact warning message to log on failure — callers keep
            authoring their own message so it stays specific to what was
            being recorded (e.g. "enqueue telemetry recording failed (job
            enqueued successfully)").
        **extra: passed through verbatim as the log record's ``extra`` dict.

    """
    try:
        yield
    except Exception:
        logger.warning(message, extra=extra, exc_info=True)


def resolve_otlp_headers(otlp_headers: SecretStr | None) -> str | None:
    """Unwraps a settings-layer ``SecretStr | None`` OTLP-headers field into
    the raw ``str | None`` ``init_telemetry`` accepts.

    Both composition roots (``apps/webhook_api/main.py``,
    ``apps/worker/main.py``) computed this exact
    ``settings.otel_exporter_otlp_headers.get_secret_value() if ... is not
    None else None`` ternary independently before calling ``init_telemetry``
    — factored out here so the two composition roots can't silently diverge on it.
    """
    return otlp_headers.get_secret_value() if otlp_headers is not None else None


def clamp_clock_skew_duration_seconds(duration_seconds: float) -> float:
    """Floors a wall-clock duration computed from two different hosts'
    clocks (e.g. a Postgres server's own timestamp vs. this process's own
    ``datetime.now(UTC)``) at zero.

    Cross-host clock skew can otherwise produce a small negative duration
    that would corrupt a duration histogram/dashboard panel — this isn't a
    real negative duration, just skew, so flooring at zero is the correct
    fix rather than a workaround. Shared by ``adapter/storage/queue.py``
    (``claim_next``'s wait-time histogram) and ``apps/worker/dispatch.py``
    (the job-duration histogram) — the two existing call sites that
    previously each hand-rolled this identical ``max(duration, 0.0)`` clamp
    independently for the same clock-skew scenario.
    """
    return max(duration_seconds, 0.0)


def coerce_empty_otel_env_value_to_none(value: object) -> object:
    """A present-but-empty OTel env value (``""``) means "unset", not "set
    to the empty string" — both settings classes (``WebhookAPISettings``,
    ``WorkerSettings``) apply this as a pydantic ``field_validator(...,
    mode="before")`` on their ``otel_exporter_otlp_endpoint``/
    ``otel_exporter_otlp_headers`` fields, per
    docs/specs/spec-observability.md's Unit F: ``docker-compose.yml``'s
    ``observability`` profile wires these two vars through as a bare
    ``KEY:`` pass-through, which still forwards a present-but-empty
    ``.env`` value (exactly what ``.env.example`` ships) as ``""``, not
    absent. Without this coercion, ``""`` would satisfy ``str | None`` as a
    real value and silently defeat US10's no-op-by-default contract.

    Factored out here, alongside ``resolve_otlp_headers`` above, so the two
    composition roots' settings classes can't silently diverge on this — same
    reasoning as that function's own docstring.
    """
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


class OtelSettingsMixin(BaseSettings):
    """Shared ``OTEL_EXPORTER_OTLP_ENDPOINT``/``OTEL_EXPORTER_OTLP_HEADERS``
    field declarations + validator, mixed into both ``WebhookAPISettings``
    (``apps/webhook_api/settings.py``) and ``WorkerSettings``
    (``apps/worker/settings.py``).

    Both composition roots' settings classes previously declared this exact pair
    of fields (with the same ``validation_alias``es and the same
    ``field_validator``) independently, byte-for-byte identical apart from
    a docstring cross-reference — this mixin is the single place that
    pairing is defined, so the two composition roots can't silently diverge on
    it. Each entrypoint still independently instantiates and owns its own
    settings instance at its own composition root, per
    ``docs/rules/architecture.md``'s "parse config once at each
    entrypoint" rule — this only removes the duplicated field/validator
    *declarations*, not the "one settings instance per entrypoint"
    property.

    Sourced from the bare ``OTEL_EXPORTER_OTLP_ENDPOINT``/
    ``OTEL_EXPORTER_OTLP_HEADERS`` env vars, not either entrypoint's own
    ``env_prefix`` — the real OTel ecosystem convention (the same literal
    names every OTel SDK/exporter/collector already recognizes). ``None``
    (unset) is this PRD's own no-op-by-default contract (US10):
    ``init_telemetry`` constructs no SDK/exporter at all until an operator
    sets ``otel_exporter_otlp_endpoint``.
    """

    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    # `SecretStr` per architecture.md's credential-handling rule: may carry
    # a real bearer token (e.g. `authorization=Bearer <token>`), never
    # logged/repr()-able by accident. Read once at the composition root and
    # passed down explicitly into `init_telemetry` — never read ad hoc from
    # adapter/domain code or `os.environ` directly (this PRD's own
    # Security NFR).
    otel_exporter_otlp_headers: SecretStr | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_HEADERS"
    )

    @field_validator("otel_exporter_otlp_endpoint", "otel_exporter_otlp_headers", mode="before")
    @classmethod
    def _empty_otel_env_value_means_unset(cls, value: object) -> object:
        """A present-but-empty OTel env value (``""``) means "unset", not
        "set to the empty string" — see ``coerce_empty_otel_env_value_to_none``'s
        own docstring for the full reasoning.
        """
        return coerce_empty_otel_env_value_to_none(value)


# Tracks whether *this process* has already registered a real
# TracerProvider/MeterProvider — see `init_telemetry`'s own docstring for
# why this matters beyond OTel's own `Once()` guard around
# `set_tracer_provider`/`set_meter_provider`. This module-level mutable
# flag is a deliberate, narrow exemption from architecture.md's "no
# module-level singletons or global mutable state" rule — see
# docs/adr/0011-otel-api-imported-directly-in-domain-packages.md.
_TELEMETRY_INITIALIZED_THIS_PROCESS = False


# Construct and globally register OTel's ``TracerProvider``/
# ``MeterProvider`` (each wired to an OTLP gRPC exporter pointed at
# ``otlp_endpoint``), or do nothing at all when ``otlp_endpoint`` is
# ``None`` (US10).
#
# Called once, at composition-root time, by each entrypoint — never from
# domain or adapter code (``architecture.md``'s "wire concrete
# implementations only at the composition root" rule).
#
# Returns ``True`` only when this call actually performed the
# registration (i.e. this process now owns the globally-registered
# providers and is responsible for calling ``shutdown_telemetry()``
# later), ``False`` otherwise (``otlp_endpoint`` is ``None``, or a
# previous call in this same process already registered real providers).
# Callers should use this return value — not a separately-computed
# "is telemetry configured" check — to decide whether to run
# US14's heartbeat loop and whether to call ``shutdown_telemetry()`` at
# teardown. Without this, a process that calls ``init_telemetry`` more
# than once with a real endpoint (e.g. a future factory-reuse path that
# builds more than one app instance) would have every caller believe it
# owns the providers: OTel's own ``set_tracer_provider``/
# ``set_meter_provider`` are "first call wins, every later call silently
# ignored" (see ``adapter/telemetry/conftest.py``'s docstring), so only
# the *first* caller actually registered anything — but a *later*
# caller's teardown calling ``shutdown_telemetry()`` would still flush
# and shut down the first caller's still-in-use providers out from under
# it, silently breaking telemetry for the rest of the process.
#
# Args:
#     otlp_endpoint: e.g. ``"http://otel-collector:4317"``. ``None``
#         (the shipped default via both ``WebhookAPISettings`` and
#         ``WorkerSettings``' ``otel_exporter_otlp_endpoint`` field)
#         means "no telemetry" — see this module's own docstring.
#     service_name: OTel resource ``service.name`` — ``"review-webhook-
#         api"`` or ``"review-worker"`` per the metric/span catalog's
#         "Resource attributes" section. Every span/metric this process
#         emits carries it.
#     otlp_headers: raw OTel ``OTEL_EXPORTER_OTLP_HEADERS`` wire format
#         (``"key1=value1,key2=value2"``, e.g.
#         ``"authorization=Bearer <token>"`` for a collector requiring a
#         bearer token) — read once from settings at the composition
#         root and passed down explicitly here
#         (``architecture.md``'s config rule), never read ad hoc from
#         this module or from ``os.environ`` directly. Passed through to
#         both OTLP exporters unparsed; they already accept and parse
#         this exact string format themselves (verified against the
#         installed ``opentelemetry-exporter-otlp-proto-grpc`` source),
#         so this module doesn't duplicate that parsing.
#
def init_telemetry(
    *,
    otlp_endpoint: str | None,
    service_name: str,
    otlp_headers: str | None = None,
) -> bool:
    global _TELEMETRY_INITIALIZED_THIS_PROCESS

    if otlp_endpoint is None:
        return False
    if _TELEMETRY_INITIALIZED_THIS_PROCESS:
        logger.warning(
            "init_telemetry called again in the same process with a real "
            "otlp_endpoint; ignoring — OTel's own providers are already "
            "registered and this call does not own them"
        )
        return False

    resource = Resource.create({SERVICE_NAME: service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=otlp_endpoint,
                headers=otlp_headers,
                timeout=_EXPORT_TIMEOUT_SECONDS,
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=otlp_endpoint,
            headers=otlp_headers,
            timeout=_EXPORT_TIMEOUT_SECONDS,
        )
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _install_rate_limited_exporter_failure_logging()
    _TELEMETRY_INITIALIZED_THIS_PROCESS = True
    return True


# Flushes and shuts down the globally-registered ``TracerProvider``/
# ``MeterProvider`` — the counterpart to ``init_telemetry`` above, meant
# to be called once by each composition root during graceful shutdown
# (e.g. FastAPI's ``lifespan`` teardown, or a worker's ``finally`` block
# around its dispatch loop).
#
# A no-op when ``init_telemetry`` was never called with a real
# ``otlp_endpoint`` (US10): the globally-registered providers are then
# still OTel's own no-op defaults, which either don't implement
# ``force_flush``/``shutdown`` or implement them as no-ops themselves —
# either way, safe to call unconditionally.
#
# Without this, ``BatchSpanProcessor``'s default 5s schedule delay and
# ``PeriodicExportingMetricReader``'s default 60s export interval mean
# whatever was recorded in the last export window before process exit
# is buffered in-process and never makes it to the collector — this
# flushes that buffer synchronously before the process is allowed to
# exit.
#
# ``async`` because ``force_flush``/``shutdown`` are blocking calls
# against a network exporter — both callers are async (FastAPI's
# ``lifespan`` teardown, the worker's ``finally`` around its dispatch
# loop), and running them directly would block the event loop for their
# full duration if the OTLP collector is slow/unreachable, risking
# exceeding a Kubernetes pod's SIGTERM grace period. Each provider's
# flush/shutdown pair runs on its own plain daemon thread (see
# ``_run_in_daemon_thread`` below — deliberately not ``asyncio.to_thread``,
# which submits to the event loop's default executor instead) under a
# bounded timeout, and both providers run concurrently rather than
# sequentially, so total worst-case wall time is bounded by one timeout
# rather than the sum of two.
#
# Guarded: `MeterProvider.force_flush`/`shutdown` (and their
# `TracerProvider` counterparts) can themselves raise — e.g. the
# installed opentelemetry-sdk's `MeterProvider.force_flush` re-raises a
# plain `Exception` summarizing any `MetricReader` that failed to
# collect, which happens whenever the configured OTLP collector is
# unreachable at shutdown time (an entirely ordinary occurrence, not an
# edge case) — exactly the kind of telemetry-recording failure this
# module's own Reliability NFR says must never affect the real outcome
# it's reporting on. Both callers of `shutdown_telemetry` (the worker's
# `finally` around its dispatch-loop `TaskGroup`, the webhook API's
# lifespan teardown) would otherwise have this exception mask/replace
# whatever real exception (if any) triggered that shutdown path.
#
# `asyncio.wait_for` alone would only be a backstop, not a real bound:
# it can only stop *awaiting* a call, not the underlying thread running
# it. If that thread were `asyncio.to_thread`'s (the event loop's own
# default executor), the abandoned thread would keep running
# `_flush_and_shutdown` to completion regardless, and `asyncio.run()`'s
# own cleanup (`loop.shutdown_default_executor()`) would then block on
# joining it before the process can exit — letting an unreachable
# collector stall process exit well past `_SHUTDOWN_TIMEOUT_SECONDS`
# despite `wait_for`'s own timeout. `_run_in_daemon_thread` below closes
# that gap: a plain `threading.Thread(daemon=True)` is never joined by
# `asyncio.run()`'s executor teardown (nor by `concurrent.futures`' own
# atexit handler, which only joins `ThreadPoolExecutor` worker threads),
# so an abandoned one genuinely can't stall process exit — the
# interpreter kills daemon threads outright at shutdown instead of
# waiting for them. Passing `timeout_millis` into `_flush_and_shutdown`
# so `force_flush()` bounds its own blocking network call still closes
# the dominant real-world case (a slow/unreachable collector) without
# even needing the timeout to fire; `TracerProvider.shutdown()` itself
# takes no timeout parameter in the installed opentelemetry-sdk, so a
# hang inside it specifically relies on the daemon-thread abandonment
# above rather than a cooperative timeout.
async def shutdown_telemetry() -> None:
    async def _guarded(provider: object) -> None:
        with suppress_telemetry_errors(
            logger, "telemetry shutdown failed (process exit unaffected)"
        ):
            await _run_in_daemon_thread(
                _flush_and_shutdown,
                provider,
                _SHUTDOWN_TIMEOUT_SECONDS * 1000,
                timeout_seconds=_SHUTDOWN_TIMEOUT_SECONDS,
            )

    await asyncio.gather(
        _guarded(trace.get_tracer_provider()),
        _guarded(metrics.get_meter_provider()),
    )


async def _run_in_daemon_thread(
    func: Callable[..., None], *args: object, timeout_seconds: float
) -> None:
    """Runs ``func(*args)`` on a plain daemon thread (never
    ``asyncio.to_thread``/``concurrent.futures.ThreadPoolExecutor``) and
    awaits it under ``timeout_seconds``.

    ``asyncio.to_thread`` submits to the event loop's default executor,
    which ``asyncio.run()``'s own teardown (``loop.shutdown_default_executor()``)
    blocks on joining — and any other ``concurrent.futures.ThreadPoolExecutor``
    is joined too, by that package's own ``atexit`` handler, regardless of
    which executor instance is used. A raw ``daemon=True`` thread is joined
    by neither: at interpreter shutdown it is killed outright rather than
    waited for, so once this function's own ``timeout_seconds`` elapses and
    the await is abandoned, the still-running thread genuinely cannot stall
    process exit the way either executor-backed alternative could.
    """
    future: concurrent.futures.Future[None] = concurrent.futures.Future()

    def _target() -> None:
        # `asyncio.wait_for` abandoning the await on timeout cancels
        # `future` while this thread keeps running (that's the whole
        # point — see the docstring above). This thread's completion,
        # whenever it happens, must not assume `future` is still pending:
        # `Future.set_result`/`set_exception` raise `InvalidStateError` on
        # an already-cancelled future, which would otherwise surface as an
        # unhandled exception on this daemon thread.
        try:
            func(*args)
        except Exception as exc:
            with contextlib.suppress(concurrent.futures.InvalidStateError):
                future.set_exception(exc)
            return
        with contextlib.suppress(concurrent.futures.InvalidStateError):
            future.set_result(None)

    threading.Thread(target=_target, daemon=True).start()
    await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_seconds)


def _accepts_timeout_millis(method: object) -> bool:
    try:
        return "timeout_millis" in inspect.signature(method).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # Some callables (e.g. certain builtins/C-extension methods)
        # don't support introspection at all — treat as "doesn't
        # accept it" rather than guessing.
        return False


def _call_optional_provider_method(
    provider: object, name: str, timeout_millis: float
) -> Exception | None:
    """Call ``provider.<name>()`` if present, returning (not raising) its failure.

    ``timeout_millis`` is passed through to whichever methods accept it
    (``TracerProvider.force_flush``, ``MeterProvider.force_flush``, and
    ``MeterProvider.shutdown`` all do; the installed opentelemetry-sdk's
    ``TracerProvider.shutdown`` does not) so the blocking network call
    itself is bounded, rather than relying solely on the caller's
    ``asyncio.wait_for`` — which only stops awaiting a background thread,
    it does not terminate one. Whether a method accepts the keyword is
    checked via ``inspect.signature`` *before* calling it, not by catching
    a ``TypeError`` the call itself might raise — a signature mismatch and
    a genuine ``TypeError`` bug inside the method's own real execution are
    otherwise indistinguishable from the caller's side, and catching the
    latter would silently retry without a timeout instead of surfacing the
    bug. Forward-compatible with provider implementations (including test
    doubles) that don't accept the keyword, the same as the ``TypeError``
    fallback this replaced.
    """
    method = getattr(provider, name, None)
    if not callable(method):
        return None
    try:
        if _accepts_timeout_millis(method):
            method(timeout_millis=timeout_millis)
        else:
            method()
    except Exception as exc:
        return exc
    return None


# Docstring prose about ``timeout_millis`` and the ``inspect.signature`` check
# lives on ``_call_optional_provider_method`` above.
def _flush_and_shutdown(provider: object, timeout_millis: float) -> None:
    """Best-effort ``force_flush()``/``shutdown()`` on a tracer or meter
    provider. Both OTel provider types expose this same optional pair of
    methods (no-ops on the SDK's own default, unconfigured providers), so
    one helper covers both call sites in ``shutdown_telemetry`` above.

    Does not itself swallow a ``force_flush``/``shutdown`` failure —
    ``shutdown_telemetry()``'s own two ``suppress_telemetry_errors`` blocks
    already do that, once per call. It does, however, guarantee ``shutdown``
    is still attempted even when ``force_flush`` itself raised: an
    unreachable OTLP collector making ``force_flush`` raise is the ordinary
    case this module's own docstring calls out, and skipping ``shutdown``
    in that case would leave the provider's background export
    thread/network resources never released. Whichever call raised first is
    what ultimately propagates (to be caught by the caller's
    ``suppress_telemetry_errors``), so a ``shutdown`` failure never hides a
    ``force_flush`` failure that happened first.
    """
    # Deferred, not swallowed: `shutdown` below must still run even
    # though `force_flush` failed — see this function's own
    # docstring for why an unreachable collector here is ordinary,
    # not exceptional.
    force_flush_exc = _call_optional_provider_method(provider, "force_flush", timeout_millis)
    shutdown_exc = _call_optional_provider_method(provider, "shutdown", timeout_millis)
    # Whichever call raised first is what propagates, per this
    # function's own docstring — if `force_flush` already failed,
    # a `shutdown` failure on top of it must not silently replace
    # that first exception by raising past the check below.
    if force_flush_exc is not None:
        raise force_flush_exc from shutdown_exc
    if shutdown_exc is not None:
        raise shutdown_exc
