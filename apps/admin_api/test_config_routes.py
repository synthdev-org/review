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
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
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


# --- plan-deliver-product-knowledge.md Phase 6 (Unit F): the
# product_knowledge_provider key — US-PK8, US-PK9, Amendments A2/A4 ------------

_PK_KEY = "product_knowledge_provider"
_PK_TOKEN = "CANARY-PK-TOKEN-never-in-a-response-9c1185a5"
_PK_MAP = (
    '{"wiki-prod": {"token": "' + _PK_TOKEN + '", "allowed_hosts": ["wiki.example.com"]},'
    ' "kb-prod": {"token": "' + _PK_TOKEN + '-2", "allowed_hosts": ["kb.example.com"]}}'
)


def _pk_settings(dsn: str, *, credentials: str | None = _PK_MAP) -> AdminAPISettings:
    return _settings(
        dsn=dsn,
        product_knowledge_credentials=SecretStr(credentials) if credentials is not None else None,
    )


def _pk_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "display_name": "Company Wiki",
        "endpoint_url": "https://wiki.example.com/mcp",
        "credential_ref": "wiki-prod",
    }
    value.update(overrides)
    return value


def _put_pk(client: TestClient, csrf: str, scope: str = "platform", **overrides: object) -> Any:
    return client.put(
        f"/api/v1/config/{scope}/{_PK_KEY}",
        json={"value": _pk_value(**overrides)},
        headers={CSRF_HEADER_NAME: csrf},
    )


async def _audit_rows(session_factory: async_sessionmaker[Any]) -> list[AdminAuditLogRow]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AdminAuditLogRow).where(
                        AdminAuditLogRow.action == "config_setting_updated"
                    )
                )
            )
            .scalars()
            .all()
        )


def test_provider_setting_is_stored_with_defaults_and_the_token_is_never_exposed(
    migrated_engine: AsyncEngine, session_factory: async_sessionmaker[Any]
) -> None:
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)

        put_response = _put_pk(client, csrf)
        get_response = client.get("/api/v1/config")

    assert put_response.status_code == 200
    assert put_response.json()["value"] == {
        **_pk_value(),
        "allow_public_repos": False,
        "issue_key_prefixes": [],
    }
    audit_rows = asyncio.run(_audit_rows(session_factory))
    for blob in (put_response.text, get_response.text, str([r.extra for r in audit_rows])):
        assert _PK_TOKEN not in blob


def test_provider_setting_audit_metadata_carries_old_and_new_host_ref_and_enabled(
    migrated_engine: AsyncEngine, session_factory: async_sessionmaker[Any]
) -> None:
    """
    Amendment A4: a credential redirect or provider swap is reconstructable from the audit trail.

    It must be reconstructable without a token or a URL path.
    """
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)
        first = _put_pk(client, csrf, endpoint_url="https://wiki.example.com/secret/path/mcp")
        second = _put_pk(
            client,
            csrf,
            endpoint_url="https://kb.example.com/mcp",
            credential_ref="kb-prod",
            enabled=False,
            allow_public_repos=True,
        )
    assert first.status_code == 200
    assert second.status_code == 200

    rows = sorted(asyncio.run(_audit_rows(session_factory)), key=lambda r: r.created_at)
    assert len(rows) == 2
    assert rows[0].extra["old"] is None
    assert rows[0].extra["new"] == {
        "endpoint_host": "wiki.example.com",
        "credential_ref": "wiki-prod",
        "enabled": True,
        "allow_public_repos": False,
    }
    assert rows[1].extra["old"] == rows[0].extra["new"]
    assert rows[1].extra["new"] == {
        "endpoint_host": "kb.example.com",
        "credential_ref": "kb-prod",
        "enabled": False,
        "allow_public_repos": True,
    }
    blob = str([r.extra for r in rows])
    assert "secret/path" not in blob
    assert _PK_TOKEN not in blob


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"credential_ref": "nope"}, "unknown_credential_ref"),
        ({"endpoint_url": "https://evil.example.org/mcp"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://wiki.example.com.evil.com/mcp"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://evilwiki.example.com/mcp"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://xn--wiki-9ib.example.com/mcp"}, "credential_host_not_bound"),
        ({"credential_ref": "kb-prod"}, "credential_host_not_bound"),
    ],
)
def test_unknown_or_unbound_credential_is_422_and_nothing_is_stored(
    migrated_engine: AsyncEngine, overrides: dict[str, Any], code: str
) -> None:
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)

        response = _put_pk(client, csrf, **overrides)
        tree = client.get("/api/v1/config").json()

    assert response.status_code == 422
    assert response.json()["detail"] == code
    assert tree["platform_settings"] == {}


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://WIKI.Example.com./mcp",  # case + trailing dot
        "https://wiki.example.com:8443/mcp",  # port
        "http://wiki.example.com/mcp",
    ],
)
def test_binding_uses_the_shared_canonicaliser_so_equivalent_hosts_are_accepted(
    migrated_engine: AsyncEngine, endpoint: str
) -> None:
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)

        response = _put_pk(client, csrf, endpoint_url=endpoint)

    assert response.status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"display_name": "<script>alert(1)</script>"},
        {"display_name": ""},
        {"endpoint_url": "https://admin:hunter2pw@wiki.example.com/mcp"},
        {"endpoint_url": "https://wiki.example.com/mcp?token=abc"},
        {"endpoint_url": "ftp://wiki.example.com/mcp"},
        {"issue_key_prefixes": ["lowercase"]},
        {"allow_public_repos": "maybe"},
    ],
)
def test_invalid_provider_value_is_422_without_echoing_the_offered_value(
    migrated_engine: AsyncEngine, overrides: dict[str, Any]
) -> None:
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)

        response = _put_pk(client, csrf, **overrides)

    assert response.status_code == 422
    assert "hunter2pw" not in response.text


