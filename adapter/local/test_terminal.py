"""Unit tests for adapter.local.terminal (plain text, no I/O beyond an in-memory stream)."""

from __future__ import annotations

import io

import pytest

from adapter.local.terminal import TerminalFindingsPublisher, render_finding
from findings.models import Finding


def _finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        "file": "src/app.py",
        "line": 12,
        "severity": "high",
        "message": "Possible SQL injection",
        "source": "llm",
    }
    fields.update(overrides)
    return Finding.model_validate(fields)


def test_render_finding_has_file_line_severity_message_and_rule() -> None:
    assert render_finding(_finding(rule_id="py.sql")) == (
        "src/app.py:12: high: Possible SQL injection (py.sql)"
    )


def test_render_finding_omits_the_line_of_a_file_level_finding() -> None:
    assert render_finding(_finding(line=None)) == "src/app.py: high: Possible SQL injection"


def test_render_finding_cannot_inject_terminal_escapes_or_extra_lines() -> None:
    hostile = _finding(message="ok\x1b[2J\x1b]0;pwned\x07\nsrc/evil.py:1: critical: fake")

    rendered = render_finding(hostile)

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "\n" not in rendered


@pytest.mark.parametrize("name", ["::error::x.py", "::add-mask::abc", "::stop-commands::tok"])
def test_render_finding_never_starts_with_a_workflow_command(name: str) -> None:
    rendered = render_finding(_finding(file=name))

    assert not rendered.startswith("::")
    assert name in rendered


@pytest.mark.asyncio
async def test_publisher_prints_each_finding_then_the_count() -> None:
    stream = io.StringIO()
    publisher = TerminalFindingsPublisher(stream)

    await publisher.post_review(
        installation_id=0,
        repo_full_name="local/x",
        repo_id="local",
        pr_number=0,
        head_sha="abc",
        findings=[_finding(), _finding(file="b.py", line=3, severity="low", message="nit")],
    )
    await publisher.post_check_run(
        installation_id=0, repo_full_name="local/x", head_sha="abc", finding_count=2
    )

    assert stream.getvalue().splitlines() == [
        "src/app.py:12: high: Possible SQL injection",
        "b.py:3: low: nit",
        "2 finding(s).",
    ]


@pytest.mark.asyncio
async def test_publisher_says_so_when_there_are_no_findings() -> None:
    stream = io.StringIO()

    await TerminalFindingsPublisher(stream).post_check_run(
        installation_id=0, repo_full_name="local/x", head_sha="abc", finding_count=0
    )

    assert stream.getvalue() == "No findings.\n"


@pytest.mark.asyncio
async def test_publisher_failure_check_run_prints_nothing() -> None:
    stream = io.StringIO()

    await TerminalFindingsPublisher(stream).post_failure_check_run(
        installation_id=0,
        repo_full_name="local/x",
        head_sha="abc",
        failed_step_name="review_generation",
        error="boom",
    )

    assert stream.getvalue() == ""
