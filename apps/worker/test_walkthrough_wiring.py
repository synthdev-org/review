"""Flag-off half of the walkthrough wiring tests (plan-phase-12, Phase 3, US-W9).

The flag-on half lands with the step in Phase 4. Here: with the flag off, or an
ineffective configuration, the registry is the one it was before Phase 12.
"""

from __future__ import annotations

from typing import cast

import pytest

import apps.worker.main as worker_main
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.posting import GitHubFindingsPublisher
from adapter.github.tree import GitHubTreeFetcher
from adapter.github.walkthrough import GitHubWalkthroughPublisher
from adapter.storage.clone_query import PostgresCloneIndexQuery
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.memory_repository import PostgresMemoryRepository
from apps.worker.settings import WorkerSettings
from apps.worker.test_main import (
    _EmptyDiffFetcher,
    _EmptyFindingsLLMClient,
    _EmptyTreeFetcher,
    _NoOpGitleaksAnalyzer,
    _NoOpSemgrepAnalyzer,
    _run_review_registry,
    _UnusedCloneQuery,
    _UnusedEmbeddingClient,
    _UnusedGraphQuery,
    _UnusedMemoryMatcher,
    _UnusedSimilarityQuery,
)
from findings.redact import NoOpTextRedactor
from review.config import (
    StepEnablement,
    StepTimeouts,
    check_step_configuration_viable,
    resolve_review_pipeline_depends_on,
)
from review.pipeline import StepRegistry
from review.walkthrough_render import WalkthroughPublisher


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post_review(self, **kwargs: object) -> None:
        self.calls.append("post_review")

    async def post_check_run(self, **kwargs: object) -> None:
        self.calls.append("post_check_run")

    async def post_failure_check_run(self, **kwargs: object) -> None:
        self.calls.append("post_failure_check_run")


def _registry(
    monkeypatch: pytest.MonkeyPatch,
    enablement: StepEnablement,
    publisher: _RecordingPublisher,
    walkthrough_publisher: object | None = None,
    **env: str,
) -> StepRegistry:
    monkeypatch.setattr(worker_main, "GitleaksAnalyzer", _NoOpGitleaksAnalyzer)
    monkeypatch.setattr(worker_main, "SemgrepAnalyzer", _NoOpSemgrepAnalyzer)
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    for key, value in env.items():
        monkeypatch.setenv(f"REVIEW_WORKER_{key}", value)
    settings = WorkerSettings(_env_file=None)
    check_step_configuration_viable(enablement)
    return worker_main._build_review_registry(
        enablement,
        StepTimeouts(),
        cast("GitHubDiffFetcher", _EmptyDiffFetcher()),
        cast("GitHubTreeFetcher", _EmptyTreeFetcher()),
        cast("PostgresGraphQuery", _UnusedGraphQuery()),
        cast("PostgresSimilarityQuery", _UnusedSimilarityQuery()),
        cast("PostgresCloneIndexQuery", _UnusedCloneQuery()),
        cast("LocalONNXEmbeddingClient", _UnusedEmbeddingClient()),
        NoOpTextRedactor(),
        _EmptyFindingsLLMClient(),
        cast("GitHubFindingsPublisher", publisher),
        settings,
        cast("PostgresMemoryRepository", _UnusedMemoryMatcher()),
        None,
        walkthrough_publisher=cast("WalkthroughPublisher | None", walkthrough_publisher),
    )


@pytest.mark.asyncio
async def test_flag_off_chain_and_publisher_calls_match_a_run_without_walkthrough_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_publisher, flag_off_publisher = _RecordingPublisher(), _RecordingPublisher()
    baseline = _registry(monkeypatch, StepEnablement(), baseline_publisher)
    flag_off = _registry(
        monkeypatch,
        StepEnablement(walkthrough_enabled=False),
        flag_off_publisher,
        WALKTHROUGH_MAX_FILES="7",
        WALKTHROUGH_STEP_TIMEOUT_SECONDS="99",
    )

    assert set(flag_off._by_name) == set(baseline._by_name)
    assert "walkthrough" not in flag_off._by_name
    assert {n: s.depends_on for n, s in flag_off._by_name.items()} == {
        n: s.depends_on for n, s in baseline._by_name.items()
    }
    assert await _run_review_registry(flag_off) == await _run_review_registry(baseline)
    assert flag_off_publisher.calls == baseline_publisher.calls


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_redaction_enabled": False},
        {"review_generation_enabled": False},
    ],
)
@pytest.mark.asyncio
async def test_ineffective_flag_on_registers_no_step_and_leaves_output_wiring(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, bool]
) -> None:
    enablement = StepEnablement(walkthrough_enabled=True, **overrides)
    baseline = StepEnablement(**overrides)
    publisher = _RecordingPublisher()

    registry = _registry(monkeypatch, enablement, publisher)

    expected = resolve_review_pipeline_depends_on(baseline)["output"]
    assert resolve_review_pipeline_depends_on(enablement)["output"] == expected
    assert "walkthrough" not in registry._by_name
    assert registry._by_name["output"].depends_on == (expected,)
    statuses = await _run_review_registry(registry)
    assert statuses["output"] == "success"


class _FakeWalkthroughPublisher:
    async def upsert(self, **kwargs: object) -> str:
        return "created"


def test_flag_on_registers_the_step_with_settings_and_output_depends_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enablement = StepEnablement(walkthrough_enabled=True)
    fake = _FakeWalkthroughPublisher()

    registry = _registry(
        monkeypatch,
        enablement,
        _RecordingPublisher(),
        fake,
        WALKTHROUGH_STEP_TIMEOUT_SECONDS="60",
        WALKTHROUGH_MIN_FILES="3",
        WALKTHROUGH_MAX_FILES="9",
        WALKTHROUGH_MAX_CHARS="1234",
    )

    step = registry._by_name["walkthrough"]
    assert step._internal_timeout_seconds == 55.0  # type: ignore[attr-defined]
    assert step._publisher is fake  # type: ignore[attr-defined]
    assert (step._min_files, step._max_files, step._max_chars) == (3, 9, 1234)  # type: ignore[attr-defined]
    assert registry._by_name["output"].depends_on == ("walkthrough",)
    assert step.depends_on == (resolve_review_pipeline_depends_on(enablement)["walkthrough"],)


@pytest.mark.asyncio
async def test_flag_on_chain_runs_to_success_for_a_job_with_no_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        monkeypatch,
        StepEnablement(walkthrough_enabled=True),
        _RecordingPublisher(),
        _FakeWalkthroughPublisher(),
    )

    statuses = await _run_review_registry(registry)

    assert statuses["walkthrough"] == "success" and statuses["output"] == "success"


def test_effective_without_a_publisher_is_a_construction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="walkthrough_publisher"):
        _registry(monkeypatch, StepEnablement(walkthrough_enabled=True), _RecordingPublisher())


def test_the_publisher_gets_settings_github_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_GITHUB_APP_ID", "424242")
    settings = WorkerSettings(_env_file=None)

    publisher = worker_main._build_walkthrough_publisher(settings, object(), object())  # type: ignore[arg-type]

    assert isinstance(publisher, GitHubWalkthroughPublisher)
    assert publisher._app_id == 424242
