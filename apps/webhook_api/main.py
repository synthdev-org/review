"""Webhook API entrypoint — composition root.

Parses config once (pydantic-settings) and configures logging here. Per
docs/rules/architecture.md's Decoupling section, concrete implementations
are wired in only here (never as module-level singletons elsewhere): the
in-process rate limiter and the two route factories below are constructed
in ``create_app`` and passed the settings-derived values they need.

This entrypoint's routes: the GitHub webhook
receiver (always registered) and the App manifest/callback setup flow
(registered only when ``settings.setup_mode`` is true — an unregistered
route 404s, enforcing the "gated behind an explicit SETUP_MODE config flag, off by
default" requirement at the routing level
rather than via a runtime check inside an always-registered route).

``create_app`` is a plain factory (not just a module-level ``app`` object)
so tests can build an app from arbitrary settings — e.g. a low rate limit
or a small body-size cap — without mutating process environment variables;
the module-level ``app`` below is what uvicorn actually serves
(``apps.webhook_api.main:app``).

This entrypoint's storage adapters: a
``PostgresTenancyStore`` and a ``JobQueueRepository`` (both
``adapter/storage``), built from one ``AsyncEngine``/session factory
constructed here — the composition root, per architecture.md's "wire
concrete implementations only at the composition root" rule — and passed
into the webhook router alongside the (optionally ``None``) per-repo
enqueue rate limiter.

A second enqueue rate limiter (``index_enqueue_rate_limiter``, `INDEX`-specific,
also optionally ``None`` per the same ``0``-disables-it sentinel) and the
``indexing_enabled`` kill switch, both passed down explicitly into the
webhook router the same way every other setting is.

``docs/specs/spec-observability.md`` adds this
entrypoint's OTel wiring: ``adapter.telemetry.sdk.init_telemetry`` is called once, inside
``create_app`` (this app's own composition-root factory — keeping
``create_app`` the one seam a test controls, per this module's own
"testable the way it is today" contract, rather than only at bare module
scope where a test couldn't reach it via arbitrary settings), and a
``lifespan`` async context manager (FastAPI's modern replacement for the
deprecated ``@app.on_event``) starts US14's heartbeat loop as a background
task on startup and cancels it cleanly on shutdown — but only when
``settings.otel_exporter_otlp_endpoint`` is set (US10's no-op default
means no heartbeat either: nothing was initialized for it to report
against).

Memory capture adds this entrypoint's ``findings.redact.TextRedactor`` and
``adapter/storage/memory_repository.py``'s ``PostgresMemoryRepository``
(reused from the same ``session_factory`` every other storage adapter here
already shares) — both wired into the webhook router's own memory-capture
handling, alongside a per-repo daily capture-ceiling ``InMemoryRateLimiter``.

US-M3 adds this
entrypoint's first outbound GitHub REST API caller other than the manifest
flow: a ``GitHubInstallationTokenProvider``/``GitHubReactionFetcher`` pair
(``_build_reaction_fetcher``), needed because US-M3's second
``Given/When/Then`` branch requires a PR-closed event's reaction fetch to
run synchronously, before the webhook handler returns — not queued for the
Worker to pick up later, unlike every other job type this entrypoint
enqueues.

The Memory admin API formerly lived here
(``apps/webhook_api/admin_routes.py``), registered unconditionally with its
own static-token auth. US-A7 fully retired that
route from this entrypoint — moved wholesale to
``apps/admin_api/memory_routes.py``, now authenticated via real per-user
sessions instead of the shared ``admin_api_key`` static Bearer token. This
is a one-way cutover, not a dual-auth transition (no live customer had used
this API yet): ``admin_routes.py``, its registration below, the
``admin_api_key`` setting, and this entrypoint's own
``LocalONNXEmbeddingClient``/startup-check wiring (which existed solely to
serve the approve handler that moved) are all deleted, not merely
unregistered.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from adapter.analyzers.gitleaks import GitleaksTextRedactor
from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.reactions import GitHubReactionFetcher
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.queue import JobQueueRepository
from adapter.storage.tenancy import PostgresTenancyStore
from adapter.telemetry.heartbeat import heartbeat_task_scope
from adapter.telemetry.sdk import (
    init_telemetry,
    resolve_otlp_headers,
    shutdown_telemetry,
)
from apps.webhook_api.logging_config import configure_logging
from apps.webhook_api.manifest_routes import create_manifest_router
from apps.webhook_api.rate_limiter import InMemoryRateLimiter
from apps.webhook_api.settings import WebhookAPISettings
from apps.webhook_api.webhook_routes import create_webhook_router
from findings.redact import NoOpTextRedactor, TextRedactor

logger = logging.getLogger(__name__)

# One rolling day, in seconds — the window
# `memory_capture_ceiling_per_repo_per_day` is measured
# over. A plain constant, not a settings field itself: the *ceiling count*
# is operator-tunable; the window it applies over is this feature's own
# fixed shape, the same way `_build_enqueue_rate_limiter`'s `window_seconds=60.0`
# below is hardcoded rather than separately configurable.
_MEMORY_CAPTURE_CEILING_WINDOW_SECONDS = 86_400.0


def _build_enqueue_rate_limiter(settings: WebhookAPISettings) -> InMemoryRateLimiter | None:
    """``review_enqueue_rate_limit_per_minute=0`` (this phase's shipped
    default) is a documented sentinel meaning "skip the rate-check call
    entirely" — handled here at the call site, not by loosening
    ``InMemoryRateLimiter``'s own ``max_requests >= 1`` guard, which stays
    exactly as strict for its existing per-IP use. Logs at ``warning`` when
    inactive (this phase's own shipped default leaves a known
    cost-exhaustion vector open) or ``info`` when an operator has actively raised it above
    ``0``, so an operator or PRD owner can confirm the deployment's actual
    state without reading config files either way.
    """
    if settings.review_enqueue_rate_limit_per_minute == 0:
        logger.warning(
            "per-repo review-enqueue rate limiting is disabled "
            "(review_enqueue_rate_limit_per_minute=0); this is the "
            "shipped default but leaves a known cost-exhaustion vector open"
        )
        return None

    logger.info(
        "per-repo review-enqueue rate limiting is active",
        extra={
            "review_enqueue_rate_limit_per_minute": settings.review_enqueue_rate_limit_per_minute
        },
    )
    return InMemoryRateLimiter(
        max_requests=settings.review_enqueue_rate_limit_per_minute,
        window_seconds=60.0,
    )


def _build_index_enqueue_rate_limiter(settings: WebhookAPISettings) -> InMemoryRateLimiter | None:
    """Same ``0``-disables-it sentinel as ``_build_enqueue_rate_limiter``
    above, for the `INDEX`-specific bucket — a single instance shared across both `push`- and
    `pull_request`-triggered `INDEX` enqueues, keyed by repo id at the call
    site (``apps/webhook_api/webhook_routes.py``), not per-instance here.
    Unlike ``review_enqueue_rate_limit_per_minute``, this setting's shipped
    default is not ``0`` (see ``apps/webhook_api/settings.py`` for why), so
    reaching the ``0`` branch below means an operator has actively disabled
    it, not that they're merely running this phase's shipped default.
    """
    if settings.index_enqueue_rate_limit_per_minute == 0:
        logger.warning(
            "per-repo index-enqueue rate limiting is disabled "
            "(index_enqueue_rate_limit_per_minute=0); an operator has "
            "actively overridden this setting's non-zero shipped default"
        )
        return None

    logger.info(
        "per-repo index-enqueue rate limiting is active",
        extra={"index_enqueue_rate_limit_per_minute": settings.index_enqueue_rate_limit_per_minute},
    )
    return InMemoryRateLimiter(
        max_requests=settings.index_enqueue_rate_limit_per_minute,
        window_seconds=60.0,
    )


def _build_lifespan(
    *, telemetry_enabled: bool
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Builds this app's ``lifespan`` async context manager, gated on
    whether ``init_telemetry`` actually constructed an SDK/exporter for
    this app (``telemetry_enabled``, computed once by ``create_app``
    below from ``settings.otel_exporter_otlp_endpoint``). Only starts
    US14's heartbeat loop when telemetry is genuinely configured — the
    no-op default (US10) runs zero background tasks, matching today's
    exact behavior.

    A plain factory function (not a nested closure inline in
    ``create_app``) purely to keep its own return type — an async context
    manager factory, not a simple value — out of ``create_app``'s body.
    """

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            async with heartbeat_task_scope(telemetry_enabled):
                yield
        finally:
            if telemetry_enabled:
                await shutdown_telemetry()

    return _lifespan


