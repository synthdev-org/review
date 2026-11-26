"""adapter.github.manifest — the GitHub App manifest flow: CSRF-protected
setup-state generation/verification, and the temporary-code-to-credentials
exchange.

CSRF protection on the manifest flow uses the standard OAuth 'state'
pattern: the initiation route generates a random state value, HMAC-signs it
with a timestamp using a dedicated setup secret, and the callback route
validates the returned ``state`` query parameter against the signed cookie
value and its expiry using the same ``hmac.compare_digest`` pattern as
webhook verification. This needs no persistence. Both functions here are pure (no I/O, no
FastAPI import) so they are directly unit-testable; ``apps/webhook_api/manifest_routes.py``
wires them to the actual cookie/query-parameter plumbing.

``exchange_temporary_code`` is the one function in this module that does
real I/O — an explicitly timeout-bound HTTP call to GitHub's
app-manifest-conversion endpoint, per architecture.md's "every I/O call
takes a timeout" rule — and is the one place ``httpx`` is imported, keeping
that import out of ``apps/webhook_api``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import httpx

from adapter.github.exceptions import (
    GitHubCodeExchangeMalformedResponseError,
    GitHubCodeExchangeUnreachableError,
    SetupStateExpiredError,
    SetupStateMismatchError,
    SetupStateMissingError,
)
from adapter.github.schemas import GitHubAppCredentials

_STATE_FIELD_COUNT = 3
_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"


def generate_setup_state(*, secret: bytes, now: float | None = None) -> str:
    """Generate a signed, timestamped, single-use setup ``state`` value.

    Format: ``"<nonce>.<timestamp>.<hmac-sha256-hex>"``. The caller sets
    this value verbatim as both the short-TTL ``HttpOnly``/``SameSite=Lax``
    cookie value and the ``state`` parameter handed to GitHub's
    manifest-creation redirect; ``verify_setup_state`` below compares the
    two with ``hmac.compare_digest`` and independently re-validates this
    value's embedded signature and timestamp.
    """
    timestamp = now if now is not None else time.time()
    nonce = secrets.token_urlsafe(16)
    signed_part = f"{nonce}.{int(timestamp)}"
    signature = hmac.new(secret, signed_part.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{signed_part}.{signature}"


def verify_setup_state(
    *,
    cookie_value: str | None,
    query_state: str | None,
    secret: bytes,
    ttl_seconds: float,
    now: float | None = None,
) -> None:
    """Validate a callback's ``state`` query parameter against the signed
    cookie value set at initiation.

    Raises one of ``SetupStateMissingError``, ``SetupStateMismatchError``,
    or ``SetupStateExpiredError`` — never returns a bare bool — so the
    caller can log/report which specific negative case was hit (a missing
    state parameter, a state that doesn't match the signed cookie, or an
    expired state). Returns ``None`` (does not raise) when the state is valid.
    """
    if not cookie_value or not query_state:
        raise SetupStateMissingError()

    # Constant-time comparison of the two values GitHub's redirect and our
    # own cookie should carry identically, using the same
    # hmac.compare_digest pattern as webhook verification.
    if not hmac.compare_digest(cookie_value, query_state):
        raise SetupStateMismatchError()

    parts = cookie_value.split(".")
    if len(parts) != _STATE_FIELD_COUNT:
        raise SetupStateMismatchError()
    nonce, timestamp_str, signature = parts

    signed_part = f"{nonce}.{timestamp_str}"
    expected_signature = hmac.new(secret, signed_part.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise SetupStateMismatchError()

    try:
        timestamp = int(timestamp_str)
    except ValueError as exc:
        raise SetupStateMismatchError() from exc

    current = now if now is not None else time.time()
    if current - timestamp > ttl_seconds:
        raise SetupStateExpiredError()


async def exchange_temporary_code(
    code: str,
    *,
    timeout: float,
    api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
) -> GitHubAppCredentials:
    """Exchange GitHub's manifest-flow temporary ``code`` for real App
    credentials via a timeout-bound HTTP call, per
    docs/rules/architecture.md's "every I/O call takes a
    timeout and respects cancellation" rule.

    Never persists the result anywhere — the caller
    (``apps/webhook_api/manifest_routes.py``) returns it straight to the
    operator, with no Postgres access. Raises
    ``GitHubCodeExchangeUnreachableError`` on any connectivity failure or
    non-2xx response, or ``GitHubCodeExchangeMalformedResponseError``
    if GitHub responds 2xx with a body missing the fields this adapter
    needs — never a raw ``httpx`` exception, per architecture.md's "specific
    exception types" rule.
    """
    url = f"{api_base_url}/app-manifests/{code}/conversions"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url)
    except httpx.TimeoutException as exc:
        raise GitHubCodeExchangeUnreachableError(detail=f"request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise GitHubCodeExchangeUnreachableError(detail=str(exc)) from exc

    if response.status_code >= 400:
        raise GitHubCodeExchangeUnreachableError(
            detail=f"GitHub returned HTTP {response.status_code}"
        )

    try:
        data = response.json()
        return GitHubAppCredentials(
            app_id=data["id"],
            app_slug=data.get("slug", ""),
            private_key=data["pem"],
            webhook_secret=data["webhook_secret"],
            client_id=data["client_id"],
            client_secret=data["client_secret"],
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise GitHubCodeExchangeMalformedResponseError(detail=str(exc)) from exc
