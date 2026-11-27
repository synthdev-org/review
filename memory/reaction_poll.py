"""memory.reaction_poll — US-M3's opportunistic reaction-polling job.

GitHub sends no webhook for a reaction added to a review comment — the only
way to observe one is to actively fetch it. This runs as
its own dedicated job type (``JobType.MEMORY_REACTION_POLL``,
``adapter/storage/models.py``), dispatched by
``apps/worker/dispatch.py`` alongside ``INDEX``/``REVIEW``, rather than
folded into the ``REVIEW`` pipeline's own macro steps — fetching *this PR's
own comment history* is an unrelated I/O concern from *this job's own
diff*, and a PR-``closed`` event (US-M3's second trigger) has no diff to
review at all.

``run_memory_reaction_poll_job`` is a plain async function, not a
``review.step.Step`` — this job type never enters ``StepRegistry``; it has
no findings, no dependency chain that framework's own machinery is for.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule — ``MemoryReactionPollJobPayload`` is a plain pydantic model (job-
payload boundary validation, the same convention
``indexing/pipeline.py``'s ``IndexingJobPayload`` already establishes), and
every real I/O dependency this job needs is one of the three
``Protocol``-typed fields on ``MemoryReactionPollJobDependencies``, wired to
concrete adapters only at the composition root (``apps/worker/main.py``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from memory.models import ReactionRecord
from memory.store import MemorySignalRecorder, MemoryStoreError, ReactionDedupStore, ReactionFetcher

logger = logging.getLogger(__name__)


class MemoryReactionPollJobFailedError(Exception):
    """Raised by ``run_memory_reaction_poll_job`` when the whole job cannot
    proceed — a ``ReactionDedupStore``/``ReactionFetcher``/
    ``MemorySignalRecorder`` failure before/between per-comment processing
    (e.g. a Postgres-level error, or the installation token exchange itself
    failing), as distinct from a single reaction's ``record_signal`` failure
    inside the per-comment loop, which is isolated to that one reaction and
    never raises this (see the loop's own ``except Exception`` below).

    Mirrors ``review.pipeline.PipelineFailedError``/
    ``indexing.pipeline.IndexingJobFailedError``'s role for ``REVIEW``/
    ``INDEX`` jobs: ``apps/worker/dispatch.py``'s dispatch loop catches this
    the same way, marking the job ``FAILED`` via its own typed-exception
    path rather than the generic "unhandled exception in dispatch loop"
    bug-resilience fallback used for genuine dispatch-loop bugs — so an
    on-call engineer can tell "this job legitimately failed" (a transient
    GitHub/Postgres error) from "the dispatch loop itself has a bug" the
    same way they already can for the other two job types.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"memory reaction poll job failed: {detail}")
        self.detail = detail


async def _fatal_on_error[T](awaitable: Awaitable[T]) -> T:
    """Await ``awaitable``, translating any exception it raises into
    ``MemoryReactionPollJobFailedError`` — the one place
    ``run_memory_reaction_poll_job`` below marks a call as "whole-job
    fatal", used at each of its three ``ReactionDedupStore``/
    ``ReactionFetcher`` calls instead of repeating the same
    ``try/except Exception as exc: raise MemoryReactionPollJobFailedError(...)
    from exc`` block inline at each call site.
    """
    try:
        return await awaitable
    except Exception as exc:
        raise MemoryReactionPollJobFailedError(detail=str(exc)) from exc


class MemoryReactionPollJobPayload(BaseModel):
    """Validated shape of a ``MEMORY_REACTION_POLL`` job's raw queue
    payload — built by ``apps/webhook_api/webhook_routes.py``'s
    ``_memory_reaction_poll_payload`` (the same ``org_id``/``repo_id``/
    ``installation_id``/``repo_full_name`` shape every other job payload's
    own ``_base_job_identity_fields`` already carries), parsed once at this
    module's own boundary, per architecture.md's "validate every boundary
    ... job payloads ... with pydantic models" rule.
    """

    model_config = ConfigDict(frozen=True)

    org_id: uuid.UUID
    repo_id: uuid.UUID
    installation_id: int
    repo_full_name: str
    pr_number: int


@dataclass(frozen=True, slots=True)
class MemoryReactionPollJobDependencies:
    """The domain-defined Protocols ``run_memory_reaction_poll_job`` depends
    on, plus its one plain-config field — wired to real adapters only at the
    composition root (``apps/worker/main.py``), per architecture.md's "wire
    concrete implementations only at the composition root" rule. Mirrors
    ``indexing.pipeline.IndexingJobDependencies``'s own bundling shape.
    """

    reaction_fetcher: ReactionFetcher
    signal_recorder: MemorySignalRecorder
    dedup_store: ReactionDedupStore
    max_comments_per_poll: int


async def _record_reaction_signal(
    deps: MemoryReactionPollJobDependencies,
    comment_id: int,
    reaction: tuple[int, str],
    pr_key: tuple[str, int],
) -> bool:
    """Record one reaction's signal; ``False`` if recording failed.
    ``reaction`` is ``(reaction_id, content)``, ``pr_key`` is
    ``(repo_id, pr_number)`` (logging only)."""
    # ``filter_new`` above already durably marked every id in
    # ``new_reaction_ids`` as captured (the dedup INSERT
    # commits in its own transaction, ahead of this loop) — so a
    # failure recording one reaction's signal must not abort the
    # whole job and lose every other already-fetched reaction too.
    # The failed id's own signal is still lost (a retry will find it
    # pre-marked in ``captured_reaction`` and skip it), which is a
    # known, accepted gap in the two-phase capture/record design —
    # but it must stay scoped to that one id, not the whole batch.
    try:
        await deps.signal_recorder.record_signal(
            posted_finding_comment_id=comment_id,
            raw_signal_type="reaction",
            raw_signal_detail={
                "reaction_id": reaction[0],
                "content": reaction[1],
            },
            github_comment_id=None,
        )
    except MemoryStoreError:
        logger.exception(
            "memory reaction poll: failed to record signal for an "
            "already-dedup-marked reaction; this reaction's signal "
            "is lost, continuing with the rest of the batch",
            extra={
                "repo_id": pr_key[0],
                "pr_number": pr_key[1],
                "posted_finding_comment_id": comment_id,
                "reaction_id": reaction[0],
            },
        )
        return False
    return True


