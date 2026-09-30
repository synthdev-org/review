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

``docs/plans/plan-deliver-admin-gui.md``'s Phase 3 (Unit D, US-A7) adds
this entrypoint's own ``adapter/storage/memory_repository.py``'s
``PostgresMemoryRepository`` (reused from the same ``session_factory``
every other storage adapter here already shares) and
``adapter.embedding.local_onnx.LocalONNXEmbeddingClient`` — moved here
wholesale from ``apps/webhook_api/main.py``, along with the same fail-fast
startup check that module previously ran
(``adapter.embedding.startup_check.check_embedding_model_loaded``, reused,
not a second copy), since the Memory admin API's approve route
(``apps/admin_api/memory_routes.py``) is this module's first embedding
consumer now that the static-token route it replaces has been fully
retired from ``apps/webhook_api``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial

from fastapi import FastAPI

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.embedding.startup_check import check_embedding_model_loaded
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.iam import (
    PostgresAuditLogRepository,
    PostgresLoginAttemptRepository,
    PostgresSessionStore,
    PostgresUserRepository,
)
from adapter.storage.memory_repository import PostgresMemoryRepository
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
from apps.admin_api.memory_routes import create_memory_router
from apps.admin_api.settings import AdminAPISettings
from apps.admin_api.user_routes import create_user_router
from indexing.embedding import EmbeddingError

logger = logging.getLogger(__name__)


def _build_embedding_client(settings: AdminAPISettings) -> LocalONNXEmbeddingClient:
    """Wire the Memory admin API's own `indexing.embedding.EmbeddingClient`
    Protocol to its local ONNX-backed implementation, per
    `docs/plans/plan-deliver-admin-gui.md`'s Phase 3 section (Unit D) —
    moved here from `apps/webhook_api/main.py`'s own (now-deleted)
    `_build_embedding_client`, along with the approve route it serves.
    """
    return LocalONNXEmbeddingClient(model_path=settings.embedding_model_path)


def _check_embedding_model_startup(
    client: LocalONNXEmbeddingClient, settings: AdminAPISettings, logger: logging.Logger
) -> None:
    """Run the local embedding model's startup load/inference check and
    raise `SystemExit(1)` if it fails — the same fail-fast pattern
    `apps/worker/main.py`'s own `_check_embedding_model_startup` already
    establishes (and `apps/webhook_api/main.py`'s own now-deleted copy
    previously used), reused via `adapter.embedding.startup_check
    .check_embedding_model_loaded` rather than a second copy of this
    check's own logic. A missing/corrupted baked model artifact fails
    loudly here, at Admin API boot, rather than silently on the first real
    admin `approve` call's own `embed` stage.
    """
    try:
        check_embedding_model_loaded(client)
    except EmbeddingError as exc:
        logger.error(
            "local embedding model is not usable at startup; exiting",
            extra={"embedding_model_path": settings.embedding_model_path, "error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info(
        "local embedding model loaded",
        extra={"embedding_model_path": settings.embedding_model_path},
    )


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

    # Memory admin API (docs/plans/plan-deliver-admin-gui.md's Phase 3
    # section, Unit D, US-A7) — registered unconditionally, the same
    # "no off-by-default state" posture `apps/webhook_api/main.py`'s own
    # now-deleted admin router previously had. `embedding_client`'s startup
    # check runs synchronously, once, before `create_app` returns — the
    # same "runs exactly once, before this app serves any request" fail-
    # fast timing `apps/worker/main.py`'s own equivalent check uses.
    memory_repository = PostgresMemoryRepository(session_factory)
    embedding_client = _build_embedding_client(settings)
    _check_embedding_model_startup(embedding_client, settings, logger)
    app.include_router(
        create_memory_router(
            repository=memory_repository,
            posted_comment_reader=memory_repository,
            embedding_client=embedding_client,
            audit_log_repository=audit_log_repository,
            require_session=require_session,
            require_admin_role=require_admin_role,
        )
    )
    logger.info("memory routes registered")

    return app


settings = AdminAPISettings()
configure_logging(settings.log_level)
logger.info("admin_api starting", extra={"environment": settings.environment})
app = create_app(settings)
