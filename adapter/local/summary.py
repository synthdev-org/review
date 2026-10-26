"""adapter.local.summary — the run summary: what a local run did, as data.

It is an *extensible structure*, not a fixed list: later phases of docs/plans/plan-local-mode.md
fill fields that exist from the start (MCP servers, the memory file, CI comments) instead of
reshaping it, and every field says "not reported"/"disabled"/"not available" until its phase sets
it, so a missing value is never mistaken for a clean one. The same model is the ``summary`` of the
``--output`` JSON and, rendered by ``render_summary_text``, the human summary on stderr.

It holds names, statuses and counts only: CI logs of public repositories are public, so no code,
query or finding text may ever appear here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from findings.markdown import strip_display_text

StageStatus = Literal["success", "failed", "timeout", "disabled"]
AnalyzerStatus = Literal["used", "skipped", "errored"]


class StageOutcome(BaseModel):
    name: str
    status: StageStatus
    duration_ms: int | None = None


class AnalyzerOutcome(BaseModel):
    """``skipped`` means review chose not to run it (not installed, disabled, no matching files);
    ``errored`` means it was present and failed or timed out."""

    name: str
    status: AnalyzerStatus
    reason: str | None = None


class BackendSummary(BaseModel):
    kind: Literal["openai_compatible", "claude_cli"] = "openai_compatible"
    model: str
    # True only when the Claude CLI backend ran in a detected CI environment because the explicit
    # override flag was passed (US-L8): the summary always states it.
    ci_guard_overridden: bool = False


class TokenUsageSummary(BaseModel):
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class RedactionSummary(BaseModel):
    status: Literal["not_reported", "applied", "failed"] = "not_reported"
    count: int | None = None
    # Names of files whose content was withheld (deny-listed): names only, never content.
    denied_paths: list[str] = Field(default_factory=list)


class TruncationSummary(BaseModel):
    status: Literal["not_reported", "none", "truncated"] = "not_reported"
    detail: str | None = None


class McpServerOutcome(BaseModel):
    """One nominated MCP server. ``not_consulted`` is a dry run, or a review that stopped before it
    reached the server. ``reason`` is a closed phrase (timeout, protocol error, ...), never server
    text; ``supplied_findings`` are ``path:line`` of the findings that cited its context."""

    name: str
    status: Literal["used", "empty", "timed_out", "errored", "not_consulted"]
    reason: str | None = None
    truncated_items: int = 0
    supplied_findings: list[str] = Field(default_factory=list)


class MemoryFileSummary(BaseModel):
    status: Literal["loaded", "not_found", "disabled"] = "disabled"
    entries: int | None = None


class CiCommentsSummary(BaseModel):
    posted: int = 0
    updated: int = 0
    outdated: int = 0
    skipped: int = 0
    rejected: int = 0  # findings GitHub refused (HTTP 422), e.g. a line outside the diff
    dedupe_failure: str | None = None


class RunSummary(BaseModel):
    stages: list[StageOutcome] = Field(default_factory=list)
    analyzers: list[AnalyzerOutcome] = Field(default_factory=list)
    backend: BackendSummary
    # None: the endpoint reported no usage, which is stated rather than shown as zero.
    token_usage: TokenUsageSummary | None = None
    # None: no memory in this run (disabled), as opposed to a real count of 0.
    suppressed_count: int | None = None
    redaction: RedactionSummary = Field(default_factory=RedactionSummary)
    truncation: TruncationSummary = Field(default_factory=TruncationSummary)
    mcp_servers: list[McpServerOutcome] = Field(default_factory=list)
    memory_file: MemoryFileSummary = Field(default_factory=MemoryFileSummary)
    # None: not a CI run.
    ci_comments: CiCommentsSummary | None = None


def _clean(text: str) -> str:
    return strip_display_text(text)


def _stage_line(stage: StageOutcome) -> str:
    duration = "" if stage.duration_ms is None else f" ({stage.duration_ms} ms)"
    return f"    {_clean(stage.name)}: {stage.status}{duration}"


def _analyzer_text(analyzer: AnalyzerOutcome) -> str:
    reason = "" if analyzer.reason is None else f" ({_clean(analyzer.reason)})"
    return f"{analyzer.status} {_clean(analyzer.name)}{reason}"


def _mcp_text(server: McpServerOutcome) -> str:
    text = f"{server.status} {_clean(server.name)}"
    if server.reason is not None:
        text += f" ({_clean(server.reason)})"
    if server.truncated_items:
        text += f"; {server.truncated_items} item(s) truncated to the size limits"
    if server.supplied_findings:
        text += "; context for " + ", ".join(_clean(f) for f in server.supplied_findings)
    return text


def _redaction_text(redaction: RedactionSummary) -> str:
    if redaction.status == "failed":
        return "failed, nothing sent"
    if redaction.status == "applied":
        text = f"applied ({redaction.count} redacted)"
        if redaction.denied_paths:
            names = ", ".join(_clean(p) for p in redaction.denied_paths)
            text += f"; content not sent for deny-listed paths: {names}"
        return text
    return "not reported"


def render_summary_text(summary: RunSummary) -> str:
    """Plain text, no colour: the stderr summary every run ends with."""
    usage = summary.token_usage
    truncation = summary.truncation
    memory = summary.memory_file
    ci = summary.ci_comments
    lines = [
        "Run summary",
        f"  backend: {summary.backend.kind}, model {_clean(summary.backend.model)}"
        + (
            "; CI guard overridden by --allow-claude-cli-in-ci"
            if summary.backend.ci_guard_overridden
            else ""
        ),
        "  stages:",
        *(_stage_line(s) for s in summary.stages),
        "  analyzers: "
        + ("; ".join(_analyzer_text(a) for a in summary.analyzers) or "none registered"),
        "  token usage: "
        + (
            "not available"
            if usage is None
            else f"{usage.total_tokens} tokens over {usage.requests} request(s)"
        ),
        "  suppressed by memory: "
        + ("not applicable" if summary.suppressed_count is None else str(summary.suppressed_count)),
        f"  redaction: {_redaction_text(summary.redaction)}",
        "  truncation: "
        + (
            truncation.status.replace("_", " ")
            if truncation.detail is None
            else f"{truncation.status} ({_clean(truncation.detail)})"
        ),
        "  mcp servers: "
        + ("; ".join(_mcp_text(s) for s in summary.mcp_servers) or "none consulted"),
        "  memory file: "
        + (
            memory.status.replace("_", " ")
            if memory.entries is None
            else f"{memory.status} with {memory.entries} entries"
        ),
        "  ci comments: "
        + (
            "not applicable"
            if ci is None
            else f"{ci.posted} posted, {ci.updated} updated, {ci.outdated} outdated, "
            f"{ci.skipped} skipped"
            + ("" if ci.rejected == 0 else f"; {ci.rejected} rejected by GitHub (HTTP 422)")
            + ("" if ci.dedupe_failure is None else f"; dedupe failed: {_clean(ci.dedupe_failure)}")
        ),
    ]
    return "\n".join(lines)
