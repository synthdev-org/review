"""apps.admin_api.auth_routes — ``POST /api/v1/auth/login``,
``POST /api/v1/auth/logout``, ``GET /api/v1/auth/session`` (US-A1/US-A2/US-A6).

``create_auth_router(...)`` follows
``apps/webhook_api/manifest_routes.py``'s exact factory-router shape,
closing over its own dependencies rather than reading module-level state,
per ``docs/rules/architecture.md``'s constructor-injection rule.

Every rejected login is logged at ``WARNING`` with ``{reason, username}``
— never the password — and increments the unlabeled
``review.auth.login_failed_total`` counter (no username attribute, to
avoid unbounded cardinality). The per-username ``login_attempt`` table
throttles one attacker hammering one account, but is blind to a
low-and-slow campaign spread across many distinct usernames, which this
aggregate counter gives operators a signal to alert on instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Response
from opentelemetry import metrics
from pydantic import BaseModel, Field

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.iam import PostgresAuditLogRepository, PostgresLoginAttemptRepository
from apps.admin_api.dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthenticatedSession,
    RequireSessionDependency,
)
from apps.admin_api.session_issue import issue_session
from iam.auth import SessionStore, UserRepository

logger = logging.getLogger(__name__)

meter = metrics.get_meter("review.auth")
_login_failed_counter = meter.create_counter(
    "review.auth.login_failed_total",
    description="Every failed login attempt, unlabeled (no username attribute) to avoid "
    "unbounded cardinality — a low-and-slow-across-many-usernames signal the per-username "
    "login_attempt counter can't surface on its own.",
)

# A generic, identical message for every login failure — wrong password and
# nonexistent username are indistinguishable to the caller, per US-A1's
# "reveals nothing about whether the username exists" requirement.
_INVALID_CREDENTIALS_DETAIL = "invalid username or password"


class _LoginRequest(BaseModel):
    username: str
    password: str = Field(repr=False)


class _SessionInfoResponse(BaseModel):
    """The shape both a successful login and ``GET /api/v1/auth/session``
    return — never the password hash or any other credential material.
    """

    username: str
    role: str


@dataclass(frozen=True)
class _AuthDeps:
    """The collaborators the login/logout handlers share, bundled so the
    module-level handler bodies stay under the parameter limit.
    """

    user_repository: UserRepository
    session_store: SessionStore
    audit_log_repository: PostgresAuditLogRepository
    login_attempt_repository: PostgresLoginAttemptRepository
    password_hasher: Argon2PasswordHasher
    csrf_secret: bytes
    session_cookie_secure: bool


async def _reject_invalid_credentials(
    deps: _AuthDeps, username: str, now: datetime, *, actor_user_id: str | None
) -> NoReturn:
    """Record a failed attempt, audit and log it, then raise the 401."""
    await deps.login_attempt_repository.record_failed_attempt(username=username, now=now)
    await deps.audit_log_repository.record(
        actor_user_id=actor_user_id,
        action="login_failed",
        metadata={"username": username, "reason": "invalid_credentials"},
    )
    logger.warning(
        "login rejected",
        extra={"reason": "invalid_credentials", "username": username},
    )
    _login_failed_counter.add(1)
    raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS_DETAIL)


async def _login(
    deps: _AuthDeps, payload: _LoginRequest, response: Response
) -> _SessionInfoResponse:
    now = datetime.now(UTC)

    # Checked before touching Postgres for the user row, or running any
    # Argon2 verification — per this phase's own Security review, an
    # already-rate-limited username is rejected before paying either
    # cost.
    if await deps.login_attempt_repository.is_rate_limited(username=payload.username, now=now):
        logger.warning(
            "login rejected", extra={"reason": "rate_limited", "username": payload.username}
        )
        _login_failed_counter.add(1)
        raise HTTPException(status_code=429, detail="too many login attempts")

    user = await deps.user_repository.get_by_username(username=payload.username)
    if user is None or not user.is_active:
        # US-A1: a dummy, constant-cost Argon2
        # verification against a fixed placeholder hash — closes the
        # username-enumeration timing side channel a bare
        # `if user is None: return 401` shortcut would otherwise leave
        # open, since the real verify() call below is deliberately
        # expensive.
        await asyncio.to_thread(deps.password_hasher.verify_dummy)
        await _reject_invalid_credentials(deps, payload.username, now, actor_user_id=None)

    password_ok = await asyncio.to_thread(
        deps.password_hasher.verify, password_hash=user.password_hash, password=payload.password
    )
    if not password_ok:
        await _reject_invalid_credentials(deps, payload.username, now, actor_user_id=user.id)

    await issue_session(
        response,
        user_id=user.id,
        session_store=deps.session_store,
        csrf_secret=deps.csrf_secret,
        secure=deps.session_cookie_secure,
        now=now,
    )

    await deps.audit_log_repository.record(
        actor_user_id=user.id, action="login_succeeded", metadata={"username": user.username}
    )
    logger.info("login succeeded", extra={"username": user.username})
    return _SessionInfoResponse(username=user.username, role=user.role)


async def _logout(
    deps: _AuthDeps, response: Response, authenticated: AuthenticatedSession
) -> dict[str, bool]:
    await deps.session_store.revoke(session_id=authenticated.session_id)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    await deps.audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="logout",
        metadata={"username": authenticated.user.username},
    )
    logger.info("logout succeeded", extra={"username": authenticated.user.username})
    return {"logged_out": True}


# Build the always-registered auth router.
#
# A factory closing over its dependencies, matching
# ``manifest_routes.py``'s ``create_manifest_router`` shape — only called
# by ``apps/admin_api/main.py``.
def create_auth_router(
    *,
    user_repository: UserRepository,
    session_store: SessionStore,
    audit_log_repository: PostgresAuditLogRepository,
    login_attempt_repository: PostgresLoginAttemptRepository,
    password_hasher: Argon2PasswordHasher,
    csrf_secret: bytes,
    session_cookie_secure: bool,
    require_session: RequireSessionDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth")
    deps = _AuthDeps(
        user_repository=user_repository,
        session_store=session_store,
        audit_log_repository=audit_log_repository,
        login_attempt_repository=login_attempt_repository,
        password_hasher=password_hasher,
        csrf_secret=csrf_secret,
        session_cookie_secure=session_cookie_secure,
    )

    @router.post("/login")
    async def login(payload: _LoginRequest, response: Response) -> _SessionInfoResponse:
        return await _login(deps, payload, response)

    @router.post("/logout")
    async def logout(
        response: Response,
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, bool]:
        return await _logout(deps, response, authenticated)

    @router.get("/session")
    async def get_session(
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> _SessionInfoResponse:
        return _SessionInfoResponse(
            username=authenticated.user.username, role=authenticated.user.role
        )

    return router