async def _capture_comment_reactions(
    comment_id: int,
    reactions: Sequence[ReactionRecord],
    deps: MemoryReactionPollJobDependencies,
    pr_key: tuple[str, int],
) -> int:
    """Record every genuinely new reaction on one comment; return how many
    were captured. ``pr_key`` is ``(repo_id, pr_number)``, used for logging."""
    dedup_store = deps.dedup_store
    captured_count = 0
    if not reactions:
        return 0
    content_by_reaction_id = {reaction.reaction_id: reaction.content for reaction in reactions}
    new_reaction_ids = await _fatal_on_error(
        dedup_store.filter_new(
            posted_finding_comment_id=comment_id,
            reaction_ids=tuple(content_by_reaction_id),
        )
    )
    for reaction_id in new_reaction_ids:
        if await _record_reaction_signal(
            deps, comment_id, (reaction_id, content_by_reaction_id[reaction_id]), pr_key
        ):
            captured_count += 1
    return captured_count


def _log_candidates(
    pr_key: tuple[str, int], candidate_count: int, max_comments_per_poll: int
) -> None:
    logger.info(
        "memory reaction poll: polling candidate comments",
        extra={
            "repo_id": pr_key[0],
            "pr_number": pr_key[1],
            "candidate_comment_count": candidate_count,
            "max_comments_per_poll": max_comments_per_poll,
            # Reaching the ceiling doesn't by itself prove older comments
            # were dropped (this PR might have exactly this many posted
            # comments), but it's the only signal cheaply available here
            # without a second COUNT(*) query (an accepted, logged gap).
            "possibly_truncated": candidate_count >= max_comments_per_poll,
        },
    )


# US-M3's both `Given/When/Then` branches (a later push, or a PR-closed
# event) converge on this one function — the caller
# (``apps/worker/dispatch.py``) is the only thing that varies by trigger.
#
# 1. Look up the ``max_comments_per_poll`` most-recently-posted
#    ``posted_finding_comment`` rows for this PR (bounded cost — see
#    ``ReactionDedupStore.recent_comment_ids``'s own docstring).
# 2. Fetch each one's current reactions via one GitHub API call per
#    comment (``ReactionFetcher.fetch_reactions``) — a single comment's
#    fetch failing is already tolerated inside that call; this function
#    never sees which ones failed, only which ones came back.
# 3. For each comment that has any reactions, ask ``dedup_store`` which of
#    them are genuinely new (already-captured ones are silently dropped —
#    ``captured_reaction`` dedup) and persist one ``pending``
#    ``memory_entry`` per newly-observed reaction.
#
# Raises ``MemoryReactionPollJobFailedError`` for a genuine whole-job
# failure (e.g. a Postgres-level error, or the installation token exchange
# itself failing before any per-comment fetch even starts) — caught by
# ``apps/worker/dispatch.py``'s dispatch loop the same way it already
# catches ``PipelineFailedError``/``IndexingJobFailedError``, marking the
# job ``FAILED`` via its own typed path rather than the generic
# "unhandled exception in dispatch loop" bug-resilience fallback. A single
# reaction's own ``record_signal`` failure inside the per-comment loop is
# isolated to that one reaction and never raises this — nothing in this
# job's own design distinguishes a "some findings failed" partial outcome
# from a "the whole job is broken" one, the same per-item isolation those
# two pipelines' own per-stage handling already establishes.
async def run_memory_reaction_poll_job(
    *,
    repo_id: str,
    pr_number: int,
    installation_id: int,
    repo_full_name: str,
    deps: MemoryReactionPollJobDependencies,
) -> None:
    pr_key = (repo_id, pr_number)
    dedup_store = deps.dedup_store
    max_comments_per_poll = deps.max_comments_per_poll
    comment_ids = await _fatal_on_error(
        dedup_store.recent_comment_ids(
            repo_id=repo_id, pr_number=pr_number, limit=max_comments_per_poll
        )
    )
    if not comment_ids:
        logger.info(
            "memory reaction poll: no posted_finding_comment rows for this pr",
            extra={"repo_id": repo_id, "pr_number": pr_number},
        )
        return

    _log_candidates(pr_key, len(comment_ids), max_comments_per_poll)

    reactions_by_comment = await _fatal_on_error(
        deps.reaction_fetcher.fetch_reactions(
            installation_id=installation_id, repo_full_name=repo_full_name, comment_ids=comment_ids
        )
    )

    captured_count = 0
    for comment_id, reactions in reactions_by_comment.items():
        captured_count += await _capture_comment_reactions(
            comment_id, reactions, deps, (repo_id, pr_number)
        )

    logger.info(
        "memory reaction poll completed",
        extra={
            "repo_id": repo_id,
            "pr_number": pr_number,
            "polled_comment_count": len(reactions_by_comment),
            "captured_count": captured_count,
        },
    )
