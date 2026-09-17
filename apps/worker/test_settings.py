"""Unit tests for WorkerSettings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from adapter.github.auth import validate_private_key
from adapter.github.exceptions import GitHubPrivateKeyInvalidError
from apps.worker.conftest import TEST_RSA_PRIVATE_KEY_PEM
from apps.worker.settings import WorkerSettings
from review.config import (
    NoViableStepConfigurationError,
    StepEnablement,
    check_step_configuration_viable,
)


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


def test_step_enablement_and_timeout_fields_default_per_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert settings.prefilter_enabled is True
    assert settings.static_analysis_enabled is True
    assert settings.review_generation_enabled is True
    assert settings.output_enabled is True
    assert settings.prefilter_step_timeout_seconds == 30
    assert settings.static_analysis_step_timeout_seconds == 240
    assert settings.static_analysis_analyzer_timeout_seconds == 60
    assert settings.review_generation_step_timeout_seconds == 90
    assert settings.output_step_timeout_seconds == 30


def test_individual_step_flags_are_overridable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("REVIEW_WORKER_REVIEW_GENERATION_STEP_TIMEOUT_SECONDS", "45")

    settings = WorkerSettings(_env_file=None)

    assert settings.static_analysis_enabled is False
    assert settings.review_generation_step_timeout_seconds == 45


def test_new_verification_pipeline_step_fields_default_per_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    the three new/renamed steps' own enablement/timeout fields."""
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert settings.intent_summary_enabled is True
    assert settings.verification_enabled is True
    assert settings.intent_summary_step_timeout_seconds == 30
    assert settings.verification_step_timeout_seconds == 60
    assert settings.stuck_job_warning_threshold_seconds == 600.0


