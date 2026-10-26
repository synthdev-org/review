"""Keeps docs/reference/operations.md's local-mode runbook in step with the code: every exit code
and every runtime-failure reason string the CLI can print is documented."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.local_cli import main
from apps.local_cli.main import RuntimeFailure

_RUNBOOK = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "operations.md"
).read_text()
_LOCAL_SECTION = _RUNBOOK.split("## Local mode (`review run`)", 1)[1]


@pytest.mark.parametrize("reason", list(RuntimeFailure))
def test_every_runtime_failure_reason_is_documented(reason: RuntimeFailure) -> None:
    assert f"`{reason.value}`" in _LOCAL_SECTION


@pytest.mark.parametrize(
    "code", [main.EXIT_OK, main.EXIT_RUNTIME_FAILURE, main.EXIT_USAGE, main.EXIT_GATE_TRIPPED]
)
def test_every_exit_code_is_documented(code: int) -> None:
    assert f"| {code} |" in _LOCAL_SECTION


def test_the_exit_codes_are_distinct() -> None:
    codes = [main.EXIT_OK, main.EXIT_RUNTIME_FAILURE, main.EXIT_USAGE, main.EXIT_GATE_TRIPPED]
    assert len(set(codes)) == 4


def test_every_summary_field_is_documented() -> None:
    from adapter.local.summary import RunSummary

    for field in RunSummary.model_fields:
        assert f"| `{field}` |" in _LOCAL_SECTION


def test_the_memory_file_limits_and_proposal_threshold_are_documented() -> None:
    from adapter.local import memory_file

    assert f"{memory_file.PROPOSAL_MIN_DISMISSALS})" in _LOCAL_SECTION
    assert f"{memory_file.MEMORY_FILE_MAX_BYTES:,} bytes" in _LOCAL_SECTION
    assert f"{memory_file.MEMORY_FILE_MAX_LINES:,} lines" in _LOCAL_SECTION
    assert f"{memory_file.MEMORY_FILE_MAX_LINE_CHARS:,} characters per line" in _LOCAL_SECTION
    assert memory_file.DEFAULT_MEMORY_FILE in _LOCAL_SECTION
