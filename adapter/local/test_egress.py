"""Unit tests for the dry-run egress report (US-L14): pure construction and rendering."""

from __future__ import annotations

import pytest

from adapter.local.egress import (
    DRY_RUN_REPLY,
    EgressReport,
    RecordingLLMClient,
    build_egress_report,
    mask_url,
    render_egress_text,
)
from adapter.local.summary import TruncationSummary
from ingestion.diff import Diff, DiffFile
from llmgateway.models import CompletionRequest, Message
from review.diff_prompt import render_diff, truncate_diff_text

_DIFF = Diff(
    files=(
        DiffFile(path="a.py", status="modified", patch="@@ -1 +1 @@\n-a\n+b"),
        DiffFile(path="b.py", status="added", patch="@@ -0,0 +1 @@\n+" + "x" * 100),
        DiffFile(path="c.py", status="added", patch="@@ -0,0 +1 @@\n+c"),
    )
)


def _request(diff_text: str) -> CompletionRequest:
    return CompletionRequest(
        model="m",
        messages=[
            Message(role="system", content="rules"),
            Message(role="user", content=f"Diff:\n{diff_text}"),
        ],
    )


def _report(diff_text: str, *, ceiling: int = 10_000) -> EgressReport:
    return build_egress_report(
        [_request(diff_text)],
        endpoint="http://user:pw@host:1234/v1?api_key=SECRET",
        diff=_DIFF,
        ceiling_bytes=ceiling,
        truncation=TruncationSummary(status="none"),
        withheld_paths=[".env"],
    )


@pytest.mark.asyncio
async def test_the_recording_client_records_and_replies_with_the_fixed_reply() -> None:
    client = RecordingLLMClient()
    request = _request("x")

    response = await client.complete(request, timeout=1)

    assert client.requests == [request] and response.content == DRY_RUN_REPLY


def test_urls_are_masked_of_credentials_and_query_values() -> None:
    masked = mask_url("https://user:pw@api.example.com:8443/v1?api_key=SECRET&x=1#frag")

    assert masked == "https://api.example.com:8443/v1?api_key=%2A%2A%2A&x=%2A%2A%2A"
    assert "SECRET" not in masked and "pw" not in masked


def test_every_file_in_the_prompt_is_listed_with_its_bytes() -> None:
    report = _report(render_diff(_DIFF))

    (destination,) = report.destinations
    assert [f.path for f in destination.files] == ["a.py", "b.py", "c.py"]
    assert not any(f.cut for f in destination.files)
    assert sum(f.bytes for f in destination.files) + 2 * 2 == len(render_diff(_DIFF).encode())
    assert "SECRET" not in report.model_dump_json()


def test_a_file_cut_by_the_ceiling_is_flagged_and_later_files_are_absent() -> None:
    full = render_diff(_DIFF)
    cut, _, _ = truncate_diff_text(full, full.index("xxxx") + 10)

    (destination,) = _report(cut).destinations

    assert [(f.path, f.cut) for f in destination.files] == [("a.py", False), ("b.py", True)]


def test_the_text_report_states_destination_files_ceiling_and_withheld_paths() -> None:
    text = render_egress_text(_report(render_diff(_DIFF), ceiling=777))

    assert "nothing was sent" in text
    assert "http://host:1234/v1" in text and "pw" not in text
    assert "a.py:" in text and "Size ceiling: 777 bytes" in text
    assert "Content withheld (deny-listed paths): .env" in text
    assert "MCP servers: none consulted" in text


def test_a_run_that_makes_no_request_says_so() -> None:
    report = build_egress_report(
        [],
        endpoint="http://h/v1",
        diff=_DIFF,
        ceiling_bytes=1,
        truncation=TruncationSummary(status="none"),
        withheld_paths=[],
    )

    assert "No request would be made." in render_egress_text(report)
