"""adapter.github.commits — ``ingestion.commits.CommitSource`` over GitHub's
REST API: ``GET pulls/{n}/commits`` for the commit list and
``GET compare/{base}...{head}`` for a range diff. Both are paginated via the
``Link`` header and followed to completion; every failure becomes the
domain-owned ``CommitsUnavailableError``.
"""

from __future__ import annotations

from typing import Any

import httpx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import GitHubAdapterError
from adapter.github.http import (
    auth_headers,
    client_context,
    request_with_retry,
    split_owner_repo,
)
from ingestion.commits import CommitInfo, CommitsUnavailableError
from ingestion.diff import Diff, DiffFile

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"


class GitHubCommitSource:
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
        self._client = client

    async def _pages(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        path: str,
        items_key: str | None,
    ) -> list[Any]:
        """All items of a paginated endpoint. ``items_key`` is the response
        object's list field (``None`` when the response body is the list)."""
        try:
            token = await self._token_provider.get_token(
                installation_id=installation_id, repo_full_name=repo_full_name
            )
            headers = auth_headers(token)
            owner, repo = split_owner_repo(repo_full_name)
            items: list[Any] = []
            url: str | None = (
                f"{self._github_api_base_url}/repos/{owner}/{repo}/{path}?per_page=100"
            )
            async with client_context(self._client, timeout=self._timeout) as client:
                while url is not None:
                    response = await request_with_retry(
                        client, "GET", url, headers=headers, max_attempts=self._max_retry_attempts
                    )
                    body = response.json()
                    page = body.get(items_key, []) if items_key and isinstance(body, dict) else body
                    if not isinstance(page, list):
                        raise ValueError(f"{path} response did not contain a JSON list")
                    items.extend(page)
                    next_link = response.links.get("next")
                    url = next_link["url"] if next_link else None
            return items
        except (GitHubAdapterError, ValueError, httpx.HTTPError) as exc:
            # GitHubPermanentAPIError/RetryExhausted, token errors, bad JSON
            raise CommitsUnavailableError(repo_full_name=repo_full_name, detail=str(exc)) from exc

    async def list_commits(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> tuple[CommitInfo, ...]:
        raw = await self._pages(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            path=f"pulls/{pr_number}/commits",
            items_key=None,
        )
        try:
            return tuple(
                CommitInfo(
                    sha=c["sha"],
                    parent_shas=tuple(p["sha"] for p in c["parents"]),
                    title=str(c["commit"]["message"]).split("\n", 1)[0],
                )
                for c in raw
            )
        except (KeyError, TypeError) as exc:
            raise CommitsUnavailableError(
                repo_full_name=repo_full_name, detail=f"malformed commit entry: {exc!r}"
            ) from exc

    async def fetch_range_diff(
        self, *, installation_id: int, repo_full_name: str, base_sha: str, head_sha: str
    ) -> Diff:
        raw = await self._pages(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            path=f"compare/{base_sha}...{head_sha}",
            items_key="files",
        )
        try:
            return Diff(
                files=tuple(
                    DiffFile(path=f["filename"], status=f["status"], patch=f.get("patch"))
                    for f in raw
                )
            )
        except (KeyError, TypeError) as exc:
            raise CommitsUnavailableError(
                repo_full_name=repo_full_name, detail=f"malformed file entry: {exc!r}"
            ) from exc
