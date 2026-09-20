"""Unit tests for WebhookAPISettings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.webhook_api.settings import WebhookAPISettings


def test_missing_required_environment_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_WEBHOOK_API_ENVIRONMENT", raising=False)

    with pytest.raises(ValidationError):
        WebhookAPISettings(_env_file=None)


def test_missing_required_github_webhook_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "production")
    monkeypatch.delenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", raising=False)

    with pytest.raises(ValidationError):
        WebhookAPISettings(_env_file=None)


def test_missing_required_admin_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/plans/plan-deliver-memory.md's Phase 5 section (Assumption 7) —
    `admin_api_key` is required with no default, the same "no safe default,
    dangerous to silently default" posture as `github_webhook_secret`."""
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.delenv("REVIEW_WEBHOOK_API_ADMIN_API_KEY", raising=False)

    with pytest.raises(ValidationError):
        WebhookAPISettings(_env_file=None)


def test_minimal_valid_env_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")

    settings = WebhookAPISettings(_env_file=None)

    assert settings.environment == "production"
    assert settings.log_level == "INFO"
    assert settings.github_webhook_secret.get_secret_value() == "a-real-webhook-secret"
    assert settings.webhook_max_body_bytes == 1_000_000
    assert settings.webhook_rate_limit_max_requests == 30
    assert settings.setup_mode is False
    assert settings.setup_state_secret is None


def test_accepts_dotenv_shaped_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest.py sets these as session-wide defaults so importing
    # apps.webhook_api.main doesn't fail; a real env var would otherwise
    # take priority over the .env file below and mask what this test checks.
    monkeypatch.delenv("REVIEW_WEBHOOK_API_ENVIRONMENT", raising=False)
    monkeypatch.delenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "REVIEW_WEBHOOK_API_ENVIRONMENT=staging\n"
        "REVIEW_WEBHOOK_API_LOG_LEVEL=DEBUG\n"
        "REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET=dotenv-webhook-secret\n"
    )

    settings = WebhookAPISettings(_env_file=env_file)

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.github_webhook_secret.get_secret_value() == "dotenv-webhook-secret"


def test_rejects_invalid_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "not-a-real-environment")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")

    with pytest.raises(ValidationError):
        WebhookAPISettings(_env_file=None)


def test_setup_mode_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.delenv("REVIEW_WEBHOOK_API_SETUP_MODE", raising=False)

    settings = WebhookAPISettings(_env_file=None)

    assert settings.setup_mode is False


def test_setup_mode_enabled_without_state_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_SETUP_MODE", "true")
    monkeypatch.delenv("REVIEW_WEBHOOK_API_SETUP_STATE_SECRET", raising=False)

    with pytest.raises(ValidationError):
        WebhookAPISettings(_env_file=None)


def test_empty_otel_endpoint_env_value_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/specs/spec-observability.md's Unit F: `docker-compose.yml`'s
    `observability` profile passes `OTEL_EXPORTER_OTLP_ENDPOINT` through
    with no `${VAR:-}` default (a bare `KEY:` entry) specifically so an
    unset host/.env value leaves the container's own env var absent — but
    `.env.example` (and any `.env` copied from it, per this repo's own
    documented workflow) ships this var *present with an empty value*
    (`OTEL_EXPORTER_OTLP_ENDPOINT=`), which still reaches this field as
    `""`. Without the field validator this test proves, that `""` would
    satisfy `str | None` as a real value and silently break US10's
    no-op-by-default contract the moment anyone runs an unmodified
    `.env.example`.
    """
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "")

    settings = WebhookAPISettings(_env_file=None)

    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.otel_exporter_otlp_headers is None


def test_non_empty_otel_endpoint_env_value_still_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    settings = WebhookAPISettings(_env_file=None)

    assert settings.otel_exporter_otlp_endpoint == "http://otel-collector:4317"


def test_setup_mode_enabled_with_state_secret_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_SETUP_MODE", "true")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_SETUP_STATE_SECRET", "a-real-setup-secret")

    settings = WebhookAPISettings(_env_file=None)

    assert settings.setup_mode is True
    assert settings.setup_state_secret is not None
    assert settings.setup_state_secret.get_secret_value() == "a-real-setup-secret"