def test_new_verification_pipeline_step_fields_are_overridable_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_INTENT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("REVIEW_WORKER_VERIFICATION_ENABLED", "false")
    monkeypatch.setenv("REVIEW_WORKER_INTENT_SUMMARY_STEP_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("REVIEW_WORKER_VERIFICATION_STEP_TIMEOUT_SECONDS", "45")

    settings = WorkerSettings(_env_file=None)

    assert settings.intent_summary_enabled is False
    assert settings.verification_enabled is False
    assert settings.intent_summary_step_timeout_seconds == 15
    assert settings.verification_step_timeout_seconds == 45


# --- US-F7: an all-disabled step configuration is caught at startup --------
#
# Per the plan's Phase 2 section, this predicate operates at the
# granularity Phase 2 actually has: static-analysis and review-generation are
# only two finding-producing macro steps (pre-filter and output produce no
# findings of their own). "Zero analyzers enabled" is expressed here as
# "static-analysis disabled" — Phase 5 refines this once the
# static-analysis step's own two per-analyzer sub-flags exist.


def _step_enablement_from(settings: WorkerSettings) -> StepEnablement:
    """Mirrors `apps/worker/main.py`'s own `_build_step_enablement` — kept
    as a small local helper here (not imported) so this test proves the
    predicate against a `StepEnablement` built the same way the real
    composition root builds one, without coupling this test file to
    `apps/worker/main.py`'s internals."""
    return StepEnablement(
        prefilter_enabled=settings.prefilter_enabled,
        static_analysis_enabled=settings.static_analysis_enabled,
        review_generation_enabled=settings.review_generation_enabled,
        output_enabled=settings.output_enabled,
    )


def test_all_finding_producing_steps_disabled_fails_the_viability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("REVIEW_WORKER_REVIEW_GENERATION_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(_step_enablement_from(settings))


def test_one_analyzer_enabled_and_review_generation_disabled_passes_the_viability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("REVIEW_WORKER_REVIEW_GENERATION_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    # Must not raise: static-analysis alone is enough to produce a review.
    check_step_configuration_viable(_step_enablement_from(settings))


def test_static_analysis_enabled_with_prefilter_disabled_fails_the_viability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("REVIEW_WORKER_PREFILTER_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    # static_analysis has a fixed dependency on prefilter's output — this
    # combination would fail every review job at runtime, not just be
    # sub-optimal.
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(_step_enablement_from(settings))


def test_review_generation_falling_back_to_disabled_prefilter_fails_the_viability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("REVIEW_WORKER_REVIEW_GENERATION_ENABLED", "true")
    monkeypatch.setenv("REVIEW_WORKER_PREFILTER_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    # static-analysis can't produce findings here, so apps/worker/main.py
    # wires review_generation to depend directly on prefilter's output — a
    # disabled prefilter would fail every review job at runtime.
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(_step_enablement_from(settings))


def test_review_generation_depending_on_static_analysis_tolerates_disabled_prefilter_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("REVIEW_WORKER_REVIEW_GENERATION_ENABLED", "true")
    monkeypatch.setenv("REVIEW_WORKER_PREFILTER_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    # static-analysis is enabled (and can produce findings) but declares a
    # fixed dependency on prefilter, so the static-analysis check above
    # (not this one) is the check that must catch this combination.
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(_step_enablement_from(settings))


def test_output_disabled_fails_the_viability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_OUTPUT_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    # output is the only step that ever posts a review/check-run — a
    # disabled output step would silently produce no visible result for
    # every job, with no failure signal at all.
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(_step_enablement_from(settings))


# --- Phase 4: GitHub App auth / diff-fetch bootstrap config -----------------


def test_github_app_fields_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.delenv("REVIEW_WORKER_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_github_fields_default_per_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert settings.github_api_base_url == "https://api.github.com"
    assert settings.github_api_timeout_seconds == 10.0
    assert settings.github_retry_max_attempts == 3


def test_empty_otel_endpoint_env_value_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """See WebhookAPISettings' identical test (same reasoning): the
    `observability` Compose profile's bare `OTEL_EXPORTER_OTLP_ENDPOINT:`
    pass-through still forwards a present-but-empty `.env` value (exactly
    what `.env.example` ships) as `""`, not absent — without the field
    validator this proves, that would silently break US10's
    no-op-by-default contract.
    """
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "")

    settings = WorkerSettings(_env_file=None)

    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.otel_exporter_otlp_headers is None


def test_non_empty_otel_endpoint_env_value_still_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    settings = WorkerSettings(_env_file=None)

    assert settings.otel_exporter_otlp_endpoint == "http://otel-collector:4317"


def test_github_app_private_key_never_appears_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert TEST_RSA_PRIVATE_KEY_PEM not in repr(settings)
    assert TEST_RSA_PRIVATE_KEY_PEM not in str(settings.github_app_private_key)


# --- Phase 4: adapter.github.auth.validate_private_key startup check -------
#
# Per the plan's Phase 4 Tests section: "a valid test RSA key passes; a
# malformed/truncated key fails startup with SystemExit(1), the same shape
# as the existing pgvector/LLM startup-check tests" — mirroring
# `test_all_finding_producing_steps_disabled_fails_the_viability_check`'s
# own precedent above of testing the pure check function directly, not
# only through `main()`'s own SystemExit wrapping (covered separately in
# `apps/worker/test_main.py`).


def test_validate_private_key_accepts_a_valid_rsa_key() -> None:
    # Must not raise.
    validate_private_key(app_id=123456, private_key_pem=TEST_RSA_PRIVATE_KEY_PEM)


def test_validate_private_key_rejects_a_malformed_key() -> None:
    with pytest.raises(GitHubPrivateKeyInvalidError):
        validate_private_key(app_id=123456, private_key_pem="not-a-real-private-key")


def test_validate_private_key_rejects_a_truncated_key() -> None:
    truncated = TEST_RSA_PRIVATE_KEY_PEM[: len(TEST_RSA_PRIVATE_KEY_PEM) // 2]
    with pytest.raises(GitHubPrivateKeyInvalidError):
        validate_private_key(app_id=123456, private_key_pem=truncated)


# --- Context Builder delivery plan's Phase 5: the timeout-margin validator -
#
# Assumption 7's own margin invariant, "the single most important
# correctness property of this phase" by the plan's own words —
# `context_builder_step_timeout_seconds` (the outer per-step timeout) must
# be strictly greater than `context_builder_internal_timeout_seconds` (the
# single `asyncio.wait_for` bounding `ContextBuilderStep`'s whole internal
# orchestration), mirroring `apps/webhook_api/test_settings.py`'s own
# `pytest.raises(ValidationError)` pattern for its own analogous validator.


def test_context_builder_default_timeouts_have_a_valid_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert settings.context_builder_step_timeout_seconds == 35
    assert settings.context_builder_internal_timeout_seconds == 25
    assert settings.context_builder_step_timeout_seconds > (
        settings.context_builder_internal_timeout_seconds
    )


def test_context_builder_internal_timeout_equal_to_step_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("REVIEW_WORKER_CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS", "20")

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_context_builder_internal_timeout_greater_than_step_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("REVIEW_WORKER_CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS", "25")

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_context_builder_internal_timeout_with_valid_margin_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("REVIEW_WORKER_CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS", "10")

    settings = WorkerSettings(_env_file=None)

    assert settings.context_builder_step_timeout_seconds == 20
    assert settings.context_builder_internal_timeout_seconds == 10
