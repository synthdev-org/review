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

from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository
from adapter.storage.schemas import ClaimedJob
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
) -> None:
    """Run ``job`` to completion — warning (not failing) if it runs past
    ``stuck_job_warning_threshold_seconds`` — and mark it ``DONE``/``FAILED``
    accordingly.

    Any exception other than ``PipelineFailedError``/``IndexingJobFailedError``/
    ``MemoryReactionPollJobFailedError`` escaping ``_dispatch_claimed_job``
    (a bug in the loop, not a pipeline/job-reported failure) is deliberately
    left uncaught here — the caller (``run_dispatch_loop``) is where that
    distinction is handled, per this module's own docstring.
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
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    worker_id: str,
    poll_interval_seconds: float,
    stuck_job_warning_threshold_seconds: float,
    dry_run: bool = False,
) -> None:
    """The Worker's main loop — see this module's own docstring for the
    full design. Runs forever; ``apps/worker/main.py`` is the only caller,
    via ``asyncio.run(...)``.

    ``dry_run`` (Phase 7 of the walking-skeleton plan) defaults to
    ``False`` so every pre-Phase-7 caller (this module's own test suite's
    fakes) is unaffected; the real composition root always passes
    ``WorkerSettings.dry_run`` explicitly.
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
            )
        except Exception as exc:
            logger.error(
                "unhandled exception in dispatch loop while processing claimed job; "
                "marking job FAILED",
                extra={"job_id": str(job.id), "job_type": job.job_type.value, "error": str(exc)},
            )
            await job_queue.mark_failed(job.id)