def _build_text_redactor(settings: WebhookAPISettings, logger: logging.Logger) -> TextRedactor:
    """Wires `findings.redact.TextRedactor` conditionally on
    `secret_redaction_enabled` alone — this entrypoint's first `TextRedactor`
    consumer (`apps/worker/main.py`'s own `_build_text_redactor` was
    previously the only one), reusing the same `GitleaksTextRedactor`/
    `NoOpTextRedactor` wiring choice rather than a second implementation.
    `True` (the shipped default) wires the real, Gitleaks-backed
    implementation; `False` wires a no-op passthrough and logs a one-time
    `warning` at startup, not per-request — the reduced protection is
    visible, not silent.
    """
    if settings.secret_redaction_enabled:
        return GitleaksTextRedactor()

    logger.warning(
        "secret redaction is disabled",
        extra={"secret_redaction_enabled": False},
    )
    return NoOpTextRedactor()


def _build_reaction_fetcher(settings: WebhookAPISettings) -> GitHubReactionFetcher:
    """Wire the Memory domain's `ReactionFetcher` Protocol to its
    GitHub-backed implementation (US-M3) — this entrypoint's own instance,
    distinct from `apps/worker/main.py`'s, since US-M3's second
    `Given/When/Then` branch (a `pull_request closed` event) requires the
    reaction fetch to run synchronously inside this process, before the
    webhook handler returns, not queued for the Worker to pick up later.

    `client=None` (unlike the Worker's own shared, long-lived
    `httpx.AsyncClient`): this route calls GitHub's API rarely (once per
    `closed` event, not per job in a tight dispatch loop), so
    `adapter/github/http.py`'s `client_context` opening/tearing down a
    throwaway client per call is an acceptable trade-off here, and avoids
    this entrypoint needing its own FastAPI lifespan-managed client
    lifecycle for what would otherwise be its only consumer.
    """
    token_provider = GitHubInstallationTokenProvider(
        app_id=settings.github_app_id,
        private_key_pem=settings.github_app_private_key.get_secret_value(),
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
    )
    return GitHubReactionFetcher(
        token_provider=token_provider,
        github_api_base_url=settings.github_api_base_url,
        timeout=settings.github_api_timeout_seconds,
        max_retry_attempts=settings.github_retry_max_attempts,
    )


