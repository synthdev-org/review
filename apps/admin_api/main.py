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
from pathlib import Path

from fastapi import FastAPI

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.embedding.startup_check import check_embedding_model_loaded
from adapter.product_knowledge.client import ClientTimeouts
from adapter.product_knowledge.credentials import CredentialMap, CredentialMapError
from adapter.product_knowledge.factory import ProviderClientFactory
from adapter.product_knowledge.transport import EgressPolicy
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.iam import (
    PostgresAuditLogRepository,
    PostgresLoginAttemptRepository,
    PostgresSessionStore,
    PostgresUserRepository,
)
from adapter.storage.llm_routing import PostgresModelRoutingRepository
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.tenancy import PostgresSettingRepository, PostgresUsageCounterRepository
from adapter.telemetry.heartbeat import heartbeat_task_scope
from adapter.telemetry.prometheus_reader import PrometheusMetricsReader
from adapter.telemetry.sdk import (
    init_telemetry,
    resolve_otlp_headers,
    shutdown_telemetry,
)
from apps.admin_api.auth_routes import create_auth_router
from apps.admin_api.bootstrap import bootstrap_admin_user
from apps.admin_api.config_routes import create_config_router
from apps.admin_api.dependencies import build_require_admin_role, build_require_session
from apps.admin_api.frontend_routes import create_frontend_router
from apps.admin_api.logging_config import configure_logging
from apps.admin_api.memory_routes import create_memory_router
from apps.admin_api.model_routing_routes import create_model_routing_router
from apps.admin_api.product_knowledge_routes import (
    ProbeFactory,
    ProductKnowledgeRouterConfig,
    create_product_knowledge_router,
    create_provider_write_validator,
)
from apps.admin_api.settings import AdminAPISettings
from apps.admin_api.stats_routes import create_stats_router
from apps.admin_api.usage_routes import create_usage_router
from apps.admin_api.user_routes import create_user_router
from indexing.embedding import EmbeddingError
from llmgateway.routing import FallbackModelRoutingResolver

logger = logging.getLogger(__name__)

# apps/admin_api/frontend/dist — built by the Dockerfile's own
# `frontend-build` Node stage and copied into the shared `runtime` stage
# at image-build time (Phase 8, Assumption 9); not present in every
# environment (see apps/admin_api/frontend_routes.py's own docstring for
# why that's expected, not fatal). A plain module-level constant, not a
# settings field — this is a build-time artifact location, never an
# operator-configurable value.
_DEFAULT_FRONTEND_DIST_DIR = Path(__file__).resolve().parent / "frontend" / "dist"


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


def _load_credential_map(settings: AdminAPISettings) -> CredentialMap | None:
    """
    Parse the product-knowledge credential map, failing *open*.

    A malformed map must not take down the config/audit UI (plan Assumption 14),
    so the problem is logged (value-free) and the product-knowledge routes
    report ``disabled`` / refuse credential references. ``None`` = unavailable.
    """
    try:
        return settings.credential_map()
    except CredentialMapError as exc:
        logger.error(
            "product-knowledge credential map is invalid; product-knowledge features are "
            "unavailable on the Admin API",
            extra={"problem": str(exc)},
        )
        return None


def _load_endpoint_allowlist(settings: AdminAPISettings) -> tuple[str, ...] | None:
    """
    Parse the operator allow-list, failing *open* like the credential map.

    A malformed value disables the product-knowledge routes (value-free log)
    instead of taking down the config/audit UI. ``None`` = unavailable.
    """
    try:
        return settings.endpoint_allowlist()
    except CredentialMapError as exc:
        logger.error(
            "product-knowledge endpoint allow-list is invalid; product-knowledge features "
            "are unavailable on the Admin API",
            extra={"problem": str(exc)},
        )
        return None


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


