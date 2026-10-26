"""Snapshot of the Worker registry (step 1a of docs/plans/plan-local-mode.md, A2).

Pins step names, order, `depends_on`, enablement and timeouts for representative
`WorkerSettings`/`StepEnablement`, so extracting the builder into `review.registry` is provably
behaviour-neutral (US-P1) and any later drift fails here.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.worker.test_walkthrough_wiring import _RecordingPublisher, _registry
from review.config import StepEnablement
from review.pipeline import StepRegistry


def _chain(registry: StepRegistry) -> list[tuple[str, tuple[str, ...]]]:
    return [(s.name, tuple(s.depends_on)) for s in registry._steps]


def test_default_registry_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(monkeypatch, StepEnablement(), _RecordingPublisher())

    assert _chain(registry) == [
        ("prefilter", ()),
        ("static_analysis", ("prefilter",)),
        ("clone_detection", ("static_analysis",)),
        ("context_builder", ("clone_detection",)),
        ("intent_summary", ("context_builder",)),
        ("review_generation", ("intent_summary",)),
        ("verification", ("review_generation",)),
        ("fix_generation", ("verification",)),
        ("output", ("verification",)),
    ]
    assert registry._terminal_step == "output"
    assert registry._enabled == {
        "prefilter": True,
        "static_analysis": True,
        "clone_detection": True,
        "fix_generation": False,
        "walkthrough": False,
        "context_builder": True,
        "intent_summary": True,
        "review_generation": True,
        "verification": True,
        "output": True,
        "gitleaks": True,
        "semgrep": True,
        "ruff": True,
        "eslint": True,
        "trivy": True,
        "phpstan": True,
        "golangci_lint": True,
    }
    assert registry._timeouts == {
        "prefilter": 30,
        "static_analysis": 240,
        "clone_detection": 30,
        "context_builder": 35,
        "intent_summary": 30,
        "review_generation": 90,
        "verification": 60,
        "output": 30,
        "fix_generation": 90,
        "walkthrough": 45,
        "gitleaks": 60,
        "semgrep": 60,
        "ruff": 60,
        "trivy": 60,
        "eslint": 60,
        "phpstan": 60,
        "golangci_lint": 60,
        "static_analysis_analyzer": 60,
    }


def test_fix_generation_and_walkthrough_enabled_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(
        monkeypatch,
        StepEnablement(fix_generation_enabled=True, walkthrough_enabled=True),
        _RecordingPublisher(),
        walkthrough_publisher=object(),
    )

    assert _chain(registry) == [
        ("prefilter", ()),
        ("static_analysis", ("prefilter",)),
        ("clone_detection", ("static_analysis",)),
        ("context_builder", ("clone_detection",)),
        ("intent_summary", ("context_builder",)),
        ("review_generation", ("intent_summary",)),
        ("verification", ("review_generation",)),
        ("fix_generation", ("verification",)),
        ("walkthrough", ("fix_generation",)),
        ("output", ("walkthrough",)),
    ]
    assert registry._enabled["fix_generation"] and registry._enabled["walkthrough"]


def test_optional_steps_disabled_rewire_chain_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(
        monkeypatch,
        StepEnablement(
            static_analysis_enabled=False,
            clone_detection_enabled=False,
            context_builder_enabled=False,
            intent_summary_enabled=False,
        ),
        _RecordingPublisher(),
    )

    assert _chain(registry) == [
        ("prefilter", ()),
        ("static_analysis", ("prefilter",)),
        ("clone_detection", ("prefilter",)),
        ("context_builder", ("prefilter",)),
        ("intent_summary", ("prefilter",)),
        ("review_generation", ("prefilter",)),
        ("verification", ("review_generation",)),
        ("fix_generation", ("verification",)),
        ("output", ("verification",)),
    ]
    assert not any(
        registry._enabled[n]
        for n in ("static_analysis", "clone_detection", "context_builder", "intent_summary")
    )


def test_constructor_arguments_derived_from_the_pipeline_config_are_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``ReviewPipelineConfig`` value gets a distinct, non-default number, so a field wired to
    the wrong constructor argument (or its ``- 5`` fail-open margin dropped) fails here."""
    registry = _registry(
        monkeypatch,
        StepEnablement(walkthrough_enabled=True),
        _RecordingPublisher(),
        walkthrough_publisher=object(),
        LLM_MODEL="snap-model",
        LLM_TIMEOUT_SECONDS="21",
        LLM_MAX_DIFF_BYTES="123456",
        STATIC_ANALYSIS_MAX_CONCURRENT_ANALYZERS="2",
        CLONE_DETECTION_STEP_TIMEOUT_SECONDS="41",
        CLONE_DETECTION_MIN_TOKENS="137",
        CLONE_DETECTION_MIN_LINES="13",
        CLONE_DETECTION_MAX_FINDINGS="7",
        CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS="27",
        PRODUCT_KNOWLEDGE_DEADLINE_SECONDS="9",
        MEMORY_SIMILARITY_THRESHOLD="0.77",
        MEMORY_DECAY_HALF_LIFE_DAYS="33.0",
        FIX_GENERATION_STEP_TIMEOUT_SECONDS="71",
        FIX_GENERATION_MAX_FIXES_PER_JOB="3",
        FIX_GENERATION_MAX_RANGE_LINES="11",
        FIX_GENERATION_MAX_REPLACEMENT_LINES="33",
        FIX_GENERATION_WINDOW_LINES="57",
        WALKTHROUGH_STEP_TIMEOUT_SECONDS="53",
        WALKTHROUGH_MIN_FILES="3",
        WALKTHROUGH_MAX_FILES="44",
        WALKTHROUGH_MAX_CHARS="5000",
    )
    steps: dict[str, Any] = dict(registry._by_name)

    for name in ("intent_summary", "review_generation", "verification"):
        step = steps[name]
        assert (step._model, step._timeout, step._max_diff_bytes) == ("snap-model", 21.0, 123456)
    clone = steps["clone_detection"]
    assert (
        clone._internal_timeout_seconds,
        clone._min_tokens,
        clone._min_lines,
        clone._max_findings,
    ) == (36.0, 137, 13, 7)
    context = steps["context_builder"]
    assert (
        context._internal_timeout_seconds,
        context._max_total_bytes,
        context._provider_deadline_seconds,
    ) == (27, 123456, 9.0)
    fix = steps["fix_generation"]
    assert (
        fix._model,
        fix._timeout,
        fix._internal_timeout_seconds,
        fix._max_fixes,
        fix._max_range_lines,
        fix._max_replacement_lines,
        fix._window_lines,
    ) == ("snap-model", 21.0, 66.0, 3, 11, 33, 57)
    walkthrough = steps["walkthrough"]
    assert (
        walkthrough._model,
        walkthrough._timeout,
        walkthrough._internal_timeout_seconds,
        walkthrough._min_files,
        walkthrough._max_files,
        walkthrough._max_chars,
    ) == ("snap-model", 21.0, 48.0, 3, 44, 5000)
    # The memory-suppression values reach the two steps that apply them.
    for name in ("clone_detection", "verification"):
        suppression = steps[name]._memory_suppression
        assert (suppression.similarity_threshold, suppression.decay_half_life_days) == (0.77, 33.0)
    # The analyzer concurrency cap is the semaphore the static analysis step admits through.
    assert steps["static_analysis"]._analyzer_slots._value == 2