def _build_memory_capture_ceiling_limiter(settings: WebhookAPISettings) -> InMemoryRateLimiter:
    """Per-repo daily cap on Memory capture writes — see
    `apps/webhook_api/settings.py`'s `memory_capture_ceiling_per_repo_per_day`
    for why this has no `0`-disables-it sentinel unlike the enqueue rate
    limiters above.
    """
    return InMemoryRateLimiter(
        max_requests=settings.memory_capture_ceiling_per_repo_per_day,
        window_seconds=_MEMORY_CAPTURE_CEILING_WINDOW_SECONDS,
    )


def create_app(settings: WebhookAPISettings) -> FastAPI:
    """Build the Webhook API's FastAPI application from parsed settings."""
    telemetry_enabled = init_telemetry(
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        service_name="review-webhook-api",
        otlp_headers=resolve_otlp_headers(settings.otel_exporter_otlp_headers),
    )

    app = FastAPI(
        title="review webhook_api", lifespan=_build_lifespan(telemetry_enabled=telemetry_enabled)
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness/readiness probe target."""
        return {"status": "ok"}

    engine = create_storage_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)
    tenancy_store = PostgresTenancyStore(session_factory)
    job_queue = JobQueueRepository(session_factory)
    enqueue_rate_limiter = _build_enqueue_rate_limiter(settings)
    index_enqueue_rate_limiter = _build_index_enqueue_rate_limiter(settings)
    # Memory capture: the same `PostgresMemoryRepository`/`session_factory` shape
    # `apps/worker/main.py` already builds for provenance
    # recording (one concrete class implements every Memory
    # Protocol, constructed once per entrypoint from that entrypoint's own
    # session factory).
    memory_repository = PostgresMemoryRepository(session_factory)
    text_redactor = _build_text_redactor(settings, logger)
    memory_capture_ceiling_limiter = _build_memory_capture_ceiling_limiter(settings)
    # US-M3's second `Given/When/Then` branch: a
    # `pull_request closed` event's reaction fetch runs synchronously in
    # this route, so this entrypoint needs its own `ReactionFetcher` —
    # `memory_repository` above already implements `ReactionDedupStore`
    # (the same instance also serving `MemorySignalRecorder`
    # for the capture handling above).
    reaction_fetcher = _build_reaction_fetcher(settings)

    if not settings.indexing_enabled:
        logger.warning(
            "indexing_enabled is False (this phase's own shipped default); "
            "no INDEX job will ever be enqueued regardless of trigger match "
            "until an operator sets REVIEW_WEBHOOK_API_INDEXING_ENABLED=true"
        )

    rate_limiter = InMemoryRateLimiter(
        max_requests=settings.webhook_rate_limit_max_requests,
        window_seconds=settings.webhook_rate_limit_window_seconds,
    )
    app.include_router(
        create_webhook_router(
            webhook_secret=settings.github_webhook_secret,
            rate_limiter=rate_limiter,
            max_body_bytes=settings.webhook_max_body_bytes,
            tenancy_store=tenancy_store,
            job_queue=job_queue,
            session_factory=session_factory,
            enqueue_rate_limiter=enqueue_rate_limiter,
            indexing_enabled=settings.indexing_enabled,
            index_enqueue_rate_limiter=index_enqueue_rate_limiter,
            provenance_recorder=memory_repository,
            text_redactor=text_redactor,
            memory_capture_ceiling_limiter=memory_capture_ceiling_limiter,
            reaction_fetcher=reaction_fetcher,
            reaction_dedup_store=memory_repository,
            memory_reaction_poll_max_comments_per_poll=(
                settings.memory_reaction_poll_max_comments_per_poll
            ),
        )
    )

    if settings.setup_mode:
        # Guaranteed non-None here by WebhookAPISettings's model validator
        # (setup_mode=True requires setup_state_secret to be set).
        assert settings.setup_state_secret is not None
        app.include_router(
            create_manifest_router(
                setup_state_secret=settings.setup_state_secret,
                state_ttl_seconds=settings.setup_state_ttl_seconds,
                github_api_base_url=settings.github_api_base_url,
                github_api_timeout_seconds=settings.github_api_timeout_seconds,
            )
        )
        logger.info("setup_mode enabled; manifest/callback routes registered")
    else:
        logger.info("setup_mode disabled; manifest/callback routes not registered")

    return app


settings = WebhookAPISettings()
configure_logging(settings.log_level)
logger.info("webhook_api starting", extra={"environment": settings.environment})
app = create_app(settings)
