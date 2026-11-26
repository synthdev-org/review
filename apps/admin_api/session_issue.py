"""apps.admin_api.session_issue — the one place a successful login (local
password or OIDC) becomes a session row plus its two cookies."""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import Response

from apps.admin_api.csrf import generate_csrf_token
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, hash_session_token
from iam.auth import SessionStore

# Raw session-token entropy: a high-entropy random
# opaque token. 32 bytes (256 bits) comfortably exceeds any realistic
# brute-force concern for a session identifier.
_SESSION_TOKEN_BYTES = 32


async def issue_session(
    response: Response,
    *,
    user_id: str,
    session_store: SessionStore,
    csrf_secret: bytes,
    secure: bool,
    now: datetime,
) -> None:
    raw_token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    session = await session_store.create(
        user_id=user_id, token_hash=hash_session_token(raw_token), now=now
    )
    response.set_cookie(
        SESSION_COOKIE_NAME, raw_token, httponly=True, secure=secure, samesite="lax"
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        generate_csrf_token(session_id=session.id, secret=csrf_secret),
        httponly=False,
        secure=secure,
        samesite="lax",
    )
