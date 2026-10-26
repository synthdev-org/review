"""Pins what the local CLI wires (plan A3): the shared builder's registry for the local flag set.
Unit tier: constructs the registry only; no step runs and no git, model or subprocess is used."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from adapter.llm.litellm_client import LiteLLMClient
from adapter.local.git import ReviewRange
from adapter.local.memory_file import FileMemoryMatcher
from adapter.local.terminal import TerminalFindingsPublisher
from apps.local_cli.main import build_diff_fetcher, build_registry
from apps.local_cli.settings import LocalSettings
from review.memory_suppression import FingerprintSuppressionConfig
from review.pipeline import StepRegistry

_ENDPOINT = "http://127.0.0.1:1/v1"
_RANGE = ReviewRange(
    base_sha="a" * 40, merge_base_sha="b" * 40, head_sha="c" * 40, has_changes=True
)


def _registry(*, static_analysis: bool, skipped_analyzers: tuple[str, ...] = ()) -> StepRegistry:
    settings = LocalSettings(
        llm_endpoint=_ENDPOINT, llm_model="m", static_analysis_enabled=static_analysis
    )
    return build_registry(
        settings,
        model="m",
        diff_fetcher=build_diff_fetcher(settings, Path("/nonexistent"), _RANGE, environ={}),
        llm_client=LiteLLMClient(endpoint=_ENDPOINT, api_key="not-needed"),
        publisher=TerminalFindingsPublisher(io.StringIO()),
        environ={},
        skipped_analyzers=skipped_analyzers,
    )


def test_a_skipped_analyzer_is_disabled_in_the_registry_and_the_rest_stay_on() -> None:
    registry = _registry(static_analysis=True, skipped_analyzers=("semgrep", "trivy"))

    assert registry._steps  # built
    static_analysis = next(s for s in registry._steps if s.name == "static_analysis")
    inner_enabled = static_analysis._enabled  # type: ignore[attr-defined]
    assert inner_enabled["semgrep"] is False
    assert inner_enabled["trivy"] is False
    assert inner_enabled["gitleaks"] is True
    assert inner_enabled["ruff"] is True


@pytest.mark.parametrize("static_analysis", [True, False])
def test_local_registry_runs_the_review_chain_with_server_only_steps_disabled(
    static_analysis: bool,
) -> None:
    registry = _registry(static_analysis=static_analysis)

    steps = {s.name: tuple(s.depends_on) for s in registry._steps}
    upstream = "static_analysis" if static_analysis else "prefilter"
    assert steps["intent_summary"] == (upstream,)
    assert steps["review_generation"] == ("intent_summary",)
    assert steps["verification"] == ("review_generation",)
    assert steps["output"] == ("verification",)
    assert registry._terminal_step == "output"
    assert "walkthrough" not in steps
    enabled = registry._enabled
    for server_only in ("clone_detection", "context_builder", "fix_generation", "walkthrough"):
        assert enabled[server_only] is False
    for local in ("prefilter", "intent_summary", "review_generation", "verification", "output"):
        assert enabled[local] is True
    assert enabled["static_analysis"] is static_analysis


def test_a_memory_file_matcher_turns_on_exact_fingerprint_suppression_in_both_steps() -> None:
    settings = LocalSettings(llm_endpoint=_ENDPOINT, llm_model="m", static_analysis_enabled=False)
    matcher = FileMemoryMatcher(())

    registry = build_registry(
        settings,
        model="m",
        diff_fetcher=build_diff_fetcher(settings, Path("/nonexistent"), _RANGE, environ={}),
        llm_client=LiteLLMClient(endpoint=_ENDPOINT, api_key="not-needed"),
        publisher=TerminalFindingsPublisher(io.StringIO()),
        environ={},
        memory_matcher=matcher,
    )

    for name in ("verification", "clone_detection"):
        step = next(s for s in registry._steps if s.name == name)
        config = step._memory_suppression  # type: ignore[attr-defined]
        assert config == FingerprintSuppressionConfig(fingerprint_matcher=matcher, enabled=True)


def test_without_a_matcher_no_step_gets_memory_suppression() -> None:
    registry = _registry(static_analysis=False)

    for name in ("verification", "clone_detection"):
        step = next(s for s in registry._steps if s.name == name)
        assert step._memory_suppression is None  # type: ignore[attr-defined]
