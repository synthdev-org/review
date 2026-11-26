"""Integration tests for US-A5's role enforcement (require_admin_role),
against a real Postgres/pgvector via testcontainers-python. Coverage:

"a require_admin_role-gated dummy route rejecting a Viewer session (403)."

No real route in this module is gated by require_admin_role yet (the auth
routes are all either unauthenticated (`/auth/login`) or
require_session-only (`/auth/logout`, `/auth/session`) — the first real
require_admin_role-gated route is `POST /api/v1/users`). This
test proves the dependency itself works correctly in isolation, by mounting
a throwaway route onto the real app (built from the real composition
root's own storage wiring) that depends on it — exactly the kind of
test-only extension `docs/rules/testing.md`'s middle tier expects for a
cross-cutting dependency with no production call site yet.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
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


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> InMemoryMetricReader:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `apps.admin_api.dependencies`'s module-level
    `_session_rejected_counter` (created once, at import time) picks it up
    via OTel's proxy-instrument migration mechanism — mirrors
    `apps/admin_api/test_auth_routes.py`'s identical fixture."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-admin-api-test"}),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    return reader


def _session_rejected_points(
    reader: InMemoryMetricReader,
) -> list[tuple[float, dict[str, object]]]:
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return []
    points: list[tuple[float, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "review.admin.session_rejected_total":
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append((data_point.value, dict(data_point.attributes or {})))
    return points


def test_require_admin_role_rejects_a_viewer_session_with_403(
    migrated_engine: AsyncEngine,
    tmp_path: Path,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    settings = _settings(dsn=dsn)
    _seed_viewer(dsn)

    # `frontend_dist_dir` pointed at a guaranteed-nonexistent path, per
    # test_main.py's own convention — without this, `create_app`'s frontend
    # catch-all route silently shadows this module's own test-only
    # `/__test-only/admin-only` route whenever a real
    # `apps/admin_api/frontend/dist/` happens to exist on disk (e.g. after
    # a developer runs `npm run build` locally).
    app = create_app(settings, frontend_dist_dir=tmp_path / "does-not-exist")
    _mount_admin_only_dummy_route(app, settings=settings, dsn=dsn)

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD},
        )

        response = client.get("/__test-only/admin-only")

        assert response.status_code == 403

    # `apps/admin_api/dependencies.py`'s `review.admin.session_rejected_total`
    # counter — the `role_forbidden` reason, the one of its four closed-set
    # values the other module (`test_auth_routes.py`) can't exercise since it
    # has no admin-role-gated route to set up a Viewer-vs-Admin mismatch
    # against.
    assert (1.0, {"reason": "role_forbidden"}) in _session_rejected_points(in_memory_metric_reader)


def test_require_admin_role_allows_an_admin_session(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    settings = _settings(dsn=dsn)

    # See the previous test's comment on why `frontend_dist_dir` is pinned
    # to a nonexistent path here.
    app = create_app(settings, frontend_dist_dir=tmp_path / "does-not-exist")
    _mount_admin_only_dummy_route(app, settings=settings, dsn=dsn)

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        response = client.get("/__test-only/admin-only")

        assert response.status_code == 200
        assert response.json() == {"username": _BOOTSTRAP_USERNAME}


def test_viewer_session_reads_the_session_endpoint_identically_to_an_admin_session(
    migrated_engine: AsyncEngine,
) -> None:
    """US-A5's second Given/When/Then: 'Given a Viewer-role session, When it
    calls any read endpoint this PRD adds, Then it succeeds identically to
    an Admin-role session.' The one read endpoint here is
    `GET /api/v1/auth/session` (require_session-gated only, no
    require_admin_role) — exercises the real production route, not a
    throwaway one, so a future regression that adds a role check to
    require_session itself would fail this test.
    """
    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    settings = _settings(dsn=dsn)
    _seed_viewer(dsn)

    app = create_app(settings)

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD},
        )

        response = client.get("/api/v1/auth/session")

        assert response.status_code == 200
        assert response.json() == {"username": _VIEWER_USERNAME, "role": "viewer"}


# --- US-PK11 ---------------------------

_PK_TOKEN = "CANARY-ROLE-PK-TOKEN-1f3870be274f6c49"


def _pk_settings(dsn: str) -> AdminAPISettings:
    return AdminAPISettings(
        environment="development",
        database_url=SecretStr(dsn),
        admin_bootstrap_username=_BOOTSTRAP_USERNAME,
        admin_bootstrap_password=SecretStr(_BOOTSTRAP_PASSWORD),
        admin_csrf_secret=SecretStr("integration-test-csrf-secret"),
        session_cookie_secure=False,
        product_knowledge_credentials=SecretStr(
            '{"wiki": {"token": "' + _PK_TOKEN + '", "allowed_hosts": ["wiki.internal"]}}'
        ),
        _env_file=None,
    )


def test_viewer_is_403_on_provider_config_put_and_test_connection_but_can_read(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    _seed_viewer(dsn)
    app = create_app(_pk_settings(dsn), frontend_dist_dir=tmp_path / "does-not-exist")
    provider_value = {
        "enabled": True,
        "display_name": "Wiki",
        "endpoint_url": "https://wiki.internal/mcp",
        "credential_ref": "wiki",
    }

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD},
        )
        csrf = {CSRF_HEADER_NAME: str(client.cookies[CSRF_COOKIE_NAME])}

        put = client.put(
            "/api/v1/config/platform/product_knowledge_provider",
            json={"value": provider_value},
            headers=csrf,
        )
        probe = client.post(
            "/api/v1/product-knowledge/test-connection",
            json={"endpoint_url": "https://wiki.internal/mcp", "credential_ref": "wiki"},
            headers=csrf,
        )
        config = client.get("/api/v1/config")
        credentials = client.get("/api/v1/product-knowledge/credentials")

    if put.status_code != 403:
        pytest.fail("expected: put.status_code == 403")
    assert probe.status_code == 403
    assert config.status_code == 200
    assert config.json()["platform_settings"] == {}  # the 403 stored nothing
    assert credentials.status_code == 200
    # Names always; bound hosts are an Admin-only detail; never a token.
    assert credentials.json() == [{"ref": "wiki", "allowed_hosts": None}]
    assert _PK_TOKEN not in credentials.text + config.text


def test_admin_sees_bound_hosts_in_the_credentials_list(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    dsn = str(migrated_engine.url.render_as_string(hide_password=False))
    app = create_app(_pk_settings(dsn), frontend_dist_dir=tmp_path / "does-not-exist")

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )
        response = client.get("/api/v1/product-knowledge/credentials")

    assert response.status_code == 200
    assert response.json() == [{"ref": "wiki", "allowed_hosts": ["wiki.internal"]}]
