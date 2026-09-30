"""Integration tests for apps/admin_api/main.py's create_app composition
root — US-A3's bootstrap-admin behavior (fresh vs. already-has-admin), and
the /healthz liveness/readiness probe, against a real Postgres/pgvector via
testcontainers-python, per
docs/plans/plan-deliver-admin-gui.md's Phase 1 (Unit B) Tests section.

The bootstrap routine's own multi-replica concurrency guarantee (the
advisory lock preventing duplicate admins) is covered separately, in
apps/admin_api/test_bootstrap.py, via a non-TestClient concurrency test —
TestClient's own `with`-block precedent is single-portal and sequential by
design and cannot exercise two overlapping bootstrap attempts on its own.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import AdminRole, AdminUserRow
from apps.admin_api.main import app, create_app
from apps.admin_api.settings import AdminAPISettings

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


def test_healthz_returns_ok() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
def test_bootstrap_creates_one_admin_user_on_a_fresh_database(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))

    with TestClient(create_app(settings)):
        pass

    session_factory = create_session_factory(migrated_engine)

    async def _assert_one_admin() -> None:
        async with session_factory() as session:
            rows = (await session.execute(select(AdminUserRow))).scalars().all()
            assert len(rows) == 1
            assert rows[0].username == _BOOTSTRAP_USERNAME
            assert rows[0].role == AdminRole.ADMIN
            assert rows[0].is_active is True

    asyncio.run(_assert_one_admin())


@pytest.mark.integration
def test_bootstrap_is_a_no_op_when_an_admin_user_already_exists(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))

    with TestClient(create_app(settings)):
        pass

    # A second app instance, with a *different* bootstrap username —
    # US-A3's "already has at least one admin user -> no-op" guarantee
    # must hold regardless of the configured username, per Assumption 11's
    # own reasoning (a narrower ON CONFLICT (username) DO NOTHING alone
    # would not satisfy this).
    second_settings = _settings(
        dsn=_dsn(migrated_engine), admin_bootstrap_username="a-different-admin-name"
    )
    with TestClient(create_app(second_settings)):
        pass

    session_factory = create_session_factory(migrated_engine)

    async def _assert_still_one_admin() -> None:
        async with session_factory() as session:
            rows = (await session.execute(select(AdminUserRow))).scalars().all()
            assert len(rows) == 1
            assert rows[0].username == _BOOTSTRAP_USERNAME

    asyncio.run(_assert_still_one_admin())
