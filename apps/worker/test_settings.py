"""Unit tests for WorkerSettings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.worker.settings import WorkerSettings


def test_missing_required_environment_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_WORKER_ENVIRONMENT", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_minimal_valid_env_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")

    settings = WorkerSettings(_env_file=None)

    assert settings.environment == "production"
    assert settings.log_level == "INFO"


def test_accepts_dotenv_shaped_input(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("REVIEW_WORKER_ENVIRONMENT=staging\nREVIEW_WORKER_LOG_LEVEL=DEBUG\n")

    settings = WorkerSettings(_env_file=env_file)

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"


def test_rejects_invalid_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "not-a-real-environment")

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_llm_fields_default_to_local_lm_studio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert str(settings.llm_endpoint) == "http://host.docker.internal:1234/v1"
    assert settings.llm_model == "local-model"
    assert settings.llm_timeout_seconds == 30.0
    assert settings.llm_api_key.get_secret_value() == "not-needed"


def test_llm_api_key_never_appears_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_LLM_API_KEY", "super-secret-value")

    settings = WorkerSettings(_env_file=None)

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.llm_api_key)


def test_rejects_malformed_llm_endpoint_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_LLM_ENDPOINT", "not-a-valid-url")

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_accepts_custom_llm_endpoint_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_LLM_ENDPOINT", "https://api.example.com/v1")
    monkeypatch.setenv("REVIEW_WORKER_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("REVIEW_WORKER_LLM_TIMEOUT_SECONDS", "15")

    settings = WorkerSettings(_env_file=None)

    assert str(settings.llm_endpoint) == "https://api.example.com/v1"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_timeout_seconds == 15.0
