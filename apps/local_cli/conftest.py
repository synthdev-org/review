"""Fixtures for the local CLI's tests: the request-capturing LLM stub from adapter/llm/conftest.py
(delivered in Phase 1b of docs/plans/plan-local-mode.md), shared rather than copied, and an
isolation guard for the process-global logging ``main`` configures."""

import logging
from collections.abc import Iterator

import pytest

from adapter.llm.conftest import (  # noqa: F401  (re-exported fixtures)
    llm_requests,
    stub_llm_capture,
    stub_llm_server,
)
from apps.local_cli.main import _FIRST_PARTY


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """``configure_logging`` (called by ``main``) sets process-global logger levels and replaces the
    root handlers; undo that after each test so it cannot silence other packages' log capture."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {name: logging.getLogger(name).level for name in _FIRST_PARTY}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, previous in levels.items():
        logging.getLogger(name).setLevel(previous)


@pytest.fixture(autouse=True)
def _not_in_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test run inside a real GitHub Actions job must not turn every local review into a
    pull-request post; a test that wants CI sets these itself."""
    for name in (
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_EVENT_NAME",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
