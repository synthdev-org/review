"""apps.admin_api.user_routes — ordinary CRUD for local admin/viewer
accounts (US-A4).

``create_user_router(...)`` follows ``apps/admin_api/auth_routes.py``'s
exact factory-router shape (constructor injection, no module-level state),
per ``docs/rules/architecture.md``'s constructor-injection rule. Every
route here — including the list route — is gated by
``require_admin_role``: a deliberate
departure from the general "every read endpoint is
``require_session``-only, Viewer parity" posture (US-A5) for the specific
case of *who else can sign in and what role they hold* — that is itself
sensitive account-management data, not the kind of operational read
(Memory queue, config, stats, usage) US-A5's Viewer-parity clause is about.

Deactivation (US-A4's second Given/When/Then) is a status flip
(``AdminUserRow.is_active = False`` via ``UserRepository.deactivate``),
never a hard delete — every ``admin_audit_log`` row already attributed to
that user (``actor_user_id``, no ``ON DELETE CASCADE``)
keeps resolving to their username afterward — plus revokes every one of
that user's ``admin_session`` rows
(``SessionStore.revoke_all_for_user``) so their next request, even with a
still-cookie-carried token, is rejected by ``require_session`` exactly like
an ordinary expired/unknown session.

Password length is enforced server-side before hashing (mirroring
``apps/admin_api/bootstrap.py``'s own
``BootstrapPasswordTooShortError`` check for the bootstrap admin) — a 422,
not left to Argon2 to hash an under-strength password without complaint. A
duplicate ``username`` is a 409 (``iam.auth.UsernameAlreadyExistsError``,
raised by ``adapter/storage/iam.py``'s ``PostgresUserRepository.create`` on
the table's own ``uq_admin_user_username`` constraint), not left to
surface as an unhandled 500.

Path parameter is named ``user_id`` (not a generic ``{id}``),
matching this codebase's established path-parameter naming convention
(``apps/webhook_api/admin_routes.py``'s ``{entry_id}``,
``apps/admin_api/dependencies.py``'s ``session_id``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.dependencies import AuthenticatedSession, RequireAdminRoleDependency
from iam.auth import SessionStore, UsernameAlreadyExistsError, UserRepository
from iam.models import Role, User

logger = logging.getLogger(__name__)


class _CreateUserRequest(BaseModel):
    """``POST /api/v1/users``'s request body — US-A4's first
    Given/When/Then: "a username, initial password, and a role".
    """

    username: str
    password: str = Field(repr=False)
    role: Role


class _UserResponse(BaseModel):
    """The shape every route in this module returns — never
    ``password_hash`` or any other credential material, the same
    exclusion ``apps/admin_api/auth_routes.py``'s own
    ``_SessionInfoResponse`` already establishes for a ``User``-derived
    response.
    """

    id: str
    username: str
    role: Role
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, user: User) -> _UserResponse:
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )


class _ListUsersResponse(BaseModel):
    users: tuple[_UserResponse, ...]


async def _create_user(
    payload: _CreateUserRequest,
    authenticated: AuthenticatedSession,
    *,
    user_repository: UserRepository,
    audit_log_repository: PostgresAuditLogRepository,
    password_hasher: Argon2PasswordHasher,
    min_password_length: int,
) -> _UserResponse:
    if len(payload.password) < min_password_length:
        raise HTTPException(
            status_code=422,
            detail=f"password must be at least {min_password_length} characters",
        )

    password_hash = await asyncio.to_thread(password_hasher.hash, payload.password)
    try:
        user = await user_repository.create(
            username=payload.username, password_hash=password_hash, role=payload.role
        )
    except UsernameAlreadyExistsError as exc:
        raise HTTPException(
            status_code=409, detail=f"username {payload.username!r} already exists"
        ) from exc

    await audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="user_created",
        resource_type="admin_user",
        resource_id=user.id,
        metadata={"username": user.username, "role": user.role},
    )
    logger.info(
        "admin user created",
        extra={
            "username": user.username,
            "role": user.role,
            "actor": authenticated.user.username,
        },
    )
    return _UserResponse.from_domain(user)


async def _deactivate_user(
    user_id: str,
    authenticated: AuthenticatedSession,
    *,
    user_repository: UserRepository,
    session_store: SessionStore,
    audit_log_repository: PostgresAuditLogRepository,
) -> _UserResponse:
    user = await user_repository.deactivate(user_id=user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"user {user_id!r} not found")

    await session_store.revoke_all_for_user(user_id=user_id)

    await audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="user_deactivated",
        resource_type="admin_user",
        resource_id=user.id,
        metadata={"username": user.username},
    )
    logger.info(
        "admin user deactivated",
        extra={"username": user.username, "actor": authenticated.user.username},
    )
    return _UserResponse.from_domain(user)


# Build the always-registered user-management router.
#
# A factory closing over its dependencies, matching
# ``apps/admin_api/auth_routes.py``'s ``create_auth_router`` shape — only
# called by ``apps/admin_api/main.py``.
def create_user_router(
    *,
    user_repository: UserRepository,
    session_store: SessionStore,
    audit_log_repository: PostgresAuditLogRepository,
    password_hasher: Argon2PasswordHasher,
    min_password_length: int,
    require_admin_role: RequireAdminRoleDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/users")

    @router.post("", status_code=201)
    async def create_user(
        payload: _CreateUserRequest,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> _UserResponse:
        return await _create_user(
            payload,
            authenticated,
            user_repository=user_repository,
            audit_log_repository=audit_log_repository,
            password_hasher=password_hasher,
            min_password_length=min_password_length,
        )

    @router.get("")
    async def list_users(
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> _ListUsersResponse:
        # authenticated is unused beyond the require_admin_role gate itself
        # — this route has no per-actor behavior, only the role check.
        _ = authenticated
        users = await user_repository.list_all()
        return _ListUsersResponse(users=tuple(_UserResponse.from_domain(user) for user in users))

    @router.patch("/{user_id}/deactivate")
    async def deactivate_user(
        user_id: str,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> _UserResponse:
        return await _deactivate_user(
            user_id,
            authenticated,
            user_repository=user_repository,
            session_store=session_store,
            audit_log_repository=audit_log_repository,
        )

    return router
