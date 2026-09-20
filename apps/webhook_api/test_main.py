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

The US10/US14 tests below (docs/specs/spec-observability.md's Unit A) use
the same `create_app`-direct pattern: `create_app` is this app's own
composition root, so it's the one seam that lets a test prove
`otel_exporter_otlp_endpoint` genuinely gates both `init_telemetry` and
the lifespan's heartbeat task, in isolation from the real process's own
module-level settings/app construction.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr

import adapter.telemetry.heartbeat as heartbeat_module
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


# --- docs/specs/spec-observability.md's Unit A: US10 (no-op default) and
# US14 (heartbeat) ---------------------------------------------------------


def test_create_app_is_a_telemetry_noop_when_otlp_endpoint_unset() -> None:
    tracer_provider_before = trace.get_tracer_provider()
    meter_provider_before = metrics.get_meter_provider()
    settings = _settings()  # otel_exporter_otlp_endpoint defaults to None

    create_app(settings)

    # US10: no SDK/exporter constructed at all — the global providers are
    # untouched, proven by identity, not merely by absence of an error.
    assert trace.get_tracer_provider() is tracer_provider_before
    assert metrics.get_meter_provider() is meter_provider_before


def test_lifespan_does_not_start_heartbeat_when_otlp_endpoint_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_started = False

    async def _spy_run_heartbeat_loop(*, interval_seconds: float = 30.0) -> None:
        nonlocal heartbeat_started
        heartbeat_started = True

    monkeypatch.setattr(heartbeat_module, "run_heartbeat_loop", _spy_run_heartbeat_loop)
    settings = _settings()

    test_app = create_app(settings)
    with TestClient(test_app):
        pass

    assert heartbeat_started is False


@pytest.mark.usefixtures("reset_otel_global_providers")
def test_lifespan_starts_and_cleanly_cancels_heartbeat_when_otlp_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_started = False
    heartbeat_cancelled = False

    async def _spy_run_heartbeat_loop(*, interval_seconds: float = 30.0) -> None:
        nonlocal heartbeat_started, heartbeat_cancelled
        heartbeat_started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            heartbeat_cancelled = True
            raise

    monkeypatch.setattr(heartbeat_module, "run_heartbeat_loop", _spy_run_heartbeat_loop)
    settings = _settings(otel_exporter_otlp_endpoint="http://127.0.0.1:4317")

    test_app = create_app(settings)
    try:
        with TestClient(test_app):
            assert heartbeat_started is True
        assert heartbeat_cancelled is True
    finally:
        tracer_provider = trace.get_tracer_provider()
        meter_provider = metrics.get_meter_provider()
        assert isinstance(tracer_provider, TracerProvider)
        assert isinstance(meter_provider, MeterProvider)
        tracer_provider.shutdown()
        meter_provider.shutdown()


def test_create_app_succeeds_with_loadable_embedding_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """docs/plans/plan-deliver-memory.md's Phase 5 section: this entrypoint's
    own `LocalONNXEmbeddingClient` startup check, mirroring
    `apps/worker/test_main.py::test_main_succeeds_with_loadable_embedding_model`
    — no mocking, the real Dockerfile-baked model, exercised for real."""
    logging.getLogger("apps.webhook_api.main").disabled = False
    settings = _settings()  # embedding_model_path defaults to the real baked model

    with caplog.at_level("INFO", logger="apps.webhook_api.main"):
        create_app(settings)  # must not raise SystemExit

    assert any(
        record.name == "apps.webhook_api.main"
        and record.getMessage() == "local embedding model loaded"
        for record in caplog.records
    )


def test_create_app_exits_non_zero_with_unloadable_embedding_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logging.getLogger("apps.webhook_api.main").disabled = False
    settings = _settings(embedding_model_path="/nonexistent/model/path")

    with (
        caplog.at_level("ERROR", logger="apps.webhook_api.main"),
        pytest.raises(SystemExit) as excinfo,
    ):
        create_app(settings)

    assert excinfo.value.code == 1
    assert any(
        record.name == "apps.webhook_api.main"
        and record.getMessage() == "local embedding model is not usable at startup; exiting"
        and record.levelname == "ERROR"
        for record in caplog.records
    )
