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
from pathlib import Path

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


# --- docs/plans/plan-deliver-admin-gui.md's Phase 8 (Unit I): the built
# frontend, mounted as a catch-all *after* every API router ------------


def test_create_app_boots_with_no_frontend_router_when_dist_missing(tmp_path: Path) -> None:
    """The default `app` object this module's own `uvicorn` entrypoint
    serves is built exactly this way in every Python test/lint image (the
    frontend is built in the Dockerfile's own separate Node stage, never
    inside the Python `dev`/`runtime` stage) — this proves that state boots
    cleanly with no frontend mounted, rather than only asserting it via
    `create_frontend_router`'s own unit tests in isolation."""
    settings = _unreachable_dsn_settings()
    app = create_app(settings, frontend_dist_dir=tmp_path / "does-not-exist")

    # Deliberately *not* entered as a `with` block (unlike the
    # bootstrap-focused tests above) — this test only needs routing
    # behavior, and skipping the lifespan context avoids triggering
    # `bootstrap_admin_user`'s real Postgres connection attempt against
    # this test's deliberately unreachable DSN, the same way
    # `test_healthz_returns_ok` above already does for the module-level
    # `app` object.
    client = TestClient(app)
    response = client.get("/dashboard")

    assert response.status_code == 404


def test_create_app_serves_the_frontend_when_dist_present_without_shadowing_api_routes(
    tmp_path: Path,
) -> None:
    """Proves registration order end-to-end against the real composition
    root (not just frontend_routes.py's own isolated router tests): every
    API route Phases 1-7 register still resolves correctly with the
    frontend catch-all mounted, and an unknown client-side route (e.g.
    `/dashboard`) falls back to the SPA shell rather than a 404."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>admin ui shell</body></html>")

    settings = _unreachable_dsn_settings()
    app = create_app(settings, frontend_dist_dir=dist_dir)

    # Not entered as a `with` block — see the previous test's own comment
    # on why (avoids triggering bootstrap's real Postgres connection
    # attempt against this test's deliberately unreachable DSN).
    client = TestClient(app)

    # A real API route (require_session-gated, no session cookie
    # supplied) still resolves as an API 401, never the SPA shell.
    session_response = client.get("/api/v1/auth/session")
    assert session_response.status_code == 401

    # /healthz still resolves as the liveness probe, never the SPA
    # shell.
    healthz_response = client.get("/healthz")
    assert healthz_response.status_code == 200
    assert healthz_response.json() == {"status": "ok"}

    # An unknown client-side route falls back to the built frontend.
    dashboard_response = client.get("/dashboard")
    assert dashboard_response.status_code == 200
    assert "admin ui shell" in dashboard_response.text


# --- plan-deliver-product-knowledge.md Phase 6 (Unit F, Assumption 14): the
# Admin API's own product-knowledge settings — pure validators, fail OPEN ---

_PK_CANARY = "CANARY-ADMIN-PK-TOKEN-5d41402a"


def _pk_json(**entry: object) -> str:
    import json

    body: dict[str, object] = {"token": _PK_CANARY, "allowed_hosts": ["wiki.internal"]}
    body.update(entry)
    return json.dumps({"wiki-prod": body})


@pytest.mark.parametrize("blank", ["", "  "])
def test_admin_empty_or_whitespace_credentials_are_the_same_as_unset(blank: str) -> None:
    settings = _unreachable_dsn_settings(product_knowledge_credentials=SecretStr(blank))

    assert settings.product_knowledge_credentials is None
    assert not settings.credential_map()


def test_admin_defaults_have_no_map_and_the_kill_switch_on() -> None:
    settings = _unreachable_dsn_settings()

    assert settings.product_knowledge_enabled is True
    assert settings.product_knowledge_call_timeout_seconds == 5
    assert settings.product_knowledge_allow_private_networks is True
    assert settings.endpoint_allowlist() == ()
    assert not settings.credential_map()


def test_admin_settings_construct_with_a_malformed_map_and_never_echo_the_token() -> None:
    """Fail open: settings construction does not validate the map (the Worker
    does); parsing is on demand and its error names no value."""
    from adapter.product_knowledge.credentials import CredentialMapError

    settings = _unreachable_dsn_settings(
        product_knowledge_credentials=SecretStr(_pk_json(allowed_hosts=[]))
    )

    with pytest.raises(CredentialMapError) as excinfo:
        settings.credential_map()

    assert "no allowed_hosts" in str(excinfo.value)
    assert _PK_CANARY not in str(excinfo.value)
    assert _PK_CANARY not in repr(settings)


def test_admin_load_credential_map_fails_open_with_a_value_free_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.admin_api.main import _load_credential_map

    settings = _unreachable_dsn_settings(
        product_knowledge_credentials=SecretStr(_pk_json(allowed_hosts=["*"]))
    )

    with caplog.at_level(logging.ERROR):
        loaded = _load_credential_map(settings)

    assert loaded is None
    assert "credential map is invalid" in caplog.text
    assert _PK_CANARY not in caplog.text


def test_admin_app_still_boots_when_the_credential_map_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apps.admin_api.main as admin_main

    monkeypatch.setattr(admin_main, "_check_embedding_model_startup", lambda *a, **k: None)
    settings = _unreachable_dsn_settings(
        product_knowledge_credentials=SecretStr("this is not json " + _PK_CANARY)
    )

    client = TestClient(create_app(settings))

    assert client.get("/healthz").status_code == 200


def test_admin_load_endpoint_allowlist_fails_open_with_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.admin_api.main import _load_endpoint_allowlist

    settings = _unreachable_dsn_settings(product_knowledge_endpoint_allowlist="*")

    with caplog.at_level(logging.ERROR):
        loaded = _load_endpoint_allowlist(settings)

    assert loaded is None
    assert "endpoint allow-list is invalid" in caplog.text


def test_admin_app_still_boots_when_the_endpoint_allowlist_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apps.admin_api.main as admin_main

    monkeypatch.setattr(admin_main, "_check_embedding_model_startup", lambda *a, **k: None)
    settings = _unreachable_dsn_settings(product_knowledge_endpoint_allowlist="*")

    client = TestClient(create_app(settings))

    assert client.get("/healthz").status_code == 200