def create_app(
    settings: AdminAPISettings,
    *,
    frontend_dist_dir: Path = _DEFAULT_FRONTEND_DIST_DIR,
    probe_factory: ProbeFactory | None = None,
) -> FastAPI:
    """Build the Admin API's FastAPI application from parsed settings.

    ``frontend_dist_dir`` defaults to the real, Docker-image-baked build
    location but is overridable — used by
    ``apps/admin_api/test_frontend_routes.py`` to point at a fixture
    directory instead, without needing a real ``npm run build`` in the
    Python test tier.

    ``probe_factory`` is a test seam for the product-knowledge
    test-connection route: production leaves it ``None`` and gets a real
    ``ProviderClientFactory``; tests inject one whose resolver/inner
    transport redirect only the *last hop* to a loopback fake provider, so
    every guard decision is still the production code path.
    """
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

    # Org/repo/team configuration (docs/plans/plan-deliver-admin-gui.md's
    # Phase 4 section, Unit E, US-A12/US-A13) — registered unconditionally,
    # the same "no off-by-default state" posture every other route module
    # here already takes.
    setting_repository = PostgresSettingRepository(session_factory)
    credential_map = _load_credential_map(settings)
    endpoint_allowlist = _load_endpoint_allowlist(settings)
    app.include_router(
        create_config_router(
            repository=setting_repository,
            audit_log_repository=audit_log_repository,
            require_session=require_session,
            require_admin_role=require_admin_role,
            provider_write_validator=create_provider_write_validator(
                credential_map, endpoint_allowlist or ()
            ),
        )
    )
    logger.info("config routes registered")

    # Optional product-knowledge provider admin surface (plan Phase 6/6b) —
    # same adapter path as the Worker (cache bypassed), never a second probe.
    app.include_router(
        create_product_knowledge_router(
            config=ProductKnowledgeRouterConfig(
                credentials=credential_map,
                enabled=settings.product_knowledge_enabled and endpoint_allowlist is not None,
                endpoint_allowlist=endpoint_allowlist or (),
                probe_factory=probe_factory
                or ProviderClientFactory(
                    credentials=credential_map or CredentialMap.empty(),
                    policy=EgressPolicy(
                        allow_private_networks=settings.product_knowledge_allow_private_networks,
                        endpoint_allowlist=endpoint_allowlist or (),
                    ),
                    timeouts=ClientTimeouts(
                        call_seconds=float(settings.product_knowledge_call_timeout_seconds)
                    ),
                    bypass_cache=True,
                    top_k=1,
                ),
            ),
            audit_log_repository=audit_log_repository,
            require_session=require_session,
            require_admin_role=require_admin_role,
        )
    )
    logger.info("product knowledge routes registered")

    # Model routing (docs/plans/plan-deliver-admin-gui.md's Phase 5
    # section, Unit F, US-A14/US-A15/US-A16, Assumption 3) — registered
    # unconditionally, the same "no off-by-default state" posture every
    # other route module here already takes. `resolver` is the same
    # `FallbackModelRoutingResolver` *class* `apps/worker/main.py`'s own
    # composition root wires (a separate instance here, sharing the class,
    # never a second independently-derived fallback chain) — see that
    # module's own docstring for why this one implementation is shared by
    # name across both entrypoints.
    model_routing_repository = PostgresModelRoutingRepository(session_factory)
    model_routing_resolver = FallbackModelRoutingResolver(
        model_routing_repository, static_default_model=settings.llm_model
    )
    app.include_router(
        create_model_routing_router(
            resolver=model_routing_resolver,
            repository=model_routing_repository,
            audit_log_repository=audit_log_repository,
            require_session=require_session,
            require_admin_role=require_admin_role,
        )
    )
    logger.info("model routing routes registered")

    # Admin-stats dashboard (docs/plans/plan-deliver-admin-gui.md's Phase 6
    # section, Unit G, US-A17, Assumption 4) — registered unconditionally,
    # the same "no off-by-default state" posture every other route module
    # here already takes; this endpoint is itself observability tooling
    # with no kill switch, per that phase's own Observability & rollback
    # section. `metrics_querier` is wired to the `MetricsQuerier`-typed
    # dependency `apps/admin_api/stats_service.py` defines, exclusively at
    # this composition root — neither `stats_service.py` nor
    # `adapter/telemetry/prometheus_reader.py` imports the other's
    # concrete wiring, per `docs/rules/architecture.md`'s
    # constructor-injection rule.
    metrics_querier = PrometheusMetricsReader(
        base_url=settings.prometheus_base_url,
        timeout_seconds=settings.prometheus_timeout_seconds,
        retry_max_attempts=settings.prometheus_retry_max_attempts,
    )
    app.include_router(
        create_stats_router(querier=metrics_querier, require_session=require_session)
    )
    logger.info("stats routes registered")

    # Free-tier usage counter + disclosure (docs/plans/plan-deliver-admin-gui.md's
    # Phase 7 section, Unit H, US-A18, Assumption 2) — registered
    # unconditionally, the same "no off-by-default state" posture every
    # other route module here already takes; this endpoint is itself
    # disclosure tooling with no kill switch, per that phase's own
    # Observability & rollback section. `usage_counter_repository` is the
    # same `PostgresUsageCounterRepository` *class* `apps/worker/main.py`'s
    # own composition root wires (a separate instance here, sharing the
    # class, reading from the same `private_repo_review_count` table the
    # Worker writes to) — neither entrypoint imports the other's own
    # composition-root wiring.
    usage_counter_repository = PostgresUsageCounterRepository(session_factory)
    app.include_router(
        create_usage_router(reader=usage_counter_repository, require_session=require_session)
    )
    logger.info("usage routes registered")

    # Frontend (docs/plans/plan-deliver-admin-gui.md's Phase 8 section,
    # Unit I, Assumption 9) — registered last, deliberately: its catch-all
    # route must never shadow an already-registered /api/*//healthz route.
    # `create_frontend_router` returns `None` (logged, not raised) when the
    # frontend hasn't been built in this environment — see that module's
    # own docstring.
    frontend_router = create_frontend_router(frontend_dist_dir)
    if frontend_router is not None:
        app.include_router(frontend_router)
        logger.info("frontend routes registered", extra={"dist_dir": str(frontend_dist_dir)})

    return app


settings = AdminAPISettings()
configure_logging(settings.log_level)
logger.info("admin_api starting", extra={"environment": settings.environment})
app = create_app(settings)
