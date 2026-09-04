"""apps.worker.dispatch — the Worker's claim/dispatch loop, per the plan's
Phase 3 ("Worker job dispatch loop") section.

Replaces the Worker's Phase-0/1/2 terminal behavior (parse config, run
startup checks, log "worker idle: no job dispatch wired yet", exit 0) with
an actual loop: poll ``JobQueueRepository.claim_next()`` (already
implemented, ``adapter/storage/queue.py``, unchanged) every
``poll_interval_seconds`` when the queue is empty, and for each claimed job,
dispatch by ``job_type`` — ``REVIEW`` jobs run through Phase 2's pipeline
registry (``review.pipeline.StepRegistry``); ``INDEX`` jobs are logged and
left unimplemented (indexing is Phase 2 of the *roadmap*, not this plan) and
marked ``DONE`` immediately — "logged and left unimplemented" is itself this
job's complete, correct handling for this phase's scope, not a partial/
deferred state.

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

from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository
from adapter.storage.schemas import ClaimedJob
from review.pipeline import PipelineFailedError, StepRegistry
from review.step import StepContext

logger = logging.getLogger(__name__)


async def _dispatch_claimed_job(
    job: ClaimedJob, *, review_registry: StepRegistry, dry_run: bool
) -> None:
    """Dispatch one claimed job by its ``job_type``.

    ``INDEX`` jobs have no pipeline to run yet — logged as unimplemented
    and left at that; there is nothing pending for one, so this is this
    job's complete, correct handling for Phase 1's scope. ``REVIEW`` jobs
    run through ``review_registry`` — a fatal macro-step failure propagates
    as ``PipelineFailedError``, which this function does not catch; the
    caller (``_run_claimed_job``) is the one place that turns it into a
    ``FAILED`` status, so this function itself never touches job status.

    ``dry_run`` (Phase 7, ``WorkerSettings.dry_run``, read once at Worker
    startup) is set on the shared ``StepContext`` every step receives —
    never read from settings by any step itself, per
    ``review/output.py``'s own docstring.
    """
    if job.job_type == JobType.INDEX:
        logger.info(
            "index job dispatch: not implemented",
            extra={"job_id": str(job.id), "job_type": job.job_type.value},
        )
        return

    logger.info("job dispatched", extra={"job_id": str(job.id), "job_type": job.job_type.value})
    context = StepContext(job_id=str(job.id), dry_run=dry_run)
    await review_registry.run(job.payload, context)


async def _run_claimed_job(
    job: ClaimedJob,
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    stuck_job_warning_threshold_seconds: float,
    dry_run: bool,
) -> None:
    """Run ``job`` to completion — warning (not failing) if it runs past
    ``stuck_job_warning_threshold_seconds`` — and mark it ``DONE``/``FAILED``
    accordingly.

    Any exception other than ``PipelineFailedError`` escaping
    ``_dispatch_claimed_job`` (a bug in the loop, not a pipeline-reported
    failure) is deliberately left uncaught here — the caller
    (``run_dispatch_loop``) is where that distinction is handled, per this
    module's own docstring.
    """
    task: asyncio.Task[None] = asyncio.create_task(
        _dispatch_claimed_job(job, review_registry=review_registry, dry_run=dry_run)
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
    except PipelineFailedError as exc:
        await job_queue.mark_failed(job.id)
        logger.error(
            "job failed",
            extra={"job_id": str(job.id), "job_type": job.job_type.value, "error": str(exc)},
        )
        return

    await job_queue.mark_done(job.id)
    logger.info("job done", extra={"job_id": str(job.id), "job_type": job.job_type.value})


async def run_dispatch_loop(
    *,
    job_queue: JobQueueRepository,
    review_registry: StepRegistry,
    worker_id: str,
    poll_interval_seconds: float,
    stuck_job_warning_threshold_seconds: float,
    dry_run: bool = False,
) -> None:
    """The Worker's main loop — see this module's own docstring for the
    full design. Runs forever; ``apps/worker/main.py`` is the only caller,
    via ``asyncio.run(...)``.

    ``dry_run`` (Phase 7) defaults to ``False`` so every pre-Phase-7 caller
    (this module's own test suite's fakes) is unaffected; the real
    composition root always passes ``WorkerSettings.dry_run`` explicitly.
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
                stuck_job_warning_threshold_seconds=stuck_job_warning_threshold_seconds,
                dry_run=dry_run,
            )
        except Exception as exc:
            logger.error(
                "unhandled exception in dispatch loop while processing claimed job; "
                "marking job FAILED",
                extra={"job_id": str(job.id), "job_type": job.job_type.value, "error": str(exc)},
            )
            await job_queue.mark_failed(job.id)
