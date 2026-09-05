"""Tests for the webhook_api entrypoint's `create_app` composition root.

`test_healthz_returns_ok` is the Phase 0 unit test (config parsing +
routing only, via the module-level `app` object). The two tests below are
Phase 1's own: they lock in the startup log *level* distinction the plan's
Phase 1 Assumptions section depends on for the per-repo enqueue rate
limiter — `warning` at the shipped default (`review_enqueue_rate_limit_per_minute=0`,
a known cost-exhaustion vector left open by default) vs. `info` once an
operator has actively raised it above `0` — since this exact level was
already corrected once during that plan's own review and would otherwise
regress silently. Uses `create_app` directly (not the module-level `app`)
so each test controls the setting under test without mutating process
environment variables, mirroring `apps/worker/test_main.py`'s convention.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.webhook_api.main import app, create_app
from apps.webhook_api.settings import WebhookAPISettings

_UNREACHABLE_DSN = "postgresql+asyncpg://unused:unused@localhost:5432/unused"


def test_healthz_returns_ok() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _settings(**overrides: object) -> WebhookAPISettings:
    defaults: dict[str, object] = {
        "environment": "development",
        "database_url": SecretStr(_UNREACHABLE_DSN),
        "github_webhook_secret": SecretStr("test-webhook-secret"),
        "_env_file": None,
    }
    defaults.update(overrides)
    return WebhookAPISettings(**defaults)  # type: ignore[arg-type]


def test_enqueue_rate_limiter_disabled_by_default_logs_warning_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logging.getLogger("apps.webhook_api.main").disabled = False
    settings = _settings()  # review_enqueue_rate_limit_per_minute defaults to 0

    with caplog.at_level("INFO", logger="apps.webhook_api.main"):
        create_app(settings)

    matching = [
        record
        for record in caplog.records
        if record.name == "apps.webhook_api.main"
        and "review-enqueue rate limiting is disabled" in record.getMessage()
    ]
    assert len(matching) == 1
    assert matching[0].levelname == "WARNING"


def test_enqueue_rate_limiter_active_logs_info_once(caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger("apps.webhook_api.main").disabled = False
    settings = _settings(review_enqueue_rate_limit_per_minute=10)

    with caplog.at_level("INFO", logger="apps.webhook_api.main"):
        create_app(settings)

    matching = [
        record
        for record in caplog.records
        if record.name == "apps.webhook_api.main"
        and "review-enqueue rate limiting is active" in record.getMessage()
    ]
    assert len(matching) == 1
    assert matching[0].levelname == "INFO"
    # Scoped to the review-enqueue rate limiter's own warning, not "no
    # WARNING record at all" — indexing-delivery plan's Phase 1 adds its
    # own, independent `indexing_enabled` kill-switch warning (this
    # settings object's own shipped default leaves it off), which is
    # correct, expected behavior unrelated to the review-enqueue limiter
    # this test is about.
    assert not [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "review-enqueue rate limiting" in r.getMessage()
    ]
