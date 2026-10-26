"""adapter.local.report — the findings file written by ``--output``.

JSON is the published, machine-readable contract: ``Report`` embeds ``findings.models.Finding``, so
its JSON schema is generated from ``Finding.model_json_schema()`` and carries a top-level
``schema_version``. Within a major version changes are additive; a breaking change is a new major
(``SCHEMA_VERSION``). ``docs/reference/review-report.schema.json`` is the committed copy and a
test fails if it drifts from the generated one.

Markdown reuses ``findings.markdown`` (plan A8) so untrusted finding text cannot form links,
images, mentions or headings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from adapter.local.summary import RunSummary
from findings.markdown import break_autolinks, escape_markdown, strip_display_text
from findings.models import Finding

SCHEMA_VERSION: Literal["1.0"] = "1.0"


class Report(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    findings: list[Finding]
    summary: RunSummary


def render_json(report: Report) -> str:
    return report.model_dump_json(indent=2) + "\n"


def _finding_line(finding: Finding) -> str:
    location = escape_markdown(strip_display_text(finding.file))
    if finding.line is not None:
        location += f":{finding.line}"
    text = (
        f"- **{location}** **[{escape_markdown(finding.severity)}]** "
        f"{escape_markdown(break_autolinks(strip_display_text(finding.message)))}"
    )
    if finding.rule_id:
        text += f" ({escape_markdown(strip_display_text(finding.rule_id))})"
    return text


def render_markdown(report: Report) -> str:
    if not report.findings:
        return "# Code review\n\nNo findings.\n"
    lines = ["# Code review", "", f"{len(report.findings)} finding(s).", ""]
    lines.extend(_finding_line(f) for f in report.findings)
    supplied = [s for s in report.summary.mcp_servers if s.supplied_findings]
    if supplied:
        lines += ["", "Context from MCP servers:"]
        for server in supplied:
            where = ", ".join(
                escape_markdown(strip_display_text(f)) for f in server.supplied_findings
            )
            lines.append(f"- {escape_markdown(strip_display_text(server.name))}: {where}")
    return "\n".join(lines) + "\n"
