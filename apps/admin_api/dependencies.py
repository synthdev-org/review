"""apps.admin_api.dependencies — ``require_session``/``require_admin_role``,
per ``docs/plans/plan-deliver-admin-gui.md``'s Phase 1 section (Unit B,
US-A5, US-A6, Assumption 13's CSRF check). Every later phase's mutating
routes reuse the same ``require_admin_role``/``require_session`` dependency
chain built here — never a per-route reimplementation.

Factory functions (``build_require_session``/``build_require_admin_role``),
not bare module-level dependencies, per
``docs/rules/architecture.md``'s constructor-injection rule: they close
over the ``SessionStore``/``UserRepository`` instances and settings values
``apps/admin_api/main.py``'s composition root constructs, rather than
reading them from module-level state.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request

from apps.admin_api.csrf import verify_csrf_token
from iam.auth import SessionStore, UserRepository, authenticate_session, is_admin
from iam.models import User

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "review_admin_session"
CSRF_COOKIE_NAME = "review_admin_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

# Methods Assumption 13's CSRF check exempts — matches the plan's own "on
# every mutating request" scope: a GET/HEAD/OPTIONS is never expected to
# mutate state, so it carries no CSRF requirement, the ordinary REST/CSRF
# convention every double-submit/session-bound-token scheme in this space
# follows.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def hash_session_token(raw_token: str) -> str:
    """SHA-256 hex digest of a raw opaque session token — the same "never
    store or compare the raw secret" pattern
    ``apps/webhook_api/admin_routes.py``'s ``admin_api_key`` check already
    establishes (Assumption 7). Shared by ``apps/admin_api/auth_routes.py``
    (to compute the value it hands ``SessionStore.create``) and this
    module's own ``require_session`` (to compute the value it looks up
    with), so the hashing scheme lives in exactly one place.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


RequireSessionDependency = Callable[[Request], Awaitable["AuthenticatedSession"]]
"""Type alias for the callable ``build_require_session`` returns — named
here so ``apps/admin_api/auth_routes.py`` and every later phase's route
modules can type-hint a ``require_session`` constructor parameter without
each repeating the full ``Callable[[Request], Awaitable[AuthenticatedSession]]``
spelling."""


@dataclass(frozen=True)
class AuthenticatedSession:
    """What ``require_session`` hands every route that depends on it — the
    validated session's own id (for a later mutating route's own audit-log
    write) and the resolved, active ``User`` behind it.
    """

    session_id: str
    user: User


def build_require_session(
    *,
    session_store: SessionStore,
    user_repository: UserRepository,
    csrf_secret: bytes,
    session_idle_timeout_minutes: int,
) -> Callable[[Request], Awaitable[AuthenticatedSession]]:
    """Build the ``require_session`` FastAPI dependency (US-A2/US-A6): a
    valid, non-idle-expired session cookie, resolved to an active user —
    every ``GET`` route in this plan depends on this alone (Viewer parity,
    US-A5's second clause); every mutating route additionally depends on
    ``build_require_admin_role``'s wrapper below.

    On a non-safe-method request, also enforces Assumption 13's CSRF check
    (``X-CSRF-Token`` header against the session-bound HMAC) — one shared
    enforcement point, not a per-route reimplementation.
    """

    async def require_session(request: Request) -> AuthenticatedSession:
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if raw_token is None:
            logger.warning("session rejected", extra={"reason": "missing_cookie"})
            raise HTTPException(status_code=401, detail="not authenticated")

        token_hash = hash_session_token(raw_token)
        identity = await authenticate_session(
            token_hash=token_hash,
            session_store=session_store,
            user_repository=user_repository,
            now=datetime.now(UTC),
            idle_timeout_minutes=session_idle_timeout_minutes,
        )
        if identity is None:
            # Deliberately one generic outcome/log reason for every one of
            # authenticate_session's internal checks (unknown token,
            # idle-expired, inactive/missing user) — matching US-A1's own
            # "reveals nothing" posture; which specific check failed is not
            # information this HTTP boundary exposes.
            logger.warning("session rejected", extra={"reason": "invalid_or_expired_session"})
            raise HTTPException(status_code=401, detail="not authenticated")

        if request.method not in _SAFE_METHODS:
            csrf_header = request.headers.get(CSRF_HEADER_NAME)
            if not verify_csrf_token(
                session_id=identity.session.id, token=csrf_header, secret=csrf_secret
            ):
                logger.warning(
                    "session rejected",
                    extra={
                        "reason": "csrf_token_missing_or_invalid",
                        "username": identity.user.username,
                    },
                )
                raise HTTPException(status_code=403, detail="missing or invalid CSRF token")

        return AuthenticatedSession(session_id=identity.session.id, user=identity.user)

    return require_session


def build_require_admin_role(
    require_session: Callable[[Request], Awaitable[AuthenticatedSession]],
) -> Callable[[AuthenticatedSession], Awaitable[AuthenticatedSession]]:
    """Build the ``require_admin_role`` FastAPI dependency (US-A5) —
    depends on ``require_session`` itself (FastAPI resolves and caches it
    once per request), so a route only ever needs
    ``Depends(require_admin_role)`` to get both checks, never both
    dependencies listed separately.
    """

    async def require_admin_role(
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> AuthenticatedSession:
        if not is_admin(authenticated.user):
            logger.warning(
                "admin action rejected",
                extra={"reason": "role_forbidden", "username": authenticated.user.username},
            )
            raise HTTPException(status_code=403, detail="admin role required")
        return authenticated

    return require_admin_role
