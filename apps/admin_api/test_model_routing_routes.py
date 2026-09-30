"""Integration tests for apps/admin_api/model_routing_routes.py — model
routing view/set (Unit F, US-A14, US-A15), driven through the real
apps/admin_api FastAPI app via TestClient, against a real Postgres via
testcontainers-python (per docs/rules/testing.md's mandatory DB-integration
rule), per docs/plans/plan-deliver-admin-gui.md's Phase 5 "Tests" section:

"view/set, unique-constraint upsert, and the effective-source field correct
across all three tiers."

Covers US-A14's Given/When/Then (effective model per stage, plus its
source) and US-A15's (a write at a scope persists and is visible; a
repeat write to the same (org_id, stage) updates in place, per
`uq_model_routing_org_stage`).
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
    ModelRoutingConfig,
    Org,
)
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings

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
        "llm_model": "local-model",
        "_env_file": None,
    }
    defaults.update(overrides)
    return AdminAPISettings(**defaults)  # type: ignore[arg-type]


def _dsn(migrated_engine: AsyncEngine) -> str:
    return str(migrated_engine.url.render_as_string(hide_password=False))


def _login_as_bootstrap_admin(client: TestClient) -> str:
    """Mirrors apps/admin_api/test_config_routes.py's own identical
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
    async with session_factory() as session, session.begin():
        session.add(
            AdminUserRow(
                username=_VIEWER_USERNAME,
                password_hash=Argon2PasswordHasher().hash(_VIEWER_PASSWORD),
                role=AdminRole.VIEWER,
            )
        )


@pytest_asyncio.fixture
async def org_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        return org.id


# --- US-A14: GET /api/v1/model-routing --------------------------------------


def test_get_model_routing_with_no_rows_returns_static_default_for_every_stage(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)

        response = client.get("/api/v1/model-routing")

    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] is None
    stages = {s["stage"]: s for s in body["stages"]}
    assert set(stages) == {"intent_summary", "review_generation", "verification"}
    for stage_view in stages.values():
        assert stage_view["model_name"] == "local-model"
        assert stage_view["source"] == "static_default"


def test_get_model_routing_without_a_session_returns_401(migrated_engine: AsyncEngine) -> None:
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.get("/api/v1/model-routing")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_viewer_session_can_read_model_routing(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
) -> None:
    """US-A5's second clause: reading routing configuration is not a
    mutating action, so a Viewer session has read parity with an Admin."""
    await _seed_viewer(session_factory)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        client.post(
            "/api/v1/auth/login", json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD}
        )
        response = client.get("/api/v1/model-routing")

    assert response.status_code == 200


def test_get_model_routing_effective_source_correct_across_all_three_tiers(
    migrated_engine: AsyncEngine,
    org_id: uuid.UUID,
) -> None:
    """Per the plan's own Tests bullet: "the effective-source field correct
    across all three tiers" — org, platform, and static default, all in one
    test."""
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        # No rows at all yet: static_default for every stage.
        static_response = client.get("/api/v1/model-routing", params={"org_id": str(org_id)})
        static_stages = {s["stage"]: s for s in static_response.json()["stages"]}
        assert static_stages["review_generation"]["source"] == "static_default"
        assert static_stages["review_generation"]["model_name"] == "local-model"

        # A platform-wide row: platform tier now wins for every org.
        client.put(
            "/api/v1/model-routing/review_generation",
            json={"org_id": None, "model_name": "platform-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        platform_response = client.get("/api/v1/model-routing", params={"org_id": str(org_id)})
        platform_stages = {s["stage"]: s for s in platform_response.json()["stages"]}
        assert platform_stages["review_generation"]["source"] == "platform"
        assert platform_stages["review_generation"]["model_name"] == "platform-model"

        # An org-specific row: org tier now wins for that org, but the
        # platform-wide view (no org_id) still sees the platform row.
        client.put(
            "/api/v1/model-routing/review_generation",
            json={"org_id": str(org_id), "model_name": "org-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        org_response = client.get("/api/v1/model-routing", params={"org_id": str(org_id)})
        org_stages = {s["stage"]: s for s in org_response.json()["stages"]}
        assert org_stages["review_generation"]["source"] == "org"
        assert org_stages["review_generation"]["model_name"] == "org-model"

        platform_only_response = client.get("/api/v1/model-routing")
        platform_only_stages = {s["stage"]: s for s in platform_only_response.json()["stages"]}
        assert platform_only_stages["review_generation"]["source"] == "platform"
        assert platform_only_stages["review_generation"]["model_name"] == "platform-model"


# --- US-A15: PUT /api/v1/model-routing/{stage} ------------------------------


def test_setting_platform_wide_model_persists_and_is_visible(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        put_response = client.put(
            "/api/v1/model-routing/verification",
            json={"org_id": None, "model_name": "stronger-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        assert put_response.status_code == 200
        body = put_response.json()
        assert body["org_id"] is None
        assert body["stage"] == "verification"
        assert body["model_name"] == "stronger-model"

        get_response = client.get("/api/v1/model-routing")

    stages = {s["stage"]: s for s in get_response.json()["stages"]}
    assert stages["verification"]["model_name"] == "stronger-model"
    assert stages["verification"]["source"] == "platform"


@pytest.mark.asyncio
async def test_repeat_write_to_same_org_and_stage_updates_in_place_not_duplicated(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    org_id: uuid.UUID,
) -> None:
    """Respects `uq_model_routing_org_stage` — a second write to the exact
    same (org_id, stage) updates the existing row rather than inserting a
    duplicate (the same NULL-unique-constraint gap
    `adapter/storage/tenancy.py`'s own `PostgresSettingRepository.upsert`
    already closes for `uq_setting_scope_key`)."""
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        client.put(
            "/api/v1/model-routing/intent_summary",
            json={"org_id": str(org_id), "model_name": "first-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.put(
            "/api/v1/model-routing/intent_summary",
            json={"org_id": str(org_id), "model_name": "second-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ModelRoutingConfig).where(
                        ModelRoutingConfig.org_id == org_id,
                        ModelRoutingConfig.stage == "intent_summary",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].model_name == "second-model"


def test_setting_an_unknown_stage_returns_422(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        response = client.put(
            "/api/v1/model-routing/not-a-real-stage",
            json={"org_id": None, "model_name": "some-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_viewer_session_cannot_write_model_routing_returns_403(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
) -> None:
    await _seed_viewer(session_factory)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        client.post(
            "/api/v1/auth/login", json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD}
        )
        csrf_token = str(client.cookies[CSRF_COOKIE_NAME])

        response = client.put(
            "/api/v1/model-routing/verification",
            json={"org_id": None, "model_name": "some-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 403


def test_setting_model_routing_writes_audit_log(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)

        client.put(
            "/api/v1/model-routing/verification",
            json={"org_id": None, "model_name": "stronger-model"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    async def _read_audit_rows() -> list[AdminAuditLogRow]:
        session_factory = create_session_factory(migrated_engine)
        async with session_factory() as session:
            result = await session.execute(
                select(AdminAuditLogRow).where(AdminAuditLogRow.action == "model_routing_updated")
            )
            return list(result.scalars().all())

    rows = asyncio.run(_read_audit_rows())
    assert len(rows) == 1
    assert rows[0].resource_type == "model_routing_config"
