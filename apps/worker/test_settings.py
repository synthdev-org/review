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


def test_memory_suppression_fields_default_per_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/plans/plan-deliver-memory.md's Phase 6 section (Assumption 6) —
    similarity-suppression's own bootstrap-config fields, read directly by
    `_build_review_registry` rather than going through `StepEnablement`."""
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert settings.memory_suppression_enabled is True
    assert settings.memory_similarity_threshold == 0.90
    assert settings.memory_decay_half_life_days == 180.0


def test_memory_suppression_fields_are_overridable_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_MEMORY_SUPPRESSION_ENABLED", "false")
    monkeypatch.setenv("REVIEW_WORKER_MEMORY_SIMILARITY_THRESHOLD", "0.75")
    monkeypatch.setenv("REVIEW_WORKER_MEMORY_DECAY_HALF_LIFE_DAYS", "90")

    settings = WorkerSettings(_env_file=None)

    assert settings.memory_suppression_enabled is False
    assert settings.memory_similarity_threshold == 0.75
    assert settings.memory_decay_half_life_days == 90.0


def test_model_routing_config_enabled_defaults_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/plans/plan-deliver-admin-gui.md's Phase 5 section (Unit F) —
    mirrors `memory_suppression_enabled`'s own default-true shape exactly."""
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")

    settings = WorkerSettings(_env_file=None)

    assert settings.model_routing_config_enabled is True


def test_model_routing_config_enabled_is_overridable_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_MODEL_ROUTING_CONFIG_ENABLED", "false")

    settings = WorkerSettings(_env_file=None)

    assert settings.model_routing_config_enabled is False


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


# --- Product knowledge (plan-deliver-product-knowledge.md Phase 6; US-PK13) --

_PK_CANARY = "CANARY-PK-TOKEN-8f14e45f"


def _pk_map(**overrides: object) -> str:
    import json

    entry: dict[str, object] = {"token": _PK_CANARY, "allowed_hosts": ["wiki.internal"]}
    entry.update(overrides)
    return json.dumps({"wiki-prod": entry})


def _worker_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> WorkerSettings:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    for key, value in env.items():
        monkeypatch.setenv(f"REVIEW_WORKER_{key}", value)
    return WorkerSettings(_env_file=None)


def test_product_knowledge_defaults_start_clean_with_no_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(monkeypatch)

    assert settings.product_knowledge_enabled is True
    assert settings.product_knowledge_call_timeout_seconds == 5
    assert settings.product_knowledge_deadline_seconds == 10
    assert settings.product_knowledge_allow_private_networks is True
    assert not settings.credential_map()
    assert settings.endpoint_allowlist() == ()


@pytest.mark.parametrize("blank", ["", "   "])
def test_empty_or_whitespace_credentials_are_the_same_as_unset(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    settings = _worker_settings(monkeypatch, PRODUCT_KNOWLEDGE_CREDENTIALS=blank)

    assert settings.product_knowledge_credentials is None
    assert not settings.credential_map()


def test_valid_map_is_parsed_and_token_stays_out_of_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _worker_settings(monkeypatch, PRODUCT_KNOWLEDGE_CREDENTIALS=_pk_map())

    assert settings.credential_map().refs == ("wiki-prod",)
    assert _PK_CANARY not in repr(settings)
    assert _PK_CANARY not in str(settings.model_dump())


def test_internal_timeout_of_8_with_no_map_starts_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An operator who tuned the Context Builder window <= 10 s still upgrades cleanly.

    The ordering rule applies only when the feature is in use.
    """
    settings = _worker_settings(
        monkeypatch,
        CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS="8",
        CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS="12",
    )

    assert settings.context_builder_internal_timeout_seconds == 8


def test_internal_timeout_of_8_with_a_map_fails_naming_the_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _worker_settings(
            monkeypatch,
            PRODUCT_KNOWLEDGE_CREDENTIALS=_pk_map(),
            CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS="8",
            CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS="12",
        )

    assert "product_knowledge_deadline_seconds" in str(excinfo.value)
    assert _PK_CANARY not in str(excinfo.value)


def test_internal_timeout_of_8_with_only_an_allowlist_fails_naming_the_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An unauthenticated-only provider (no credential map) counts as in use.

    It is reachable only via the allow-list, so a non-empty allow-list also
    means in use.
    """
    with pytest.raises(ValidationError) as excinfo:
        _worker_settings(
            monkeypatch,
            PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST="wiki.internal",
            CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS="8",
            CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS="12",
        )

    assert "product_knowledge_deadline_seconds" in str(excinfo.value)


def test_internal_timeout_of_8_with_an_allowlist_but_kill_switch_off_starts_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        monkeypatch,
        PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST="wiki.internal",
        PRODUCT_KNOWLEDGE_ENABLED="false",
        CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS="8",
        CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS="12",
    )

    assert settings.context_builder_internal_timeout_seconds == 8


