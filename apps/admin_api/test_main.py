"""Integration tests for apps/admin_api/main.py's create_app composition
root — US-A3's bootstrap-admin behavior (fresh vs. already-has-admin), the
/healthz liveness/readiness probe, and (docs/plans/plan-deliver-admin-gui.md's
Phase 3 section, Unit D) the Memory admin API's own local embedding-model
startup check, moved here from apps/webhook_api along with the approve
route it serves — against a real Postgres/pgvector via
testcontainers-python, per that same phase's Tests section.

The bootstrap routine's own multi-replica concurrency guarantee (the
advisory lock preventing duplicate admins) is covered separately, in
apps/admin_api/test_bootstrap.py, via a non-TestClient concurrency test —
TestClient's own `with`-block precedent is single-portal and sequential by
design and cannot exercise two overlapping bootstrap attempts on its own.
"""

from __future__ import annotations

import asyncio
import logging

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


# --- docs/plans/plan-deliver-admin-gui.md's Phase 3 (Unit D): the Memory
# admin API's local embedding-model startup check, moved here from
# apps/webhook_api along with the approve route it serves --------------

_UNREACHABLE_DSN = "postgresql+asyncpg://unused:unused@localhost:5432/unused"


def _unreachable_dsn_settings(**overrides: object) -> AdminAPISettings:
    """Settings built against a syntactically-valid but unreachable DSN —
    safe here because `adapter.storage.engine.create_storage_engine`/
    `create_session_factory` are non-connecting, synchronous factory calls
    (see their own docstrings) and `_check_embedding_model_startup` runs
    synchronously inside `create_app` itself, before any DB connection is
    ever attempted — mirrors `apps/webhook_api/test_main.py`'s own
    (deleted) identical pattern for this exact check."""
    return _settings(dsn=_UNREACHABLE_DSN, **overrides)


def test_create_app_succeeds_with_loadable_embedding_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """docs/plans/plan-deliver-admin-gui.md's Phase 3 section: this
    entrypoint's own `LocalONNXEmbeddingClient` startup check, mirroring
    `apps/worker/test_main.py::test_main_succeeds_with_loadable_embedding_model`
    (and `apps/webhook_api/test_main.py`'s own, now-deleted, identical
    test) — no mocking, the real Dockerfile-baked model, exercised for
    real."""
    logging.getLogger("apps.admin_api.main").disabled = False
    settings = _unreachable_dsn_settings()  # embedding_model_path defaults to the real baked model

    with caplog.at_level("INFO", logger="apps.admin_api.main"):
        create_app(settings)  # must not raise SystemExit

    assert any(
        record.name == "apps.admin_api.main"
        and record.getMessage() == "local embedding model loaded"
        for record in caplog.records
    )


def test_create_app_exits_non_zero_with_unloadable_embedding_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logging.getLogger("apps.admin_api.main").disabled = False
    settings = _unreachable_dsn_settings(embedding_model_path="/nonexistent/model/path")

    with (
        caplog.at_level("ERROR", logger="apps.admin_api.main"),
        pytest.raises(SystemExit) as excinfo,
    ):
        create_app(settings)

    assert excinfo.value.code == 1
    assert any(
        record.name == "apps.admin_api.main"
        and record.getMessage() == "local embedding model is not usable at startup; exiting"
        and record.levelname == "ERROR"
        for record in caplog.records
    )
