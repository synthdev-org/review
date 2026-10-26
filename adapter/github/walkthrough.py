"""adapter.github.walkthrough — the concrete ``review.walkthrough_render.
WalkthroughPublisher``: creates or edits the PR's single walkthrough issue
comment (plan-phase-12, Phase 2).

**Identity fails closed (US-W4).** A comment is the platform's own only when
``performed_via_github_app.id`` is an ``int`` (never a bool, string or float)
equal to ``app_id`` **and** its body starts with the marker **and** carries the
fixed footer line. Anything else — a PAT comment, a pasted marker — is never
edited and never deleted.

**The only destructive write** is the dedupe ``DELETE``, and it targets only the
numeric id this job's own ``POST`` returned. The adapter holds no deadline; the
step owns the time budget. Errors carry status and path, never a body, and logs
never carry comment text.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from adapter.github.exceptions import (
    GitHubPermanentAPIError,
    GitHubPostingMalformedRequestError,
    GitHubPostingUnreachableError,
    GitHubRetryExhaustedError,
    GitHubWalkthroughCommentLimitError,
    GitHubWalkthroughDedupeError,
)
from adapter.github.http import auth_headers, client_context, request_with_retry, split_owner_repo
from adapter.github.token_provider import InstallationTokenProvider
from review.step import JobIdentity
from review.walkthrough_render import FIXED_FOOTER_LINE, MARKER, WalkthroughPublisher

logger = logging.getLogger(__name__)

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
_MAX_PAGES = 5
_PER_PAGE = 100

Action = Literal["created", "edited", "deduped"]


def is_platform_comment(comment: object, app_id: int) -> bool:
    """Pure identity check (ADR-0005): App id (``type(x) is int``), marker as the
    first body line, and the fixed footer line."""
    if not isinstance(comment, dict):
        return False
    app = comment.get("performed_via_github_app")
    if not isinstance(app, dict):
        return False
    found_id = app.get("id")
    if type(found_id) is not int or found_id != app_id:
        return False
    body = comment.get("body")
    if not isinstance(body, str):
        return False
    lines = body.splitlines()
    return bool(lines) and lines[0] == MARKER and FIXED_FOOTER_LINE in lines


def _comment_id(comment: dict[str, Any]) -> int | None:
    value = comment.get("id")
    return value if type(value) is int else None


class GitHubWalkthroughPublisher:
    def __init__(
        self,
        *,
        token_provider: InstallationTokenProvider,
        app_id: int,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._app_id = app_id
        self._base = github_api_base_url.rstrip("/")
        self._timeout = timeout
        self._max_retry_attempts = max_retry_attempts
        self._client = client

    async def upsert(self, *, job_id: str, identity: JobIdentity, body: str) -> Action:
        try:
            owner, repo = split_owner_repo(identity.repo_full_name)
        except ValueError as exc:
            raise GitHubPostingMalformedRequestError(detail=str(exc)) from exc
        issue_comments = f"{self._base}/repos/{owner}/{repo}/issues/{identity.pr_number}/comments"
        comment_url = f"{self._base}/repos/{owner}/{repo}/issues/comments"
        token = await self._token_provider.get_token(
            installation_id=identity.installation_id, repo_full_name=identity.repo_full_name
        )
        headers = auth_headers(token)
        log = {"job_id": job_id, "pr_number": identity.pr_number, "head_sha": identity.head_sha}

        async with client_context(self._client, timeout=self._timeout) as client:
            try:
                existing, pages, capped = await self._find(client, headers, issue_comments)
                if existing is not None:
                    await self._call(
                        client, "PATCH", f"{comment_url}/{existing}", headers, json={"body": body}
                    )
                    logger.info(
                        "walkthrough: comment edited",
                        extra={**log, "pages": pages, "action": "edited"},
                    )
                    return "edited"
                if capped:
                    raise GitHubWalkthroughCommentLimitError(
                        detail=f"more than {_MAX_PAGES} comment pages without a walkthrough comment"
                    )
                created = await self._call(
                    client,
                    "POST",
                    issue_comments,
                    headers,
                    json={"body": body},
                    safe_writes_only=True,
                )
            except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
                raise GitHubPostingUnreachableError(detail=str(exc)) from exc

            own_id = _own_id(created)
            if own_id is None:
                logger.info(
                    "walkthrough: comment created",
                    extra={**log, "pages": pages, "action": "created"},
                )
                return "created"
            try:
                action = await self._dedupe(
                    client, headers, issue_comments, comment_url, own_id, body
                )
            except (
                GitHubPermanentAPIError,
                GitHubRetryExhaustedError,
                GitHubPostingUnreachableError,
            ) as exc:
                logger.warning(
                    "walkthrough: dedupe failed", extra={**log, "action": "dedupe_failed"}
                )
                raise GitHubWalkthroughDedupeError(detail=str(exc)) from exc
            logger.info("walkthrough: comment %s", action, extra={**log, "action": action})
            return action

    async def _dedupe(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        issue_comments: str,
        comment_url: str,
        own_id: int,
        body: str,
    ) -> Action:
        """Converge a create race: an older platform comment wins and takes this
        job's content; only the comment this job's own ``POST`` returned is
        deleted. A 404 anywhere, or an older comment out of sight, is a no-op."""
        try:
            older, _, _ = await self._find(client, headers, issue_comments, older_than=own_id)
            if older is None:
                return "created"
            await self._call(
                client, "PATCH", f"{comment_url}/{older}", headers, json={"body": body}
            )
            await self._call(client, "DELETE", f"{comment_url}/{own_id}", headers)
        except GitHubPermanentAPIError as exc:
            if exc.status_code == 404:
                return "created"
            raise
        return "deduped"

    async def _find(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        list_url: str,
        *,
        older_than: int | None = None,
    ) -> tuple[int | None, int, bool]:
        """First (oldest) platform comment, optionally only with id below
        ``older_than``. Returns ``(id, pages_read, capped)``; ``capped`` means
        the page cap stopped the walk with more pages remaining."""
        base = urlsplit(self._base)
        url: str | None = f"{list_url}?per_page={_PER_PAGE}"
        pages = 0
        while url is not None:
            if pages >= _MAX_PAGES:
                return None, pages, True
            response = await self._call(client, "GET", url, headers)
            pages += 1
            try:
                page = response.json()
            except ValueError:
                raise GitHubPostingUnreachableError(
                    detail="comments response was not JSON"
                ) from None
            if not isinstance(page, list):
                raise GitHubPostingUnreachableError(detail="comments response was not a list")
            for comment in page:
                cid = _comment_id(comment) if isinstance(comment, dict) else None
                if cid is None or not is_platform_comment(comment, self._app_id):
                    continue
                if older_than is None or cid < older_than:
                    return cid, pages, False
            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
            if url is not None:
                nxt = urlsplit(url)
                # Never send the installation token to a host a response names.
                if (nxt.scheme, nxt.netloc) != (base.scheme, base.netloc):
                    raise GitHubPostingUnreachableError(detail="pagination link left the API host")
        return None, pages, False

    async def _call(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        return await request_with_retry(
            client, method, url, headers=headers, max_attempts=self._max_retry_attempts, **kwargs
        )


def _own_id(response: httpx.Response) -> int | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return _comment_id(data) if isinstance(data, dict) else None


_: type[WalkthroughPublisher] = GitHubWalkthroughPublisher
