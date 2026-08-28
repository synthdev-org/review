"""apps.webhook_api.webhook_routes — the GitHub webhook receiver route.

Per the plan's Phase 4 section: "apps/webhook_api gains its first real
route: a webhook receiver that verifies the signature and returns 401/200
accordingly, with a request body size limit and basic rate limiting in
front of it ... Installation-ID resolution at this phase is payload parsing
only — no DB lookup, no persisted mapping — and the route intentionally
does *not* enqueue a real review job yet."

Rejection order matches the plan and Security review precisely: rate limit
(cheapest — no body read needed) -> body size limit (enforced while
streaming, before the full body is ever buffered) -> signature verification
(the trust boundary) -> payload shape validation. Every rejection is logged
at ``warning`` with the reason only — never the payload body, per the
plan's Observability & rollback section.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError

from adapter.github.schemas import GitHubWebhookEnvelope
from adapter.github.signature import verify_webhook_signature
from apps.webhook_api.rate_limiter import InMemoryRateLimiter

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "x-hub-signature-256"
_DELIVERY_HEADER = "x-github-delivery"


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


def create_webhook_router(
    *,
    webhook_secret: SecretStr,
    rate_limiter: InMemoryRateLimiter,
    max_body_bytes: int,
) -> APIRouter:
    """Build the webhook receiver router.

    A factory closing over its dependencies rather than reading
    settings/globals directly, per docs/rules/architecture.md's "wire
    concrete implementations only at the composition root via constructor
    injection. No module-level singletons or global mutable state." rule —
    ``apps/webhook_api/main.py`` is the only caller.
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

        # Phase 0's job here is proving the trust boundary, not the
        # pipeline: no DB lookup, no persisted installation mapping, and no
        # job enqueued yet — that's Phase 1 of the roadmap (see the plan's
        # Phase 4 section).
        logger.info(
            "webhook accepted",
            extra={
                "installation_id": envelope.installation.id,
                "delivery_id": request.headers.get(_DELIVERY_HEADER),
            },
        )
        return JSONResponse({"status": "accepted"}, status_code=200)

    return router
