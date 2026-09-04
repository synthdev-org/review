"""adapter.github.posting — the concrete ``review.output.FindingsPublisher``
implementation: posts one batched PR review (every inline comment in a
single API call, per both PRDs' NFR batching decision) and a check-run
(``"neutral"`` on the happy path, ``"failure"`` on the terminal-step failure
path), per the plan's Phase 7 section.

**No new auth code, no new config, of its own.** Constructor-injects the
same ``adapter.github.auth.GitHubInstallationTokenProvider`` instance Phase
4 already built and the composition root (``apps/worker/main.py``) already
wires into ``adapter.github.diff.GitHubDiffFetcher`` — sharing the one
``(installation_id, repo_full_name)``-keyed token cache rather than a
second, independent provider instance. See ``adapter/github/auth.py``'s own
module docstring for the full "why here" reasoning on why this phase needs
none of its own.

**Idempotency-scoped retries, not the same unconditional policy
``diff.py``/``auth.py`` get.** Both writes here (create PR review, create
check-run) are *not* documented as idempotent by GitHub's API — every call
routes through ``adapter/github/http.py``'s shared ``request_with_retry``
with ``safe_writes_only=True``, its own narrower Phase 7 addition: only a
429/secondary-rate-limit response or a pre-send connection failure is
retried; a bare 5xx or any other ambiguous connection failure fails fatally
on the first occurrence, since GitHub may already have processed the write.
See that module's own docstring for the full policy.

**Markdown-escaping, on both surfaces.** Every piece of ``Finding``-derived
or failure-message-derived text this module writes into a comment body or a
check-run summary is passed through ``findings.markdown.escape_markdown``
first — no raw PR-derived or LLM-derived text reaches GitHub's renderer
unescaped, per this plan's own Security review.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import (
    GitHubPermanentAPIError,
    GitHubPostingMalformedRequestError,
    GitHubPostingUnreachableError,
    GitHubRetryExhaustedError,
)
from adapter.github.http import auth_headers, client_context, request_with_retry, split_owner_repo
from findings.markdown import escape_markdown
from findings.models import Finding
from review.output import FindingsPublisher

logger = logging.getLogger(__name__)

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
_CHECK_RUN_NAME = "review"
_REVIEW_EVENT = "COMMENT"
"""Always ``COMMENT``, never ``APPROVE``/``REQUEST_CHANGES`` — Phase 1 never
blocks or approves a merge based on unverified single-pass output, per both
PRDs' confirmed "conclusion always neutral" scope decision applied to the
review submission's own event type too."""


def _finding_body(finding: Finding) -> str:
    severity = escape_markdown(finding.severity)
    message = escape_markdown(finding.message)
    detail = f"**[{severity}]** {message}"
    if finding.rule_id:
        # Not wrapped in literal backticks: CommonMark/GFM resolves code-span
        # delimiters *before* backslash escapes are interpreted, so a raw
        # backtick pair here could still be split by an escaped backtick
        # inside `escape_markdown`'s output, reopening exactly the injection
        # this function exists to close.
        detail += f" ({escape_markdown(finding.rule_id)})"
    detail += f"\n\n_source: {escape_markdown(finding.source)}_"
    return detail


def _review_comments(line_findings: list[Finding]) -> list[dict[str, Any]]:
    """GitHub's create-review API requires a ``line`` (or ``position``) per
    inline comment — only findings with a real ``line`` can be posted this
    way; a file-level finding (``line is None``) is folded into the
    review's own top-level ``body`` instead (see ``_review_body`` below)."""
    return [
        {"path": finding.file, "line": finding.line, "body": _finding_body(finding)}
        for finding in line_findings
    ]


def _review_body(file_level_findings: list[Finding], total_count: int) -> str:
    summary = escape_markdown(f"Automated review found {total_count} finding(s).")
    if not file_level_findings:
        return summary
    lines = [summary, "", "Findings not attributable to a specific line:"]
    for finding in file_level_findings:
        lines.append(f"- **{escape_markdown(finding.file)}**: {_finding_body(finding)}")
    return "\n".join(lines)


def _split_owner_repo(repo_full_name: str) -> tuple[str, str]:
    """Wraps ``adapter/github/http.py``'s shared ``split_owner_repo`` in
    this module's own typed error instead of an unhandled ``ValueError``.
    Especially important here: an unguarded crash on the terminal-step
    failure-signal path (``post_failure_check_run``) would silently defeat
    this platform's own "every job gets GitHub-visible output" guarantee —
    ``review/pipeline.py``'s ``_run_terminal_step_on_failure`` only logs a
    double-failure error for an exception here, it doesn't retry with a
    different value.
    """
    try:
        return split_owner_repo(repo_full_name)
    except ValueError as exc:
        raise GitHubPostingMalformedRequestError(detail=str(exc)) from exc


class GitHubFindingsPublisher:
    """``review.output.FindingsPublisher`` implementation backed by
    GitHub's REST API. Constructed once at the composition root
    (``apps/worker/main.py``) with the same ``GitHubInstallationTokenProvider``
    ``adapter.github.diff.GitHubDiffFetcher`` already holds.
    """

    def __init__(
        self,
        *,
        token_provider: GitHubInstallationTokenProvider,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._github_api_base_url = github_api_base_url
        self._timeout = timeout
        self._max_retry_attempts = max_retry_attempts
        # Optional shared, long-lived client (constructed once at the
        # composition root — apps/worker/main.py) — reusing it keeps HTTP
        # keep-alive/TLS session resumption across calls instead of paying a
        # fresh TCP+TLS handshake per GitHub API call. Defaults to None so
        # existing callers/tests that don't inject one keep the prior
        # per-call `httpx.AsyncClient` behavior unchanged.
        self._client = client

    async def post_review(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        pr_number: int,
        findings: list[Finding],
    ) -> None:
        line_findings = [finding for finding in findings if finding.line is not None]
        file_level_findings = [finding for finding in findings if finding.line is None]
        body = {
            "event": _REVIEW_EVENT,
            "body": _review_body(file_level_findings, len(findings)),
            "comments": _review_comments(line_findings),
        }
        owner, repo = _split_owner_repo(repo_full_name)
        await self._post(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            url=f"{self._github_api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json_body=body,
        )

    async def post_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        finding_count: int,
    ) -> None:
        summary = escape_markdown(
            "No findings." if finding_count == 0 else f"{finding_count} finding(s) posted."
        )
        await self._post_check_run(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            conclusion="neutral",
            title="Automated review",
            summary=summary,
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
        summary = escape_markdown(
            f"The automated review did not complete: step '{failed_step_name}' failed ({error})."
        )
        await self._post_check_run(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            conclusion="failure",
            title="Automated review failed",
            summary=summary,
        )

    async def _post_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        conclusion: str,
        title: str,
        summary: str,
    ) -> None:
        body = {
            "name": _CHECK_RUN_NAME,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
        }
        owner, repo = _split_owner_repo(repo_full_name)
        await self._post(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            url=f"{self._github_api_base_url}/repos/{owner}/{repo}/check-runs",
            json_body=body,
        )

    async def _post(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        url: str,
        json_body: dict[str, Any],
    ) -> None:
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        try:
            async with client_context(self._client, timeout=self._timeout) as client:
                await request_with_retry(
                    client,
                    "POST",
                    url,
                    headers=headers,
                    json=json_body,
                    max_attempts=self._max_retry_attempts,
                    safe_writes_only=True,
                )
        except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
            raise GitHubPostingUnreachableError(detail=str(exc)) from exc


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[FindingsPublisher] = GitHubFindingsPublisher
