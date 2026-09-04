"""apps.webhook_api.webhook_routes — the GitHub webhook receiver route.

Per the plan's Phase 4 section: "apps/webhook_api gains its first real
route: a webhook receiver that verifies the signature and returns 401/200
accordingly, with a request body size limit and basic rate limiting in
front of it." Phase 1 (this module's current state) adds the rest of Unit
A on top of that trust boundary: installation/org/repo resolution
(auto-provisioning on first sight), delivery-id idempotency, a declarative
trigger-set lookup, a per-repo enqueue rate limit, and the actual
`JobQueueRepository.enqueue()` call — see the plan's Phase 1 section for
the full design (particularly "Transaction boundary", which this module's
`_handle_matched_trigger` implements).

Rejection order matches the plan and Security review precisely: rate limit
(cheapest — no body read needed) -> body size limit (enforced while
streaming, before the full body is ever buffered) -> signature verification
(the trust boundary) -> payload shape validation. Every rejection is logged
at ``warning`` with the reason only — never the payload body, per the
plan's Observability & rollback section. Once past that boundary, this
module never rejects a well-formed, correctly-signed webhook with a 4xx/5xx
except for the one genuine mid-request-failure case documented on
`_handle_matched_trigger` below — every other non-enqueuing outcome (no
trigger match, a duplicate delivery, a rate-limited repo, missing PR
fields) is still accepted (200) and logged at ``info`` with the reason, per
the plan's Phase 1 section.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.github.schemas import GitHubWebhookEnvelope
from adapter.github.signature import verify_webhook_signature
from adapter.github.triggers import is_enqueue_trigger
from adapter.storage.models import JobType
from adapter.storage.queue import JobQueueRepository
from apps.webhook_api.rate_limiter import InMemoryRateLimiter
from tenancy.resolution import ResolutionResult, TenancyStore, WebhookEvent, resolve_installation

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "x-hub-signature-256"
_DELIVERY_HEADER = "x-github-delivery"
_EVENT_TYPE_HEADER = "x-github-event"


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
    )


def _enqueue_payload(event: WebhookEvent, result: ResolutionResult) -> dict[str, object]:
    # `repo_full_name` (Phase 4 addition): the Worker's diff-fetch step
    # (`review/prefilter.py`, `adapter/github/diff.py`) needs GitHub's own
    # "owner/repo" identifier to call the Files/Trees/Blobs APIs and to
    # request a repo-scoped installation token — it cannot derive that from
    # `org_id`/`repo_id` alone (this platform's own UUIDs), and the Worker
    # only ever sees this job's `payload`, never the original webhook
    # envelope `event` came from. Sourced from `event.repo_full_name`
    # (`tenancy.resolution.WebhookEvent`, already parsed from
    # `GitHubWebhookEnvelope` at this route's own boundary) — closing a gap
    # where this field was resolved here but never threaded into the
    # payload the Worker actually reads.
    return {
        "org_id": str(result.org_id),
        "repo_id": str(result.repo_id),
        "installation_id": event.installation_id,
        "repo_full_name": event.repo_full_name,
        "pr_number": event.pr_number,
        "head_sha": event.head_sha,
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
    ``_handle_matched_trigger`` and the trigger-not-matched route branch
    need, per the plan's Phase 1 "Transaction boundary" section.

    A mid-request failure (Postgres blip, an ``enqueue()`` exception) rolls
    the whole transaction back — so GitHub's own retry of that same
    delivery is processed cleanly from scratch rather than some of this
    work being durably committed with the rest lost. Logged at ``warning``
    (self-healing via GitHub's own retry, not a confirmed defect) and
    re-raised as an ``HTTPException(500)`` — surfaced, not swallowed.

    ``should_commit=False`` (used by ``_handle_matched_trigger`` for a
    rate-limited delivery) rolls back without treating it as a failure —
    rate limiting is not the same as "this delivery was processed";
    committing the delivery-id record in that case would durably mark it
    "seen" with no job ever enqueued, permanently losing that trigger even
    after the rate-limit window passes and a later redelivery of the exact
    same delivery id arrives.
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


async def _handle_matched_trigger(
    *,
    event: WebhookEvent,
    store: TenancyStore,
    job_queue: JobQueueRepository,
    session_factory: async_sessionmaker[AsyncSession],
    enqueue_rate_limiter: InMemoryRateLimiter | None,
) -> tuple[ResolutionResult, uuid.UUID | None, str | None]:
    """Resolve installation/org/repo, record the delivery, and (if this is
    a new delivery, under the rate limit, and carries the fields a review
    job needs) enqueue one — all inside one shared transaction (see
    ``_run_in_transaction``).
    """

    async def _work(
        session: AsyncSession,
    ) -> tuple[tuple[ResolutionResult, uuid.UUID | None, str | None], bool]:
        result = await resolve_installation(event, store, unit_of_work=session)

        job_id: uuid.UUID | None = None
        skipped_reason: str | None = None
        rate_limit_token: int | None = None
        if not result.is_new_delivery:
            skipped_reason = "duplicate_delivery"
        elif event.pr_number is None or event.head_sha is None:
            skipped_reason = "missing_required_fields"
        else:
            rate_limited = False
            if enqueue_rate_limiter is not None:
                allowed, rate_limit_token = enqueue_rate_limiter.reserve(
                    str(result.repo_id)
                )
                rate_limited = not allowed
            if rate_limited:
                skipped_reason = "rate_limited"
            else:
                try:
                    job_id = await job_queue.enqueue(
                        JobType.REVIEW,
                        _enqueue_payload(event, result),
                        org_id=result.org_id,
                        repo_id=result.repo_id,
                        session=session,
                    )
                except Exception:
                    # The rate-limit slot above was already consumed
                    # in-process before this transaction's outcome was
                    # known. If enqueue() itself fails, the DB work is
                    # about to be rolled back by `_run_in_transaction` and
                    # GitHub will redeliver the same webhook — release
                    # exactly this reservation (by token, not "whichever
                    # hit is most recent" — a concurrent request for the
                    # same repo may have reserved its own hit in the
                    # meantime) so that redelivery (the same logical
                    # event, not a new one) isn't spuriously counted twice
                    # against the small per-repo budget.
                    if enqueue_rate_limiter is not None:
                        enqueue_rate_limiter.release(
                            str(result.repo_id), rate_limit_token
                        )
                    raise

        return (result, job_id, skipped_reason), skipped_reason != "rate_limited"

    return await _run_in_transaction(session_factory, event=event, work=_work)


def create_webhook_router(
    *,
    webhook_secret: SecretStr,
    rate_limiter: InMemoryRateLimiter,
    max_body_bytes: int,
    tenancy_store: TenancyStore,
    job_queue: JobQueueRepository,
    session_factory: async_sessionmaker[AsyncSession],
    enqueue_rate_limiter: InMemoryRateLimiter | None,
) -> APIRouter:
    """Build the webhook receiver router.

    A factory closing over its dependencies rather than reading
    settings/globals directly, per docs/rules/architecture.md's "wire
    concrete implementations only at the composition root via constructor
    injection. No module-level singletons or global mutable state." rule —
    ``apps/webhook_api/main.py`` is the only caller. ``enqueue_rate_limiter``
    is ``None`` when ``review_enqueue_rate_limit_per_minute`` is configured
    at its shipped default of ``0`` — the documented sentinel meaning "skip
    the rate-check call entirely" (see ``apps/webhook_api/settings.py``).
    """
    router = APIRouter()

    @router.post("/webhooks/github")
    async def receive_github_webhook(request: Request) -> JSONResponse:
        client_key = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client_key):
            logger.warning("webhook rejected", extra={"reason": "rate_limited"})
            raise HTTPException(status_code=429, detail="rate limit exceeded")

        try:
            body = await _read_body_within_limit(request, max_bytes=max_body_bytes)
        except _BodyTooLargeError:
            logger.warning("webhook rejected", extra={"reason": "body_too_large"})
            raise HTTPException(status_code=413, detail="request body too large") from None

        signature_header = request.headers.get(_SIGNATURE_HEADER)
        if not verify_webhook_signature(
            payload=body,
            signature_header=signature_header,
            secret=webhook_secret.get_secret_value(),
        ):
            reason = "missing_signature" if not signature_header else "invalid_signature"
            logger.warning("webhook rejected", extra={"reason": reason})
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        try:
            raw_payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("webhook rejected", extra={"reason": "malformed_json"})
            raise HTTPException(status_code=422, detail="malformed JSON payload") from None

        try:
            envelope = GitHubWebhookEnvelope.model_validate(raw_payload)
        except ValidationError:
            logger.warning("webhook rejected", extra={"reason": "invalid_payload_shape"})
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

        job_id: uuid.UUID | None
        skipped_reason: str | None
        if is_enqueue_trigger(event_type, event.action):
            result, job_id, skipped_reason = await _handle_matched_trigger(
                event=event,
                store=tenancy_store,
                job_queue=job_queue,
                session_factory=session_factory,
                enqueue_rate_limiter=enqueue_rate_limiter,
            )
        else:
            # Same shared-transaction wrapper as `_handle_matched_trigger`'s
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
            job_id = None
            skipped_reason = "trigger_not_matched"

        logger.info(
            "webhook accepted" if job_id is not None else "webhook accepted; not enqueued",
            extra={
                "installation_id": event.installation_id,
                "org_id": str(result.org_id),
                "repo_id": str(result.repo_id),
                "pr_number": event.pr_number,
                "delivery_id": event.delivery_id,
                "job_id": str(job_id) if job_id is not None else None,
                "skipped_reason": skipped_reason,
            },
        )
        return JSONResponse({"status": "accepted"}, status_code=200)

    return router
