"""Integration tests for apps/admin_api/user_routes.py — ordinary CRUD for
local admin/viewer accounts (Unit C, US-A4), driven through the real
apps/admin_api FastAPI app via TestClient, against a real Postgres/pgvector
via testcontainers-python (per docs/rules/testing.md's mandatory
DB-integration rule), per docs/plans/plan-deliver-admin-gui.md's Phase 2
Tests section:

"create user with a role, deactivated user's existing session rejected on
the next request, deactivated user's past audit rows still resolve to
their username."

Plus additional CRUD-completeness cases this module's own behavior needs
covered: list, duplicate-username conflict, under-length-password
rejection, and Viewer-role rejection on the real (not throwaway) routes —
apps/admin_api/test_role_enforcement.py already proves require_admin_role
itself works in isolation; this module's own Viewer case proves this
phase's real routes are actually wired to it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import AdminAuditLogRow, AdminUserRow
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"


def _settings(*, dsn: str, **overrides: object) -> AdminAPISettings:
    defaults: dict[str, object] = {
        "environment": "development",
        "database_url": SecretStr(dsn),
        "admin_bootstrap_username": _BOOTSTRAP_USERNAME,
        "admin_bootstrap_password": SecretStr(_BOOTSTRAP_PASSWORD),
        "admin_csrf_secret": SecretStr("integration-test-csrf-secret"),
        "session_cookie_secure": False,
        "_env_file": None,
    }
    defaults.update(overrides)
    return AdminAPISettings(**defaults)  # type: ignore[arg-type]


def _dsn(migrated_engine: AsyncEngine) -> str:
    return str(migrated_engine.url.render_as_string(hide_password=False))


def _login_as_bootstrap_admin(client: TestClient) -> str:
    """Logs the bootstrap admin in on ``client`` and returns the CSRF token
    every subsequent mutating request in these tests must echo back via
    ``CSRF_HEADER_NAME``."""
    client.post(
        "/api/v1/auth/login",
        json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
    )
    return str(client.cookies[CSRF_COOKIE_NAME])


def test_admin_creates_a_user_with_a_role(migrated_engine: AsyncEngine) -> None:
    """US-A4's first Given/When/Then: the created user can log in and is
    scoped to the role assigned at creation."""
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        create_response = client.post(
            "/api/v1/users",
            json={
                "username": "new-viewer",
                "password": "a-strong-enough-password",
                "role": "viewer",
            },
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert create_response.status_code == 201
        body = create_response.json()
        assert body["username"] == "new-viewer"
        assert body["role"] == "viewer"
        assert body["is_active"] is True
        assert "password" not in body
        assert "password_hash" not in body

    # The new user can actually log in and is scoped to "viewer" — proven
    # against a fresh client/session, the real end-to-end proof US-A4's
    # first Given/When/Then asks for, not just a row existing in the DB.
    with TestClient(create_app(settings)) as fresh_client:
        login_response = fresh_client.post(
            "/api/v1/auth/login",
            json={"username": "new-viewer", "password": "a-strong-enough-password"},
        )
        assert login_response.status_code == 200
        assert login_response.json() == {"username": "new-viewer", "role": "viewer"}


def test_list_users_includes_bootstrap_admin_and_newly_created_user(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        client.post(
            "/api/v1/users",
            json={
                "username": "second-admin",
                "password": "a-strong-enough-password",
                "role": "admin",
            },
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        list_response = client.get("/api/v1/users")

        assert list_response.status_code == 200
        usernames = {user["username"] for user in list_response.json()["users"]}
        assert usernames == {_BOOTSTRAP_USERNAME, "second-admin"}


def test_create_user_with_duplicate_username_returns_409(migrated_engine: AsyncEngine) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        response = client.post(
            "/api/v1/users",
            json={
                "username": _BOOTSTRAP_USERNAME,
                "password": "a-strong-enough-password",
                "role": "viewer",
            },
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        assert response.status_code == 409


def test_create_user_with_too_short_password_returns_422(migrated_engine: AsyncEngine) -> None:
    settings = _settings(dsn=_dsn(migrated_engine), min_password_length=12)
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        response = client.post(
            "/api/v1/users",
            json={"username": "short-pw-user", "password": "short", "role": "viewer"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        assert response.status_code == 422


def test_viewer_session_is_rejected_on_every_mutating_and_list_user_route(
    migrated_engine: AsyncEngine,
) -> None:
    """US-A5: every route this module adds — including the list route,
    per Phase 2's own plan section grouping all three under one
    require_admin_role-gated bullet — rejects a Viewer session with 403."""
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        client.post(
            "/api/v1/users",
            json={"username": "a-viewer", "password": "a-strong-enough-password", "role": "viewer"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf_token})

    with TestClient(create_app(settings)) as viewer_client:
        viewer_client.post(
            "/api/v1/auth/login",
            json={"username": "a-viewer", "password": "a-strong-enough-password"},
        )
        viewer_csrf_token = viewer_client.cookies[CSRF_COOKIE_NAME]

        list_response = viewer_client.get("/api/v1/users")
        assert list_response.status_code == 403

        create_response = viewer_client.post(
            "/api/v1/users",
            json={
                "username": "should-not-be-created",
                "password": "irrelevant-password",
                "role": "viewer",
            },
            headers={CSRF_HEADER_NAME: viewer_csrf_token},
        )
        assert create_response.status_code == 403


def test_deactivating_an_unknown_user_id_returns_404(migrated_engine: AsyncEngine) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        response = client.patch(
            "/api/v1/users/00000000-0000-0000-0000-000000000000/deactivate",
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        assert response.status_code == 404


def test_deactivation_revokes_the_users_existing_session(migrated_engine: AsyncEngine) -> None:
    """US-A4's second Given/When/Then: deactivation revokes that user's
    existing sessions and they can no longer log in."""
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as admin_client:
        admin_csrf_token = _login_as_bootstrap_admin(admin_client)
        create_response = admin_client.post(
            "/api/v1/users",
            json={
                "username": "soon-deactivated",
                "password": "a-strong-enough-password",
                "role": "viewer",
            },
            headers={CSRF_HEADER_NAME: admin_csrf_token},
        )
        target_user_id = create_response.json()["id"]

        with TestClient(create_app(settings)) as target_client:
            target_client.post(
                "/api/v1/auth/login",
                json={"username": "soon-deactivated", "password": "a-strong-enough-password"},
            )
            target_session_cookie = target_client.cookies[SESSION_COOKIE_NAME]

            # Sanity check: the session is valid before deactivation.
            pre_deactivation_response = target_client.get("/api/v1/auth/session")
            assert pre_deactivation_response.status_code == 200

            deactivate_response = admin_client.patch(
                f"/api/v1/users/{target_user_id}/deactivate",
                headers={CSRF_HEADER_NAME: admin_csrf_token},
            )
            assert deactivate_response.status_code == 200
            assert deactivate_response.json()["is_active"] is False

            # The already-issued session cookie is rejected on the very
            # next request, even though the cookie itself is untouched.
            replay_response = target_client.get(
                "/api/v1/auth/session",
                cookies={SESSION_COOKIE_NAME: target_session_cookie},
            )
            assert replay_response.status_code == 401

        # The deactivated user can no longer log in at all.
        with TestClient(create_app(settings)) as retry_client:
            retry_login_response = retry_client.post(
                "/api/v1/auth/login",
                json={"username": "soon-deactivated", "password": "a-strong-enough-password"},
            )
            assert retry_login_response.status_code == 401


def test_deactivated_users_past_audit_rows_still_resolve_to_their_username(
    migrated_engine: AsyncEngine,
) -> None:
    """US-A4's second Given/When/Then: historical actions (here, the
    user's own login_succeeded audit row) remain attributed to them after
    deactivation — deactivation is a status flip, never a hard delete
    (Assumption 6), so the admin_audit_log -> admin_user join keeps
    resolving."""
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as admin_client:
        admin_csrf_token = _login_as_bootstrap_admin(admin_client)
        create_response = admin_client.post(
            "/api/v1/users",
            json={
                "username": "audited-then-deactivated",
                "password": "a-strong-enough-password",
                "role": "viewer",
            },
            headers={CSRF_HEADER_NAME: admin_csrf_token},
        )
        target_user_id = create_response.json()["id"]

        # Generates a login_succeeded admin_audit_log row attributed to
        # this user's id, before they're deactivated.
        with TestClient(create_app(settings)) as target_client:
            target_client.post(
                "/api/v1/auth/login",
                json={
                    "username": "audited-then-deactivated",
                    "password": "a-strong-enough-password",
                },
            )

        deactivate_response = admin_client.patch(
            f"/api/v1/users/{target_user_id}/deactivate",
            headers={CSRF_HEADER_NAME: admin_csrf_token},
        )
        assert deactivate_response.status_code == 200

    session_factory = create_session_factory(migrated_engine)

    async def _assert_audit_row_still_resolves() -> None:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(AdminAuditLogRow, AdminUserRow)
                    .join(AdminUserRow, AdminAuditLogRow.actor_user_id == AdminUserRow.id)
                    .where(
                        AdminAuditLogRow.action == "login_succeeded",
                        AdminUserRow.username == "audited-then-deactivated",
                    )
                )
            ).one_or_none()
            assert row is not None, "audit row no longer resolves to the deactivated user"
            _, user_row = row
            assert user_row.username == "audited-then-deactivated"
            assert user_row.is_active is False

    asyncio.run(_assert_audit_row_still_resolves())


def test_user_creation_and_deactivation_are_recorded_in_the_audit_log(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        create_response = client.post(
            "/api/v1/users",
            json={
                "username": "audit-check-user",
                "password": "a-strong-enough-password",
                "role": "viewer",
            },
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        target_user_id = create_response.json()["id"]

        client.patch(
            f"/api/v1/users/{target_user_id}/deactivate",
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    session_factory = create_session_factory(migrated_engine)

    async def _assert_actions_logged() -> None:
        async with session_factory() as session:
            actions = (
                (
                    await session.execute(
                        select(AdminAuditLogRow.action).where(
                            AdminAuditLogRow.resource_id == target_user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(actions) == {"user_created", "user_deactivated"}

    asyncio.run(_assert_actions_logged())
