"""Integration tests for apps/admin_api/config_routes.py — org/repo/team
configuration (Unit E, US-A12, US-A13), driven through the real
apps/admin_api FastAPI app via TestClient, against a real Postgres via
testcontainers-python (per docs/rules/testing.md's mandatory
DB-integration rule), per docs/plans/plan-deliver-admin-gui.md's Phase 4
Tests section:

"write at each scope, read-time override confirmed, invalid value → 422,
empty-scope UI shape."

Covers US-A12's Given/When/Then (the org → repo → team hierarchy, with
empty scopes shown as "using platform defaults") and US-A13's (a write at
an exact scope persists and is visible; an invalid value is rejected with
422, never silently coerced or stored malformed; a narrower scope's value
overrides a broader one wherever that setting is read).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.engine import create_session_factory
from adapter.storage.models import (
    AdminAuditLogRow,
    AdminRole,
    AdminUserRow,
    Org,
    OrgRepoTeamSetting,
    Repo,
    Team,
)
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings
from tenancy.settings import SettingRow, resolve_setting

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"
_VIEWER_USERNAME = "integration-test-viewer"
_VIEWER_PASSWORD = "viewer-password-1234"


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
    """Mirrors apps/admin_api/test_memory_routes.py's own identical
    helper."""
    client.post(
        "/api/v1/auth/login",
        json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
    )
    return str(client.cookies[CSRF_COOKIE_NAME])


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


async def _seed_viewer(session_factory: async_sessionmaker[Any]) -> None:
    """Mirrors apps/admin_api/test_memory_routes.py's own identical
    helper."""
    async with session_factory() as session, session.begin():
        session.add(
            AdminUserRow(
                username=_VIEWER_USERNAME,
                password_hash=Argon2PasswordHasher().hash(_VIEWER_PASSWORD),
                role=AdminRole.VIEWER,
            )
        )


@pytest_asyncio.fixture
async def org_repo_team(
    session_factory: async_sessionmaker[Any],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seeds one Org with one Repo and one Team under it — the hierarchy
    US-A12's config screen renders."""
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        team = Team(org_id=org.id, name="platform-team")
        session.add_all([repo, team])
        await session.flush()
        return org.id, repo.id, team.id


def _login_and_csrf(client: TestClient) -> str:
    return _login_as_bootstrap_admin(client)


# --- US-A12: GET /api/v1/config ---------------------------------------------


def test_get_config_shows_hierarchy_with_empty_scopes_as_using_platform_defaults(
    migrated_engine: AsyncEngine,
    org_repo_team: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_id, repo_id, team_id = org_repo_team
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_and_csrf(client)

        response = client.get("/api/v1/config")

    assert response.status_code == 200
    body = response.json()
    assert body["platform_settings"] == {}

    org_node = next(org for org in body["orgs"] if org["id"] == str(org_id))
    assert org_node["settings"] == {}
    assert org_node["using_platform_defaults"] is True

    repo_node = next(repo for repo in org_node["repos"] if repo["id"] == str(repo_id))
    assert repo_node["settings"] == {}
    assert repo_node["using_platform_defaults"] is True

    team_node = next(team for team in org_node["teams"] if team["id"] == str(team_id))
    assert team_node["settings"] == {}
    assert team_node["using_platform_defaults"] is True


@pytest.mark.asyncio
async def test_viewer_session_can_read_config(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
) -> None:
    """US-A5's second clause: reading configuration is not a mutating
    action, so a Viewer session has read parity with an Admin."""
    await _seed_viewer(session_factory)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        client.post(
            "/api/v1/auth/login", json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD}
        )
        response = client.get("/api/v1/config")

    assert response.status_code == 200


def test_get_config_without_a_session_returns_401(migrated_engine: AsyncEngine) -> None:
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.get("/api/v1/config")

    assert response.status_code == 401


# --- US-A13: PUT /api/v1/config/{scope}/{key} -------------------------------


