"""memory.reaction_poll — US-M3's opportunistic reaction-polling job, per
``docs/plans/plan-deliver-memory.md``'s Phase 4 section (Unit D).

GitHub sends no webhook for a reaction added to a review comment — the only
way to observe one is to actively fetch it. Per Assumption 4, this runs as
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
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from memory.store import MemorySignalRecorder, ReactionDedupStore, ReactionFetcher

logger = logging.getLogger(__name__)


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


async def run_memory_reaction_poll_job(
    *,
    repo_id: str,
    pr_number: int,
    installation_id: int,
    repo_full_name: str,
    reaction_fetcher: ReactionFetcher,
    signal_recorder: MemorySignalRecorder,
    dedup_store: ReactionDedupStore,
    max_comments_per_poll: int,
) -> None:
    """US-M3's both `Given/When/Then` branches (a later push, or a PR-closed
    event) converge on this one function — the caller
    (``apps/worker/dispatch.py``) is the only thing that varies by trigger.

    1. Look up the ``max_comments_per_poll`` most-recently-posted
       ``posted_finding_comment`` rows for this PR (bounded cost, per the
       plan's own "not just bounded per-job" design — see
       ``ReactionDedupStore.recent_comment_ids``'s own docstring).
    2. Fetch each one's current reactions via one GitHub API call per
       comment (``ReactionFetcher.fetch_reactions``) — a single comment's
       fetch failing is already tolerated inside that call; this function
       never sees which ones failed, only which ones came back.
    3. For each comment that has any reactions, ask ``dedup_store`` which of
       them are genuinely new (already-captured ones are silently dropped —
       Assumption 9's ``captured_reaction`` dedup) and persist one ``pending``
       ``memory_entry`` per newly-observed reaction.

    Raises whatever ``ReactionDedupStore``/``ReactionFetcher``/
    ``MemorySignalRecorder`` themselves raise for a genuine whole-job
    failure (e.g. a Postgres-level error, or the installation token
    exchange itself failing before any per-comment fetch even starts) —
    uncaught here, propagating to ``apps/worker/dispatch.py``'s own generic
    bug-resilience handling, which marks the job ``FAILED``. This job type
    has no dedicated whole-job-failure exception of its own (unlike
    ``review.pipeline.PipelineFailedError``/``indexing.pipeline.IndexingJobFailedError``)
    — nothing in this job's own design distinguishes a "some findings failed"
    partial outcome from a "the whole job is broken" one the way those two
    pipelines' own per-stage isolation does.
    """
    comment_ids = await dedup_store.recent_comment_ids(
        repo_id=repo_id, pr_number=pr_number, limit=max_comments_per_poll
    )
    if not comment_ids:
        logger.info(
            "memory reaction poll: no posted_finding_comment rows for this pr",
            extra={"repo_id": repo_id, "pr_number": pr_number},
        )
        return

    logger.info(
        "memory reaction poll: polling candidate comments",
        extra={
            "repo_id": repo_id,
            "pr_number": pr_number,
            "candidate_comment_count": len(comment_ids),
            "max_comments_per_poll": max_comments_per_poll,
            # Reaching the ceiling doesn't by itself prove older comments
            # were dropped (this PR might have exactly this many posted
            # comments), but it's the only signal cheaply available here
            # without a second COUNT(*) query — per the plan's own
            # "accepted, logged gap" design.
            "possibly_truncated": len(comment_ids) >= max_comments_per_poll,
        },
    )

    reactions_by_comment = await reaction_fetcher.fetch_reactions(
        installation_id=installation_id, repo_full_name=repo_full_name, comment_ids=comment_ids
    )

    captured_count = 0
    for comment_id, reactions in reactions_by_comment.items():
        if not reactions:
            continue
        content_by_reaction_id = {reaction.reaction_id: reaction.content for reaction in reactions}
        new_reaction_ids = await dedup_store.filter_new(
            posted_finding_comment_id=comment_id,
            reaction_ids=tuple(content_by_reaction_id),
        )
        for reaction_id in new_reaction_ids:
            await signal_recorder.record_signal(
                posted_finding_comment_id=comment_id,
                raw_signal_type="reaction",
                raw_signal_detail={
                    "reaction_id": reaction_id,
                    "content": content_by_reaction_id[reaction_id],
                },
                github_comment_id=None,
            )
            captured_count += 1

    logger.info(
        "memory reaction poll completed",
        extra={
            "repo_id": repo_id,
            "pr_number": pr_number,
            "polled_comment_count": len(reactions_by_comment),
            "captured_count": captured_count,
        },
    )
