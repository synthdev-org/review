"""apps.worker.dispatch — the Worker's claim/dispatch loop, per the plan's
Phase 3 ("Worker job dispatch loop") section.

Replaces the Worker's Phase-0/1/2 terminal behavior (parse config, run
startup checks, log "worker idle: no job dispatch wired yet", exit 0) with
an actual loop: poll ``JobQueueRepository.claim_next()`` (already
implemented, ``adapter/storage/queue.py``, unchanged) every
``poll_interval_seconds`` when the queue is empty, and for each claimed job,
dispatch by ``job_type`` — ``REVIEW`` jobs run through Phase 2's pipeline
registry (``review.pipeline.StepRegistry``).

Phase 7 of the indexing delivery plan (``docs/plans/plan-indexing-delivery.md``
— a separate plan from the "Phase N" numbering used elsewhere in this
module's own history, disambiguated explicitly here) replaces the prior
``INDEX``-job stub (log "not implemented", mark ``DONE`` immediately) with a
real call into ``indexing.pipeline.run_indexing_job``, wired with real
adapters at the composition root (``apps/worker/main.py``) via
``indexing.pipeline.IndexingJobDependencies``. ``IndexingJobFailedError``
(the ``INDEX``-job analog of ``review.pipeline.PipelineFailedError``) is
caught by ``_run_claimed_job`` the same way — marking the job ``FAILED``
with a "job failed" log line, never the generic bug-resilience catch-all.

``docs/plans/plan-deliver-memory.md``'s Phase 4 section (Unit D, US-M3)
adds a third branch: ``MEMORY_REACTION_POLL`` jobs run through
``memory.reaction_poll.run_memory_reaction_poll_job``, wired with real
adapters via ``memory.reaction_poll.MemoryReactionPollJobDependencies``
(the same small-bundle pattern ``IndexingJobDependencies`` already
establishes). ``MemoryReactionPollJobFailedError`` (that job type's own
analog of ``IndexingJobFailedError``/``PipelineFailedError``) is caught by
``_run_claimed_job`` the same way as the other two — marking the job
``FAILED`` with a "job failed" log line, never the generic bug-resilience
catch-all.

Lives in ``apps/worker`` (an entrypoint package, per
docs/rules/architecture.md — entrypoints depend on the domain and wire
adapters in) rather than a domain package: this module imports
``adapter.storage`` types directly (``JobQueueRepository``, ``ClaimedJob``),
which a domain package may never do. ``apps/worker/main.py`` (the
composition root) is the only caller, via ``asyncio.run(run_dispatch_loop(...))``
— this coroutine runs forever and is never expected to return.

**Bug-resilience, not just the happy path:** an unhandled exception escaping
this loop's own per-job handling (a bug in this loop, not a normal
``PipelineFailedError`` the loop already turns into a ``FAILED`` status) is
caught, logged at ``error``, and the already-``CLAIMED`` job for that
iteration is marked ``FAILED`` before the loop continues to the next poll —
a crash-looping Worker pod is worse than one bad job marked ``FAILED`` for
investigation. Likewise, ``claim_next()`` itself raising (e.g. Postgres
unreachable/restarting) is caught at the same poll-loop level, logged at
``error``, and the loop continues polling rather than crashing the process.
**Residual gap, acknowledged rather than silently assumed away:** this
crash-resilience design still assumes the ``mark_failed()``/``mark_done()``
write itself succeeds; if that write also fails (e.g. a Postgres blip
coinciding with the very failure being handled), the job stays permanently
``CLAIMED`` with no further, distinct log line marking the double failure —
the same reclaim-timeout gap named below would eventually need to cover
this case too. Not solved here, just named rather than left implicit, per
the plan's own Phase 3 "Observability & rollback" section.

**Stuck-job visibility, not resolution:** ``claim_next()`` only ever selects
``PENDING`` rows — it never re-examines a stale ``CLAIMED`` row, so a job
whose Worker process is terminated mid-processing stays ``CLAIMED`` forever
with no automatic recovery in this phase (no reclaim-timeout logic exists
yet — Phase 8 hardening). As a cheap, in-scope mitigation for the gap's
*visibility*, not its resolution, a claimed job's own dispatch coroutine
runs as a single ``asyncio.Task`` awaited via ``asyncio.wait(...)`` in a
loop rather than being awaited directly: every time the wait times out
before the task finishes, a ``warning`` log names the job as plausibly
stuck, then the loop waits again — turning "an operator has to think to run
a SQL query" into "the existing structured-log tooling this phase already
relies on surfaces it," without solving the underlying reclaim gap itself.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal

from opentelemetry import metrics

from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository
from adapter.storage.schemas import ClaimedJob
from adapter.telemetry.sdk import clamp_clock_skew_duration_seconds, suppress_telemetry_errors
from indexing.pipeline import IndexingJobDependencies, IndexingJobFailedError, run_indexing_job
from memory.reaction_poll import (
    MemoryReactionPollJobDependencies,
    MemoryReactionPollJobFailedError,
    MemoryReactionPollJobPayload,
    run_memory_reaction_poll_job,
)
from review.pipeline import PipelineFailedError, StepRegistry
from review.step import StepContext
from tenancy.usage import UsageCounterRecorder

logger = logging.getLogger(__name__)

# US1 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) — imports
# the OTel API directly, same style as `adapter/storage/queue.py` and
# `adapter/telemetry/heartbeat.py`. Attributes are bounded-cardinality tags
# only (`job_type`, `outcome`), never job payload content. Meter name is the
# catalog's mandated "review.queue" literal (shared with
# `adapter/storage/queue.py`), not `__name__` — this module's own dotted
# path (`apps.worker.dispatch`) wouldn't match it.
meter = metrics.get_meter("review.queue")

_completed_counter = meter.create_counter(
    "review.queue.completed_total", description="Jobs that reached a terminal DONE/FAILED status."
)
_job_duration_histogram = meter.create_histogram(
    "review.job.duration_seconds",
    unit="s",
    description="Wall-clock time from claim to terminal DONE/FAILED status.",
)

# docs/plans/plan-deliver-admin-gui.md's Phase 7 section (Unit H, US-A19) —
# a separate meter/name (`review.usage_counter`, not `review.queue`) since
# this signal is about the usage-counter side effect specifically, not
# generic job-queue throughput.
_usage_counter_meter = metrics.get_meter("review.usage_counter")
_usage_counter_increment_failed_counter = _usage_counter_meter.create_counter(
    "review.usage_counter.increment_failed_total",
    description="Count of usage-counter increment attempts (on a completed private-repo REVIEW "
    "job) that failed — an operator-alertable signal for a systemic write-failure pattern (e.g. "
    "a degraded Postgres path silently undercounting every org), since this side effect fails "
    "soft (logged, never raised) and so would otherwise be discoverable only by grepping logs.",
)

# The only two terminal outcomes `_record_job_completion` is ever called
# with (see its three call sites below) — a `Literal` alias rather than a
# bare `str` so a typo (e.g. `"faild"`) is caught by the strict type
# checker instead of silently creating a new, uncounted metric series on
# `_completed_counter`/`_job_duration_histogram`.
_JobOutcome = Literal["done", "failed"]


def _record_job_completion(job: ClaimedJob, *, outcome: _JobOutcome) -> None:
    """Records US1's completion counter/duration histogram for ``job`` —
    called from every path that reaches a terminal DONE/FAILED status
    (the normal `_run_claimed_job` success/`PipelineFailedError`/
    `IndexingJobFailedError` paths, and `run_dispatch_loop`'s own
    bug-resilience `mark_failed` path), so every terminal transition is
    counted exactly once regardless of which path produced it.

    Guarded end-to-end: every caller here has already committed the job's
    real DONE/FAILED transition (``job_queue.mark_done``/``mark_failed``)
    before calling this. A telemetry-recording exception must never
    escape and be mistaken by a caller for that real transition having
    failed — e.g. ``_run_claimed_job``'s own ``except (PipelineFailedError,
    IndexingJobFailedError)`` block would otherwise let such an exception
    propagate past its own ``return``, straight into
    ``run_dispatch_loop``'s catch-all, which would then call
    ``mark_failed``/this function a second time for the same job.
    """
    with suppress_telemetry_errors(
        logger,
        "job completion metrics recording failed (job's terminal status unaffected)",
        job_id=str(job.id),
        job_type=job.job_type.value,
        outcome=outcome,
    ):
        attributes = {"job_type": job.job_type.value, "outcome": outcome}
        _completed_counter.add(1, attributes)
        if job.claimed_at is not None:
            # `claimed_at` and this `datetime.now(UTC)` are both this
            # worker process's own clock, read at two different points in
            # time (see `adapter/storage/queue.py`'s `claim_next`, which
            # sets `claimed_at = datetime.now(UTC)`) — a clock step
            # (e.g. an NTP correction) between the two reads can still
            # make the naive difference negative, so this needs clamping
            # the same as `clamp_clock_skew_duration_seconds`'s own
            # docstring describes for the cross-clock case.
            duration_seconds = clamp_clock_skew_duration_seconds(
                (datetime.now(UTC) - job.claimed_at).total_seconds()
            )
            _job_duration_histogram.record(duration_seconds, attributes)


async def _mark_terminal_and_record(
    job_queue: JobQueueRepository, job: ClaimedJob, *, outcome: _JobOutcome
) -> bool:
    """Mark ``job`` DONE/FAILED (``job_queue.mark_done``/``mark_failed``,
    selected by ``outcome``) and record its completion telemetry via
    `_record_job_completion` — but only when the transition actually
    happened. All three call sites below (`_run_claimed_job`'s success and
    failure paths, `run_dispatch_loop`'s own bug-resilience catch-all) need
    the exact same "only count it if this call is the one that actually
    moved CLAIMED -> DONE/FAILED" guard, so it lives here once rather than
    being repeated at each site — see `_record_job_completion`'s own
    docstring for why a job must never be double-counted.

    Returns whether this call performed the transition, so callers can
    still include it in their own "job done"/"job failed" log line.
    """
    if outcome == "done":
        transitioned = await job_queue.mark_done(job.id)
    else:
        transitioned = await job_queue.mark_failed(job.id)
    if transitioned:
        _record_job_completion(job, outcome=outcome)
    return transitioned


async def _record_usage_increment(
    job: ClaimedJob, usage_counter_recorder: UsageCounterRecorder | None
) -> None:
    """US-A19: after a ``REVIEW`` job completes successfully, if the repo
    is private on GitHub (``job.payload["private"]``, threaded through
    from the webhook envelope's own ``repository.private`` — see
    ``apps/webhook_api/webhook_routes.py``'s ``_base_job_identity_fields``),
    increments the free-tier usage counter for that repo's GitHub App
    installation. Public repos, and jobs with no ``usage_counter_recorder``
    wired in at all (every pre-Phase-7 test call site in this package,
    which pass ``None`` and get exactly today's pre-Phase-7 behavior — no
    increment attempted), are both unconditional no-ops.

    Only called for ``JobType.REVIEW`` jobs, and only once ``_run_claimed_job``
    has confirmed this call is the one that actually transitioned the job
    to DONE (mirroring `_mark_terminal_and_record`'s own double-count
    guard) — never for `INDEX`/`MEMORY_REACTION_POLL` jobs, which have no
    such payload field to read and no usage semantics of their own.

    **Deliberately fails soft — the one narrow, documented exception to
    ``docs/rules/architecture.md``'s "never swallow a domain error" rule
    in this codebase.** A `REVIEW` job that otherwise completed
    successfully must never be marked FAILED because of this side effect
    (this phase's own "Observability & rollback" section); justified
    because ADR-0008 makes this metering side effect explicitly
    lower-priority than the review job it's attached to. The broad
    `except Exception` below is intentional, not an oversight — any
    failure (a malformed/missing `installation_id`, a transient Postgres
    error inside `increment()`, anything else) is logged at `ERROR` and
    counted by `review.usage_counter.increment_failed_total` (itself
    wrapped in `suppress_telemetry_errors` so even a telemetry-recording
    failure can't escape this function), never re-raised.
    """
    if usage_counter_recorder is None:
        return

    payload = job.payload
    if not bool(payload.get("private")):
        return

    installation_id = payload.get("installation_id")
    now = datetime.now(UTC)
    try:
        if not isinstance(installation_id, int):
            raise TypeError(f"job payload's installation_id is {installation_id!r}, expected int")
        await usage_counter_recorder.increment(installation_id=installation_id, now=now)
    except Exception as exc:  # Deliberate fail-soft boundary — see docstring above.
        logger.error(
            "usage counter increment failed (REVIEW job's own DONE status unaffected)",
            extra={"job_id": str(job.id), "error": str(exc)},
        )
        with suppress_telemetry_errors(
            logger,
            "usage counter increment_failed metric recording failed",
            job_id=str(job.id),
        ):
            _usage_counter_increment_failed_counter.add(1)


async def _dispatch_claimed_job(
    job: ClaimedJob,
    *,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    dry_run: bool,
) -> None:
    """Dispatch one claimed job by its ``job_type``.

    ``INDEX`` jobs run through ``indexing.pipeline.run_indexing_job``
    (Phase 7 of the indexing delivery plan) — a fatal whole-job failure
    propagates as ``IndexingJobFailedError``, which this function does not
    catch; the caller (``_run_claimed_job``) is the one place that turns it
    into a ``FAILED`` status, mirroring ``REVIEW``'s own
    ``PipelineFailedError`` handling exactly. ``MEMORY_REACTION_POLL`` jobs
    (``docs/plans/plan-deliver-memory.md``'s Phase 4 section) run through
    ``memory.reaction_poll.run_memory_reaction_poll_job``, parsing the raw
    queue payload via ``MemoryReactionPollJobPayload`` first — the same
    parse-at-the-boundary shape ``run_indexing_job`` already establishes for
    ``INDEX``. ``REVIEW`` jobs run through ``review_registry`` the same way
    they always have.

    ``dry_run`` (Phase 7 of the walking-skeleton plan, ``WorkerSettings.dry_run``,
    read once at Worker startup) is set on the shared ``StepContext`` every
    ``REVIEW`` step receives — never read from settings by any step itself,
    per ``review/output.py``'s own docstring. ``INDEX``/``MEMORY_REACTION_POLL``
    jobs have no equivalent dry-run concept (per each spec's own scope):
    both always perform their real writes.
    """
    logger.info("job dispatched", extra={"job_id": str(job.id), "job_type": job.job_type.value})

    if job.job_type == JobType.INDEX:
        await run_indexing_job(job.payload, indexing_deps, job_id=str(job.id))
        return

    if job.job_type == JobType.MEMORY_REACTION_POLL:
        parsed = MemoryReactionPollJobPayload.model_validate(job.payload)
        await run_memory_reaction_poll_job(
            repo_id=str(parsed.repo_id),
            pr_number=parsed.pr_number,
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            reaction_fetcher=memory_reaction_poll_deps.reaction_fetcher,
            signal_recorder=memory_reaction_poll_deps.signal_recorder,
            dedup_store=memory_reaction_poll_deps.dedup_store,
            max_comments_per_poll=memory_reaction_poll_deps.max_comments_per_poll,
        )
        return

    context = StepContext(job_id=str(job.id), dry_run=dry_run)
    await review_registry.run(job.payload, context)


async def _run_claimed_job(
    job: ClaimedJob,
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    stuck_job_warning_threshold_seconds: float,
    dry_run: bool,
    usage_counter_recorder: UsageCounterRecorder | None = None,
) -> None:
    """Run ``job`` to completion — warning (not failing) if it runs past
    ``stuck_job_warning_threshold_seconds`` — and mark it ``DONE``/``FAILED``
    accordingly.

    Any exception other than ``PipelineFailedError``/``IndexingJobFailedError``/
    ``MemoryReactionPollJobFailedError`` escaping ``_dispatch_claimed_job``
    (a bug in the loop, not a pipeline/job-reported failure) is deliberately
    left uncaught here — the caller (``run_dispatch_loop``) is where that
    distinction is handled, per this module's own docstring.

    ``usage_counter_recorder`` (docs/plans/plan-deliver-admin-gui.md's
    Phase 7 section, Unit H, US-A19) defaults to ``None`` — restoring
    exactly today's pre-Phase-7 behavior (no usage-counter increment
    attempted) — the same "``None`` restores byte-for-byte today's
    behavior" pattern ``model_resolver``/``dry_run`` already establish
    elsewhere in this composition-root-to-loop chain; the real composition
    root (``apps/worker/main.py``) always passes a real
    ``PostgresUsageCounterRepository`` instance.
    """
    task: asyncio.Task[None] = asyncio.create_task(
        _dispatch_claimed_job(
            job,
            review_registry=review_registry,
            indexing_deps=indexing_deps,
            memory_reaction_poll_deps=memory_reaction_poll_deps,
            dry_run=dry_run,
        )
    )
    done, _pending = await asyncio.wait({task}, timeout=stuck_job_warning_threshold_seconds)
    if task not in done:
        # Log once when the threshold is first crossed, then wait for the
        # task to complete — via `asyncio.wait` again, not a plain
        # `await task`, since the latter re-raises whatever exception the
        # task completed with immediately, propagating straight out of this
        # function and skipping the shared `task.result()`/
        # `except PipelineFailedError` handling below (the "job failed"
        # log + `mark_failed` call), the same way `task.result()` itself
        # would if called here directly. Re-entering the wait in a loop
        # would log the same warning indefinitely (unbounded log volume)
        # for any job that runs long past the threshold, with no backoff or
        # cap, hence a single log line before this one-shot wait.
        logger.warning(
            "job still running past stuck-job warning threshold",
            extra={"job_id": str(job.id), "job_type": job.job_type.value},
        )
        await asyncio.wait({task})

    try:
        task.result()
    except (PipelineFailedError, IndexingJobFailedError, MemoryReactionPollJobFailedError) as exc:
        transitioned = await _mark_terminal_and_record(job_queue, job, outcome="failed")
        logger.error(
            "job failed",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type.value,
                "error": str(exc),
                "transitioned": transitioned,
            },
        )
        return

    transitioned = await _mark_terminal_and_record(job_queue, job, outcome="done")
    logger.info(
        "job done",
        extra={
            "job_id": str(job.id),
            "job_type": job.job_type.value,
            "transitioned": transitioned,
        },
    )
    # US-A19: only for a REVIEW job this call itself actually transitioned
    # to DONE (mirroring `_mark_terminal_and_record`'s own "only count it
    # if this call is the one that moved the status" guard, so a
    # theoretical double-processing bug can never double-increment the
    # counter either) — `_record_usage_increment` itself is an
    # unconditional no-op for a public repo or a `None` recorder.
    if transitioned and job.job_type == JobType.REVIEW:
        await _record_usage_increment(job, usage_counter_recorder)


async def run_dispatch_loop(
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    worker_id: str,
    poll_interval_seconds: float,
    stuck_job_warning_threshold_seconds: float,
    dry_run: bool = False,
    usage_counter_recorder: UsageCounterRecorder | None = None,
) -> None:
    """The Worker's main loop — see this module's own docstring for the
    full design. Runs forever; ``apps/worker/main.py`` is the only caller,
    via ``asyncio.run(...)``.

    ``dry_run`` (Phase 7 of the walking-skeleton plan — an unrelated,
    earlier "Phase 7," disambiguated the same way this module's own
    docstring already disambiguates the indexing plan's Phase 7) defaults
    to ``False`` so every pre-this-feature caller (this module's own test
    suite's fakes) is unaffected; the real composition root always passes
    ``WorkerSettings.dry_run`` explicitly.

    ``usage_counter_recorder`` (docs/plans/plan-deliver-admin-gui.md's
    Phase 7 section, Unit H, US-A19) defaults to ``None`` — see
    ``_run_claimed_job``'s own docstring for why.
    """
    while True:
        try:
            job = await job_queue.claim_next(worker_id)
        except Exception as exc:
            logger.error("claim_next failed", extra={"worker_id": worker_id, "error": str(exc)})
            await asyncio.sleep(poll_interval_seconds)
            continue

        if job is None:
            await asyncio.sleep(poll_interval_seconds)
            continue

        logger.info("job claimed", extra={"job_id": str(job.id), "job_type": job.job_type.value})
        try:
            await _run_claimed_job(
                job,
                job_queue=job_queue,
                review_registry=review_registry,
                indexing_deps=indexing_deps,
                memory_reaction_poll_deps=memory_reaction_poll_deps,
                stuck_job_warning_threshold_seconds=stuck_job_warning_threshold_seconds,
                dry_run=dry_run,
                usage_counter_recorder=usage_counter_recorder,
            )
        except Exception as exc:
            logger.error(
                "unhandled exception in dispatch loop while processing claimed job; "
                "marking job FAILED",
                extra={"job_id": str(job.id), "job_type": job.job_type.value, "error": str(exc)},
            )
            # `_mark_terminal_and_record` only records completion telemetry
            # when this call actually transitioned the row — e.g. if the bug
            # that landed us here happened after `_run_claimed_job` had
            # already called `mark_done`/`mark_failed` (and its own
            # completion metric) for this job, this is a no-op transition
            # and must not double-record the same job's completion under a
            # second outcome.
            await _mark_terminal_and_record(job_queue, job, outcome="failed")