def test_internal_timeout_of_8_with_a_map_but_kill_switch_off_starts_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        monkeypatch,
        PRODUCT_KNOWLEDGE_CREDENTIALS=_pk_map(),
        PRODUCT_KNOWLEDGE_ENABLED="false",
        CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS="8",
        CONTEXT_BUILDER_STEP_TIMEOUT_SECONDS="12",
    )

    assert settings.product_knowledge_enabled is False


@pytest.mark.parametrize(
    ("call", "deadline"),
    [("10", "10"), ("11", "10"), ("5", "25"), ("5", "30")],
)
def test_call_deadline_internal_ordering_is_strict_when_in_use(
    monkeypatch: pytest.MonkeyPatch, call: str, deadline: str
) -> None:
    with pytest.raises(ValidationError):
        _worker_settings(
            monkeypatch,
            PRODUCT_KNOWLEDGE_CREDENTIALS=_pk_map(),
            PRODUCT_KNOWLEDGE_CALL_TIMEOUT_SECONDS=call,
            PRODUCT_KNOWLEDGE_DEADLINE_SECONDS=deadline,
        )


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("not json", "not valid JSON"),
        ('["a"]', "non-empty JSON object"),
        ('{"ref": {"token": "", "allowed_hosts": ["h"]}}', "empty token"),
        ('{"ref": {"token": "x", "allowed_hosts": []}}', "no allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["*"]}}', "invalid allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["https://h"]}}', "invalid allowed_hosts"),
        ('{"Bad Ref": {"token": "x", "allowed_hosts": ["h"]}}', "invalid ref"),
        ('{"r": {"token": "x", "allowed_hosts": ["h"]}, "r": {"token": "y"}}', "duplicate"),
    ],
)
def test_malformed_credential_map_fails_fast_naming_the_problem_not_the_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str, problem: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _worker_settings(monkeypatch, PRODUCT_KNOWLEDGE_CREDENTIALS=raw)

    assert problem in str(excinfo.value)
    assert "REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS" not in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        # Canary token inside every malformed shape: pydantic's `input_value`
        # echo must be suppressed everywhere.
        '{"ref": {"token": "' + _PK_CANARY + '", "allowed_hosts": []}}',
        '{"ref": {"token": "' + _PK_CANARY + '", "allowed_hosts": ["*"]}}',
        '{"ref": {"token": "' + _PK_CANARY + '"',
        '{"Bad Ref": {"token": "' + _PK_CANARY + '", "allowed_hosts": ["h"]}}',
    ],
)
def test_token_canary_is_never_echoed_by_a_validation_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _worker_settings(monkeypatch, PRODUCT_KNOWLEDGE_CREDENTIALS=raw)

    assert _PK_CANARY not in str(excinfo.value)
    assert _PK_CANARY not in repr(excinfo.value)
    assert _PK_CANARY not in caplog.text
    # `ValidationError.errors()` (programmatic, never rendered by the
    # entrypoint) still carries pydantic's raw `input`; the loggable text
    # channels above are the contract.


