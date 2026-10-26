"""adapter.github.ci_review — the ``review.output.FindingsPublisher`` for local mode in GitHub
Actions (docs/plans/plan-local-mode.md, Phase 5; US-C1, US-C3): one pull-request review per push,
refreshed rather than duplicated.

**Identity is login plus marker (US-C3).** A ``GITHUB_TOKEN`` has no GitHub App id, so
``walkthrough.is_platform_comment`` cannot recognise our comments. A comment is ours only when its
author login is the configured bot's **and** its body carries ``CI_COMMENT_MARKER``: a pull-request
author can copy the hidden marker into their own comment, but not post as the bot. Anything else is
never patched and never minimised.

**One run does three things** against the comments already on the PR: a finding whose file and line
range match an own comment is not posted again (its text is patched if it changed); a finding with
no match is posted, once, through ``GitHubFindingsPublisher``; an own comment no current finding
matches is minimised as ``OUTDATED`` (GraphQL: the REST API cannot do it). A run with no findings
never reaches ``post_review`` (the output step skips it), so ``post_check_run`` does the same
reconciliation then, and posts no check-run (a ``GITHUB_TOKEN`` would need ``checks: write``).

**GitHub failures are recorded, not raised.** The caller has already printed the findings to the job
log; this class records the first failure (``failure``) and any dedupe problem so the entry point
can exit non-zero naming it, instead of failing the pipeline and losing the output. A failed
listing posts nothing (it would duplicate); a failed patch or minimise is counted and the rest goes
on. Messages carry a status code only, never a body or a header, so the token cannot leak.

**Posted text is sanitised**: control characters and newlines are collapsed, bare URLs are broken so
GitHub does not auto-link them, the message is length-capped, and ``GitHubFindingsPublisher`` then
escapes markdown, HTML and ``@mentions`` as in server mode.

The "suppressed by the memory file" note rides on a posted review only: a run whose findings are all
suppressed stays silent on the pull request (the job log lists them), so a quiet push stays quiet.

A comment minimised as outdated is tagged and never matched again: if its finding returns on a
later push it is posted anew, because the author may have fixed it and then broken it again and a
collapsed comment would otherwise hide that.

A file-level finding (no line) lives in the review body: it is skipped when an earlier review by the
bot on the same head commit already carries the same text.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx

from adapter.github.exceptions import (
    GitHubAdapterError,
    GitHubCiError,
    GitHubPermanentAPIError,
    GitHubRetryExhaustedError,
)
from adapter.github.http import (
    DEFAULT_FAN_OUT_CONCURRENCY,
    auth_headers,
    client_context,
    request_with_retry,
    split_owner_repo,
)
from adapter.github.posting import GitHubFindingsPublisher, comment_body, file_level_line
from adapter.github.token_provider import InstallationTokenProvider
from findings.markdown import sanitize_message, strip_display_text
from findings.models import Finding
from findings.redact import REDACTED_PATH_PREFIX
from review.output import FindingsPublisher

logger = logging.getLogger(__name__)

CI_COMMENT_MARKER: Final = "<!-- review:ci-comment:v1 -->"
# Appended to a comment once it is minimised: the REST listing does not say whether it is.
OUTDATED_MARKER: Final = "<!-- review:ci-outdated:v1 -->"
_MAX_PAGES: Final = 50
_PER_PAGE: Final = 100
_MINIMIZE_MUTATION: Final = (
    "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) "
    "{ minimizedComment { isMinimized } } }"
)

Key = tuple[str, int, int]


@dataclass
class CiCommentCounts:
    posted: int = 0
    updated: int = 0
    outdated: int = 0
    skipped: int = 0
    rejected: int = 0
    dedupe_failure: str | None = None


def is_ci_comment(comment: object, bot_login: str) -> bool:
    """Pure identity check: the bot's login (exact) **and** the marker on a line of its own, on a
    top-level review comment with an integer id."""
    if not isinstance(comment, dict):
        return False
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("login") != bot_login:
        return False
    if type(comment.get("id")) is not int or comment.get("in_reply_to_id") is not None:
        return False
    body = comment.get("body")
    return isinstance(body, str) and CI_COMMENT_MARKER in body.splitlines()


def _is_bot_review(review: object, bot_login: str) -> bool:
    if not isinstance(review, dict):
        return False
    user = review.get("user")
    return (
        isinstance(user, dict)
        and user.get("login") == bot_login
        and "Automated review found" in str(review.get("body"))
    )


def _int(value: object) -> int | None:
    return value if type(value) is int else None


def _comment_key(comment: dict[str, Any]) -> Key | None:
    """File and line range of an existing comment. GitHub nulls ``line`` once a push moves the
    code (the comment is "outdated" in its UI); such a comment has no key, so it never counts as
    covering a finding and is treated as stale."""
    path = comment.get("path")
    end = _int(comment.get("line"))
    if not isinstance(path, str) or end is None:
        return None
    return path, _int(comment.get("start_line")) or end, end


def _finding_key(finding: Finding) -> Key | None:
    if finding.suggestion is not None:
        return finding.file, finding.suggestion.start_line, finding.suggestion.end_line
    if finding.line is None:
        return None
    return finding.file, finding.line, finding.line


def _sanitize(finding: Finding) -> Finding:
    message = sanitize_message(finding.message)
    rule_id = None if finding.rule_id is None else strip_display_text(finding.rule_id)
    return finding.model_copy(update={"message": message, "rule_id": rule_id})


_PERMISSION_STATUSES: Final = frozenset({401, 403, 404})
_GRAPHQL_PERMISSION_TYPES: Final = frozenset({"FORBIDDEN", "INSUFFICIENT_SCOPES"})


def _permanent_kind(status_code: int) -> Literal["permission", "unavailable"]:
    """Only a refusal of the token is a permission problem; a 422 (a line outside the diff) or
    another 4xx is not, and must not tell the user to change workflow permissions."""
    return "permission" if status_code in _PERMISSION_STATUSES else "unavailable"


def _status(exc: GitHubPermanentAPIError | GitHubRetryExhaustedError) -> str:
    return f"HTTP {exc.status_code}" if exc.status_code is not None else "no response"


def _same_body(stored: object, built: str) -> bool:
    # GitHub normalises line endings and edge whitespace on stored bodies.
    return str(stored or "").replace("\r\n", "\n").strip() == built.replace("\r\n", "\n").strip()


class GitHubCiReviewPublisher:
    """Bound to one pull request (``pr_number`` of ``repo_full_name``); the per-call identity the
    ``FindingsPublisher`` Protocol passes is ignored in favour of it."""

    def __init__(
        self,
        *,
        token_provider: InstallationTokenProvider,
        repo_full_name: str,
        pr_number: int,
        bot_login: str,
        head_sha: str | None = None,
        github_api_base_url: str = "https://api.github.com",
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        suppressed_lines: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._repo_full_name = repo_full_name
        self._pr_number = pr_number
        self._bot_login = bot_login
        # The pull request's own head. The call's ``head_sha`` is the checked-out commit, which is
        # the merge commit when the head was not fetched, and GitHub refuses it as a ``commit_id``.
        self._head_sha = head_sha
        self._base = github_api_base_url.rstrip("/")
        # GitHub Enterprise Server serves REST at ``/api/v3`` but GraphQL at ``/api/graphql``.
        self._graphql_url = (
            f"{self._base.removesuffix('/v3')}/graphql"
            if self._base.endswith("/api/v3")
            else f"{self._base}/graphql"
        )
        self._timeout = timeout
        self._max_retry_attempts = max_retry_attempts
        self._client = client
        self._inner = GitHubFindingsPublisher(
            token_provider=token_provider,
            github_api_base_url=self._base,
            timeout=timeout,
            max_retry_attempts=max_retry_attempts,
            client=client,
            comment_marker=CI_COMMENT_MARKER,
            suppressed_lines=self._suppressed_once,
        )
        self._suppressed_source = suppressed_lines
        self._suppressed_shown = False
        self._reviewed = False
        self.counts = CiCommentCounts()
        # Set while posting: "has the pull request's head moved?", asked once before bisecting.
        self._head_check: Callable[[], Awaitable[bool]] | None = None
        # The first failure: ("permission" | "unavailable", detail). None means GitHub was fine.
        self.failure: tuple[str, str] | None = None

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
        self._reviewed = True
        await self._reconcile_safely(findings, head_sha=self._head_sha or head_sha)

    async def post_check_run(
        self, *, installation_id: int, repo_full_name: str, head_sha: str, finding_count: int
    ) -> None:
        if not self._reviewed:
            self._reviewed = True
            await self._reconcile_safely([], head_sha=self._head_sha or head_sha)

    async def post_failure_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        failed_step_name: str,
        error: str,
    ) -> None:
        return None

    def _suppressed_once(self) -> Sequence[str]:
        """The memory-file section goes in one review only, not in each one-by-one retry."""
        if self._suppressed_source is None or self._suppressed_shown:
            return ()
        self._suppressed_shown = True
        return self._suppressed_source()

    def _record(self, exc: GitHubCiError, *, dedupe: bool) -> None:
        logger.warning("ci review: github call failed", extra={"kind": exc.kind})
        if self.failure is None:
            self.failure = (exc.kind, exc.detail)
        if dedupe and self.counts.dedupe_failure is None:
            self.counts.dedupe_failure = exc.detail

    async def _reconcile_safely(self, findings: list[Finding], *, head_sha: str) -> None:
        # Findings stay in the job log whatever goes wrong here: record, never raise.
        try:
            await self._reconcile(findings, head_sha=head_sha)
        except Exception as exc:
            logger.warning("ci review: unexpected failure", exc_info=True)
            self._record(
                GitHubCiError(
                    kind="unavailable", detail=f"reconciling comments: {type(exc).__name__}"
                ),
                dedupe=False,
            )

    async def _reconcile(self, findings: list[Finding], *, head_sha: str) -> None:
        # A path input redaction replaced is not a file in the pull request: GitHub would reject
        # the whole review (422) over it. Such a finding stays in the job log only.
        sanitized = [_sanitize(f) for f in findings if not f.file.startswith(REDACTED_PATH_PREFIX)]
        try:
            owner, repo = split_owner_repo(self._repo_full_name)
        except ValueError:
            self._record(
                GitHubCiError(kind="unavailable", detail="repository name is not owner/repo"),
                dedupe=False,
            )
            return
        token = await self._token_provider.get_token(
            installation_id=0, repo_full_name=self._repo_full_name
        )
        headers = auth_headers(token)
        async with client_context(self._client, timeout=self._timeout) as client:
            wants_reviews = any(f.line is None for f in sanitized)
            # Both listings at once; the review listing is only for file-level findings.
            listed = await asyncio.gather(
                self._list_own(client, headers, owner, repo),
                self._list_own(client, headers, owner, repo, reviews=True)
                if wants_reviews
                else _nothing(),
                return_exceptions=True,
            )
            own_listing, review_listing = listed
            if isinstance(own_listing, BaseException):
                if not isinstance(own_listing, GitHubCiError):
                    raise own_listing
                # Without the existing comments a post would duplicate them: post nothing.
                self._record(own_listing, dedupe=True)
                return
            own = own_listing

            # Already minimised by an earlier run: neither a match for a finding nor stale again.
            # The marker is only ever appended as the last line, so only the last line counts.
            own = [
                c
                for c in own
                if str(c.get("body", "")).rstrip().rsplit("\n", 1)[-1] != OUTDATED_MARKER
            ]
            existing: dict[Key, list[dict[str, Any]]] = {}
            for comment in own:
                key = _comment_key(comment)
                if key is not None:
                    existing.setdefault(key, []).append(comment)
            if wants_reviews:
                if isinstance(review_listing, BaseException):
                    if not isinstance(review_listing, GitHubCiError):
                        raise review_listing
                    self._record(review_listing, dedupe=True)
                    return
                # Only this head's reviews: a finding fixed and later reintroduced is new again.
                bodies = [r["body"] for r in review_listing if r.get("commit_id") == head_sha]
                kept = []
                for f in sanitized:
                    if f.line is None and any(file_level_line(f) in b for b in bodies):
                        self.counts.skipped += 1
                    else:
                        kept.append(f)
                sanitized = kept
            to_post: list[Finding] = []
            to_patch: list[tuple[dict[str, Any], str]] = []
            for finding in sanitized:
                key = _finding_key(finding)
                matches = existing.get(key) if key is not None else None
                if not matches:
                    to_post.append(finding)
                    continue
                body = comment_body(finding, CI_COMMENT_MARKER)
                # An identical body first, so two findings on one line swap places without churn.
                same = next(
                    (i for i, c in enumerate(matches) if _same_body(c.get("body"), body)), 0
                )
                match = matches.pop(same)
                if _same_body(match.get("body"), body):
                    self.counts.skipped += 1
                else:
                    to_patch.append((match, body))
            # Own comments no current finding matched, plus any own comment whose position GitHub
            # no longer reports (unkeyable): the code they were about is gone.
            stale = [c for comments in existing.values() for c in comments]
            stale += [c for c in own if _comment_key(c) is None]

            if to_post:
                rejected_before = self.counts.rejected
                self._head_check = functools.partial(
                    self._head_moved, client, headers, owner, repo, head_sha
                )
                # A failed post is recorded; patching and outdating still run.
                posted = await self._post(to_post, head_sha)
                # One finding outside the diff is expected; all of them refused points at the
                # request itself (a stale head commit, a bad path): never a quiet pass. A lone
                # finding cannot tell the two apart, so the head commit is checked.
                if (
                    posted
                    and self.counts.rejected - rejected_before == len(to_post)
                    and (
                        len(to_post) > 1
                        or await self._head_moved(client, headers, owner, repo, head_sha)
                    )
                ):
                    self._record(
                        GitHubCiError(
                            kind="unavailable",
                            detail=(
                                "GitHub refused every comment (HTTP 422); "
                                "is the head commit part of the pull request?"
                            ),
                        ),
                        dedupe=False,
                    )
            slots = asyncio.Semaphore(DEFAULT_FAN_OUT_CONCURRENCY)

            async def bounded(work: Awaitable[None]) -> None:
                async with slots:
                    await work

            # Independent calls, a bounded number at once; each records its own failure.
            await asyncio.gather(
                *(
                    bounded(self._patch(client, headers, owner, repo, comment, body))
                    for comment, body in to_patch
                ),
                *(
                    bounded(self._outdate(client, headers, owner, repo, comment))
                    for comment in stale
                ),
            )

    async def _patch(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        owner: str,
        repo: str,
        comment: dict[str, Any],
        body: str,
    ) -> None:
        try:
            await self._call(
                client,
                "PATCH",
                f"{self._base}/repos/{owner}/{repo}/pulls/comments/{comment['id']}",
                headers,
                "updating a review comment",
                json={"body": body},
            )
            self.counts.updated += 1
        except GitHubCiError as exc:
            self._record(exc, dedupe=True)

    async def _head_moved(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        owner: str,
        repo: str,
        head_sha: str,
    ) -> bool:
        """True unless the pull request's head is ``head_sha`` (a failed lookup counts as moved:
        the run could not show the request was sound)."""
        try:
            response = await self._call(
                client,
                "GET",
                f"{self._base}/repos/{owner}/{repo}/pulls/{self._pr_number}",
                headers,
                "reading the pull request",
            )
            head = response.json()["head"]["sha"]
        except (GitHubCiError, ValueError, KeyError, TypeError):
            return True
        return head != head_sha

    async def _post(self, findings: list[Finding], head_sha: str) -> bool:
        shown_before = self._suppressed_shown
        try:
            await self._inner.post_review(
                installation_id=0,
                repo_full_name=self._repo_full_name,
                repo_id="local",
                pr_number=self._pr_number,
                head_sha=head_sha,
                findings=findings,
            )
        except GitHubAdapterError as exc:
            cause = exc.__cause__
            # ``request_with_retry`` raises ``GitHubPermanentAPIError`` only for 401/403/404; a 422
            # on a non-retried write arrives as ``GitHubRetryExhaustedError``.
            if (
                isinstance(cause, GitHubPermanentAPIError | GitHubRetryExhaustedError)
                and cause.status_code == 422
            ):
                # One comment on a line outside the diff rejects the whole review: post the
                # findings one by one so only the offending one is lost (it stays in the log).
                self._suppressed_shown = shown_before  # the refused review never showed it
                if len(findings) > 1:
                    # A request-wide refusal (a moved head) would cost ~2N reviews to bisect:
                    # ask once, before the first split, whether the head is still the PR's.
                    check, self._head_check = self._head_check, None
                    if check is not None and await check():
                        self._record(
                            GitHubCiError(
                                kind="unavailable",
                                detail=(
                                    "GitHub refused the review (HTTP 422) and the pull request's "
                                    "head is no longer the commit under review"
                                ),
                            ),
                            dedupe=False,
                        )
                        return False
                    # Bisect: the good half goes through in one review, so the number of reviews
                    # (and notifications) grows with the bad findings, not with all of them.
                    middle = len(findings) // 2
                    # Both halves are tried: one failing must not drop the other.
                    first = await self._post(findings[:middle], head_sha)
                    second = await self._post(findings[middle:], head_sha)
                    return first and second
                self.counts.rejected += 1  # visible in the run summary, not only the log
                logger.warning("ci review: a finding outside the pull request diff was not posted")
                return True
            if isinstance(cause, GitHubPermanentAPIError):
                kind = _permanent_kind(cause.status_code)
                detail = f"posting the review: HTTP {cause.status_code}"
            elif isinstance(cause, GitHubRetryExhaustedError):
                kind, detail = "unavailable", f"posting the review: {_status(cause)}"
            else:
                kind, detail = "unavailable", f"posting the review: {type(exc).__name__}"
            self._suppressed_shown = shown_before  # the failed review never showed it
            self._record(GitHubCiError(kind=kind, detail=detail), dedupe=False)
            return False
        except Exception as exc:
            logger.warning("ci review: posting failed unexpectedly", exc_info=True)
            self._suppressed_shown = shown_before
            self._record(
                GitHubCiError(
                    kind="unavailable", detail=f"posting the review: {type(exc).__name__}"
                ),
                dedupe=False,
            )
            return False
        self.counts.posted += len(findings)
        return True

    async def _outdate(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        owner: str,
        repo: str,
        comment: dict[str, Any],
    ) -> None:
        node_id = comment.get("node_id")
        try:
            if not isinstance(node_id, str):
                raise GitHubCiError(kind="unavailable", detail="an own comment has no node id")
            response = await self._call(
                client,
                "POST",
                self._graphql_url,
                headers,
                "marking a review comment outdated",
                json={"query": _MINIMIZE_MUTATION, "variables": {"id": node_id}},
            )
            try:
                errors = response.json().get("errors")
            except (ValueError, AttributeError):
                errors = [{}]
            if errors:
                # GraphQL reports refusals as a 200 with an errors list. Only a refusal of the
                # token is a permission problem; a rate limit or a missing node is not, and must
                # not tell the user to change workflow permissions.
                refused = isinstance(errors, list) and any(
                    isinstance(e, dict) and e.get("type") in _GRAPHQL_PERMISSION_TYPES
                    for e in errors
                )
                raise GitHubCiError(
                    kind="permission" if refused else "unavailable",
                    detail="marking a review comment outdated: GraphQL error",
                )
            self.counts.outdated += 1
            try:
                await self._call(
                    client,
                    "PATCH",
                    f"{self._base}/repos/{owner}/{repo}/pulls/comments/{comment['id']}",
                    headers,
                    "tagging an outdated review comment",
                    json={"body": f"{comment.get('body', '')}\n{OUTDATED_MARKER}"},
                )
            except GitHubCiError:
                # Only a repeat minimise next push is at stake: not worth failing the run.
                logger.warning("ci review: could not tag a minimised comment")
        except GitHubCiError as exc:
            self._record(exc, dedupe=True)

    async def _list_own(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        owner: str,
        repo: str,
        *,
        reviews: bool = False,
    ) -> list[dict[str, Any]]:
        base = urlsplit(self._base)
        kind = "reviews" if reviews else "comments"
        url: str | None = (
            f"{self._base}/repos/{owner}/{repo}/pulls/{self._pr_number}/{kind}?per_page={_PER_PAGE}"
        )
        own: list[dict[str, Any]] = []
        pages = 0
        while url is not None:
            if pages >= _MAX_PAGES:
                raise GitHubCiError(
                    kind="unavailable", detail=f"more than {_MAX_PAGES} pages of review comments"
                )
            response = await self._call(client, "GET", url, headers, "listing review comments")
            pages += 1
            try:
                page = response.json()
            except ValueError:
                page = None
            if not isinstance(page, list):
                raise GitHubCiError(
                    kind="unavailable", detail="listing review comments: not a list"
                )
            own += [
                c
                for c in page
                if (_is_bot_review if reviews else is_ci_comment)(c, self._bot_login)
            ]
            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
            if url is not None:
                nxt = urlsplit(url)
                # Never send the token to a host a response names.
                if (nxt.scheme, nxt.netloc) != (base.scheme, base.netloc):
                    raise GitHubCiError(
                        kind="unavailable", detail="listing review comments: foreign page link"
                    )
        return own

    async def _call(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        what: str,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return await request_with_retry(
                client,
                method,
                url,
                headers=headers,
                max_attempts=self._max_retry_attempts,
                **kwargs,
            )
        except GitHubPermanentAPIError as exc:
            raise GitHubCiError(
                kind=_permanent_kind(exc.status_code), detail=f"{what}: {_status(exc)}"
            ) from exc
        except GitHubRetryExhaustedError as exc:
            raise GitHubCiError(kind="unavailable", detail=f"{what}: {_status(exc)}") from exc


async def _nothing() -> list[dict[str, Any]]:
    return []


# Runtime confirmation that this class satisfies the Protocol it is built to implement.
_: type[FindingsPublisher] = GitHubCiReviewPublisher
