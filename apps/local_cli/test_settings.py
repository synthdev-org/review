"""Unit tests for apps.local_cli.settings and the seed: parsing, mapping into the shared pipeline
configuration, and that the local flag set (plan A3) is viable without loosening any rule the
Worker can reach."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from adapter.local.git import ReviewRange
from apps.local_cli.main import build_seed
from apps.local_cli.settings import LocalSettings, ModelNotConfiguredError
from review.config import (
    NoViableStepConfigurationError,
    ReviewPipelineConfig,
    StepEnablement,
    check_step_configuration_viable,
)
from review.prefilter import PrefilterInput


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def test_nothing_is_configured_by_default() -> None:
    settings = LocalSettings()

    assert settings.llm_model is None
    assert settings.llm_endpoint is None
    with pytest.raises(ModelNotConfiguredError) as caught:
        settings.require_model()
    assert "REVIEW_LOCAL_LLM_MODEL" in str(caught.value)
    assert "REVIEW_LOCAL_LLM_ENDPOINT" in str(caught.value)


def test_reads_the_review_local_prefix_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_LOCAL_LLM_MODEL", "my-model")
    monkeypatch.setenv("REVIEW_LOCAL_LLM_ENDPOINT", "http://localhost:1234/v1")
    monkeypatch.setenv("REVIEW_WORKER_LLM_MODEL", "worker-model")

    assert LocalSettings().require_model() == ("my-model", "http://localhost:1234/v1")


def test_blank_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_LOCAL_LLM_MODEL", "  ")
    monkeypatch.setenv("REVIEW_LOCAL_LLM_ENDPOINT", "")

    with pytest.raises(ModelNotConfiguredError):
        LocalSettings().require_model()


def test_a_bad_endpoint_is_rejected_without_echoing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_LOCAL_LLM_ENDPOINT", "not a url")
    monkeypatch.setenv("REVIEW_LOCAL_LLM_API_KEY", "sk-super-secret")

    with pytest.raises(ValidationError) as caught:
        LocalSettings()
    assert "sk-super-secret" not in str(caught.value)


def test_api_key_is_never_shown_in_repr() -> None:
    assert "sk-123" not in repr(LocalSettings(llm_api_key=SecretStr("sk-123")))


@pytest.mark.parametrize("static_analysis", [True, False])
def test_the_local_flag_set_is_viable_under_the_unchanged_worker_rules(
    static_analysis: bool,
) -> None:
    enablement = LocalSettings(static_analysis_enabled=static_analysis).step_enablement()

    check_step_configuration_viable(enablement)  # raises if the local flags could never review

    assert not enablement.clone_detection_enabled
    assert not enablement.context_builder_enabled
    assert not enablement.fix_generation_enabled
    assert not enablement.walkthrough_enabled
    assert enablement.review_generation_enabled
    assert enablement.output_enabled


def test_the_worker_rejections_still_hold() -> None:
    """Local mode relaxes no rule: the combinations the Worker refuses are refused here too."""
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(
            StepEnablement(static_analysis_enabled=True, prefilter_enabled=False)
        )
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(
            StepEnablement(
                prefilter_enabled=False,
                static_analysis_enabled=False,
                review_generation_enabled=True,
            )
        )


def test_pipeline_config_states_the_model_and_disables_memory_suppression() -> None:
    config = LocalSettings(llm_timeout_seconds=12.5, llm_max_diff_bytes=999).pipeline_config("m")

    assert isinstance(config, ReviewPipelineConfig)
    assert (config.llm_model, config.llm_timeout_seconds, config.llm_max_diff_bytes) == (
        "m",
        12.5,
        999,
    )
    assert not config.memory_suppression_enabled


def test_llm_step_timeouts_stay_above_the_model_timeout() -> None:
    defaults = LocalSettings().step_timeouts()
    slow = LocalSettings(llm_timeout_seconds=200).step_timeouts()

    assert defaults.review_generation_step_timeout_seconds == 90
    for seconds in (
        slow.intent_summary_step_timeout_seconds,
        slow.review_generation_step_timeout_seconds,
        slow.verification_step_timeout_seconds,
    ):
        assert seconds >= 215


def test_seed_is_a_valid_job_identity(tmp_path: Path) -> None:
    review_range = ReviewRange(
        base_sha="a" * 40, merge_base_sha="b" * 40, head_sha="c" * 40, has_changes=True
    )

    parsed = PrefilterInput.model_validate(build_seed(tmp_path / "my-repo", review_range))

    assert parsed.repo_full_name == "local/my-repo"
    assert parsed.head_sha == "c" * 40
    assert parsed.base_sha == "a" * 40
    assert parsed.pr_number == 0


def test_the_memory_file_is_on_by_default_and_its_path_is_a_setting() -> None:
    settings = LocalSettings()

    assert settings.memory_file_enabled
    assert settings.memory_file == ".review/memory.jsonl"
    assert LocalSettings(memory_file="x/m.jsonl").memory_file == "x/m.jsonl"
    assert LocalSettings().pipeline_config("m", memory_suppression=True).memory_suppression_enabled
