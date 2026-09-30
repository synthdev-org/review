"""Admin API entrypoint — composition root, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 1 section (Unit B).

Mirrors ``apps/webhook_api/main.py``'s exact shape: ``create_app(settings)``
is a plain factory (not just a module-level ``app`` object) so tests can
build an app from arbitrary settings without mutating process environment
variables; the module-level ``app`` below is what uvicorn actually serves
(``apps.admin_api.main:app``).

Phase 1 wires this entrypoint's first storage adapters (``iam``'s
``PostgresUserRepository``/``PostgresSessionStore``, plus the two
concrete, no-Protocol repositories — ``PostgresAuditLogRepository``,
``PostgresLoginAttemptRepository``) and its first real routes: local
login/session auth (``apps/admin_api/auth_routes.py``), registered
unconditionally — there is no "off by default" state for local auth the
way there is for ``webhook_api``'s ``SETUP_MODE``-gated manifest flow.

The idempotent bootstrap-admin routine (``apps/admin_api/bootstrap.py``)
runs once, inside this app's own ``lifespan`` (ASGI startup), not at
module-import time — it needs a real event loop for its async Postgres
call, which module-level ``create_app(settings)`` (called synchronously at
import time, mirroring every other entrypoint here) does not have.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial

from fastapi import FastAPI

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.iam import (
    PostgresAuditLogRepository,
    PostgresLoginAttemptRepository,
    PostgresSessionStore,
    PostgresUserRepository,
)
from adapter.telemetry.heartbeat import heartbeat_task_scope
from adapter.telemetry.sdk import (
    init_telemetry,
    resolve_otlp_headers,
    shutdown_telemetry,
)
from apps.admin_api.auth_routes import create_auth_router
from apps.admin_api.bootstrap import bootstrap_admin_user
from apps.admin_api.dependencies import build_require_admin_role, build_require_session
from apps.admin_api.logging_config import configure_logging
from apps.admin_api.settings import AdminAPISettings
from apps.admin_api.user_routes import create_user_router

logger = logging.getLogger(__name__)


def _build_lifespan(
    *,
    telemetry_enabled: bool,
    run_bootstrap: Callable[[], Awaitable[None]],
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Builds this app's ``lifespan`` async context manager — the same
    telemetry-heartbeat shape ``apps/webhook_api/main.py``'s own
    ``_build_lifespan`` establishes, plus this entrypoint's own bootstrap
    call, run once, before the heartbeat/serving loop starts.
    """

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        await run_bootstrap()
        try:
            async with heartbeat_task_scope(telemetry_enabled):
                yield
        finally:
            if telemetry_enabled:
                await shutdown_telemetry()

    return _lifespan


def create_app(settings: AdminAPISettings) -> FastAPI:
    """Build the Admin API's FastAPI application from parsed settings."""
    telemetry_enabled = init_telemetry(
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        service_name="review-admin-api",
        otlp_headers=resolve_otlp_headers(settings.otel_exporter_otlp_headers),
    )

    engine = create_storage_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    user_repository = PostgresUserRepository(session_factory)
    session_store = PostgresSessionStore(session_factory)
    audit_log_repository = PostgresAuditLogRepository(session_factory)
    login_attempt_repository = PostgresLoginAttemptRepository(
        session_factory,
        max_attempts=settings.login_rate_limit_attempts,
        window_minutes=settings.login_rate_limit_window_minutes,
    )
    password_hasher = Argon2PasswordHasher()

    run_bootstrap = partial(
        bootstrap_admin_user,
        session_factory,
        username=settings.admin_bootstrap_username,
        password=settings.admin_bootstrap_password.get_secret_value(),
        password_hasher=password_hasher,
        min_password_length=settings.min_password_length,
    )

    app = FastAPI(
        title="review admin_api",
        lifespan=_build_lifespan(telemetry_enabled=telemetry_enabled, run_bootstrap=run_bootstrap),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness/readiness probe target — same convention
        ``apps/webhook_api/main.py``'s own ``/healthz`` establishes."""
        return {"status": "ok"}

    require_session = build_require_session(
        session_store=session_store,
        user_repository=user_repository,
        csrf_secret=settings.admin_csrf_secret.get_secret_value().encode("utf-8"),
        session_idle_timeout_minutes=settings.session_idle_timeout_minutes,
    )

    app.include_router(
        create_auth_router(
            user_repository=user_repository,
            session_store=session_store,
            audit_log_repository=audit_log_repository,
            login_attempt_repository=login_attempt_repository,
            password_hasher=password_hasher,
            csrf_secret=settings.admin_csrf_secret.get_secret_value().encode("utf-8"),
            session_cookie_secure=settings.session_cookie_secure,
            require_session=require_session,
        )
    )
    logger.info("auth routes registered")

    require_admin_role = build_require_admin_role(require_session)
    app.include_router(
        create_user_router(
            user_repository=user_repository,
            session_store=session_store,
            audit_log_repository=audit_log_repository,
            password_hasher=password_hasher,
            min_password_length=settings.min_password_length,
            require_admin_role=require_admin_role,
        )
    )
    logger.info("user routes registered")

    return app


settings = AdminAPISettings()
configure_logging(settings.log_level)
logger.info("admin_api starting", extra={"environment": settings.environment})
app = create_app(settings)