def test_team_scope_is_rejected_for_the_provider_key(
    migrated_engine: AsyncEngine, org_repo_team: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    org_id, _repo_id, team_id = org_repo_team
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)

        response = _put_pk(client, csrf, scope=f"team:{org_id}:{team_id}")

    assert response.status_code == 422
    assert response.json()["detail"] == "team_scope_not_supported"


@pytest.mark.parametrize("scope_kind", ["repo", "team"])
def test_show_nits_is_rejected_below_org_scope_but_accepted_at_org_and_platform(
    migrated_engine: AsyncEngine,
    org_repo_team: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    scope_kind: str,
) -> None:
    org_id, repo_id, team_id = org_repo_team
    narrow = {"repo": f"repo:{org_id}:{repo_id}", "team": f"team:{org_id}:{team_id}"}[scope_kind]
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)
        results = {
            scope: client.put(
                f"/api/v1/config/{scope}/findings_show_nits",
                json={"value": {"enabled": True}},
                headers={CSRF_HEADER_NAME: csrf},
            )
            for scope in (narrow, f"org:{org_id}", "platform")
        }

    assert results[narrow].status_code == 422
    assert results[narrow].json()["detail"] == "platform_or_org_scope_only"
    assert results[f"org:{org_id}"].status_code == 200
    assert results["platform"].status_code == 200


def test_non_ascii_endpoint_host_lookalike_is_rejected_outright(
    migrated_engine: AsyncEngine,
) -> None:
    """
    A Cyrillic lookalike never reaches the binding check.

    Endpoint URLs are printable ASCII (IDN hosts are supplied as punycode).
    """
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)

        response = _put_pk(client, csrf, endpoint_url="https://w\u0456ki.example.com/mcp")

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("allowlist", "url", "expected_status"),
    [
        ("kb:8080", "http://kb:8080/mcp", 200),  # listed host:port
        ("kb", "http://kb:8080/mcp", 200),  # listed host (any port)
        ("*.svc.local", "http://kb.svc.local/mcp", 200),
        ("", "http://kb:8080/mcp", 422),  # M1: no allow-list, no credential host
        ("other", "http://kb:8080/mcp", 422),
        ("kb:9090", "http://kb:8080/mcp", 422),  # M2: port must match
        ("", "https://kb.example.com/mcp", 200),  # a credential is bound to the host
    ],
)
def test_null_credential_ref_must_match_the_allowlist_or_a_credential_host(
    migrated_engine: AsyncEngine, allowlist: str, url: str, expected_status: int
) -> None:
    settings = _settings(
        dsn=_dsn(migrated_engine),
        product_knowledge_credentials=SecretStr(_PK_MAP),
        product_knowledge_endpoint_allowlist=allowlist,
    )
    with TestClient(create_app(settings)) as client:
        csrf = _login_and_csrf(client)

        response = _put_pk(client, csrf, credential_ref=None, endpoint_url=url)

    assert response.status_code == expected_status
    if expected_status == 422:
        assert response.json()["detail"] == "unauthenticated_host_not_allowed"


def test_a_malformed_allowlist_fails_closed_for_null_ref_but_credentialed_writes_work(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(
        dsn=_dsn(migrated_engine),
        product_knowledge_credentials=SecretStr(_PK_MAP),
        product_knowledge_endpoint_allowlist="*",  # malformed: wildcard-only is rejected
    )
    with TestClient(create_app(settings)) as client:
        csrf = _login_and_csrf(client)

        null_ref = _put_pk(client, csrf, credential_ref=None, endpoint_url="http://kb:8080/mcp")
        credentialed = _put_pk(client, csrf)

    assert null_ref.status_code == 422
    assert null_ref.json()["detail"] == "unauthenticated_host_not_allowed"
    assert credentialed.status_code == 200


def test_null_credential_ref_with_no_credential_map_needs_the_allowlist(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine), product_knowledge_endpoint_allowlist="kb:8080")
    with TestClient(create_app(settings)) as client:
        csrf = _login_and_csrf(client)

        listed = _put_pk(client, csrf, credential_ref=None, endpoint_url="http://kb:8080/mcp")
        unlisted = _put_pk(client, csrf, credential_ref=None, endpoint_url="http://x:8080/mcp")

    assert listed.status_code == 200
    assert unlisted.status_code == 422


