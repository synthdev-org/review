"""adapter.github.posting — the concrete ``review.output.FindingsPublisher``
implementation: posts one batched PR review (every inline comment in a
single API call, per both PRDs' NFR batching decision) and a check-run
(``"neutral"`` on the happy path, ``"failure"`` on the terminal-step failure
path).

**No new auth code, no new config, of its own.** Constructor-injects the
same ``adapter.github.auth.GitHubInstallationTokenProvider`` instance
the auth module already provides and the composition root (``apps/worker/main.py``) already
wires into ``adapter.github.diff.GitHubDiffFetcher`` — sharing the one
``(installation_id, repo_full_name)``-keyed token cache rather than a
second, independent provider instance. See ``adapter/github/auth.py``'s own
module docstring for the full "why here" reasoning on why this module needs
none of its own.

**Idempotency-scoped retries, not the same unconditional policy
``diff.py``/``auth.py`` get.** Both writes here (create PR review, create
check-run) are *not* documented as idempotent by GitHub's API — every call
routes through ``adapter/github/http.py``'s shared ``request_with_retry``
with ``safe_writes_only=True``, its own narrower addition: only a
429/secondary-rate-limit response or a pre-send connection failure is
retried; a bare 5xx or any other ambiguous connection failure fails fatally
on the first occurrence, since GitHub may already have processed the write.
See that module's own docstring for the full policy.

**Markdown-escaping, on both surfaces.** Every piece of ``Finding``-derived
or failure-message-derived text this module writes into a comment body or a
check-run summary is passed through ``findings.markdown.escape_markdown``
first — no raw PR-derived or LLM-derived text reaches GitHub's renderer
unescaped.

**Provenance persistence (US-M11).** After a successful ``post_review`` call, this class issues one
follow-up ``GET .../reviews/{review_id}/comments`` call — GitHub's
create-review response carries no per-comment ids of its own —
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
import re
from typing import Any

import httpx
from opentelemetry import metrics

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import (
    GitHubPermanentAPIError,
    GitHubPostingMalformedRequestError,
    GitHubPostingUnreachableError,
    GitHubRetryExhaustedError,
)
from adapter.github.http import auth_headers, client_context, request_with_retry
from adapter.telemetry.sdk import suppress_telemetry_errors
from findings.agent_prompt import render_agent_prompt
from findings.markdown import escape_markdown
from findings.models import Finding
from memory.models import PostedComment
from memory.store import MemorySignalRecorder, MemoryStoreError
from review.output import FindingsPublisher
from tenancy.resolution import split_owner_repo

logger = logging.getLogger(__name__)

# Catalog: docs/specs/spec-observability.md. Failures here are otherwise only log lines.
_meter = metrics.get_meter("review.github")
_post_counter = _meter.create_counter(
    "review.github.post_total",
    description="GitHub write outcomes: review and check-run posts, plus provenance failures.",
)
_truncated_counter = _meter.create_counter(
    "review.github.post_truncated_total",
    description="Posted content cut or moved to stay within GitHub size limits.",
)


def _record_post(operation: str, outcome: str) -> None:
    """`operation` ∈ {review, check_run, provenance_fetch, provenance_persist,
    provenance_unexpected}; `outcome` ∈ {ok, permanent_error, retry_exhausted, other}.
    Provenance operations record failures only (the `review` series is the denominator).
    """
    with suppress_telemetry_errors(logger, "github post metric recording failed"):
        _post_counter.add(1, {"operation": operation, "outcome": outcome})


def _record_truncated(kind: str) -> None:
    """`kind` ∈ {comment, body_hard, file_level_overflow}."""
    with suppress_telemetry_errors(logger, "github truncation metric recording failed"):
        _truncated_counter.add(1, {"kind": kind})


_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
_CHECK_RUN_NAME = "review"
_REVIEW_EVENT = "COMMENT"
"""Always ``COMMENT``, never ``APPROVE``/``REQUEST_CHANGES`` — this platform never
blocks or approves a merge based on unverified single-pass output, per both
PRDs' confirmed "conclusion always neutral" scope decision applied to the
review submission's own event type too."""


# Collapsed-summary bounds (v2): keeps the review body far below GitHub's
# 65,536-character limit however many findings were folded into it.
_COLLAPSED_MAX_ITEMS = 50
_COLLAPSED_MESSAGE_CHARS = 200
_COLLAPSED_PATH_CHARS = 200
# GitHub rejects a review body over 65,536 chars; stay well under it.
_BODY_HARD_LIMIT = 60_000
_FILE_LEVEL_BUDGET_CHARS = 30_000
# One inline comment may not exceed GitHub's 65,536 chars (a 422 rejects the whole
# batched review); v2 carries the message twice (escaped + agent prompt).
_COMMENT_HARD_LIMIT = 20_000


