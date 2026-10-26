"""Unit tests for the places MCP knowledge shows up in local mode's own outputs: the dry-run egress
report, the run summary, the findings printed to the terminal and the markdown report, the batched
input redaction of MCP text, and the stand-in index the context builder runs on."""

from __future__ import annotations

import io
from collections.abc import Sequence

import pytest

from adapter.local.egress import EgressMcpQuery, build_egress_report, render_egress_text
from adapter.local.exceptions import RedactionFailedError
from adapter.local.redaction import REDACTION_MARKER, redact_texts
from adapter.local.report import Report, render_markdown
from adapter.local.summary import (
    BackendSummary,
    McpServerOutcome,
    RunSummary,
    TruncationSummary,
    render_summary_text,
)
from adapter.local.terminal import TerminalFindingsPublisher
from adapter.local.unavailable import EmptyIndexServices, UnavailableIndexServices
from findings.models import Finding
from ingestion.diff import Diff


def _egress(*servers: EgressMcpQuery) -> str:
    report = build_egress_report(
        [],
        endpoint="http://m/v1",
        diff=Diff(files=()),
        ceiling_bytes=1000,
        truncation=TruncationSummary(status="none"),
        withheld_paths=[],
        mcp=servers,
    )
    return render_egress_text(report)


def test_the_dry_run_says_none_consulted_without_nominations() -> None:
    assert "MCP servers: none consulted" in _egress()


def test_the_dry_run_lists_each_server_its_transport_host_tool_and_exact_query() -> None:
    text = _egress(
        EgressMcpQuery(server="docs", transport="stdio", tool="search_docs", query="a/b.py run"),
        EgressMcpQuery(server="kb", transport="http", host="kb.example.test", query="a/b.py run"),
        EgressMcpQuery(server="idle", transport="stdio", note="nothing in the diff to ask about"),
    )

    assert "MCP server docs (stdio): tool search_docs; query 'a/b.py run'" in text
    assert (
        "MCP server kb (http, kb.example.test): the tool is chosen from the server's tool list at "
        "run time; query 'a/b.py run'"
    ) in text
    assert "MCP server idle (stdio): the tool is chosen" in text
    assert "no query (nothing in the diff to ask about)" in text
    assert "MCP servers: none consulted" not in text
    assert "what an MCP server answers" in text  # the answer is not previewed, and it says so


def test_a_hostile_query_or_name_cannot_inject_lines_into_the_report() -> None:
    text = _egress(EgressMcpQuery(server="d\nFAKE", transport="stdio", query="q\x1b[31m\nFAKE2"))

    assert "\x1b" not in text
    assert "\nFAKE" not in text


def test_the_summary_states_status_reason_truncation_and_which_findings_a_server_backed() -> None:
    summary = RunSummary(
        backend=BackendSummary(model="m"),
        mcp_servers=[
            McpServerOutcome(
                name="docs", status="used", truncated_items=2, supplied_findings=["a.py:3", "b.py"]
            ),
            McpServerOutcome(name="wiki", status="timed_out", reason="timeout"),
            McpServerOutcome(name="kb", status="not_consulted", reason="dry run"),
        ],
    )

    line = next(x for x in render_summary_text(summary).splitlines() if "mcp servers" in x)

    assert line == (
        "  mcp servers: used docs; 2 item(s) truncated to the size limits; context for a.py:3, b.py"
        "; timed_out wiki (timeout); not_consulted kb (dry run)"
    )


@pytest.mark.asyncio
async def test_the_terminal_prints_a_note_under_a_finding_only_when_there_is_one() -> None:
    stream = io.StringIO()
    publisher = TerminalFindingsPublisher(
        stream, annotate=lambda f: "context from MCP server docs" if f.line == 1 else None
    )
    first = Finding(file="a.py", line=1, severity="low", message="m", source="llm")
    second = Finding(file="a.py", line=2, severity="low", message="n", source="llm")

    await publisher.post_review(
        installation_id=0,
        repo_full_name="r",
        repo_id="r",
        pr_number=0,
        head_sha="h",
        findings=[first, second],
    )

    assert stream.getvalue().splitlines() == [
        "a.py:1: low: m",
        "  context from MCP server docs",
        "a.py:2: low: n",
    ]


def test_the_markdown_report_lists_which_server_backed_which_findings() -> None:
    finding = Finding(file="a.py", line=1, severity="low", message="m", source="llm")
    summary = RunSummary(
        backend=BackendSummary(model="m"),
        mcp_servers=[McpServerOutcome(name="docs", status="used", supplied_findings=["a.py:1"])],
    )

    text = render_markdown(Report(findings=[finding], summary=summary))

    assert "Context from MCP servers:" in text and "- docs: a\\.py:1" in text


class _Finder:
    def __init__(self, *, fail: bool = False, short: bool = False) -> None:
        self.fail, self.short = fail, short
        self.seen: list[list[str]] = []

    async def secret_lines(self, texts: Sequence[str]) -> list[frozenset[int]]:
        self.seen.append(list(texts))
        if self.fail:
            raise OSError("scanner missing")
        if self.short:
            return []
        return [
            frozenset(n for n, line in enumerate(t.split("\n"), 1) if "SECRET" in line)
            for t in texts
        ]


@pytest.mark.asyncio
async def test_redact_texts_replaces_flagged_lines_in_one_batched_scan() -> None:
    finder = _Finder()

    texts, count = await redact_texts(finder, ["fine\nkey = SECRET1\nend", "clean", "SECRET2"])

    assert texts == [f"fine\n{REDACTION_MARKER}\nend", "clean", REDACTION_MARKER]
    assert count == 2 and len(finder.seen) == 1  # one scan for all of it
    assert await redact_texts(finder, []) == ([], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("finder", [_Finder(fail=True), _Finder(short=True)])
async def test_redact_texts_fails_closed(finder: _Finder) -> None:
    with pytest.raises(RedactionFailedError):
        await redact_texts(finder, ["anything"])


@pytest.mark.asyncio
async def test_the_empty_index_finishes_the_code_branch_empty() -> None:
    empty, unavailable = EmptyIndexServices(), UnavailableIndexServices()

    assert await empty.fetch_tree(installation_id=0, repo_full_name="r", commit_sha="c") == ()
    assert await empty.find_callers(repo_id="r", symbol_names=["f"], limit_per_symbol=1) == {}
    assert len(empty.embed(["a", "b"])) == 2  # one vector per text, so the batch stays aligned
    with pytest.raises(Exception, match="not available in local mode"):
        await unavailable.fetch_tree(installation_id=0, repo_full_name="r", commit_sha="c")
