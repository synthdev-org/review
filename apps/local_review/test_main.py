"""Tests for the local review runner's argument parsing and wiring
(docs/specs/spec-review-context.md US-X11 local half). No LLM, no git: the
registry is built, never run."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adapter.localgit.commits import LocalGitCommitSource
from adapter.localgit.diff import LocalGitDiffFetcher
from apps.local_review import __main__ as local_main
from apps.local_review.__main__ import LocalSettings

_REQUIRED: dict[str, Any] = {
    "llm_endpoint": "http://llm.invalid",
    "llm_model": "m",
    "llm_api_key": "k",
}


def _settings(**overrides: Any) -> LocalSettings:
    # `_env_file=None`: never read a developer's real .env.local in a test.
    return LocalSettings(_env_file=None, **{**_REQUIRED, **overrides})


def _steps(settings: LocalSettings, commits: LocalGitCommitSource | None = None) -> dict[str, Any]:
    fetcher = LocalGitDiffFetcher(Path("."), "main", "HEAD")
    registry = local_main._build_registry(settings, fetcher, commits)
    return {step.name: step for step in registry._steps}  # Any: reads privates


def test_arguments_default_to_cwd_main_and_head() -> None:
    args = local_main._parse_args([])
    assert (args.repo, args.base, args.head) == (Path("."), "main", "HEAD")


def test_arguments_are_parsed() -> None:
    args = local_main._parse_args(["--repo", "/r", "--base", "v1", "--head", "abc"])
    assert (args.repo, args.base, args.head) == (Path("/r"), "v1", "abc")


def test_defaults_match_the_worker() -> None:
    settings = _settings()
    assert settings.review_shrink_enabled is True and settings.review_stub_data_min_bytes == 8000
    assert settings.review_hunk_expansion_enabled is False
    assert settings.verification_file_context_enabled is False
    assert settings.review_commit_mode_enabled is False


def test_default_wiring_shrinks_and_has_no_file_context_or_commits() -> None:
    steps = _steps(_settings())

    context = steps["review_generation"]._context
    assert context.shrink is not None and context.expand is None and context.commits is None
    assert context.pr_map_max_bytes == 8000 and context.risk_ordering is True
    assert steps["verification"]._file_context is None


def test_flags_wire_expansion_file_context_and_commit_mode() -> None:
    settings = _settings(
        review_shrink_enabled=False,
        review_pr_map_enabled=False,
        review_hunk_expansion_enabled=True,
        verification_file_context_enabled=True,
        review_commit_mode_enabled=True,
    )
    source = LocalGitCommitSource(Path("."), "main", "HEAD")
    steps = _steps(settings, source)

    context = steps["review_generation"]._context
    assert context.shrink is None and context.pr_map_max_bytes == 0
    assert context.expand is not None and context.commits is source
    assert steps["verification"]._file_context.timeout_seconds == 10.0


def test_commit_mode_off_ignores_a_supplied_commit_source() -> None:
    source = LocalGitCommitSource(Path("."), "main", "HEAD")
    steps = _steps(_settings(), source)
    assert steps["review_generation"]._context.commits is None


def test_recording_classifier_counts_calls_and_failures() -> None:
    from apps.local_review.__main__ import RecordingClassifier
    from llmgateway.classifier import ClassificationResponse, NoulAnswer

    class Inner:
        def __init__(self) -> None:
            self.fail = False

        async def classify(self, request: object, *, timeout: float) -> ClassificationResponse:
            if self.fail:
                raise RuntimeError("down")
            return ClassificationResponse(model="m", answers={"valid": NoulAnswer(p=0.912)})

    inner = Inner()
    request = SimpleNamespace(state="framing Finding in a.py (high): msg\n\nDiff hunk:\n@@")
    recording = RecordingClassifier(inner)
    asyncio.run(recording.classify(request, timeout=1))  # type: ignore[arg-type]
    inner.fail = True
    with pytest.raises(RuntimeError):
        asyncio.run(recording.classify(request, timeout=1))  # type: ignore[arg-type]

    assert (recording.calls, recording.errors) == (2, 1)
    assert recording.records == [("a.py (high): msg", {"valid": 0.91})]


def test_classifier_is_off_without_a_jev_key() -> None:
    from apps.local_review.__main__ import LocalSettings, _build_classifier

    settings = LocalSettings(
        _env_file=None, llm_endpoint="http://x", llm_model="m", llm_api_key="k"
    )

    assert _build_classifier(settings) is None