def test_credential_allowed_hosts_port_is_enforced_at_write_time(
    migrated_engine: AsyncEngine,
) -> None:
    pk_map = '{"wiki-prod": {"token": "t-0123", "allowed_hosts": ["wiki.example.com:8443"]}}'
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine), credentials=pk_map))) as client:
        csrf = _login_and_csrf(client)

        wrong = _put_pk(client, csrf, endpoint_url="https://wiki.example.com/mcp")
        right = _put_pk(client, csrf, endpoint_url="https://wiki.example.com:8443/mcp")

    assert wrong.status_code == 422
    assert wrong.json()["detail"] == "credential_host_not_bound"
    assert right.status_code == 200


def test_provider_setting_is_still_stored_when_the_kill_switch_is_off(
    migrated_engine: AsyncEngine,
) -> None:
    """
    The kill switch gates test-connection and runtime use, never config writes.

    An operator can stage the setting while the feature is off.
    """
    settings = _settings(
        dsn=_dsn(migrated_engine),
        product_knowledge_credentials=SecretStr(_PK_MAP),
        product_knowledge_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        csrf = _login_and_csrf(client)

        put_response = _put_pk(client, csrf)
        tree = client.get("/api/v1/config").json()

    assert put_response.status_code == 200
    assert tree["platform_settings"][_PK_KEY]["display_name"] == "Company Wiki"


def test_a_malformed_credential_map_fails_open_but_refuses_credential_references(
    migrated_engine: AsyncEngine,
) -> None:
    bad = SecretStr('{"wiki-prod": {"token": "' + _PK_TOKEN + '", "allowed_hosts": []}}')
    app = create_app(
        _settings(
            dsn=_dsn(migrated_engine),
            product_knowledge_credentials=bad,
            product_knowledge_endpoint_allowlist="wiki.example.com",
        )
    )
    with TestClient(app) as client:
        csrf = _login_and_csrf(client)

        with_ref = _put_pk(client, csrf)
        without_ref = _put_pk(client, csrf, credential_ref=None)
        other_key = client.put(
            "/api/v1/config/platform/memory_suppression_enabled",
            json={"value": {"enabled": True}},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert with_ref.status_code == 422
    assert with_ref.json()["detail"] == "credential_map_unavailable"
    assert without_ref.status_code == 200
    assert other_key.status_code == 200


def test_config_tree_effective_field_resolves_precedence_and_repo_opt_out(
    migrated_engine: AsyncEngine, org_repo_team: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    org_id, repo_id, team_id = org_repo_team
    with TestClient(create_app(_pk_settings(_dsn(migrated_engine)))) as client:
        csrf = _login_and_csrf(client)
        assert _put_pk(client, csrf, display_name="Platform Wiki").status_code == 200
        assert (
            _put_pk(client, csrf, scope=f"org:{org_id}", display_name="Acme Wiki").status_code
            == 200
        )
        assert (
            _put_pk(client, csrf, scope=f"repo:{org_id}:{repo_id}", enabled=False).status_code
            == 200
        )

        body = client.get("/api/v1/config").json()

    assert body["platform_effective"][_PK_KEY]["value"]["display_name"] == "Platform Wiki"
    assert body["platform_effective"][_PK_KEY]["source"] == "platform"
    org_node = next(o for o in body["orgs"] if o["id"] == str(org_id))
    assert org_node["effective"][_PK_KEY]["value"]["display_name"] == "Acme Wiki"
    assert org_node["effective"][_PK_KEY]["source"] == f"org:{org_id}"
    repo_node = next(r for r in org_node["repos"] if r["id"] == str(repo_id))
    assert repo_node["effective"][_PK_KEY]["value"]["enabled"] is False
    assert repo_node["effective"][_PK_KEY]["source"] == f"repo:{org_id}:{repo_id}"
    team_node = next(t for t in org_node["teams"] if t["id"] == str(team_id))
    assert team_node["effective"][_PK_KEY]["value"]["display_name"] == "Acme Wiki"


def test_config_write_total_counts_ok_and_rejected_writes_by_registry_key(
    migrated_engine: AsyncEngine, reset_otel_global_providers: None
) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    try:
        with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
            csrf = _login_and_csrf(client)
            headers = {CSRF_HEADER_NAME: csrf}
            put = "/api/v1/config/platform/"
            client.put(
                put + "memory_suppression_enabled",
                json={"value": {"enabled": True}},
                headers=headers,
            )
            client.put(
                put + "memory_suppression_enabled",
                json={"value": {"enabled": "x"}},
                headers=headers,
            )
            client.put(put + "typo_key", json={"value": {}}, headers=headers)
        data = reader.get_metrics_data()
    finally:
        provider.shutdown()

    assert data is not None
    points = {
        (a["key"], a["outcome"]): p.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.config.write_total"
        for p in m.data.data_points
        for a in [dict(p.attributes or {})]
    }
    assert points == {
        ("memory_suppression_enabled", "ok"): 1,
        ("memory_suppression_enabled", "validation_rejected"): 1,
        ("unknown", "validation_rejected"): 1,
    }
