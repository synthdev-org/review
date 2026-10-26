"""adapter.local.egress — ``--dry-run``: what a run would send, and where (US-L14).

There is no parallel reporting path. A dry run executes the real pipeline, so the real request
builders run, against ``RecordingLLMClient``: an ``LLMClient`` that records every
``CompletionRequest`` and sends nothing. The report is computed from those recorded requests, so
it cannot drift from what a real run sends. (Distinct from ``StepContext.dry_run``, which only
suppresses *posting*; local mode uses both together so that a dry run prints no findings.)

The recording client answers every request with ``DRY_RUN_REPLY``: a model that finds nothing.
Requests that depend on what the model says (verification of findings) therefore do not occur and
are not previewed; the report says so. A real run against a model that gives the same reply makes
exactly the requests that were previewed, which is how the equality test is built.

The report holds destinations (URLs with credentials and query values masked), file paths and byte
counts: no code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from adapter.local.summary import TruncationSummary
from findings.markdown import strip_display_text
from ingestion.diff import Diff
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse
from review.diff_prompt import render_diff

DRY_RUN_REPLY = '{"findings": [], "verdicts": []}'


class RecordingLLMClient:
    """Records each request and sends nothing; replies ``DRY_RUN_REPLY``."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(content=DRY_RUN_REPLY, model=request.model)


class EgressFile(BaseModel):
    path: str
    bytes: int
    cut: bool = False


class EgressDestination(BaseModel):
    kind: Literal["model"] = "model"
    target: str
    model: str
    requests: int
    request_bytes: list[int]
    files: list[EgressFile]


class EgressMcpQuery(BaseModel):
    """What a nominated MCP server would be asked: its name, transport, host (never userinfo, path,
    query or headers), the tool if the nomination named one, and the exact query text (already
    redacted). ``note`` says why there is no query."""

    server: str
    transport: str
    host: str | None = None
    tool: str | None = None
    query: str | None = None
    note: str | None = None


class EgressReport(BaseModel):
    destinations: list[EgressDestination]
    ceiling_bytes: int
    truncation: TruncationSummary
    withheld_paths: list[str] = Field(default_factory=list)
    mcp: list[EgressMcpQuery] = Field(default_factory=list)


def mask_url(url: str) -> str:
    """The URL without userinfo or fragment, and with every query value masked: a token in a URL
    must never appear in the report."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    query = urlencode([(k, "***") for k, _ in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def _prompt(request: CompletionRequest) -> str:
    return "\n".join(m.content for m in request.messages)


def _files_in_prompt(prompt: str, sections: Mapping[str, str]) -> dict[str, EgressFile]:
    """The files whose diff section the prompt carries, with the bytes of it that were sent. A
    section is rendered by the same ``render_diff`` the steps use; a section the prompt holds only
    a prefix of was cut by the size ceiling."""
    found: dict[str, EgressFile] = {}
    for path, section in sections.items():
        if section in prompt:
            found[path] = EgressFile(path=path, bytes=len(section.encode("utf-8")))
            continue
        start = prompt.find(section.split("\n", 1)[0] + "\n")
        if start >= 0:
            kept = os.path.commonprefix([section, prompt[start:]])
            found[path] = EgressFile(path=path, bytes=len(kept.encode("utf-8")), cut=True)
    return found


def build_egress_report(
    requests: Sequence[CompletionRequest],
    *,
    endpoint: str,
    diff: Diff,
    ceiling_bytes: int,
    truncation: TruncationSummary,
    withheld_paths: Sequence[str],
    mcp: Sequence[EgressMcpQuery] = (),
) -> EgressReport:
    files: dict[str, EgressFile] = {}
    # Rendered once, not per request: each render and scan is proportional to the whole diff.
    sections = {file.path: render_diff(Diff(files=(file,))) for file in diff.files}
    for request in requests:
        for path, found in _files_in_prompt(_prompt(request), sections).items():
            known = files.get(path)
            if known is None or found.bytes > known.bytes:
                files[path] = found
    destinations = (
        [
            EgressDestination(
                target=mask_url(endpoint),
                model=requests[0].model,
                requests=len(requests),
                request_bytes=[len(_prompt(r).encode("utf-8")) for r in requests],
                files=list(files.values()),
            )
        ]
        if requests
        else []
    )
    return EgressReport(
        destinations=destinations,
        ceiling_bytes=ceiling_bytes,
        truncation=truncation,
        withheld_paths=list(withheld_paths),
        mcp=list(mcp),
    )


def render_egress_text(report: EgressReport) -> str:
    """Plain text for stdout. Nothing was sent."""
    lines = ["Dry run: nothing was sent. This is what a real run would send."]
    if not report.destinations:
        lines.append("No request would be made.")
    for destination in report.destinations:
        lines.append(
            f"Model endpoint {strip_display_text(destination.target)} "
            f"(model {strip_display_text(destination.model)}): {destination.requests} request(s), "
            f"{sum(destination.request_bytes)} bytes in total"
        )
        for file in destination.files:
            cut = " (cut at the size ceiling)" if file.cut else ""
            path = strip_display_text(file.path)
            if path.startswith(":"):  # a leading "::" would be a workflow command in CI logs
                path = "./" + path
            lines.append(f"  {path}: {file.bytes} bytes{cut}")
    lines.append(f"Size ceiling: {report.ceiling_bytes} bytes of diff per request")
    detail = report.truncation.detail
    lines.append(
        "Truncation: "
        + (
            report.truncation.status.replace("_", " ")
            if detail is None
            else f"{report.truncation.status} ({strip_display_text(detail)})"
        )
    )
    if report.withheld_paths:
        names = ", ".join(strip_display_text(p) for p in report.withheld_paths)
        lines.append(f"Content withheld (deny-listed paths): {names}")
    if not report.mcp:
        lines.append("MCP servers: none consulted")
    for server in report.mcp:
        where = server.transport if server.host is None else f"{server.transport}, {server.host}"
        tool = (
            "the tool is chosen from the server's tool list at run time"
            if server.tool is None
            else f"tool {server.tool}"
        )
        asked = (
            f"query {strip_display_text(server.query)!r}"
            if server.query is not None
            else f"no query ({strip_display_text(server.note or 'none could be built')})"
        )
        lines.append(
            f"MCP server {strip_display_text(server.server)} ({strip_display_text(where)}): "
            f"{tool}; {asked}"
        )
    if report.mcp:
        lines.append("Not previewed: what an MCP server answers, which then enters the prompt.")
    lines.append(
        "Not previewed: requests that depend on the model's findings (verification), "
        "which a model that reports nothing would not trigger."
    )
    return "\n".join(lines)


_: type[LLMClient] = RecordingLLMClient
