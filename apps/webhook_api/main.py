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
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from apps.webhook_api.logging_config import configure_logging
from apps.webhook_api.manifest_routes import create_manifest_router
from apps.webhook_api.rate_limiter import InMemoryRateLimiter
from apps.webhook_api.settings import WebhookAPISettings
from apps.webhook_api.webhook_routes import create_webhook_router

logger = logging.getLogger(__name__)


def create_app(settings: WebhookAPISettings) -> FastAPI:
    """Build the Webhook API's FastAPI application from parsed settings."""
    app = FastAPI(title="review webhook_api")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness/readiness probe target."""
        return {"status": "ok"}

    rate_limiter = InMemoryRateLimiter(
        max_requests=settings.webhook_rate_limit_max_requests,
        window_seconds=settings.webhook_rate_limit_window_seconds,
    )
    app.include_router(
        create_webhook_router(
            webhook_secret=settings.github_webhook_secret,
            rate_limiter=rate_limiter,
            max_body_bytes=settings.webhook_max_body_bytes,
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
