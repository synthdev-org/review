"""Integration tests for US-A5's role enforcement (require_admin_role),
against a real Postgres/pgvector via testcontainers-python, per
docs/plans/plan-deliver-admin-gui.md's Phase 1 (Unit B) Tests section:
"a require_admin_role-gated dummy route rejecting a Viewer session (403)."

No real route in this phase is gated by require_admin_role yet (Phase 1's
own routes are all either unauthenticated (`/auth/login`) or
require_session-only (`/auth/logout`, `/auth/session`) — the first real
require_admin_role-gated route is Phase 2's `POST /api/v1/users`). This
test proves the dependency itself works correctly in isolation, by mounting
a throwaway route onto the real app (built from the real composition
root's own storage wiring) that depends on it — exactly the kind of
test-only extension `docs/rules/testing.md`'s middle tier expects for a
cross-cutting dependency with no production call site yet in this phase.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.iam import PostgresSessionStore, PostgresUserRepository
from adapter.storage.models import AdminRole, AdminUserRow
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    build_require_admin_role,
    build_require_session,
)
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"
_VIEWER_USERNAME = "integration-test-viewer"
_VIEWER_PASSWORD = "viewer-password-1234"


def _settings(*, dsn: str) -> AdminAPISettings:
    return AdminAPISettings(
        environment="development",
        database_url=SecretStr(dsn),
        admin_bootstrap_username=_BOOTSTRAP_USERNAME,
        admin_bootstrap_password=SecretStr(_BOOTSTRAP_PASSWORD),
        admin_csrf_secret=SecretStr("integration-test-csrf-secret"),
        session_cookie_secure=False,
        _env_file=None,
    )


def _seed_viewer(dsn: str) -> None:
    """Uses its own freshly-created, self-contained engine (created, used,
    and disposed within one ``asyncio.run()`` call) rather than the
    ``migrated_engine`` fixture's own engine — asyncpg connections are tied
    to the event loop that opened them, and reusing a pooled connection
    under a *different* loop later (e.g. the one ``TestClient`` below opens
    its own portal on) raises ``RuntimeError: ... attached to a different
    loop``. Every helper in this module that touches Postgres outside a
    ``with TestClient(...)`` block follows this same self-contained-engine
    convention for the same reason.
    """

    async def _seed() -> None:
        engine = create_storage_engine(dsn)
        try:
            session_factory = create_session_factory(engine)
            password_hasher = Argon2PasswordHasher()
            async with session_factory() as session, session.begin():
                session.add(
                    AdminUserRow(
                        username=_VIEWER_USERNAME,
                        password_hash=password_hasher.hash(_VIEWER_PASSWORD),
                        role=AdminRole.VIEWER,
                        is_active=True,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_seed())


def _mount_admin_only_dummy_route(app: FastAPI, *, settings: AdminAPISettings, dsn: str) -> None:
    """Mounts a throwaway `GET /__test-only/admin-only` route gated by a
    fresh `require_admin_role` dependency, built from its own engine/
    `PostgresUserRepository`/`PostgresSessionStore` pair pointed at the
    same database `create_app` already wired the real app against — the
    composition root's own instances are private to `create_app`, but any
    instance built against the same tables resolves the same rows, so this
    is equivalent for this test's purposes. A fresh engine (not the
    `migrated_engine` fixture's own — see `_seed_viewer`'s docstring for
    why), first used inside `TestClient`'s own portal below, so it's never
    shared across two different event loops.
    """
    engine = create_storage_engine(dsn)
    session_store = PostgresSessionStore(create_session_factory(engine))
    user_repository = PostgresUserRepository(create_session_factory(engine))
    require_session = build_require_session(
        session_store=session_store,
        user_repository=user_repository,
        csrf_secret=settings.admin_csrf_secret.get_secret_value().encode("utf-8"),
        session_idle_timeout_minutes=settings.session_idle_timeout_minutes,
    )
    require_admin_role = build_require_admin_role(require_session)

    @app.get("/__test-only/admin-only")
    async def _admin_only_dummy_route(
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> dict[str, str]:
        return {"username": authenticated.user.username}


def test_require_admin_role_rejects_a_viewer_session_with_403(
    migrated_engine: AsyncEngine,
) -> None:
    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    settings = _settings(dsn=dsn)
    _seed_viewer(dsn)

    app = create_app(settings)
    _mount_admin_only_dummy_route(app, settings=settings, dsn=dsn)

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD},
        )

        response = client.get("/__test-only/admin-only")

        assert response.status_code == 403


def test_require_admin_role_allows_an_admin_session(migrated_engine: AsyncEngine) -> None:
    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    settings = _settings(dsn=dsn)

    app = create_app(settings)
    _mount_admin_only_dummy_route(app, settings=settings, dsn=dsn)

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        response = client.get("/__test-only/admin-only")

        assert response.status_code == 200
        assert response.json() == {"username": _BOOTSTRAP_USERNAME}