def test_setting_a_value_at_platform_scope_persists_and_is_visible(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        put_response = client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert put_response.status_code == 200
        assert put_response.json()["value"] == {"enabled": True}
        assert put_response.json()["org_id"] is None

        get_response = client.get("/api/v1/config")

    assert get_response.json()["platform_settings"] == {
        "memory_suppression_enabled": {"enabled": True}
    }


def test_setting_a_value_at_org_repo_and_team_scope_each_persist_independently(
    migrated_engine: AsyncEngine,
    org_repo_team: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    org_id, repo_id, team_id = org_repo_team
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        org_response = client.put(
            f"/api/v1/config/org:{org_id}/memory_suppression_enabled",
            json={"value": {"enabled": False}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        repo_response = client.put(
            f"/api/v1/config/repo:{org_id}:{repo_id}/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        team_response = client.put(
            f"/api/v1/config/team:{org_id}:{team_id}/findings_severity_threshold",
            json={"value": {"threshold": "high"}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert org_response.status_code == 200
        assert repo_response.status_code == 200
        assert team_response.status_code == 200

        get_response = client.get("/api/v1/config")

    org_node = next(org for org in get_response.json()["orgs"] if org["id"] == str(org_id))
    assert org_node["settings"] == {"memory_suppression_enabled": {"enabled": False}}
    assert org_node["using_platform_defaults"] is False

    repo_node = next(repo for repo in org_node["repos"] if repo["id"] == str(repo_id))
    assert repo_node["settings"] == {"memory_suppression_enabled": {"enabled": True}}
    assert repo_node["using_platform_defaults"] is False

    team_node = next(team for team in org_node["teams"] if team["id"] == str(team_id))
    assert team_node["settings"] == {"findings_severity_threshold": {"threshold": "high"}}
    assert team_node["using_platform_defaults"] is False


def test_repeated_write_at_the_same_scope_and_key_updates_in_place_not_duplicates(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Proves PostgresSettingRepository.upsert's own
    isnot_distinct_from-based read-then-write correctly updates the
    existing (NULL-scoped) platform row in place rather than inserting a
    second, silently-duplicate row — the exact Postgres "NULL is never
    equal to NULL in a unique constraint" pitfall this method's own
    docstring names."""
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        second_response = client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": False}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert second_response.status_code == 200
        assert second_response.json()["value"] == {"enabled": False}

    async def _count_rows() -> int:
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(OrgRepoTeamSetting).where(
                            OrgRepoTeamSetting.key == "memory_suppression_enabled"
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)

    row_count = asyncio.run(_count_rows())
    assert row_count == 1


def test_narrower_scope_value_overrides_broader_scope_at_read_time(
    migrated_engine: AsyncEngine,
    org_repo_team: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """US-A13's second Given/When/Then: a narrower scope's value overrides
    a broader one wherever that setting is read. Writes at all three
    scopes through the real HTTP API, then feeds the real, round-tripped
    rows from GET /api/v1/config into tenancy.settings.resolve_setting —
    proving the real Postgres-backed write path and the pure
    precedence-resolution function compose correctly together, not just
    that each works in isolation."""
    org_id, repo_id, _team_id = org_repo_team
    other_org_id = uuid.uuid4()

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.put(
            f"/api/v1/config/org:{org_id}/memory_suppression_enabled",
            json={"value": {"enabled": False}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.put(
            f"/api/v1/config/repo:{org_id}:{repo_id}/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        get_response = client.get("/api/v1/config")

    body = get_response.json()
    org_node = next(org for org in body["orgs"] if org["id"] == str(org_id))
    repo_node = next(repo for repo in org_node["repos"] if repo["id"] == str(repo_id))

    rows = (
        [
            SettingRow(org_id=None, repo_id=None, team_id=None, key=k, value=v)
            for k, v in body["platform_settings"].items()
        ]
        + [
            SettingRow(org_id=str(org_id), repo_id=None, team_id=None, key=k, value=v)
            for k, v in org_node["settings"].items()
        ]
        + [
            SettingRow(org_id=str(org_id), repo_id=str(repo_id), team_id=None, key=k, value=v)
            for k, v in repo_node["settings"].items()
        ]
    )

    # The repo itself resolves to its own override.
    repo_resolved = resolve_setting(
        "memory_suppression_enabled", rows, org_id=str(org_id), repo_id=str(repo_id)
    )
    assert repo_resolved is not None
    assert repo_resolved.value == {"enabled": True}

    # The org itself (no repo_id given) resolves to the org-level value,
    # not the repo-scoped override.
    org_resolved = resolve_setting("memory_suppression_enabled", rows, org_id=str(org_id))
    assert org_resolved is not None
    assert org_resolved.value == {"enabled": False}

    # An unrelated org with no row of its own falls all the way back to
    # the platform default.
    platform_resolved = resolve_setting(
        "memory_suppression_enabled", rows, org_id=str(other_org_id)
    )
    assert platform_resolved is not None
    assert platform_resolved.value == {"enabled": True}


def test_unknown_setting_key_returns_422(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        response = client.put(
            "/api/v1/config/platform/not_a_real_setting_key",
            json={"value": {"anything": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 422


def test_malformed_value_for_a_known_key_returns_422_and_is_not_stored(
    migrated_engine: AsyncEngine,
) -> None:
    """US-A13's second Given/When/Then: "never silently coerced or stored
    malformed" — asserted here by also proving GET /api/v1/config still
    shows platform defaults afterward, not a half-written row."""
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        put_response = client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": "not-a-boolean"}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert put_response.status_code == 422

        get_response = client.get("/api/v1/config")

    assert get_response.json()["platform_settings"] == {}


def test_malformed_scope_descriptor_returns_400(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)

        response = client.put(
            "/api/v1/config/not-a-real-scope/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_viewer_session_is_rejected_on_the_write_route(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
) -> None:
    await _seed_viewer(session_factory)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        client.post(
            "/api/v1/auth/login", json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD}
        )
        viewer_csrf_token = client.cookies[CSRF_COOKIE_NAME]

        response = client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: viewer_csrf_token},
        )

    assert response.status_code == 403


def test_config_write_is_recorded_in_the_audit_log(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_and_csrf(client)
        client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    async def _assert_logged() -> None:
        async with session_factory() as session:
            actions = (
                (
                    await session.execute(
                        select(AdminAuditLogRow.action).where(
                            AdminAuditLogRow.action == "config_setting_updated"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(actions) == 1

    asyncio.run(_assert_logged())
