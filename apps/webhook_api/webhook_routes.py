"""apps.webhook_api.webhook_routes — the GitHub webhook receiver route.

Per the plan's Phase 4 section: "apps/webhook_api gains its first real
route: a webhook receiver that verifies the signature and returns 401/200
accordingly, with a request body size limit and basic rate limiting in
front of it." Phase 1 of `plan-phase-1-walking-skeleton.md` (this module's
prior state) added the rest of that Unit A on top of that trust boundary:
installation/org/repo resolution (auto-provisioning on first sight),
delivery-id idempotency, a declarative trigger-set lookup, a per-repo
enqueue rate limit, and the actual `JobQueueRepository.enqueue()` call.

Phase 1 of `plan-indexing-delivery.md` (this module's current state)
generalizes that single-job-type machinery to serve a second, independent
trigger kind (`INDEX`, alongside the existing `REVIEW`) without duplicating
it: `_JobCandidate`/`_handle_matched_triggers` replace the old
`REVIEW`-only `_handle_matched_trigger`, resolving installation/org/repo
and recording delivery idempotency exactly once per request (see
"Transaction boundary" below), then attempting an enqueue for every trigger
kind that matched this event — a `pull_request opened`/`synchronize`
delivery can therefore enqueue both a `REVIEW` and an `INDEX` job from the
same request, sharing one transaction.

``docs/plans/plan-deliver-memory.md``'s Phase 4 section (Unit D, US-M3) adds
a fourth, independent trigger kind (`MEMORY_REACTION_POLL`) to the same
`_JobCandidate`/`_handle_matched_triggers` fan-out: a `pull_request`
event whose action is `opened`/`synchronize`/`reopened`/`closed` also
enqueues a `MEMORY_REACTION_POLL` job from the same shared transaction —
unconditionally, with no kill switch or rate limiter of its own (Assumption
4), so it always accompanies `REVIEW`'s own trigger set plus `closed`
(US-M3's second `Given/When/Then` branch, which `REVIEW`/`INDEX` never
trigger on).

Rejection order matches the plan and Security review precisely: rate limit
(cheapest — no body read needed) -> body size limit (enforced while
streaming, before the full body is ever buffered) -> signature verification
(the trust boundary) -> payload shape validation. Every rejection is logged
at ``warning`` with the reason only — never the payload body, per the
plan's Observability & rollback section. Once past that boundary, this
module never rejects a well-formed, correctly-signed webhook with a 4xx/5xx
except for the one genuine mid-request-failure case documented on
`_handle_matched_triggers` below — every other non-enqueuing outcome (no
trigger match, a duplicate delivery, a rate-limited repo, missing required
fields, indexing disabled) is still accepted (200) and logged at ``info``
with the reason, per the plan's Phase 1 sections.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import metrics
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.github.schemas import (
    GitHubWebhookEnvelope,
    PullRequestReviewCommentWebhookEnvelope,
    PullRequestReviewThreadWebhookEnvelope,
    PushCommit,
)
from adapter.github.signature import verify_webhook_signature
from adapter.github.triggers import (
    is_enqueue_trigger,
    is_index_trigger,
    is_memory_capture_trigger,
    is_memory_reaction_poll_trigger,
)
from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository
from adapter.telemetry.sdk import suppress_telemetry_errors
from apps.webhook_api.rate_limiter import InMemoryRateLimiter
from findings.redact import TextRedactor
from ingestion.pr_metadata import (
    extract_issue_keys,
    is_fork_pr,
    normalize_head_ref,
    normalize_title,
)
from memory.reaction_poll import run_memory_reaction_poll_job
from memory.store import MemorySignalRecorder, MemoryStoreError, ReactionDedupStore, ReactionFetcher
from tenancy.resolution import ResolutionResult, TenancyStore, WebhookEvent, resolve_installation

# Skipped-reason values a rate-limited candidate can carry (used by the
# should-commit rule in `_handle_matched_triggers` below: a request where
# *nothing* enqueued and *something* was rate-limited rolls its whole
# transaction back, per that function's own docstring).
_RATE_LIMITED_REASONS = frozenset({"rate_limited", "index_rate_limited"})

logger = logging.getLogger(__name__)

# US8 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) — imports
# the OTel API directly, same style as `adapter/storage/queue.py` and
# `adapter/telemetry/heartbeat.py`. `reason` is the same bounded-cardinality
# literal already passed to `logger.warning("webhook rejected", ...)` below
# — never payload/body content, per the spec's Privacy NFR. Meter name is
# the catalog's mandated "review.webhook" literal, not `__name__` — this
# module's own dotted path (`apps.webhook_api.webhook_routes`) wouldn't
# match it.
meter = metrics.get_meter("review.webhook")
_rejected_counter = meter.create_counter(
    "review.webhook.rejected_total", description="Webhook deliveries rejected, by reason."
)

_SIGNATURE_HEADER = "x-hub-signature-256"
_DELIVERY_HEADER = "x-github-delivery"
_EVENT_TYPE_HEADER = "x-github-event"


def _reject_webhook(reason: str) -> None:
    """Logs the rejection and records it against ``_rejected_counter`` —
    one call per rejection site instead of a hand-copied log-then-count
    pair, so the log's ``reason`` and the metric's ``reason`` tag can never
    drift apart. The counter recording is best-effort — a
    telemetry-recording failure here must never suppress the caller's own
    ``raise HTTPException(...)`` for a genuine rejection, which would
    otherwise surface to GitHub as an unhandled 500 instead of the intended
    4xx."""
    logger.warning("webhook rejected", extra={"reason": reason})
    with suppress_telemetry_errors(
        logger,
        "webhook rejection telemetry recording failed (rejection unaffected)",
        reason=reason,
    ):
        _rejected_counter.add(1, {"reason": reason})


class _BodyTooLargeError(Exception):
    """Raised internally when the request body exceeds the configured
    limit; never crosses this module's boundary as a raised exception —
    the route handler below catches it and returns a 413."""


async def _read_body_within_limit(request: Request, *, max_bytes: int) -> bytes:
    """Read ``request``'s body, aborting as soon as more than ``max_bytes``
    have arrived rather than buffering an unbounded amount first.

    Checks ``Content-Length`` up front as a fast rejection when the client
    is honest about an oversized body, but does not rely on it being
    present or accurate — the streaming loop below enforces the real limit
    regardless (chunked-transfer requests may omit ``Content-Length``
    entirely, and it's just a client-supplied header in any case).
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise _BodyTooLargeError()

    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _BodyTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


def _build_webhook_event(envelope: GitHubWebhookEnvelope, *, delivery_id: str) -> WebhookEvent:
    """Translate the adapter's own envelope into the domain-owned
    ``WebhookEvent`` — the adapter-to-domain translation happens once, here
    at the boundary, so ``tenancy.resolution`` never imports
    ``adapter.github`` (see that module's own docstring).

    Only called once the route has already returned early for a
    ``repository is None`` envelope (a ``ping``/``installation``-family
    delivery) — the assert below documents that invariant for the type
    checker rather than silently trusting it.
    """
    assert envelope.repository is not None
    pull_request = envelope.pull_request
    return WebhookEvent(
        delivery_id=delivery_id,
        installation_id=envelope.installation.id,
        action=envelope.action,
        repo_full_name=envelope.repository.full_name,
        pr_number=pull_request.number if pull_request is not None else None,
        head_sha=(
            pull_request.head.sha
            if pull_request is not None and pull_request.head is not None
            else None
        ),
        base_sha=(
            pull_request.base.sha
            if pull_request is not None and pull_request.base is not None
            else None
        ),
        repo_private=envelope.repository.private,
        pr_title=normalize_title(pull_request.title) if pull_request is not None else None,
        pr_head_ref=(
            normalize_head_ref(pull_request.head.ref)
            if pull_request is not None and pull_request.head is not None
            else None
        ),
        # The PR body is read only inside `extract_issue_keys` and dropped
        # here; it is never placed on the event or the job payload.
        pr_issue_keys=(
            extract_issue_keys(
                pull_request.title,
                pull_request.head.ref if pull_request.head is not None else None,
                pull_request.body,
            )
            if pull_request is not None
            else ()
        ),
        pr_is_fork=_pr_is_fork(envelope) if pull_request is not None else None,
    )


def _pr_is_fork(envelope: GitHubWebhookEnvelope) -> bool:
    """
    Derive whether the PR is a fork, failing closed (A1(b)).

    Works over the envelope's head/base repo objects; the base repo falls back
    to the envelope's own repository.
    """
    assert envelope.pull_request is not None and envelope.repository is not None
    head = envelope.pull_request.head
    base = envelope.pull_request.base
    head_repo = head.repo if head is not None else None
    base_repo = base.repo if base is not None else None
    return is_fork_pr(
        head_repo_full_name=head_repo.full_name if head_repo is not None else None,
        head_repo_fork=head_repo.fork if head_repo is not None else None,
        base_repo_full_name=(
            base_repo.full_name
            if base_repo is not None and base_repo.full_name is not None
            else envelope.repository.full_name
        ),
    )


def _base_job_identity_fields(event: WebhookEvent, result: ResolutionResult) -> dict[str, object]:
    """The `org_id`/`repo_id`/`installation_id`/`repo_full_name` fields
    every job payload below carries, regardless of job type — shared here
    rather than independently re-assembled at each of `_review_enqueue_payload`'s
    and `_index_enqueue_payload`'s own return points.

    `repo_full_name` (Phase 4 addition): the Worker's diff-fetch step
    (`review/prefilter.py`, `adapter/github/diff.py`) needs GitHub's own
    "owner/repo" identifier to call the Files/Trees/Blobs APIs and to
    request a repo-scoped installation token — it cannot derive that from
    `org_id`/`repo_id` alone (this platform's own UUIDs), and the Worker
    only ever sees this job's `payload`, never the original webhook
    envelope `event` came from. Sourced from `event.repo_full_name`
    (`tenancy.resolution.WebhookEvent`, already parsed from
    `GitHubWebhookEnvelope` at this route's own boundary) — closing a gap
    where this field was resolved here but never threaded into the
    payload the Worker actually reads.

    `private`: sourced from `event.repo_private` the same way
    `repo_full_name` is, so the review pipeline (product-knowledge
    gating) knows whether the repo is private without a second GitHub API
    round-trip. Carried on every job payload
    (not only `REVIEW`'s), matching `installation_id`/`repo_full_name`'s
    own "shared base field, even though only some job types consume it"
    placement — `INDEX`/`MEMORY_REACTION_POLL` jobs simply never read it.
    """
    return {
        "org_id": str(result.org_id),
        "repo_id": str(result.repo_id),
        "installation_id": event.installation_id,
        "repo_full_name": event.repo_full_name,
        "private": event.repo_private,
    }


def _review_enqueue_payload(
    event: WebhookEvent, result: ResolutionResult
) -> dict[str, object] | None:
    """Build the `REVIEW` job payload, or `None` when `event` is missing a
    field a review job cannot run without (the caller logs this as
    `missing_required_fields` and enqueues nothing for this candidate).
    """
    if event.pr_number is None or event.head_sha is None:
        return None
    return {
        **_base_job_identity_fields(event, result),
        "pr_number": event.pr_number,
        "head_sha": event.head_sha,
        # Optional (per docs/plans/plan-deliver-context-builder.md's Phase 1
        # section) — `event.base_sha` can legitimately be `None` when
        # GitHub's payload omits `pull_request.base.sha`; a review job still
        # runs without it (Phase 5's Context Builder skips the pre-change
        # fetch and degrades to an empty blast-radius context, never a
        # missing-required-field rejection the way `pr_number`/`head_sha`
        # above are).
        "base_sha": event.base_sha,
        # Product-knowledge capture (docs/plans/plan-deliver-product-knowledge.md
        # Phase 2, A1): REVIEW only. No PR body is ever included.
        "pr_title": event.pr_title,
        "pr_head_ref": event.pr_head_ref,
        "pr_issue_keys": list(event.pr_issue_keys),
        "pr_is_fork": event.pr_is_fork,
    }


def _memory_reaction_poll_payload(
    event: WebhookEvent, result: ResolutionResult
) -> dict[str, object] | None:
    """Build the `MEMORY_REACTION_POLL` job payload
    (``docs/plans/plan-deliver-memory.md``'s Phase 4 section, Unit D), or
    `None` when `event` is missing the one field this job type cannot run
    without — mirrors `_review_enqueue_payload`'s own shape, but needs only
    `pr_number` (no `head_sha`/`commit_sha`: this job polls reactions on
    already-posted comments, it never fetches a diff).
    """
    if event.pr_number is None:
        return None
    return {
        **_base_job_identity_fields(event, result),
        "pr_number": event.pr_number,
    }


def _merged_push_file_lists(
    commits: Sequence[PushCommit],
) -> tuple[list[str], list[str], list[str]]:
    """Merge a `push` payload's per-commit `added`/`removed`/`modified`
    lists into one flat, de-duplicated (insertion-order-preserving) triple —
    the shape `indexing/pipeline.py`'s Phase 7 file-set resolution consumes
    directly. GitHub's `push` payload only ever gives per-commit lists, not
    a single already-merged "net change of this push" list, so this merge
    is this payload builder's own responsibility, not something upstream
    already did for us.

    Callers must pass `envelope.head_commit` alongside `envelope.commits`
    (see `_index_enqueue_payload`) — GitHub's `commits` array excludes
    merge commits, so a push whose only new commit is a merge (e.g. landed
    via the "Merge pull request" button) has an empty `commits` array with
    the actual file changes present only on `head_commit`. Re-merging the
    same paths `head_commit` and the last entry of `commits` already share,
    in the ordinary non-merge-commit case, is harmless: this dict-keyed
    merge is idempotent on path.
    """
    added: dict[str, None] = {}
    removed: dict[str, None] = {}
    modified: dict[str, None] = {}
    for commit in commits:
        added.update(dict.fromkeys(commit.added))
        removed.update(dict.fromkeys(commit.removed))
        modified.update(dict.fromkeys(commit.modified))
    return list(added), list(removed), list(modified)


def _index_enqueue_payload(
    *,
    event_type: str,
    envelope: GitHubWebhookEnvelope,
    event: WebhookEvent,
    result: ResolutionResult,
) -> dict[str, object] | None:
    """Build the `INDEX` job payload Phase 7's pipeline consumes, per
    Assumption 9's `pr_number` contract (`0` for a `push`-triggered job,
    the real PR number for a `pull_request`-triggered job) — or `None` when
    the triggering event is missing a field an index job cannot run
    without.
    """
    if event_type == "push":
        if envelope.after is None or envelope.deleted:
            # A branch/tag deletion push: `after` is the all-zeros SHA (a
            # string, not `None`) — GitHub's own `deleted` flag is what
            # actually distinguishes this from a real push, so `after is
            # None` alone never catches it. Nothing to index for a commit
            # that never existed.
            return None
        commits_with_head = (
            (*envelope.commits, envelope.head_commit)
            if envelope.head_commit is not None
            else envelope.commits
        )
        added, removed, modified = _merged_push_file_lists(commits_with_head)
        return {
            **_base_job_identity_fields(event, result),
            "pr_number": 0,
            "commit_sha": envelope.after,
            "added": added,
            "removed": removed,
            "modified": modified,
            # US8's fallback signal: GitHub caps the `commits` array at 20
            # entries and sets no other "there's more" marker on this
            # payload shape, so a full commits array is itself the
            # truncation signal; a non-fast-forward update (`forced`) can
            # equally invalidate the per-commit lists above as the correct
            # delta, so either condition forces Phase 7's full-tree-diff
            # fallback rather than trusting `added`/`removed`/`modified`
            # above.
            "force_full_tree_fallback": len(envelope.commits) == 20 or bool(envelope.forced),
        }
    # pull_request
    if event.pr_number is None or event.head_sha is None:
        return None
    return {
        **_base_job_identity_fields(event, result),
        "pr_number": event.pr_number,
        "commit_sha": event.head_sha,
        # A `pull_request` payload never carries per-commit file lists at
        # all (only `push` events do) — there is no delta data to give
        # Phase 7's pipeline here, so the fallback signal is unconditional
        # for a PR-triggered job rather than misrepresenting "no data" as
        # "no files changed".
        "added": [],
        "removed": [],
        "modified": [],
        "force_full_tree_fallback": True,
    }


async def _run_in_transaction[T](
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event: WebhookEvent,
    work: Callable[[AsyncSession], Awaitable[tuple[T, bool]]],
) -> T:
    """Runs ``work(session)`` inside one shared transaction, committing on
    success unless ``work`` itself signals otherwise (its returned
    ``should_commit`` bool) — the one transaction-wrapper shape both
    ``_handle_matched_triggers`` and the trigger-not-matched route branch
    need, per the plan's Phase 1 "Transaction boundary" section.

    Returns just ``result`` — neither caller needs ``should_commit`` back
    (``_handle_matched_triggers`` computes it purely to decide whether to
    commit here and has no further use for it once this call returns; the
    trigger-not-matched branch's ``work`` always commits), so it isn't
    part of this function's own return value.

    A mid-request failure (Postgres blip, an ``enqueue()`` exception) rolls
    the whole transaction back — so GitHub's own retry of that same
    delivery is processed cleanly from scratch rather than some of this
    work being durably committed with the rest lost. Logged at ``warning``
    (self-healing via GitHub's own retry, not a confirmed defect) and
    re-raised as an ``HTTPException(500)`` — surfaced, not swallowed.

    ``should_commit=False`` (used by ``_handle_matched_triggers`` whenever
    at least one candidate was rate-limited, even if a sibling candidate in
    the same request enqueued successfully) rolls back without treating it
    as a failure — rate limiting is not the same as "this delivery was
    processed"; committing the delivery-id record in that case would
    durably mark it "seen" (for every candidate, since the guard is
    per-delivery, not per-candidate) with the rate-limited candidate's own
    job never enqueued, permanently losing that trigger even after the
    rate-limit window passes and a later redelivery of the exact same
    delivery id arrives. A sibling candidate that did enqueue successfully
    this same pass rolls back too, rather than committing it while
    discarding the rate-limited one — it re-enqueues cleanly on that same
    later redelivery instead.
    """
    async with session_factory() as session:
        await session.begin()
        try:
            result, should_commit = await work(session)
            if should_commit:
                await session.commit()
            else:
                await session.rollback()
            return result
        except HTTPException:
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            logger.warning(
                "enqueue transaction rolled back",
                extra={
                    "delivery_id": event.delivery_id,
                    "installation_id": event.installation_id,
                },
                exc_info=exc,
            )
            raise HTTPException(status_code=500, detail="failed to process webhook") from exc


@dataclass(frozen=True)
class _JobCandidate:
    """One job type this request's event matched a trigger for, and
    everything ``_handle_matched_triggers`` needs to attempt enqueuing it.

    ``payload_builder`` takes the ``ResolutionResult`` (only known once
    ``resolve_installation`` has actually run inside the shared
    transaction) and returns the job payload, or ``None`` when this
    specific candidate is missing a field its job type cannot run without
    — logged as ``missing_fields_reason`` rather than attempted.
    """

    job_type: JobType
    payload_builder: Callable[[ResolutionResult], dict[str, object] | None]
    missing_fields_reason: str
    rate_limiter: InMemoryRateLimiter | None
    rate_limited_reason: str


async def _handle_matched_triggers(
    *,
    event: WebhookEvent,
    store: TenancyStore,
    job_queue: JobQueueRepository,
    session_factory: async_sessionmaker[AsyncSession],
    candidates: Sequence[_JobCandidate],
) -> tuple[ResolutionResult, list[tuple[JobType, uuid.UUID | None, str | None]]]:
    """Resolve installation/org/repo, record the delivery once, then attempt
    an enqueue for every ``candidate`` this request's event matched a
    trigger for — all inside one shared transaction (see
    ``_run_in_transaction``). Replaces the old ``REVIEW``-only
    ``_handle_matched_trigger``: the delivery-idempotency/org-repo
    resolution work happens exactly once per request regardless of how many
    trigger kinds matched, so a ``pull_request opened``/``synchronize``
    event that matches both ``REVIEW`` and ``INDEX`` never resolves
    installation twice or records the delivery twice.

    Returns one ``(job_type, job_id, skipped_reason)`` triple per
    candidate, in the same order as ``candidates`` — every candidate
    produces exactly one outcome (an enqueued ``job_id`` or a
    ``skipped_reason``), never both.
    """

    async def _work(
        session: AsyncSession,
    ) -> tuple[tuple[ResolutionResult, list[tuple[JobType, uuid.UUID | None, str | None]]], bool]:
        result = await resolve_installation(event, store, unit_of_work=session)

        if not result.is_new_delivery:
            duplicate_outcomes: list[tuple[JobType, uuid.UUID | None, str | None]] = [
                (candidate.job_type, None, "duplicate_delivery") for candidate in candidates
            ]
            return (result, duplicate_outcomes), True

        outcomes: list[tuple[JobType, uuid.UUID | None, str | None]] = []
        # Reservations made so far this call, released together if a later
        # candidate's `enqueue()` call fails and the whole transaction is
        # about to roll back — otherwise an earlier candidate's successful
        # in-process reservation would durably (if only in-memory) survive
        # a rollback that undoes the row it was gating, permanently
        # burning a slot for work that never actually landed.
        reservations: list[tuple[InMemoryRateLimiter, str, int]] = []
        for candidate in candidates:
            payload = candidate.payload_builder(result)
            if payload is None:
                outcomes.append((candidate.job_type, None, candidate.missing_fields_reason))
                continue

            token: int | None = None
            if candidate.rate_limiter is not None:
                allowed, token = candidate.rate_limiter.reserve(str(result.repo_id))
                if not allowed:
                    outcomes.append((candidate.job_type, None, candidate.rate_limited_reason))
                    continue

            try:
                job_id = await job_queue.enqueue(
                    candidate.job_type,
                    payload,
                    org_id=result.org_id,
                    repo_id=result.repo_id,
                    session=session,
                )
            except Exception:
                # Same reasoning as the single-candidate design this
                # replaces: release every reservation made during this
                # call (this candidate's own, plus any earlier candidate's)
                # so GitHub's redelivery of this same logical event isn't
                # spuriously double-charged against the small per-repo
                # budget once the transaction below rolls everything back.
                if candidate.rate_limiter is not None and token is not None:
                    candidate.rate_limiter.release(str(result.repo_id), token)
                for limiter, key, reserved_token in reservations:
                    limiter.release(key, reserved_token)
                raise

            if candidate.rate_limiter is not None and token is not None:
                reservations.append((candidate.rate_limiter, str(result.repo_id), token))
            outcomes.append((candidate.job_type, job_id, None))

        any_rate_limited = any(
            reason in _RATE_LIMITED_REASONS for _job_type, _job_id, reason in outcomes
        )
        should_commit = not any_rate_limited
        if not should_commit:
            # The whole transaction rolls back together — including any
            # sibling candidate's own successful enqueue this same pass
            # (e.g. REVIEW, when INDEX was the one rate-limited) — never
            # just the rate-limited candidate's own outcome. The
            # delivery-id guard above is per-*delivery*, not per-candidate:
            # committing a sibling's successful enqueue here would still
            # commit (and thus permanently mark "seen") the very delivery
            # id a later redelivery needs to re-attempt the rate-limited
            # candidate against, silently losing that candidate forever
            # once the rate-limit window passes, even though this
            # function's/`` _run_in_transaction``'s own stated intent is
            # that a rate-limited outcome always remains retriable. Every
            # candidate that DID succeed this pass must release its own
            # rate-limiter reservation too, or its slot is burned for a job
            # this rollback just discarded.
            for limiter, key, reserved_token in reservations:
                limiter.release(key, reserved_token)
        return (result, outcomes), should_commit

    result, outcomes = await _run_in_transaction(session_factory, event=event, work=_work)
    # Nothing to record here: `JobQueueRepository.enqueue()` itself
    # registers a one-shot `after_commit` hook on this shared session for
    # every candidate it enqueues above (see that method's own docstring),
    # so telemetry for every candidate that actually committed is already
    # recorded by the time `_run_in_transaction` returns — and, since that
    # hook only fires on a real commit, a rate-limited pass that rolled the
    # whole transaction back (`should_commit=False`) correctly records
    # nothing for any candidate, sibling successes included.
    return result, outcomes


# Every outcome `_handle_memory_capture` can return, per its own docstring —
# `create_webhook_router`'s route handler's own info-level summary log line
# maps `capture_reason` through this dict rather than an if/elif chain that
# would re-derive the same enumeration at a second call site. `"captured"`/
# `"capture_failed"` get their own distinct wording; the two routine,
# expected non-events share one, so a genuine failure never reads as an
# unremarkable no-op at the info level (see that log line's own comment).
_MEMORY_CAPTURE_LOG_MESSAGES: dict[str, str] = {
    "captured": "memory capture accepted",
    "capture_failed": "memory capture accepted; capture attempt failed",
    "not_a_platform_comment": "memory capture accepted; not captured",
    "capture_ceiling_exceeded": "memory capture accepted; not captured",
}


async def _handle_memory_capture(
    *,
    raw_payload: object,
    event_type: str,
    action: str | None,
    repo_id: uuid.UUID,
    provenance_recorder: MemorySignalRecorder,
    text_redactor: TextRedactor,
    capture_ceiling_limiter: InMemoryRateLimiter,
) -> str:
    """Attempt to capture a reply/thread-resolution raw signal for a
    webhook event already confirmed to match ``is_memory_capture_trigger``
    (``adapter/github/triggers.py``), per
    ``docs/plans/plan-deliver-memory.md``'s Phase 3 section (Unit C,
    US-M1/US-M2).

    Deliberately outside ``_handle_matched_triggers``'s own job-queue
    transaction (per the plan's own design): this writes directly to
    ``memory_entry``, never enqueues a job, and relies on
    ``MemorySignalRecorder.record_signal``'s own ``ON CONFLICT DO NOTHING``
    upsert for redelivery-safety rather than the job-queue path's
    delivery-id guard.

    Returns the outcome reason for the caller's own accepted-but-not-
    enqueued-style log line: ``"captured"``, ``"not_a_platform_comment"``
    (the webhook comment/thread this signal is about isn't one the
    platform posted, or isn't a reply to one — including a malformed
    envelope, treated the same as "no match" rather than a 4xx, per this
    route's "every non-enqueuing outcome is still accepted" posture),
    ``"capture_ceiling_exceeded"`` (the per-repo daily cap — see
    ``apps/webhook_api/settings.py``'s ``memory_capture_ceiling_per_repo_per_day``),
    or ``"capture_failed"`` (a genuine Postgres-level failure — logged at
    ``error`` and swallowed here, never failing the webhook response
    itself, the same "a secondary write-path concern" posture Phase 2's own
    provenance capture already establishes for `GitHubFindingsPublisher.post_review`).
    For a ``pull_request_review_thread`` event with more than one correlating
    comment id, each id is its own capture attempt (its own ceiling check,
    its own isolated ``MemoryStoreError``) — ``"captured"`` is returned if
    *any* candidate succeeded (even if a later one then hit the ceiling or
    failed), so a partial per-thread outcome is never reported as a total
    failure/rejection.
    """

    def _ceiling_exceeded() -> bool:
        # Shared by both branches below: the ceiling bounds actual capture
        # attempts (and the Postgres lookup/write each one costs), not the
        # volume of unrelated webhook deliveries a repo happens to receive,
        # so each branch only calls this once it has a genuine capture
        # candidate in hand.
        if capture_ceiling_limiter.allow(str(repo_id)):
            return False
        logger.warning(
            "memory capture rejected",
            extra={"repo_id": str(repo_id), "reason": "capture_ceiling_exceeded"},
        )
        return True

    try:
        if event_type == "pull_request_review_comment":
            comment_envelope = PullRequestReviewCommentWebhookEnvelope.model_validate(raw_payload)
            in_reply_to_id = comment_envelope.comment.in_reply_to_id
            if in_reply_to_id is None:
                # A fresh top-level review comment, not a reply — Assumption
                # 3, never captured. Checked before the ceiling: an ordinary
                # (non-reply) review comment isn't a capture candidate at
                # all, so it must not consume the per-repo daily quota meant
                # to bound genuine capture attempts.
                return "not_a_platform_comment"

            if _ceiling_exceeded():
                return "capture_ceiling_exceeded"

            redacted_body = await text_redactor.redact(comment_envelope.comment.body)
            found = await provenance_recorder.record_signal(
                posted_finding_comment_id=in_reply_to_id,
                raw_signal_type="reply",
                raw_signal_detail={"body": redacted_body},
                github_comment_id=comment_envelope.comment.id,
            )
            return "captured" if found else "not_a_platform_comment"

        # pull_request_review_thread — "resolved" or "unresolved".
        thread_envelope = PullRequestReviewThreadWebhookEnvelope.model_validate(raw_payload)
        comment_ids = [comment.id for comment in thread_envelope.thread.comments]
        if not comment_ids:
            return "not_a_platform_comment"

        resolved = action == "resolved"
        # The thread event's own comment id, per `MemoryEntryRow.github_comment_id`'s
        # own docstring — the most recent comment in the thread (the last
        # entry GitHub's own `thread.comments` array carries), independent
        # of which id among them turns out to correlate to a
        # `posted_finding_comment` row below.
        thread_github_comment_id = comment_ids[-1]
        # Record against every candidate id that correlates to a
        # `posted_finding_comment` row, not just the first — a single
        # resolved/unresolved thread can contain more than one of the
        # platform's own posted findings (e.g. two separate review passes
        # attaching comments to the same line/thread), and each still needs
        # its own `thread_resolution` signal.
        #
        # The ceiling is checked once per candidate here, not once for the
        # whole thread — each iteration is its own Postgres lookup/write,
        # exactly what the ceiling bounds (see `_ceiling_exceeded`'s own
        # comment), so a thread with several correlating comments must
        # consume several units of the daily cap, not one. A candidate's own
        # `MemoryStoreError` is isolated to that candidate rather than
        # aborting the loop, so one transient failure doesn't discard
        # signals already durably recorded for earlier candidates in this
        # same thread.
        # A single `outcome` set in priority order as candidates are
        # processed — "captured" (any candidate succeeded) outranks
        # "capture_failed" (a candidate's own `MemoryStoreError`), which
        # outranks "capture_ceiling_exceeded" (nothing succeeded or failed
        # before the ceiling stopped the loop), which outranks the
        # "not_a_platform_comment" baseline (nothing correlated at all).
        # Each `if outcome == ...`/`if outcome != ...` guard below only ever
        # *raises* the outcome's priority, never lowers it.
        outcome = "not_a_platform_comment"
        for candidate_id in comment_ids:
            if _ceiling_exceeded():
                if outcome == "not_a_platform_comment":
                    outcome = "capture_ceiling_exceeded"
                break
            try:
                found = await provenance_recorder.record_signal(
                    posted_finding_comment_id=candidate_id,
                    raw_signal_type="thread_resolution",
                    raw_signal_detail={"resolved": resolved},
                    github_comment_id=thread_github_comment_id,
                )
            except MemoryStoreError as exc:
                if outcome != "captured":
                    outcome = "capture_failed"
                logger.error(
                    "memory capture failed for one thread candidate; continuing with the rest",
                    extra={
                        "repo_id": str(repo_id),
                        "event_type": event_type,
                        "posted_finding_comment_id": candidate_id,
                    },
                    exc_info=exc,
                )
                continue
            if found:
                outcome = "captured"
        return outcome
    except ValidationError:
        logger.warning(
            "memory capture rejected",
            extra={"repo_id": str(repo_id), "reason": "invalid_payload_shape"},
        )
        return "not_a_platform_comment"
    except MemoryStoreError as exc:
        logger.error(
            "memory capture failed",
            extra={"repo_id": str(repo_id), "event_type": event_type},
            exc_info=exc,
        )
        return "capture_failed"


async def _run_synchronous_reaction_poll(
    *,
    event: WebhookEvent,
    result: ResolutionResult,
    reaction_fetcher: ReactionFetcher,
    signal_recorder: MemorySignalRecorder,
    dedup_store: ReactionDedupStore,
    max_comments_per_poll: int,
) -> str:
    """US-M3's second `Given/When/Then` branch (``spec-memory.md`` lines
    273-275): runs the same reaction-fetch-and-capture logic
    ``memory.reaction_poll.run_memory_reaction_poll_job`` performs for a
    `MEMORY_REACTION_POLL` job, but synchronously and inline — called by the
    route handler for a `pull_request closed` event, before it returns its
    HTTP response. Enqueuing this as an async job like every other trigger
    this route handles would only satisfy the spec's *first*
    `Given/When/Then` branch ("a later push enqueues a new REVIEW job"),
    never this one's literal "the same reaction fetch runs once for that PR
    before the webhook handler returns" requirement.

    ``event.pr_number is None`` mirrors ``_memory_reaction_poll_payload``'s
    own guard — nothing to poll without a PR number, returns
    ``"missing_required_fields"`` rather than attempting the call.

    A genuine whole-job failure (a Postgres-level error, or the
    installation-token exchange itself failing before any per-comment fetch
    even starts — see ``run_memory_reaction_poll_job``'s own docstring,
    which raises ``MemoryReactionPollJobFailedError`` for exactly this case)
    is logged at ``error`` and swallowed here, never failing the webhook
    response itself — the same "a secondary write-path concern, not this
    route's own trust boundary" posture ``_handle_memory_capture`` above
    already establishes for its own Postgres writes. A single comment's own
    reaction fetch failing is already tolerated inside
    ``GitHubReactionFetcher`` itself, and a single reaction's own
    ``record_signal`` failure is isolated and logged inside
    ``run_memory_reaction_poll_job``'s own per-comment loop — neither
    ordinarily reaches this function as an exception at all.

    The catch below is still bare ``except Exception``, not narrowed to
    ``MemoryReactionPollJobFailedError`` — deliberately: this function's own
    "never failing the webhook response itself" promise is unconditional,
    not scoped to Postgres/GitHub failures alone. A genuine bug inside this
    secondary write path (a ``TypeError``/``KeyError`` from a code defect,
    not just an anticipated infra failure) must not be allowed to take down
    a GitHub-facing webhook response any more than an anticipated one would
    — the same "belt-and-braces, every failure mode, not just the
    anticipated ones" posture ``adapter/github/posting.py``'s own
    ``post_review`` already establishes for its own terminal-step provenance
    capture (see that module's docstring's "Provenance persistence"
    section). Narrowing this to a specific exception type would trade a
    known, accepted risk (a genuinely unanticipated bug here fails silently
    instead of loudly) for a worse one (a bug in a secondary, best-effort
    signal-capture path takes down the primary webhook response GitHub is
    waiting on).
    """
    if event.pr_number is None:
        return "missing_required_fields"

    try:
        await run_memory_reaction_poll_job(
            repo_id=str(result.repo_id),
            pr_number=event.pr_number,
            installation_id=event.installation_id,
            repo_full_name=event.repo_full_name,
            reaction_fetcher=reaction_fetcher,
            signal_recorder=signal_recorder,
            dedup_store=dedup_store,
            max_comments_per_poll=max_comments_per_poll,
        )
    except Exception as exc:
        logger.error(
            "synchronous memory reaction poll failed",
            extra={
                "repo_id": str(result.repo_id),
                "pr_number": event.pr_number,
                "installation_id": event.installation_id,
            },
            exc_info=exc,
        )
        return "reaction_poll_failed"
    return "reaction_poll_completed"


def create_webhook_router(
    *,
    webhook_secret: SecretStr,
    rate_limiter: InMemoryRateLimiter,
    max_body_bytes: int,
    tenancy_store: TenancyStore,
    job_queue: JobQueueRepository,
    session_factory: async_sessionmaker[AsyncSession],
    enqueue_rate_limiter: InMemoryRateLimiter | None,
    indexing_enabled: bool,
    index_enqueue_rate_limiter: InMemoryRateLimiter | None,
    provenance_recorder: MemorySignalRecorder,
    text_redactor: TextRedactor,
    memory_capture_ceiling_limiter: InMemoryRateLimiter,
    reaction_fetcher: ReactionFetcher,
    reaction_dedup_store: ReactionDedupStore,
    memory_reaction_poll_max_comments_per_poll: int,
) -> APIRouter:
    """Build the webhook receiver router.

    A factory closing over its dependencies rather than reading
    settings/globals directly, per docs/rules/architecture.md's "wire
    concrete implementations only at the composition root via constructor
    injection. No module-level singletons or global mutable state." rule —
    ``apps/webhook_api/main.py`` is the only caller. ``enqueue_rate_limiter``/
    ``index_enqueue_rate_limiter`` are each ``None`` when their respective
    ``*_enqueue_rate_limit_per_minute`` setting is configured at the
    documented sentinel value of ``0``, meaning "skip the rate-check call
    entirely" (see ``apps/webhook_api/settings.py``). ``indexing_enabled``
    is this route's own copy of the kill switch, passed down explicitly per
    architecture.md's "parse config once, pass down explicitly" rule —
    ``is_index_trigger`` itself never reads it (see that function's own
    docstring); only this route decides whether a syntactic index-trigger
    match actually gets to attempt an enqueue.

    ``provenance_recorder``/``text_redactor``/``memory_capture_ceiling_limiter``
    (``docs/plans/plan-deliver-memory.md``'s Phase 3, Unit C) back this
    route's memory-capture handling (see ``_handle_memory_capture`` above) —
    always registered, unlike the enqueue rate limiters, since capture has
    no ``0``-disables-it sentinel of its own (US-M12: only suppression, a
    later phase's own kill switch, is independently disable-able).

    ``reaction_fetcher``/``reaction_dedup_store``/``memory_reaction_poll_max_comments_per_poll``
    (Phase 4, Unit D) back US-M3's second ``Given/When/Then`` branch: a
    `pull_request` `closed` event runs the same reaction-fetch-and-capture
    logic a `MEMORY_REACTION_POLL` job runs (``memory/reaction_poll.py``'s
    ``run_memory_reaction_poll_job``), but synchronously, in this request,
    before the handler returns — see ``_run_synchronous_reaction_poll``
    below. ``reaction_dedup_store`` is the same ``PostgresMemoryRepository``
    instance as ``provenance_recorder`` (Assumption 8: one concrete class
    implements both ``MemorySignalRecorder`` and ``ReactionDedupStore``),
    typed and named separately here to match each Protocol role
    ``run_memory_reaction_poll_job`` itself declares.
    """
    router = APIRouter()

    @router.post("/webhooks/github")
    async def receive_github_webhook(request: Request) -> JSONResponse:
        client_key = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client_key):
            _reject_webhook("rate_limited")
            raise HTTPException(status_code=429, detail="rate limit exceeded")

        try:
            body = await _read_body_within_limit(request, max_bytes=max_body_bytes)
        except _BodyTooLargeError:
            _reject_webhook("body_too_large")
            raise HTTPException(status_code=413, detail="request body too large") from None

        signature_header = request.headers.get(_SIGNATURE_HEADER)
        if not verify_webhook_signature(
            payload=body,
            signature_header=signature_header,
            secret=webhook_secret.get_secret_value(),
        ):
            reason = "missing_signature" if not signature_header else "invalid_signature"
            _reject_webhook(reason)
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        try:
            raw_payload = json.loads(body)
        except json.JSONDecodeError:
            _reject_webhook("malformed_json")
            raise HTTPException(status_code=422, detail="malformed JSON payload") from None

        try:
            envelope = GitHubWebhookEnvelope.model_validate(raw_payload)
        except ValidationError:
            _reject_webhook("invalid_payload_shape")
            raise HTTPException(
                status_code=422, detail="payload missing a valid installation id"
            ) from None

        # A missing X-GitHub-Delivery header never happens for a genuine
        # GitHub-originated delivery; falling back to a fresh id (rather
        # than rejecting) keeps this phase's "every non-enqueuing outcome
        # is still accepted" design intact even for that edge case, at the
        # cost of that one request's dedupe guarantee against itself only.
        delivery_id = request.headers.get(_DELIVERY_HEADER) or str(uuid.uuid4())
        event_type = request.headers.get(_EVENT_TYPE_HEADER, "")

        if envelope.repository is None:
            # `ping`/`installation`-family deliveries carry no `repository`
            # at all — nothing to resolve into an org/repo, so accept
            # without touching the tenancy store rather than forcing a
            # placeholder `repo_full_name` through `WebhookEvent`.
            logger.info(
                "webhook accepted; not repository-scoped",
                extra={
                    "installation_id": envelope.installation.id,
                    "event_type": event_type,
                    "delivery_id": delivery_id,
                },
            )
            return JSONResponse({"status": "accepted"}, status_code=200)

        event = _build_webhook_event(envelope, delivery_id=delivery_id)

        candidates: list[_JobCandidate] = []
        if is_enqueue_trigger(event_type, event.action):
            candidates.append(
                _JobCandidate(
                    job_type=JobType.REVIEW,
                    payload_builder=lambda result: _review_enqueue_payload(event, result),
                    missing_fields_reason="missing_required_fields",
                    rate_limiter=enqueue_rate_limiter,
                    rate_limited_reason="rate_limited",
                )
            )

        default_branch = envelope.repository.default_branch
        if is_index_trigger(event_type, event.action, envelope.ref, default_branch):
            if indexing_enabled:
                candidates.append(
                    _JobCandidate(
                        job_type=JobType.INDEX,
                        payload_builder=lambda result: _index_enqueue_payload(
                            event_type=event_type, envelope=envelope, event=event, result=result
                        ),
                        missing_fields_reason="missing_required_fields",
                        rate_limiter=index_enqueue_rate_limiter,
                        rate_limited_reason="index_rate_limited",
                    )
                )
            else:
                # Syntactically an index trigger, but the kill switch is
                # off — logged with its own `indexing_disabled` reason
                # rather than silently dropped, so an operator scanning
                # logs can tell "no index job" apart from "wasn't even a
                # trigger". Never attempts payload building or an enqueue.
                candidates.append(
                    _JobCandidate(
                        job_type=JobType.INDEX,
                        payload_builder=lambda _result: None,
                        missing_fields_reason="indexing_disabled",
                        rate_limiter=None,
                        rate_limited_reason="index_rate_limited",
                    )
                )

        # US-M3's second `Given/When/Then` branch (spec-memory.md lines
        # 273-275) requires a `pull_request closed` event's reaction fetch
        # to run "before the webhook handler returns" — enqueuing a
        # `MEMORY_REACTION_POLL` job for the Worker to pick up later would
        # not satisfy that (an indeterminate delay, not "before the handler
        # returns"). `closed` is therefore handled synchronously below
        # (`_run_synchronous_reaction_poll`), never as an async job
        # candidate here — unlike `opened`/`synchronize`/`reopened` (US-M3's
        # first branch, "a later push"), which stay the async job the
        # Worker's own dispatch loop already handles.
        is_reaction_poll_close_trigger = event_type == "pull_request" and event.action == "closed"
        if is_memory_reaction_poll_trigger(event_type, event.action) and not (
            is_reaction_poll_close_trigger
        ):
            # docs/plans/plan-deliver-memory.md's Phase 4 section (Unit D,
            # Assumption 4): no kill switch, no rate limiter of its own —
            # this job type does no LLM/expensive work, so `rate_limiter=None`
            # (the same shape the "indexing disabled" candidate above already
            # uses), unconditionally attempted whenever the trigger matches.
            candidates.append(
                _JobCandidate(
                    job_type=JobType.MEMORY_REACTION_POLL,
                    payload_builder=lambda result: _memory_reaction_poll_payload(event, result),
                    missing_fields_reason="missing_required_fields",
                    rate_limiter=None,
                    rate_limited_reason="memory_reaction_poll_rate_limited",
                )
            )

        outcomes: list[tuple[JobType | None, uuid.UUID | None, str | None]]
        if candidates:
            result, matched_outcomes = await _handle_matched_triggers(
                event=event,
                store=tenancy_store,
                job_queue=job_queue,
                session_factory=session_factory,
                candidates=candidates,
            )
            outcomes = [(job_type, job_id, reason) for job_type, job_id, reason in matched_outcomes]
        else:
            # Same shared-transaction wrapper as `_handle_matched_triggers`'s
            # enqueue path, not two independently-committed calls: without
            # `unit_of_work`, `get_or_create_org_repo` and
            # `record_delivery_if_new` each open/commit their own separate
            # transaction, so a mid-request failure between them could leave
            # the org/repo auto-provisioned but the delivery unrecorded (or
            # vice versa) instead of committing atomically together.

            async def _work(session: AsyncSession) -> tuple[ResolutionResult, bool]:
                result = await resolve_installation(event, tenancy_store, unit_of_work=session)
                return result, True

            result = await _run_in_transaction(session_factory, event=event, work=_work)
            outcomes = [(None, None, "trigger_not_matched")]

        for job_type, job_id, skipped_reason in outcomes:
            logger.info(
                "webhook accepted" if job_id is not None else "webhook accepted; not enqueued",
                extra={
                    "installation_id": event.installation_id,
                    "org_id": str(result.org_id),
                    "repo_id": str(result.repo_id),
                    "pr_number": event.pr_number,
                    "delivery_id": event.delivery_id,
                    "job_type": job_type.value if job_type is not None else None,
                    "job_id": str(job_id) if job_id is not None else None,
                    "skipped_reason": skipped_reason,
                },
            )

        # US-M3's second `Given/When/Then` branch (Phase 4, Unit D) — runs
        # after `result` is resolved above (via either branch: `candidates`
        # is always empty for a `closed` action, since `is_enqueue_trigger`/
        # `is_index_trigger` never match it, so this always goes through the
        # plain `resolve_installation` path), and before this handler
        # returns its HTTP response below, per the spec's own literal
        # wording — see `_run_synchronous_reaction_poll`'s own docstring.
        if is_reaction_poll_close_trigger:
            reaction_poll_reason = await _run_synchronous_reaction_poll(
                event=event,
                result=result,
                reaction_fetcher=reaction_fetcher,
                signal_recorder=provenance_recorder,
                dedup_store=reaction_dedup_store,
                max_comments_per_poll=memory_reaction_poll_max_comments_per_poll,
            )
            logger.info(
                "synchronous memory reaction poll"
                if reaction_poll_reason == "reaction_poll_completed"
                else "synchronous memory reaction poll; not completed",
                extra={
                    "installation_id": event.installation_id,
                    "org_id": str(result.org_id),
                    "repo_id": str(result.repo_id),
                    "pr_number": event.pr_number,
                    "delivery_id": event.delivery_id,
                    "reason": reaction_poll_reason,
                },
            )

        # Memory capture (docs/plans/plan-deliver-memory.md's Phase 3
        # section, Unit C) — deliberately independent of the `candidates`/
        # `_handle_matched_triggers` fan-out above: `is_enqueue_trigger`/
        # `is_index_trigger` never match a `pull_request_review_comment`/
        # `pull_request_review_thread` event (different event types
        # entirely), so `candidates` is always empty here and `result` was
        # already resolved via the plain `resolve_installation` branch
        # above — reused, not re-resolved a second time.
        if is_memory_capture_trigger(event_type, event.action):
            capture_reason = await _handle_memory_capture(
                raw_payload=raw_payload,
                event_type=event_type,
                action=event.action,
                repo_id=result.repo_id,
                provenance_recorder=provenance_recorder,
                text_redactor=text_redactor,
                capture_ceiling_limiter=memory_capture_ceiling_limiter,
            )
            # A genuine `MemoryStoreError` is already logged at `error` with
            # `exc_info` inside `_handle_memory_capture` itself — this line
            # is a per-request summary, not the only signal a failure
            # produced. It still must not describe `capture_failed` with the
            # same "accepted; not captured" wording used for routine,
            # expected non-events (`not_a_platform_comment`,
            # `capture_ceiling_exceeded`) — someone scanning info-level
            # messages alone (not `extra.reason`, not the error-level log
            # above) would otherwise see a real Postgres-level failure as an
            # unremarkable no-op. One dict, keyed by every outcome
            # `_handle_memory_capture` can return, rather than an if/elif
            # chain re-deriving that same enumeration at a second call site.
            capture_log_message = _MEMORY_CAPTURE_LOG_MESSAGES.get(
                capture_reason, "memory capture accepted; not captured"
            )
            logger.info(
                capture_log_message,
                extra={
                    "installation_id": event.installation_id,
                    "org_id": str(result.org_id),
                    "repo_id": str(result.repo_id),
                    "delivery_id": event.delivery_id,
                    "event_type": event_type,
                    "raw_signal_type": (
                        "reply"
                        if event_type == "pull_request_review_comment"
                        else "thread_resolution"
                    ),
                    "reason": capture_reason,
                },
            )

        return JSONResponse({"status": "accepted"}, status_code=200)

    return router
