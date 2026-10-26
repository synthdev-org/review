"""Unit tests for the run summary: the structure accepts every field the plan lists (so a later
phase cannot omit one silently) and renders them as plain text without echoing hostile strings."""

from __future__ import annotations

from adapter.local.summary import (
    AnalyzerOutcome,
    BackendSummary,
    CiCommentsSummary,
    McpServerOutcome,
    MemoryFileSummary,
    RedactionSummary,
    RunSummary,
    StageOutcome,
    TokenUsageSummary,
    TruncationSummary,
    render_summary_text,
)


def _minimal() -> RunSummary:
    return RunSummary(backend=BackendSummary(model="m"))


def test_a_fresh_summary_says_not_reported_rather_than_zero() -> None:
    text = render_summary_text(_minimal())

    assert "token usage: not available" in text
    assert "suppressed by memory: not applicable" in text
    assert "redaction: not reported" in text
    assert "truncation: not reported" in text
    assert "mcp servers: none consulted" in text
    assert "memory file: disabled" in text
    assert "ci comments: not applicable" in text


def test_the_structure_accepts_every_planned_field_and_round_trips() -> None:
    summary = RunSummary(
        stages=[StageOutcome(name="prefilter", status="success", duration_ms=12)],
        analyzers=[
            AnalyzerOutcome(name="ruff", status="used"),
            AnalyzerOutcome(name="semgrep", status="skipped", reason="not installed"),
            AnalyzerOutcome(name="trivy", status="errored", reason="timed out"),
        ],
        backend=BackendSummary(model="m"),
        token_usage=TokenUsageSummary(
            requests=3, prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
        suppressed_count=2,
        redaction=RedactionSummary(status="applied", count=4),
        truncation=TruncationSummary(status="truncated", detail="2 files cut"),
        mcp_servers=[
            McpServerOutcome(name="docs", status="used"),
            McpServerOutcome(name="wiki", status="timed_out"),
        ],
        memory_file=MemoryFileSummary(status="loaded", entries=7),
        ci_comments=CiCommentsSummary(posted=1, updated=2, outdated=3, skipped=4),
    )

    assert RunSummary.model_validate_json(summary.model_dump_json()) == summary
    text = render_summary_text(summary)
    assert "prefilter: success (12 ms)" in text
    assert "used ruff; skipped semgrep (not installed); errored trivy (timed out)" in text
    assert "15 tokens over 3 request(s)" in text
    assert "suppressed by memory: 2" in text
    assert "redaction: applied (4 redacted)" in text
    assert "truncation: truncated (2 files cut)" in text
    assert "used docs; timed_out wiki" in text
    assert "memory file: loaded with 7 entries" in text
    assert "1 posted, 2 updated, 3 outdated, 4 skipped" in text


def test_a_failed_redaction_and_a_dedupe_failure_are_stated() -> None:
    summary = _minimal().model_copy(
        update={
            "redaction": RedactionSummary(status="failed"),
            "ci_comments": CiCommentsSummary(dedupe_failure="marker lookup failed"),
        }
    )

    text = render_summary_text(summary)

    assert "redaction: failed, nothing sent" in text
    assert "dedupe failed: marker lookup failed" in text


def test_hostile_strings_cannot_inject_lines_or_control_characters() -> None:
    summary = RunSummary(
        backend=BackendSummary(model="m\nFAKE: line\x1b[31m"),
        analyzers=[AnalyzerOutcome(name="a\nb", status="skipped", reason="x\ry")],
    )

    text = render_summary_text(summary)

    assert "\x1b" not in text
    assert "\nFAKE" not in text
    assert "a b" in text
