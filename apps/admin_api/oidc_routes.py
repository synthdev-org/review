"""apps.admin_api.oidc_routes — ``GET /api/v1/auth/oidc/{config,login,callback}``
(US-A24, ADR-0010).

Pre-provisioned users only: the callback signs in the existing, active
``admin_user`` whose username equals the ID token's username claim. Every
failure sends the browser to ``/login?error=sso_failed`` with one generic
reason; the real reason goes to the log and audit trail only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.session_issue import issue_session
from iam.auth import SessionStore, UserRepository
from iam.oidc import (
    OIDCError,
    OIDCProvider,
    decode_login_cookie,
    encode_login_cookie,
    new_login_request,
)

logger = logging.getLogger(__name__)

OIDC_COOKIE_NAME = "review_admin_oidc"
_OIDC_COOKIE_MAX_AGE_SECONDS = 600
_COOKIE_PATH = "/api/v1/auth/oidc"
_FAILURE_URL = "/login?error=sso_failed"


def _fail(reason: str, *, username: str | None = None) -> RedirectResponse:
    logger.warning("oidc login rejected", extra={"reason": reason, "username": username})
    redirect = RedirectResponse(_FAILURE_URL, status_code=303)
    redirect.delete_cookie(OIDC_COOKIE_NAME, path=_COOKIE_PATH)
    return redirect


async def _start_login(
    provider: OIDCProvider, *, redirect_uri: str, session_cookie_secure: bool
) -> RedirectResponse:
    login = new_login_request()
    try:
        url = await provider.authorization_url(login=login, redirect_uri=redirect_uri)
    except OIDCError:
        logger.exception("oidc provider unreachable")
        return RedirectResponse(_FAILURE_URL, status_code=303)
    response = RedirectResponse(url, status_code=303)
    # lax, so the browser still sends it on the top-level redirect back.
    response.set_cookie(
        OIDC_COOKIE_NAME,
        encode_login_cookie(login),
        max_age=_OIDC_COOKIE_MAX_AGE_SECONDS,
        path=_COOKIE_PATH,
        httponly=True,
        secure=session_cookie_secure,
        samesite="lax",
    )
    return response


@dataclass(frozen=True)
class _CallbackDeps:
    provider: OIDCProvider
    redirect_uri: str
    user_repository: UserRepository
    session_store: SessionStore
    audit_log_repository: PostgresAuditLogRepository
    csrf_secret: bytes
    session_cookie_secure: bool


async def _finish_login(
    request: Request, *, code: str | None, state: str | None, deps: _CallbackDeps
) -> RedirectResponse:
    provider = deps.provider
    redirect_uri = deps.redirect_uri
    user_repository = deps.user_repository
    session_store = deps.session_store
    audit_log_repository = deps.audit_log_repository
    csrf_secret = deps.csrf_secret
    session_cookie_secure = deps.session_cookie_secure
    login = decode_login_cookie(request.cookies.get(OIDC_COOKIE_NAME))
    if login is None or not code or state != login.state:
        return _fail("state_mismatch")
    try:
        identity = await provider.complete_login(code=code, login=login, redirect_uri=redirect_uri)
    except OIDCError:
        logger.exception("oidc login failed")
        await audit_log_repository.record(
            actor_user_id=None, action="login_failed", metadata={"reason": "oidc_error"}
        )
        return _fail("oidc_error")

    user = await user_repository.get_by_username(username=identity.username)
    if user is None or not user.is_active:
        await audit_log_repository.record(
            actor_user_id=None,
            action="login_failed",
            metadata={"username": identity.username, "reason": "oidc_unknown_user"},
        )
        return _fail("unknown_or_inactive_user", username=identity.username)

    response = RedirectResponse("/dashboard", status_code=303)
    response.delete_cookie(OIDC_COOKIE_NAME, path=_COOKIE_PATH)
    await issue_session(
        response,
        user_id=user.id,
        session_store=session_store,
        csrf_secret=csrf_secret,
        secure=session_cookie_secure,
        now=datetime.now(UTC),
    )
    await audit_log_repository.record(
        actor_user_id=user.id,
        action="login_succeeded",
        metadata={"username": user.username, "method": "oidc", "subject": identity.subject},
    )
    logger.info("oidc login succeeded", extra={"username": user.username})
    return response


# ``provider=None`` means OIDC is not configured: ``/config`` reports
# disabled and the other two routes 404.
def create_oidc_router(
    *,
    provider: OIDCProvider | None,
    redirect_uri: str,
    user_repository: UserRepository,
    session_store: SessionStore,
    audit_log_repository: PostgresAuditLogRepository,
    csrf_secret: bytes,
    session_cookie_secure: bool,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth/oidc")

    @router.get("/config")
    async def get_config() -> dict[str, bool]:
        return {"enabled": provider is not None}

    @router.get("/login")
    async def start_login() -> RedirectResponse:
        if provider is None:
            raise HTTPException(status_code=404)
        return await _start_login(
            provider, redirect_uri=redirect_uri, session_cookie_secure=session_cookie_secure
        )

    @router.get("/callback")
    async def finish_login(
        request: Request,
        code: str | None = None,
        state: str | None = None,
    ) -> RedirectResponse:
        if provider is None:
            raise HTTPException(status_code=404)
        deps = _CallbackDeps(
            provider=provider,
            redirect_uri=redirect_uri,
            user_repository=user_repository,
            session_store=session_store,
            audit_log_repository=audit_log_repository,
            csrf_secret=csrf_secret,
            session_cookie_secure=session_cookie_secure,
        )
        return await _finish_login(request, code=code, state=state, deps=deps)

    return router
