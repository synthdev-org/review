"""adapter.local.terminal — ``review.output.FindingsPublisher`` that prints findings as plain text.

Findings go to the stream it is given (stdout, so they can be piped); progress and logs go to
stderr elsewhere. Text is cleaned with ``findings.markdown.strip_display_text`` (A8 of
docs/plans/plan-local-mode.md), which drops control characters, so a finding built from a hostile
diff cannot inject terminal escape sequences or extra lines. No colour: nothing is conveyed by it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from findings.markdown import strip_display_text
from findings.models import Finding
from review.output import FindingsPublisher


def render_finding(finding: Finding) -> str:
    """``path:line: severity: message (rule id)`` on one line; the line is omitted for a
    file-level finding."""
    location = strip_display_text(finding.file)
    # A line starting "::" is a GitHub Actions workflow command; the path is PR-controlled.
    if location.startswith(":"):
        location = "./" + location
    if finding.line is not None:
        location += f":{finding.line}"
    text = f"{location}: {finding.severity}: {strip_display_text(finding.message)}"
    if finding.rule_id:
        text += f" ({strip_display_text(finding.rule_id)})"
    return text


class TerminalFindingsPublisher:
    def __init__(
        self, stream: TextIO, *, annotate: Callable[[Finding], str | None] | None = None
    ) -> None:
        self._stream = stream
        # A line printed under a finding (for example which MCP server supplied its context).
        self._annotate = annotate
        # What was printed, so the same findings can be written to the --output file.
        self.findings: list[Finding] = []

    async def post_review(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        findings: list[Finding],
    ) -> None:
        for finding in findings:
            self.findings.append(finding)
            print(render_finding(finding), file=self._stream)
            note = None if self._annotate is None else self._annotate(finding)
            if note:
                # A leading ``::`` would be a workflow command once the runner trims the indent.
                print(f"  {strip_display_text(note).lstrip(':')}", file=self._stream)

    async def post_check_run(
        self, *, installation_id: int, repo_full_name: str, head_sha: str, finding_count: int
    ) -> None:
        # Called whether or not there are findings, so this is the one place the run's result is
        # always stated.
        print(
            "No findings." if finding_count == 0 else f"{finding_count} finding(s).",
            file=self._stream,
        )

    async def post_failure_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        failed_step_name: str,
        error: str,
    ) -> None:
        # Nothing to print: the failure reaches the user as the pipeline's exception, which the
        # entry point reports on stderr with a non-zero exit status.
        return None


# Runtime confirmation that this class satisfies the Protocol it is built to implement.
_: type[FindingsPublisher] = TerminalFindingsPublisher