def _finding_extras(finding: Finding) -> str:
    """The v2-only scenario and other-locations paragraphs."""
    out = ""
    if finding.scenario:
        out += f"\n\n**Scenario:** {escape_markdown(finding.scenario)}"
    if finding.other_locations:
        also = ", ".join(escape_markdown(loc) for loc in finding.other_locations)
        out += f"\n\n_Also at: {also}_"
    return out


def _finding_body(finding: Finding, *, v2: bool = False) -> str:
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
    if v2:
        detail += _finding_extras(finding)
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
    if v2:
        detail += f"\n\n{render_agent_prompt(finding)}"
        if len(detail) > _COMMENT_HARD_LIMIT:  # v1 stays byte-identical
            logger.warning("posting: finding body truncated", extra={"length": len(detail)})
            _record_truncated("comment")
            detail = _truncate_body(detail, _COMMENT_HARD_LIMIT)
    return detail


def _review_comments(line_findings: list[Finding], *, v2: bool = False) -> list[dict[str, Any]]:
    """GitHub's create-review API requires a ``line`` (or ``position``) per
    inline comment — only findings with a real ``line`` can be posted this
    way; a file-level finding (``line is None``) is folded into the
    review's own top-level ``body`` instead (see ``_review_body`` below).
    """
    return [
        {"path": finding.file, "line": finding.line, "body": _finding_body(finding, v2=v2)}
        for finding in line_findings
    ]


def _collapsed_section(collapsed: list[Finding]) -> str:
    """The v2 collapsed summary: nits (when the org opted in) and findings cut
    by the ceiling, one escaped line each. Untrusted text is escaped; the
    ``<details>`` wrapper is trusted template syntax.
    """
    lines = [f"<details><summary>{len(collapsed)} more finding(s)</summary>", ""]
    for finding in collapsed[:_COLLAPSED_MAX_ITEMS]:
        path = finding.file[:_COLLAPSED_PATH_CHARS]
        where = path if finding.line is None else f"{path}:{finding.line}"
        message = finding.message[:_COLLAPSED_MESSAGE_CHARS].replace("\n", " ")
        lines.append(
            f"- **{escape_markdown(where)}** [{escape_markdown(finding.severity)}] "
            f"{escape_markdown(message)}"
        )
    if len(collapsed) > _COLLAPSED_MAX_ITEMS:
        lines.append(f"- ...and {len(collapsed) - _COLLAPSED_MAX_ITEMS} more not listed.")
    lines += ["", "</details>"]
    return "\n".join(lines)


_UNREVIEWED_MAX_PATHS = 50
_UNREVIEWED_PATH_CHARS = 200


def _unreviewed_section(paths: tuple[str, ...]) -> str:
    """The "Not reviewed" list: paths are PR-author-controlled, so escaped."""
    lines = ["Not reviewed (diff too large for one review, or a review call failed):"]
    lines += [
        f"- {escape_markdown(p[:_UNREVIEWED_PATH_CHARS])}" for p in paths[:_UNREVIEWED_MAX_PATHS]
    ]
    if len(paths) > _UNREVIEWED_MAX_PATHS:
        lines.append(f"- and {len(paths) - _UNREVIEWED_MAX_PATHS} more")
    return "\n".join(lines)


def _split_file_level_budget(
    findings: list[Finding],
) -> tuple[list[Finding], list[Finding], dict[int, str]]:
    """v2 bounds the body: file-level findings past the budget move, one line each,
    into the collapsed summary (never dropped; counted and logged).
    """
    kept: list[Finding] = []
    overflow: list[Finding] = []
    details: dict[int, str] = {}
    used = 0
    for finding in findings:
        detail = _indent_under_bullet(_finding_body(finding, v2=True))
        size = len(detail) + len(finding.file) + 8
        if used + size > _FILE_LEVEL_BUDGET_CHARS:
            overflow.append(finding)  # skip this one; smaller later ones may still fit
            continue
        kept.append(finding)
        details[id(finding)] = detail
        used += size
    if overflow:
        logger.warning(
            "posting: file-level findings over the review-body budget moved to the "
            "collapsed summary",
            extra={"overflow_count": len(overflow)},
        )
        _record_truncated("file_level_overflow")
    return kept, overflow, details


