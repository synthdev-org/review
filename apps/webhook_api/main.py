"""Webhook API entrypoint — composition root.

Parses config once (pydantic-settings) and configures logging here. Per
docs/rules/architecture.md's Decoupling section, concrete implementations
are wired in only here (never as module-level singletons elsewhere): the
in-process rate limiter and the two route factories below are constructed
in ``create_app`` and passed the settings-derived values they need.

Phase 4 adds this entrypoint's first real routes: the GitHub webhook
receiver (always registered) and the App manifest/callback setup flow
(registered only when ``settings.setup_mode`` is true — an unregistered
route 404s, enforcing the plan's Phase 4 "gated behind an explicit
SETUP_MODE config flag, off by default" requirement at the routing level
rather than via a runtime check inside an always-registered route).

``create_app`` is a plain factory (not just a module-level ``app`` object)
so tests can build an app from arbitrary settings — e.g. a low rate limit
or a small body-size cap — without mutating process environment variables;
the module-level ``app`` below is what uvicorn actually serves
(``apps.webhook_api.main:app``).

Phase 1 wires this entrypoint's first storage adapters: a
``PostgresTenancyStore`` and a ``JobQueueRepository`` (both
``adapter/storage``), built from one ``AsyncEngine``/session factory
constructed here — the composition root, per architecture.md's "wire
concrete implementations only at the composition root" rule — and passed
into the webhook router alongside the (optionally ``None``) per-repo
enqueue rate limiter.

``plan-indexing-delivery.md``'s Phase 1 adds this entrypoint's second
enqueue rate limiter (``index_enqueue_rate_limiter``, `INDEX`-specific,
also optionally ``None`` per the same ``0``-disables-it sentinel) and the
``indexing_enabled`` kill switch, both passed down explicitly into the
webhook router the same way every other Phase 1 setting already is.

``docs/plans/plan-deliver-memory.md``'s Phase 3 (Unit C) adds this
entrypoint's first ``findings.redact.TextRedactor`` and
``adapter/storage/memory_repository.py``'s ``PostgresMemoryRepository``
(reused from the same ``session_factory`` every other storage adapter here
already shares) — both wired into the webhook router's own memory-capture
handling, alongside a per-repo daily capture-ceiling ``InMemoryRateLimiter``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from adapter.analyzers.gitleaks import GitleaksTextRedactor
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.queue import JobQueueRepository
from adapter.storage.tenancy import PostgresTenancyStore
from apps.webhook_api.logging_config import configure_logging
from apps.webhook_api.manifest_routes import create_manifest_router
from apps.webhook_api.rate_limiter import InMemoryRateLimiter
from apps.webhook_api.settings import WebhookAPISettings
from apps.webhook_api.webhook_routes import create_webhook_router
from findings.redact import NoOpTextRedactor, TextRedactor

logger = logging.getLogger(__name__)

# One rolling day, in seconds — the window
# `memory_capture_ceiling_per_repo_per_day` (Phase 3, Unit C) is measured
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
    cost-exhaustion vector open — see the plan's Phase 1 "Assumptions"
    section) or ``info`` when an operator has actively raised it above
    ``0``, so an operator or PRD owner can confirm the deployment's actual
    state without reading config files either way.
    """
    if settings.review_enqueue_rate_limit_per_minute == 0:
        logger.warning(
            "per-repo review-enqueue rate limiting is disabled "
            "(review_enqueue_rate_limit_per_minute=0); this is Phase 1's own "
            "shipped default, matching phase-1-review-pipeline.md's current "
            "NFR text, but leaves a known cost-exhaustion vector open — see "
            "the plan's Phase 1 Assumptions section"
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
    above, for the `INDEX`-specific bucket (indexing-delivery plan's Phase
    1, Assumption 10) — a single instance shared across both `push`- and
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


def _build_memory_capture_ceiling_limiter(settings: WebhookAPISettings) -> InMemoryRateLimiter:
    """Per-repo daily cap on Memory capture writes (Phase 3, Unit C) — see
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
    app = FastAPI(title="review webhook_api")

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
    # Memory capture (docs/plans/plan-deliver-memory.md's Phase 3 section,
    # Unit C): the same `PostgresMemoryRepository`/`session_factory` shape
    # `apps/worker/main.py` already builds for Phase 2's provenance
    # recording (Assumption 8 — one concrete class implements every Memory
    # Protocol, constructed once per entrypoint from that entrypoint's own
    # session factory).
    memory_repository = PostgresMemoryRepository(session_factory)
    text_redactor = _build_text_redactor(settings, logger)
    memory_capture_ceiling_limiter = _build_memory_capture_ceiling_limiter(settings)

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
