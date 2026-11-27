"""apps.worker.dispatch — the Worker's claim/dispatch loop.

Polls ``JobQueueRepository.claim_next()`` (``adapter/storage/queue.py``)
every ``poll_interval_seconds`` when the queue is empty, and for each
claimed job dispatches by ``job_type`` — ``REVIEW`` jobs run through the
review pipeline registry (``review.pipeline.StepRegistry``).

``INDEX`` jobs call ``indexing.pipeline.run_indexing_job``, wired with real
adapters at the composition root (``apps/worker/main.py``) via
``indexing.pipeline.IndexingJobDependencies``. ``IndexingJobFailedError``
(the ``INDEX``-job analog of ``review.pipeline.PipelineFailedError``) is
caught by ``_run_claimed_job`` the same way — marking the job ``FAILED``
with a "job failed" log line, never the generic bug-resilience catch-all.

A third branch (US-M3): ``MEMORY_REACTION_POLL`` jobs run through
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
this case too. Not solved here, just named rather than left implicit.

**Stuck-job visibility, not resolution:** ``claim_next()`` only ever selects ``PENDING`` rows — it
never re-examines a stale ``CLAIMED`` row, so a job whose Worker process is terminated
mid-processing stays ``CLAIMED`` forever with no automatic recovery (no reclaim-timeout logic
exists yet). As a cheap, in-scope mitigation for the gap's *visibility*, not its resolution, a
claimed job's own dispatch coroutine runs as a single ``asyncio.Task`` awaited via
``asyncio.wait(...)`` in a loop rather than being awaited directly: every time the wait times out
before the task finishes, a ``warning`` log names the job as plausibly stuck, then the loop waits
again — turning "an operator has to think to run a SQL query" into "the existing structured-log
tooling surfaces it," without solving the underlying reclaim gap itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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


async def _run_memory_reaction_poll(
    job: ClaimedJob, memory_reaction_poll_deps: MemoryReactionPollJobDependencies
) -> None:
    """Parse the raw queue payload at the boundary, then run the reaction poll job."""
    parsed = MemoryReactionPollJobPayload.model_validate(job.payload)
    await run_memory_reaction_poll_job(
        repo_id=str(parsed.repo_id),
        pr_number=parsed.pr_number,
        installation_id=parsed.installation_id,
        repo_full_name=parsed.repo_full_name,
        deps=memory_reaction_poll_deps,
    )


# Dispatch one claimed job by its ``job_type``.
#
# ``INDEX`` jobs run through ``indexing.pipeline.run_indexing_job``
# — a fatal whole-job failure
# propagates as ``IndexingJobFailedError``, which this function does not
# catch; the caller (``_run_claimed_job``) is the one place that turns it
# into a ``FAILED`` status, mirroring ``REVIEW``'s own
# ``PipelineFailedError`` handling exactly. ``MEMORY_REACTION_POLL`` jobs
# (US-M3) run through
# ``memory.reaction_poll.run_memory_reaction_poll_job``, parsing the raw
# queue payload via ``MemoryReactionPollJobPayload`` first — the same
# parse-at-the-boundary shape ``run_indexing_job`` already establishes for
# ``INDEX``. ``REVIEW`` jobs run through ``review_registry`` the same way
# they always have.
#
# ``dry_run`` (``WorkerSettings.dry_run``, read once at Worker startup) is set on the shared
# ``StepContext`` every ``REVIEW`` step receives — never read from settings by any step itself,
# per ``review/output.py``'s own docstring. ``INDEX``/``MEMORY_REACTION_POLL`` jobs have no
# equivalent dry-run concept (per each spec's own scope): both always perform their real writes.
#
async def _dispatch_claimed_job(
    job: ClaimedJob,
    *,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    dry_run: bool,
    review_quality_v2: bool = False,
) -> None:
    logger.info("job dispatched", extra={"job_id": str(job.id), "job_type": job.job_type.value})

    if job.job_type == JobType.INDEX:
        await run_indexing_job(job.payload, indexing_deps, job_id=str(job.id))
        return

    if job.job_type == JobType.MEMORY_REACTION_POLL:
        await _run_memory_reaction_poll(job, memory_reaction_poll_deps)
        return

    # ``review_quality_v2`` is the per-process A8 switch, snapshotted onto this
    # job's context so every step of the job sees one value.
    context = StepContext(job_id=str(job.id), dry_run=dry_run, review_quality_v2=review_quality_v2)
    logger.info(
        "review job context",
        extra={
            "job_id": str(job.id),
            "review_quality_version": "v2" if review_quality_v2 else "v1",
        },
    )
    await review_registry.run(job.payload, context)


async def _await_task_warning_if_stuck(
    task: asyncio.Task[None], job: ClaimedJob, threshold_seconds: float
) -> None:
    """Wait for ``task`` to finish, logging once if it runs past the threshold."""
    done, _pending = await asyncio.wait({task}, timeout=threshold_seconds)
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


# Run ``job`` to completion — warning (not failing) if it runs past
# ``stuck_job_warning_threshold_seconds`` — and mark it ``DONE``/``FAILED``
# accordingly.
#
# Any exception other than ``PipelineFailedError``/``IndexingJobFailedError``/
# ``MemoryReactionPollJobFailedError`` escaping ``_dispatch_claimed_job``
# (a bug in the loop, not a pipeline/job-reported failure) is deliberately
# left uncaught here — the caller (``run_dispatch_loop``) is where that
# distinction is handled, per this module's own docstring.
#
async def _run_claimed_job(
    job: ClaimedJob,
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    stuck_job_warning_threshold_seconds: float,
    dry_run: bool,
    review_quality_v2: bool = False,
) -> None:
    task: asyncio.Task[None] = asyncio.create_task(
        _dispatch_claimed_job(
            job,
            review_registry=review_registry,
            indexing_deps=indexing_deps,
            memory_reaction_poll_deps=memory_reaction_poll_deps,
            dry_run=dry_run,
            review_quality_v2=review_quality_v2,
        )
    )
    await _await_task_warning_if_stuck(task, job, stuck_job_warning_threshold_seconds)

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


@dataclass(frozen=True)
class DispatchFlags:
    """Per-worker switches read once at start (``WorkerSettings.dry_run`` and
    ``WorkerSettings.review_quality_v2_enabled``); both default to ``False``
    so callers that predate them (this module's test suite's fakes) are
    unaffected."""

    dry_run: bool = False
    review_quality_v2: bool = False


# The Worker's main loop — see this module's own docstring for the
# full design. Runs forever; ``apps/worker/main.py`` is the only caller,
# via ``asyncio.run(...)``.
#
# ``flags.dry_run`` defaults to ``False`` so every pre-this-feature caller (this module's own
# test suite's fakes) is unaffected; the real composition root always passes
# ``WorkerSettings.dry_run`` explicitly.
#
# ``flags.review_quality_v2`` (``WorkerSettings.review_quality_v2_enabled``)
# is read once at worker start and defaults to ``False`` (v1).
#
async def run_dispatch_loop(
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    worker_id: str,
    poll_interval_seconds: float,
    stuck_job_warning_threshold_seconds: float,
    flags: DispatchFlags = DispatchFlags(),  # noqa: B008  # frozen, safe as a default
) -> None:
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
                dry_run=flags.dry_run,
                review_quality_v2=flags.review_quality_v2,
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