def test_endpoint_allowlist_is_parsed_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _worker_settings(
        monkeypatch, PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST="wiki.internal, *.corp.example"
    )
    assert settings.endpoint_allowlist() == ("wiki.internal", "*.corp.example")

    with pytest.raises(ValidationError):
        _worker_settings(monkeypatch, PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST="*")


def test_max_concurrent_analyzers_defaults_to_three_and_rejects_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    assert WorkerSettings(_env_file=None).static_analysis_max_concurrent_analyzers == 3

    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_MAX_CONCURRENT_ANALYZERS", "0")
    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_analyzer_timeouts_that_cannot_fit_the_step_budget_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_MAX_CONCURRENT_ANALYZERS", "1")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ANALYZER_TIMEOUT_SECONDS", "200")

    with pytest.raises(ValidationError, match="exceeds static_analysis_step_timeout_seconds"):
        WorkerSettings(_env_file=None)


def test_ruff_flag_defaults_on_and_timeout_override_inherits_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    settings = WorkerSettings(_env_file=None)
    assert settings.static_analysis_ruff_enabled is True
    assert settings.static_analysis_ruff_timeout_seconds is None

    # Compose passes an unset override as an empty string.
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_RUFF_TIMEOUT_SECONDS", "")
    assert WorkerSettings(_env_file=None).static_analysis_ruff_timeout_seconds is None

    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_RUFF_TIMEOUT_SECONDS", "20")
    assert WorkerSettings(_env_file=None).static_analysis_ruff_timeout_seconds == 20


def test_ruff_timeout_override_larger_than_the_step_budget_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_RUFF_TIMEOUT_SECONDS", "300")

    with pytest.raises(ValidationError, match="exceeds static_analysis_step_timeout_seconds"):
        WorkerSettings(_env_file=None)


def test_eslint_flag_defaults_on_and_timeout_override_inherits_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    settings = WorkerSettings(_env_file=None)
    assert settings.static_analysis_eslint_enabled is True
    assert settings.static_analysis_eslint_timeout_seconds is None

    # Compose passes an unset override as an empty string.
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ESLINT_TIMEOUT_SECONDS", "")
    assert WorkerSettings(_env_file=None).static_analysis_eslint_timeout_seconds is None

    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ESLINT_TIMEOUT_SECONDS", "45")
    assert WorkerSettings(_env_file=None).static_analysis_eslint_timeout_seconds == 45


def test_eslint_timeout_override_larger_than_the_step_budget_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_ESLINT_TIMEOUT_SECONDS", "300")

    with pytest.raises(ValidationError, match="exceeds static_analysis_step_timeout_seconds"):
        WorkerSettings(_env_file=None)


def test_trivy_flag_defaults_on_and_timeout_override_inherits_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    settings = WorkerSettings(_env_file=None)
    assert settings.static_analysis_trivy_enabled is True
    assert settings.static_analysis_trivy_timeout_seconds is None

    # Compose passes an unset override as an empty string.
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_TRIVY_TIMEOUT_SECONDS", "")
    assert WorkerSettings(_env_file=None).static_analysis_trivy_timeout_seconds is None

    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_TRIVY_TIMEOUT_SECONDS", "45")
    assert WorkerSettings(_env_file=None).static_analysis_trivy_timeout_seconds == 45


def test_trivy_timeout_override_larger_than_the_step_budget_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_TRIVY_TIMEOUT_SECONDS", "300")

    with pytest.raises(ValidationError, match="exceeds static_analysis_step_timeout_seconds"):
        WorkerSettings(_env_file=None)


def test_phpstan_flag_defaults_on_and_timeout_override_inherits_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    settings = WorkerSettings(_env_file=None)
    assert settings.static_analysis_phpstan_enabled is True
    assert settings.static_analysis_phpstan_timeout_seconds is None

    # Compose passes an unset override as an empty string.
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_PHPSTAN_TIMEOUT_SECONDS", "")
    assert WorkerSettings(_env_file=None).static_analysis_phpstan_timeout_seconds is None

    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_PHPSTAN_TIMEOUT_SECONDS", "45")
    assert WorkerSettings(_env_file=None).static_analysis_phpstan_timeout_seconds == 45