def _review_body(
    file_level_findings: list[Finding],
    total_count: int,
    collapsed: list[Finding] | None = None,
    unreviewed_files: tuple[str, ...] = (),
) -> str:
    v2 = collapsed is not None
    summary = escape_markdown(f"Automated review found {total_count} finding(s).")
    parts = [summary]
    # Rendered once per finding; v2 bodies are budgeted on, then reused below.
    details: dict[int, str] = {}
    if v2:
        file_level_findings, overflow, details = _split_file_level_budget(file_level_findings)
        collapsed = [*overflow, *(collapsed or [])]
    if file_level_findings:
        lines = ["Findings not attributable to a specific line:"]
        for finding in file_level_findings:
            detail = details.get(id(finding)) or _finding_body(finding, v2=v2)
            lines.append(f"- **{escape_markdown(finding.file)}**: {detail}")
        parts.append("\n".join(lines))
    if collapsed:
        parts.append(_collapsed_section(collapsed))
    if unreviewed_files:
        parts.append(_unreviewed_section(unreviewed_files))
    body = "\n\n".join(parts) if len(parts) > 1 else summary
    if len(body) > _BODY_HARD_LIMIT:  # last resort; escaping can inflate the bounded parts
        logger.warning("posting: review body hard-truncated", extra={"length": len(body)})
        _record_truncated("body_hard")
        body = _truncate_body(body)
    return body


def _indent_under_bullet(text: str) -> str:
    """Indent continuation lines so a multi-paragraph body stays inside its list item."""
    return "\n".join(f"  {line}" if line else line for line in text.split("\n"))


def _fence_line(line: str) -> tuple[str, str, str] | None:
    """``(indent, backtick run, rest)`` of a backtick fence line, else None (linear scan)."""
    body = line.lstrip()
    run = len(body) - len(body.lstrip("`"))
    return (line[: len(line) - len(body)], body[:run], body[run:]) if run >= 3 else None


_DETAILS_TAG = re.compile(r"</?details\b[^>]*>", re.IGNORECASE)


def _opener_fence(line: str, opened: tuple[str, str] | None) -> tuple[str, str, str] | None:
    """The fence match for ``line``, discarding a backtick-bearing info string on open."""
    match = _fence_line(line)
    # CommonMark: a backtick fence's info string may not contain a backtick, so
    # "```a`b" is not an opener (mirrors adapter/analyzers/markdown_links.py;
    # duplicated, not imported, to keep adapters independent).
    return None if match is not None and opened is None and "`" in match[2] else match


def _details_delta(line: str) -> int:
    return sum(-1 if tag.startswith("</") else 1 for tag in _DETAILS_TAG.findall(line))


def _scan_fences(text: str) -> tuple[tuple[str, str] | None, int]:
    """The fence left open and the ``<details>`` left open (outside code fences only)."""
    opened: tuple[str, str] | None = None
    details = 0
    for line in text.split("\n"):
        match = _opener_fence(line, opened)
        if match is None:
            if opened is None:
                details += _details_delta(line)
            continue
        indent, ticks, rest = match
        if opened is None:
            opened = (indent, ticks)
        elif not rest.strip() and len(ticks) >= len(opened[1]):
            opened = None
    return opened, max(details, 0)


def _truncate_body(body: str, limit: int = _BODY_HARD_LIMIT) -> str:
    """Cut at the last complete line, never after a lone backslash (it would
    escape the marker's newline), and re-close any ``<details>`` left open.
    """
    cut = body[:limit]
    if body[limit] != "\n":
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    stripped = cut.rstrip("\\")
    if (len(cut) - len(stripped)) % 2:
        cut = cut[:-1]
    fence, closers = _scan_fences(cut)
    if fence is not None:
        cut += f"\n{fence[0]}{fence[1]}"
    return cut + "\n\n_(truncated)_" + "\n</details>" * closers


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


def _index_submitted(
    line_findings: list[Finding], *, v2: bool
) -> dict[tuple[str, int | None, str], list[Finding]]:
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
        key = (finding.file, finding.line, _finding_body(finding, v2=v2))
        submitted.setdefault(key, []).append(finding)
    return submitted


