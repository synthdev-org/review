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

**Provenance persistence (``docs/plans/plan-deliver-memory.md``'s Phase 2,
US-M11).** After a successful ``post_review`` call, this class issues one
follow-up ``GET .../reviews/{review_id}/comments`` call — GitHub's
create-review response carries no per-comment ids of its own (that plan's
own Assumption 2, checked against GitHub's REST API docs during drafting) —
and correlates each returned comment back to the ``Finding`` it came from by
matching ``(path, line, body)`` against ``_review_comments``'s own submitted
shape, then hands the matched batch to ``provenance_recorder`` in one call.
Provenance capture is a secondary write-path concern: a fetch failure or an
individual unmatched comment is logged, never raised — the already-succeeded
review post is never retroactively failed because of it. File-level findings
(no ``line``) are never eligible; only ``_review_comments``'s own
line-attributed findings are.
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
from memory.models import PostedComment
from memory.store import MemorySignalRecorder, MemoryStoreError
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
    if finding.citation_markdown:
        # Deliberately NOT passed through `escape_markdown` again:
        # `review.citation.render_citation` already escaped its own embedded
        # file-path/symbol values and built the rest of this string as
        # literal, trusted Markdown template syntax (backticks, bold) — a
        # second blanket escape pass here would backslash-escape that
        # template too, rendering the citation as literal unformatted text
        # instead of the intended bold/code styling. See
        # `Finding.citation_markdown`'s own docstring for the full reasoning.
        detail += f"\n\n{finding.citation_markdown}"
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
        provenance_recorder: MemorySignalRecorder,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._provenance_recorder = provenance_recorder
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
        repo_id: str,
        pr_number: int,
        head_sha: str,
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
        response = await self._post(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            url=f"{self._github_api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json_body=body,
        )

        # File-level-only findings never posted a line comment in the first
        # place (see `_review_comments` above) — nothing to correlate, and
        # no reason to spend a second GitHub API call finding that out.
        if not line_findings:
            return

        try:
            await self._record_provenance(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                repo_id=repo_id,
                pr_number=pr_number,
                commit_sha=head_sha,
                review_response=response,
                line_findings=line_findings,
                owner=owner,
                repo=repo,
            )
        except Exception as exc:
            # Belt-and-braces on top of `_record_provenance`'s own internal
            # handling below: this module's docstring and this plan's Phase
            # 2 section both promise provenance capture "never raises" —
            # *every* failure mode in this follow-up path (a malformed
            # `review_id`/comments-GET response body, a second token-fetch
            # failure, a `KeyError` on an unexpected comment shape, ...),
            # not just the ones `_record_provenance` explicitly anticipates.
            # `post_review`'s own review post already succeeded by this
            # point and must never be retroactively failed — and must never
            # crash `FindingsOutputStep`, this pipeline's terminal step,
            # with no GitHub-visible failure signal — because this
            # secondary write-path concern broke in a way nothing here
            # anticipated.
            logger.warning(
                "posting: provenance capture failed unexpectedly; the review post itself "
                "already succeeded and is unaffected",
                extra={"pr_number": pr_number, "error": str(exc)},
            )

    async def _record_provenance(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        repo_id: str,
        pr_number: int,
        commit_sha: str,
        review_response: httpx.Response,
        line_findings: list[Finding],
        owner: str,
        repo: str,
    ) -> None:
        """The Phase 2 (US-M11) follow-up: fetch the ids GitHub assigned the
        comments this call just posted, correlate them back to the
        `Finding`s that produced them, and persist one batched
        `posted_finding_comment` row per matched comment.

        Never raises — a fetch failure, a correlation gap, or a storage
        failure here is logged and swallowed; the review post it's
        attributing already succeeded and must not be retroactively failed
        because a secondary write-path concern broke. See this module's own
        docstring's "Provenance persistence" section. (`post_review`'s own
        broad `except Exception` around this call is the final backstop for
        any failure mode not explicitly anticipated below — e.g. a
        malformed `review_id` in `review_response`'s own body, or a second
        token-fetch failure — but every failure this method *does* expect is
        still handled at its own, more specific level first.)
        """
        try:
            review_id = review_response.json()["id"]
            token = await self._token_provider.get_token(
                installation_id=installation_id, repo_full_name=repo_full_name
            )
            headers = auth_headers(token)
            url = (
                f"{self._github_api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
                f"/reviews/{review_id}/comments"
            )
            async with client_context(self._client, timeout=self._timeout) as client:
                response = await request_with_retry(
                    client,
                    "GET",
                    url,
                    headers=headers,
                    max_attempts=self._max_retry_attempts,
                )
        except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
            logger.warning(
                "posting: failed to fetch posted review comments for provenance capture",
                extra={"pr_number": pr_number, "error": str(exc)},
            )
            return

        # A list per key, not a single `Finding`: two distinct findings can
        # legitimately render an identical `(file, line, body)` tuple (e.g.
        # two rules producing the same severity/message/source/citation on
        # the same line). A single-value dict would let the second finding
        # silently overwrite the first in the comprehension below, so both
        # of GitHub's echoed-back comments for that line would incorrectly
        # correlate to only the later finding — losing the first finding's
        # provenance and mis-attributing a `dismissed_finding` approval on
        # its comment to the wrong finding's fingerprint. Matching one
        # comment per submission in submission order (GitHub echoes review
        # comments back in the order they were submitted) keeps duplicates
        # correctly disambiguated.
        submitted: dict[tuple[str, int | None, str], list[Finding]] = {}
        for finding in line_findings:
            key = (finding.file, finding.line, _finding_body(finding))
            submitted.setdefault(key, []).append(finding)

        matched: list[PostedComment] = []
        unmatched_count = 0
        for raw_comment in response.json():
            key = (raw_comment.get("path"), raw_comment.get("line"), raw_comment.get("body"))
            candidates = submitted.get(key)
            matched_finding = candidates.pop(0) if candidates else None
            comment_id = raw_comment.get("id")
            if matched_finding is None or comment_id is None:
                unmatched_count += 1
                logger.warning(
                    "posting: a posted review comment did not correlate to any submitted finding",
                    extra={"pr_number": pr_number, "comment_id": comment_id},
                )
                continue
            matched.append(
                PostedComment(
                    comment_id=comment_id,
                    file=matched_finding.file,
                    line=matched_finding.line,
                    rule_id=matched_finding.rule_id,
                    message=matched_finding.message,
                )
            )

        # Aggregated, not just per-occurrence, so a persistent breakage
        # (e.g. a GitHub response-shape change breaking every correlation)
        # is actually noticeable to an operator/alert rule watching for a
        # sustained non-zero `unmatched_count`, without needing to grep
        # individual per-comment warnings above.
        summary_extra = {
            "pr_number": pr_number,
            "matched_count": len(matched),
            "unmatched_count": unmatched_count,
        }
        if unmatched_count > 0:
            logger.warning("posting: provenance capture correlation summary", extra=summary_extra)
        else:
            logger.info("posting: provenance capture correlation summary", extra=summary_extra)

        if not matched:
            return

        try:
            await self._provenance_recorder.record_comment_provenance(
                repo_id=repo_id, pr_number=pr_number, commit_sha=commit_sha, comments=matched
            )
        except MemoryStoreError as exc:
            logger.warning(
                "posting: failed to persist comment provenance",
                extra={"pr_number": pr_number, "error": str(exc)},
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
    ) -> httpx.Response:
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        try:
            async with client_context(self._client, timeout=self._timeout) as client:
                return await request_with_retry(
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
