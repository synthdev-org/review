"""review.pipeline — the step registry/runner, per
docs/specs/spec-pipeline-step-framework.md's Scope section.

Resolves each registered step's execution order from its declared
``depends_on`` (``review.step.Step``), running every dependency-satisfied
"level" concurrently via ``asyncio.gather`` (US-F2) rather than one step at
a time. Wraps every step's own ``run()`` call in ``asyncio.wait_for`` using
that step's configured timeout — a timeout is handled identically to any
other exception the step raises.

Used at **two nested levels**: one ``StepRegistry`` instance for the four
macro pipeline steps (constructed by ``apps/worker/main.py``, the
composition root, and extended with the real steps), and a
second instance the static-analysis macro step constructs
internally for its own analyzer sub-steps. The same class serves both —
the only difference is ``on_failure``, which controls whether a step's
exception or timeout is fatal to the whole run (``FailurePolicy.FATAL``,
the macro level, per US-F6 — a fatal failure propagates as
``PipelineFailedError`` and no downstream step runs) or isolated
(``FailurePolicy.ISOLATE``, the sub-step level, per US-F5 — logged,
excluded from the run's results, the remaining enabled steps still run and
the run itself still succeeds).

**Terminal step — US-F6's one stated exception, not a contradiction of
it.** ``docs/specs/spec-pipeline-step-framework.md``'s
US-F6 states a macro step's fatal failure means "no downstream step
executes" — but there is a real gap in that: if an *earlier* macro
step (e.g. pre-filter/diff-fetch) fails, the findings-merge-and-output step
never runs at all under the rule above, so a fatal failure produces **zero
GitHub-visible signal** — a `FAILED` row in Postgres, nothing on the PR
itself. `spec-pipeline-step-framework.md` was corrected accordingly (see that
document's own US-F6 text): the registered findings-output step, if any
(``terminal_step``, an optional constructor parameter — ``None`` preserves
every pre-Phase-7 caller's exact existing behavior unchanged), is invoked
**exactly once per job regardless of outcome** — either with its normal
dependency's successful output (the ordinary happy path, unaffected by this
mechanism), or, when some *other* step's failure would otherwise short-circuit
the run before the terminal step's own level is ever reached, with a
``StepFailureInfo`` describing that failure. This is mechanism the registry
itself owns (decided here, at the exact point a fatal failure propagates),
not something ``apps/worker/main.py`` special-cases in the dispatch loop.

Zero references to any concrete step's implementation — no import of
anything from ``adapter/analyzers``, ``adapter/llm``, or ``adapter/github``
here — per US-F1's boundary check and this phase's own Acceptance
criteria: the framework genuinely doesn't know which steps exist.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any

from opentelemetry import metrics, trace
from pydantic import BaseModel, ConfigDict

from review._telemetry import start_span as _shared_start_span
from review._telemetry import suppress_telemetry_errors
from review.step import Step, StepContext

logger = logging.getLogger(__name__)

# Imports only the OTel API (`opentelemetry.trace`/`opentelemetry.metrics`),
# the same `tracer = trace.get_tracer(__name__)` / `meter =
# metrics.get_meter(__name__)` style already established across this
# codebase (adapter/telemetry/heartbeat.py, review/verification.py,
# review/prefilter.py) — no SDK/exporter construction here, that stays
# exclusively at the composition root (apps/webhook_api/main.py,
# apps/worker/main.py). `__name__` here is ``"review.pipeline"``,
# matching docs/specs/spec-observability.md's METRIC & SPAN CATALOG (US2)
# tracer/meter name verbatim, so no separate string literal is needed. With
# no TracerProvider/MeterProvider registered (no OTLP endpoint configured,
# US10), every span/`.record()` call below is OTel's own no-op. These
# module-level handles (plus the domain-package import above) are a
# deliberate, scoped exemption from architecture.md's zero-infra-import and
# no-module-level-singleton rules — see
# docs/adr/0011-otel-api-imported-directly-in-domain-packages.md.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# Catalog name, verbatim (US2) — recorded at the same point each per-step
# child span records its own "review.step.duration_ms" attribute, in
# `_run_one` below.
_step_duration_histogram = meter.create_histogram(
    "review.pipeline.step.duration_ms",
    unit="ms",
    description="Duration of a single pipeline step's run() call, in milliseconds.",
)


def _start_span(name: str) -> AbstractContextManager[trace.Span]:
    """This module's ``tracer`` bound into the shared
    ``review._telemetry.start_span`` helper — see that function's docstring
    for why the ``record_exception=False``/``set_status_on_exception=False``
    pair is centralized there instead of redefined per call site.
    """
    return _shared_start_span(tracer, name)


def _field_from_seed_input(seed_input: Any, field_name: str) -> str | None:
    """Best-effort ``str`` field lookup off ``run()``'s ``seed_input``, for
    the root span's ``review.job.org_id``/``review.job.repo_id``
    attributes. Handles both shapes a real macro-level call passes:
    ``apps/worker/dispatch.py``'s actual production call site
    (``review_registry.run(job.payload, context)``, a plain
    ``dict[str, object]`` off ``adapter.storage.schemas.ClaimedJob``) and
    an attribute-bearing ``JobIdentity``-derived model — ``getattr`` alone
    silently returns ``None`` for the former (dict keys aren't attributes),
    which previously meant these attributes never populated for any real
    Worker-dispatched job. Any other shape (or a non-``str`` value)
    resolves to ``None``, matching this method's own "never blocks or
    fails the trace" contract.
    """
    if isinstance(seed_input, Mapping):
        value = seed_input.get(field_name)
    else:
        value = getattr(seed_input, field_name, None)
    return value if isinstance(value, str) else None


class StepFailureInfo(BaseModel):
    """The terminal step's own ``input`` when it's invoked on the
    failure path (see this module's own docstring's "Terminal step" note) —
    built by the registry itself from whichever step's ``PipelineFailedError``
    propagated, never by the terminal step's own caller.

    ``seed_input`` — a deliberate, documented addition beyond the
    ``(failed_step_name: str, error: str)`` field list, not a silent
    deviation: those two fields alone are not enough, because the
    terminal step (``review/output.py``) needs the job's PR
    identity (``installation_id``/``repo_full_name``/``head_sha``) to post a
    failure-conclusion check-run *to the right PR* — data no macro step
    downstream of a failure ever produced (the failing step, by definition,
    never returned an output carrying it forward the way every *successful*
    macro step's own output model does). ``seed_input`` is the one piece of
    job-identifying data guaranteed present regardless of which step failed
    — it's the very same value passed into ``StepRegistry.run()`` as
    ``seed_input``, which every macro step's own first-level input already
    derives its own job identity from (see ``review/prefilter.py``'s
    ``PrefilterInput``, the first consumer of this exact shape). Untyped
    (``Any``) here rather than a named pydantic model: the registry itself
    has no opinion on the seed's shape (per this module's own "zero
    references to any concrete step's implementation" rule) — only
    ``review/output.py``'s own ``FindingsOutputInput.model_validate(...,
    from_attributes=True)`` (or, for a plain ``dict`` seed, ordinary
    ``dict`` validation) gives it meaning.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    failed_step_name: str
    error: str
    seed_input: Any


class PipelineFailedError(Exception):
    """Raised when a step running under ``FailurePolicy.FATAL`` raises an
    unhandled exception or exceeds its configured timeout.

    Per US-F6, this is fatal to the whole run: ``StepRegistry.run()``
    raises this instead of running any further level, and the caller
    (the dispatch loop) marks the job ``FAILED``.
    """

    def __init__(self, step_name: str, cause: BaseException) -> None:
        super().__init__(f"macro step {step_name!r} failed: {cause}")
        self.step_name = step_name


class FailurePolicy(StrEnum):
    """What a registry does when one of its steps raises or times out.

    ``FATAL`` is the macro pipeline's own policy (US-F6); ``ISOLATE`` is
    the static-analysis step's inner sub-step registry's policy (US-F5).
    """

    FATAL = "fatal"
    ISOLATE = "isolate"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """One step's result from a single ``StepRegistry.run()`` call.

    Used directly by ``ISOLATE``-policy callers (the static-analysis step) to tell
    which sub-steps' output actually made it into the
    merged result and why any others didn't, per US-F5's "the failure is
    logged ... excluded from the merged output" requirement. Also returned
    for ``FATAL``-policy callers' own completed (non-fatal) runs, for
    parity and because the findings-merge-and-output step benefits
    from the same shape.
    """

    name: str
    status: str
    """One of ``"success"``, ``"disabled"``, ``"failed"``, ``"timeout"``."""
    output: Any = None
    error: BaseException | None = None


class StepRegistry:
    """Resolves execution order for a fixed set of steps from their
    declared ``depends_on``, running each dependency-satisfied level
    concurrently via ``asyncio.gather``, and applies ``on_failure``
    uniformly to every step's outcome.

    ``enabled``/``timeouts`` are plain ``Mapping[str, bool]``/
    ``Mapping[str, float]`` keyed by ``Step.name`` — not
    ``review.config``'s ``StepEnablement``/``StepTimeouts`` pydantic
    models directly. This keeps ``review.pipeline`` free of any knowledge
    of those models' field-naming convention (``<step>_enabled``,
    ``<step>_step_timeout_seconds``, and the static-analysis
    sub-steps' *shared* ``static_analysis_analyzer_timeout_seconds``
    field, which doesn't follow that per-step convention at all — both
    ``gitleaks`` and ``semgrep`` read the same field). The composition
    root (``apps/worker/main.py``), which already knows both which
    concrete steps it is registering and ``StepEnablement``/
    ``StepTimeouts``' actual field names, does that one small translation
    via ``StepEnablement.as_mapping()``/``StepTimeouts.as_mapping()`` (see
    ``review/config.py``) before constructing either registry — not this
    module.
    """

    def __init__(
        self,
        steps: Sequence[Step[Any, Any]],
        *,
        enabled: Mapping[str, bool],
        timeouts: Mapping[str, float],
        on_failure: FailurePolicy,
        terminal_step: str | None = None,
        root_span_name: str = "review.job.run",
    ) -> None:
        self._steps = list(steps)
        self._by_name: dict[str, Step[Any, Any]] = {}
        for step in self._steps:
            if step.name in self._by_name:
                raise ValueError(f"duplicate step name {step.name!r} registered")
            self._by_name[step.name] = step
        for step in self._steps:
            if len(step.depends_on) > 1:
                raise ValueError(
                    f"step {step.name!r} declares {len(step.depends_on)} dependencies "
                    f"({step.depends_on!r}); this registry only resolves a single "
                    "dependency's output into a step's typed input today — no concrete "
                    "step needs more yet, per architecture.md's 'no interface/abstraction "
                    "without a second real implementation already in view' rule"
                )
            for dependency in step.depends_on:
                if dependency not in self._by_name:
                    raise ValueError(
                        f"step {step.name!r} declares a dependency on unregistered step "
                        f"{dependency!r}"
                    )
        if terminal_step is not None and terminal_step not in self._by_name:
            raise ValueError(f"terminal_step {terminal_step!r} is not a registered step name")
        self._enabled = dict(enabled)
        self._timeouts = dict(timeouts)
        self._on_failure = on_failure
        self._terminal_step = terminal_step
        self._root_span_name = root_span_name
        self._levels = _resolve_levels(self._steps)

    async def run(self, seed_input: Any, context: StepContext) -> dict[str, StepOutcome]:
        """Run every enabled step, level by level — a level is every step
        whose declared dependency (if any) was satisfied by a prior level
        — concurrently within a level.

        A step with no declared dependency receives ``seed_input``
        directly; a step with exactly one receives that dependency's own
        output. Returns every step's outcome keyed by name, including
        ``"disabled"`` entries, so an ``ISOLATE``-policy caller can tell
        which sub-steps' output actually contributed.

        Under ``FailurePolicy.FATAL``, the first level containing a
        failed/timed-out step stops the run: every step in that level
        still finishes (they were already running concurrently — this
        does not forcibly cancel siblings), but no further level runs and
        ``PipelineFailedError`` is raised instead of returning normally
        (US-F6) — except this registry's own registered ``terminal_step``
        (if any), which still runs exactly once on this path too, per this
        module's own "Terminal step" docstring note, before that exception
        is raised. Under ``FailurePolicy.ISOLATE``, a failed/timed-out step
        never stops the run — it's logged and excluded, and every
        subsequent level still runs (US-F5).

        Wrapped in a root span, "review.job.run" (US2, catalog verbatim) —
        one per call, tagged with ``context.job_id``. ``seed_input``'s own
        ``org_id``/``repo_id`` (present whenever ``seed_input`` is a
        ``review.step.JobIdentity``-derived model *or* the raw
        ``dict[str, object]`` job payload ``apps/worker/dispatch.py``'s
        real macro-level call actually passes — not the static-analysis
        sub-step registry's own inner ``StepRegistry``, whose
        ``seed_input`` is a static-analysis-specific input, not a job
        identity) are added too, best-effort per the catalog's own "set
        org/repo attributes too if/when available in that scope" note — a
        ``seed_input`` without them (or of any other shape) never blocks
        or fails the trace. ``record_exception``/
        ``set_status_on_exception`` are disabled: a fatal
        ``PipelineFailedError``'s own message embeds ``str(cause)``, which
        may originate from a step's own exception text — never turned into
        span data, per the Privacy/Compliance NFR's fixed, reviewed
        attribute allowlist (this span's own allowlist is exactly
        ``review.job.id``/``review.job.org_id``/``review.job.repo_id``).

        The span name is ``self._root_span_name`` (default
        ``"review.job.run"``) rather than a hardcoded literal so a nested
        ``StepRegistry`` — e.g. the static-analysis sub-step registry —
        can pass a distinct name and avoid emitting a second span with the
        same name as the real per-job root span in the same trace.
        """
        with _start_span(self._root_span_name) as span:
            with suppress_telemetry_errors(
                logger,
                "root span attribute recording failed (job unaffected)",
                job_id=context.job_id,
            ):
                span.set_attribute("review.job.id", context.job_id)
                for field_name in ("org_id", "repo_id"):
                    value = _field_from_seed_input(seed_input, field_name)
                    if value is not None:
                        span.set_attribute(f"review.job.{field_name}", value)

            outcomes: dict[str, StepOutcome] = {}
            results: dict[str, Any] = {}

            for level in self._levels:
                runnable = [step for step in level if self._enabled.get(step.name, True)]
                for step in level:
                    if step in runnable:
                        continue
                    outcomes[step.name] = StepOutcome(name=step.name, status="disabled")
                    logger.info(
                        "step skipped: disabled",
                        extra={
                            "job_id": context.job_id,
                            "step_name": step.name,
                            "status": "disabled",
                        },
                    )

                if not runnable:
                    continue

                coros = [
                    self._run_one(
                        step,
                        context,
                        resolve_input=partial(self._resolve_input, step, seed_input, results),
                    )
                    for step in runnable
                ]
                level_outcomes = await asyncio.gather(*coros)

                fatal: tuple[str, BaseException] | None = None
                for step, outcome in zip(runnable, level_outcomes, strict=True):
                    outcomes[step.name] = outcome
                    if outcome.status == "success":
                        results[step.name] = outcome.output
                    elif self._on_failure is FailurePolicy.FATAL and fatal is None:
                        assert outcome.error is not None
                        fatal = (step.name, outcome.error)

                if fatal is not None:
                    name, cause = fatal
                    await self._run_terminal_step_on_failure(
                        name, cause, seed_input, context, outcomes
                    )
                    raise PipelineFailedError(name, cause) from cause

            return outcomes

    async def _run_terminal_step_on_failure(
        self,
        failed_step_name: str,
        cause: BaseException,
        seed_input: Any,
        context: StepContext,
        outcomes: dict[str, StepOutcome],
    ) -> None:
        """Invoke this registry's own registered ``terminal_step`` (if any)
        on the fatal-failure path, per this module's own "Terminal step"
        docstring note — a no-op when:

        - no ``terminal_step`` is registered on this instance (every
          registry built before the terminal step existed, and the static-analysis
          step's own inner analyzer-sub-step registry, which never sets it);
        - the terminal step *is* the step that just failed (it already ran,
          as an ordinary ``FailurePolicy.FATAL`` step, in the loop above —
          invoking it a second time here would be a genuine double-run, not
          this mechanism's own "exactly once" contract);
        - the terminal step already has an outcome recorded (it ran
          successfully in an earlier level — not a shape any of this
          pipeline's own macro steps actually produce today, since the terminal step
          is always the pipeline's last step, but not assumed impossible
          either); or
        - the terminal step is disabled (US-F4 applies on this path
          exactly as it does on the happy path — a step an operator has
          turned off is never invoked, full stop).

        A failure of the terminal step's own invocation here (e.g. the
        failure-conclusion check-run's own GitHub call failing) is caught and logged as a
        distinct, ``error``-level double-failure — never allowed to mask or
        replace the original fatal failure this method was called to
        report, which the caller still raises as ``PipelineFailedError``
        regardless of this method's own outcome.
        """
        terminal_name = self._terminal_step
        if (
            terminal_name is None
            or terminal_name == failed_step_name
            or terminal_name in outcomes
            or not self._enabled.get(terminal_name, True)
        ):
            return

        terminal_step = self._by_name[terminal_name]
        failure_input = StepFailureInfo(
            failed_step_name=failed_step_name, error=str(cause), seed_input=seed_input
        )
        outcome = await self._run_one(terminal_step, context, resolve_input=lambda: failure_input)
        outcomes[terminal_name] = outcome
        if outcome.status != "success":
            assert outcome.error is not None
            logger.error(
                "terminal step itself failed while posting the failure signal for an "
                "earlier upstream failure — double failure, needs operator attention",
                extra={
                    "job_id": context.job_id,
                    "failed_step_name": failed_step_name,
                    "original_error": str(cause),
                    "terminal_step_name": terminal_name,
                    "terminal_step_error": str(outcome.error),
                },
            )

    def _resolve_input(
        self, step: Step[Any, Any], seed_input: Any, results: Mapping[str, Any]
    ) -> Any:
        if not step.depends_on:
            return seed_input
        (dependency,) = step.depends_on
        if dependency not in results:
            raise RuntimeError(
                f"step {step.name!r} depends on {dependency!r}, which produced no output "
                "(disabled, or its own failure was isolated) — this registry has no "
                "defined behavior for that combination yet"
            )
        return results[dependency]

    def _record_step_span(
        self,
        span: trace.Span,
        *,
        job_id: str,
        step_name: str,
        status: str,
        duration_ms: int,
    ) -> None:
        """Sets this step's span attributes and records
        ``_step_duration_histogram``, guarding both the success and the
        failed/timed-out path identically: a span/metric-recording failure
        here must never surface as this step's own failure (that would
        bypass ``run()``'s fatal/terminal-step handling, which only
        triggers from a genuine ``StepOutcome`` failure) — so it's caught,
        logged, and the step outcome is unaffected either way.

        Also sets ``review.step.name`` (fixed at every call, not just on
        the happy path) rather than ``_run_one`` setting it separately
        right after opening the span — one guarded region per step instead
        of two doing the same shape of log-and-continue work.
        """
        with suppress_telemetry_errors(
            logger,
            f"step telemetry recording failed after step {status} (step outcome unaffected)",
            job_id=job_id,
            step_name=step_name,
        ):
            span.set_attribute("review.step.name", step_name)
            span.set_attribute("review.step.status", status)
            span.set_attribute("review.step.duration_ms", duration_ms)
            _step_duration_histogram.record(duration_ms, {"step_name": step_name, "status": status})

    async def _run_one(
        self,
        step: Step[Any, Any],
        context: StepContext,
        *,
        resolve_input: Callable[[], Any],
    ) -> StepOutcome:
        """Run a single step, catching any exception/timeout itself rather
        than letting it propagate through ``asyncio.gather`` — this is what
        lets the failure/timeout log line below carry ``duration_ms``
        alongside ``job_id``/``step_name``/``status``
        (every step logs start/completion/failure with all four fields,
        including the failure path — not just the success path). ``run()``
        decides fatality from the returned ``StepOutcome.status``; it does no logging of its own for
        this case.

        ``resolve_input`` (the caller's ``_resolve_input`` call for the
        normal level path, or a trivial ``lambda: failure_input`` on the
        terminal-step failure path) is invoked inside this method's own
        ``try`` rather than eagerly at the call site, so a step whose
        dependency produced no output (disabled, or isolated failure) is
        reported as this step's own failed ``StepOutcome`` — going through
        the same fatal/terminal-step handling as any other step failure —
        instead of raising synchronously out of ``run()`` before the level's
        other steps even start and bypassing the terminal-step guarantee.

        Wrapped in a child span, "review.step.{step.name}" (US2, catalog
        verbatim), and records ``review.pipeline.step.duration_ms`` (this
        module's own ``_step_duration_histogram``) at the same point the
        span's own ``review.step.duration_ms``/``review.step.status``
        attributes are set — both the success and the failed/timed-out
        path. Attribute set is fixed to exactly ``review.step.name``/
        ``review.step.status``/``review.step.duration_ms`` (the catalog's
        own allowlist) — ``record_exception``/``set_status_on_exception``
        are disabled for the same Privacy/Compliance reason ``run()``'s own
        root span disables them (an exception's ``str()`` never becomes
        span data).
        """
        timeout = self._timeouts[step.name]
        start = time.monotonic()
        logger.info("step started", extra={"job_id": context.job_id, "step_name": step.name})
        with _start_span(f"review.step.{step.name}") as span:
            try:
                step_input = resolve_input()
                output = await asyncio.wait_for(step.run(step_input, context), timeout=timeout)
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                status = "timeout" if isinstance(exc, TimeoutError) else "failed"
                self._record_step_span(
                    span,
                    job_id=context.job_id,
                    step_name=step.name,
                    status=status,
                    duration_ms=duration_ms,
                )
                log_fields = {
                    "job_id": context.job_id,
                    "step_name": step.name,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                }
                if self._on_failure is FailurePolicy.ISOLATE:
                    logger.warning("step isolated: failed", extra=log_fields)
                else:
                    logger.error("step failed", extra=log_fields)
                return StepOutcome(name=step.name, status=status, error=exc)
            duration_ms = int((time.monotonic() - start) * 1000)
            # Telemetry recording is deliberately guarded here (and nowhere
            # else in this method's happy path): the step itself already
            # succeeded by this point, so a span/metric-recording failure
            # must never surface as this step's own failure — that would
            # bypass run()'s fatal/terminal-step handling (which only
            # triggers from a genuine StepOutcome failure) and instead
            # raise an unhandled exception straight out of asyncio.gather.
            self._record_step_span(
                span,
                job_id=context.job_id,
                step_name=step.name,
                status="success",
                duration_ms=duration_ms,
            )
            logger.info(
                "step completed",
                extra={
                    "job_id": context.job_id,
                    "step_name": step.name,
                    "duration_ms": duration_ms,
                    "status": "success",
                },
            )
            return StepOutcome(name=step.name, status="success", output=output)


def _resolve_levels(steps: Sequence[Step[Any, Any]]) -> list[list[Step[Any, Any]]]:
    """Kahn's-algorithm-style leveling: level N is every step whose
    dependency (if any) was satisfied by some level < N. Raises
    ``ValueError`` on a circular dependency — a registration-time
    configuration error, not one that should surface later as a hang.
    """
    remaining = {step.name: step for step in steps}
    satisfied: set[str] = set()
    levels: list[list[Step[Any, Any]]] = []

    while remaining:
        level = [
            step for step in remaining.values() if all(dep in satisfied for dep in step.depends_on)
        ]
        if not level:
            raise ValueError(f"circular dependency among steps: {sorted(remaining.keys())!r}")
        levels.append(level)
        for step in level:
            satisfied.add(step.name)
            del remaining[step.name]

    return levels