def test_golangci_lint_flag_defaults_on_and_timeout_override_inherits_when_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    settings = WorkerSettings(_env_file=None)
    assert settings.static_analysis_golangci_lint_enabled is True
    assert settings.static_analysis_golangci_lint_timeout_seconds is None

    # Compose passes an unset override as an empty string.
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_GOLANGCI_LINT_TIMEOUT_SECONDS", "")
    assert WorkerSettings(_env_file=None).static_analysis_golangci_lint_timeout_seconds is None

    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_GOLANGCI_LINT_TIMEOUT_SECONDS", "45")
    assert WorkerSettings(_env_file=None).static_analysis_golangci_lint_timeout_seconds == 45


def test_golangci_lint_timeout_override_larger_than_the_step_budget_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_GOLANGCI_LINT_TIMEOUT_SECONDS", "300")

    with pytest.raises(ValidationError, match="exceeds static_analysis_step_timeout_seconds"):
        WorkerSettings(_env_file=None)


def test_phpstan_timeout_override_larger_than_the_step_budget_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_PHPSTAN_TIMEOUT_SECONDS", "300")

    with pytest.raises(ValidationError, match="exceeds static_analysis_step_timeout_seconds"):
        WorkerSettings(_env_file=None)


# --- clone_detection (docs/plans/plan-phase-10.md, Phase 4) -----------------


def _clone_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    for key, value in env.items():
        monkeypatch.setenv(f"REVIEW_WORKER_{key}", value)


def test_clone_detection_defaults_per_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _clone_env(monkeypatch)

    settings = WorkerSettings(_env_file=None)

    assert settings.clone_detection_enabled is True
    assert settings.clone_detection_step_timeout_seconds == 30
    assert settings.clone_detection_min_tokens == 100
    assert settings.clone_detection_min_lines == 10
    assert settings.clone_detection_max_findings == 5


@pytest.mark.parametrize(
    "name",
    [
        "CLONE_DETECTION_STEP_TIMEOUT_SECONDS",
        "CLONE_DETECTION_MIN_TOKENS",
        "CLONE_DETECTION_MIN_LINES",
        "CLONE_DETECTION_MAX_FINDINGS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_clone_detection_non_positive_values_fail_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _clone_env(monkeypatch, **{name: value})

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_clone_detection_min_tokens_below_the_winnowing_guarantee_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clone_env(monkeypatch, CLONE_DETECTION_MIN_TOKENS="99")

    with pytest.raises(ValidationError, match="must be at least 100"):
        WorkerSettings(_env_file=None)


def test_clone_detection_min_tokens_can_be_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _clone_env(monkeypatch, CLONE_DETECTION_MIN_TOKENS="250")

    assert WorkerSettings(_env_file=None).clone_detection_min_tokens == 250


@pytest.mark.parametrize("value", ["5", "3"])
def test_clone_detection_step_timeout_must_exceed_the_internal_margin(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _clone_env(monkeypatch, CLONE_DETECTION_STEP_TIMEOUT_SECONDS=value)

    with pytest.raises(ValidationError, match="greater than 5"):
        WorkerSettings(_env_file=None)


def test_stuck_job_threshold_must_cover_the_clone_detection_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 545 s is the new worst-case chain (515 + 30); 530 covered the old sum only.
    _clone_env(monkeypatch, STUCK_JOB_WARNING_THRESHOLD_SECONDS="530")

    with pytest.raises(ValidationError, match="545"):
        WorkerSettings(_env_file=None)


def test_stuck_job_threshold_accepts_the_new_chain_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    _clone_env(monkeypatch, STUCK_JOB_WARNING_THRESHOLD_SECONDS="545")

    assert WorkerSettings(_env_file=None).stuck_job_warning_threshold_seconds == 545.0
