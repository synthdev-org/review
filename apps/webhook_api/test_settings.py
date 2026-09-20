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


def test_setup_mode_enabled_with_state_secret_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WEBHOOK_API_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "a-real-webhook-secret")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_SETUP_MODE", "true")
    monkeypatch.setenv("REVIEW_WEBHOOK_API_SETUP_STATE_SECRET", "a-real-setup-secret")

    settings = WebhookAPISettings(_env_file=None)

    assert settings.setup_mode is True
    assert settings.setup_state_secret is not None
    assert settings.setup_state_secret.get_secret_value() == "a-real-setup-secret"