def _correlate_comments(
    raw_comments: Any,
    submitted: dict[tuple[str, int | None, str], list[Finding]],
    pr_number: int,
) -> tuple[list[PostedComment], int]:
    """Match posted comments back to the submitted findings; returns (matched, unmatched_count)."""
    matched: list[PostedComment] = []
    unmatched_count = 0
    for raw_comment in raw_comments:
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
    return matched, unmatched_count


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
        collapsed_findings: list[Finding] | None = None,
        unreviewed_files: tuple[str, ...] = (),
    ) -> None:
        # `collapsed_findings is not None` selects the v2 layout.
        v2 = collapsed_findings is not None
        line_findings = [finding for finding in findings if finding.line is not None]
        file_level_findings = [finding for finding in findings if finding.line is None]
        body = {
            "event": _REVIEW_EVENT,
            "body": _review_body(
                file_level_findings,
                len(findings) + len(collapsed_findings or []),
                collapsed_findings,
                unreviewed_files,
            ),
            "comments": _review_comments(line_findings, v2=v2),
        }
        owner, repo = _split_owner_repo(repo_full_name)
        response = await self._post(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            url=f"{self._github_api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json_body=body,
            operation="review",
        )

        # File-level-only findings never posted a line comment in the first
        # place (see `_review_comments` above) — nothing to correlate, and
        # no reason to spend a second GitHub API call finding that out.
        if not line_findings:
            return

        try:
            await self._record_provenance(
                installation_repo=(installation_id, repo_full_name),
                repo_id=repo_id,
                pr_number=pr_number,
                commit_sha=head_sha,
                review_response=response,
                submitted=_index_submitted(line_findings, v2=v2),
            )
        except Exception as exc:
            # Belt-and-braces on top of `_record_provenance`'s own internal
            # handling below: this module's docstring
            # promises provenance capture "never raises" —
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
            _record_post("provenance_unexpected", "other")

    async def _fetch_review_comments(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        pr_number: int,
        review_response: httpx.Response,
    ) -> httpx.Response | None:
        """GET the comments GitHub assigned ids to; None (logged) on a fetch failure."""
        owner, repo = _split_owner_repo(repo_full_name)
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
            _record_post(
                "provenance_fetch",
                "permanent_error"
                if isinstance(exc, GitHubPermanentAPIError)
                else "retry_exhausted",
            )
            return None
        return response

    # The US-M11 follow-up: fetch the ids GitHub assigned the
    # comments this call just posted, correlate them back to the
    # `Finding`s that produced them, and persist one batched
    # `posted_finding_comment` row per matched comment.
    #
    # Never raises — a fetch failure, a correlation gap, or a storage
    # failure here is logged and swallowed; the review post it's
    # attributing already succeeded and must not be retroactively failed
    # because a secondary write-path concern broke. See this module's own
    # docstring's "Provenance persistence" section. (`post_review`'s own
    # broad `except Exception` around this call is the final backstop for
    # any failure mode not explicitly anticipated below — e.g. a
    # malformed `review_id` in `review_response`'s own body, or a second
    # token-fetch failure — but every failure this method *does* expect is
    # still handled at its own, more specific level first.)
    async def _record_provenance(
        self,
        *,
        installation_repo: tuple[int, str],
        repo_id: str,
        pr_number: int,
        commit_sha: str,
        review_response: httpx.Response,
        submitted: dict[tuple[str, int | None, str], list[Finding]],
    ) -> None:
        installation_id, repo_full_name = installation_repo
        response = await self._fetch_review_comments(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            review_response=review_response,
        )
        if response is None:
            return
        matched, unmatched_count = _correlate_comments(response.json(), submitted, pr_number)

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
            _record_post("provenance_persist", "other")

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
            operation="check_run",
        )

    async def _post(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        url: str,
        json_body: dict[str, Any],
        operation: str,
    ) -> httpx.Response:
        outcome = "other"  # e.g. token-fetch failure, which propagates unchanged
        try:
            token = await self._token_provider.get_token(
                installation_id=installation_id, repo_full_name=repo_full_name
            )
            headers = auth_headers(token)
            try:
                async with client_context(self._client, timeout=self._timeout) as client:
                    response = await request_with_retry(
                        client,
                        "POST",
                        url,
                        headers=headers,
                        json=json_body,
                        max_attempts=self._max_retry_attempts,
                        safe_writes_only=True,
                    )
            except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
                outcome = (
                    "permanent_error"
                    if isinstance(exc, GitHubPermanentAPIError)
                    else "retry_exhausted"
                )
                raise GitHubPostingUnreachableError(detail=str(exc)) from exc
            outcome = "ok"
            return response
        finally:
            _record_post(operation, outcome)


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[FindingsPublisher] = GitHubFindingsPublisher
