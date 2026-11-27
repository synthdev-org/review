"""adapter.github.reactions — ``memory.store.ReactionFetcher``'s concrete
GitHub-backed implementation (US-M3).

GitHub's REST API has no batched multi-comment reactions endpoint — one
``GET .../pulls/comments/{comment_id}/reactions`` call is required per
comment. Fanned out concurrently, bounded by
``adapter/github/http.py``'s shared ``DEFAULT_FAN_OUT_CONCURRENCY`` (the
same bound ``adapter/github/diff.py``'s own blob fetch already uses), through
the exact same helpers every other GitHub-calling module in this codebase
uses together (``request_with_retry``/``auth_headers``/``client_context``),
reusing the composition root's already-built
``GitHubInstallationTokenProvider``/shared ``httpx.AsyncClient`` — no new
auth code. Each per-comment call is issued with ``?per_page=100`` (GitHub's
own maximum), the same explicit-``per_page`` convention
``adapter/github/diff.py``'s own list call already establishes, instead of
silently relying on GitHub's much smaller 30-item default — a comment with
more than 100 reactions (unlikely in practice) still only has its first 100
observed, since this call, unlike ``diff.py``'s own file-list fetch, does
not follow the response's ``Link`` header to additional pages; an accepted
gap given the 30->100 jump already closes the realistic case, not a full
fix for arbitrarily large reaction counts.

**One comment's fetch failing never aborts the batch.** Unlike
``adapter/github/git_data.py``'s ``fetch_blobs`` (fail-closed: one blob
fetch failing cancels every other in-flight fetch and re-raises, since a
silently incomplete content set would be a worse outcome than a job that
fails visibly and retries), a missed reaction is a lost secondary signal,
not a security- or correctness-relevant gap — each per-comment fetch is
wrapped individually (``asyncio.gather(..., return_exceptions=True)``) and a
failure is logged at ``warning`` (``comment_id``, ``error``) and simply
omitted from the returned mapping; the job still completes for every
comment that did succeed. A failure *before* the per-comment fan-out even
starts (the installation token exchange itself, or a malformed
``repo_full_name``) is a genuine whole-job failure and propagates as a real
exception, uncaught here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import httpx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.http import (
    DEFAULT_FAN_OUT_CONCURRENCY,
    auth_headers,
    client_context,
    request_with_retry,
)
from memory.models import ReactionRecord
from memory.store import ReactionFetcher
from tenancy.resolution import split_owner_repo

logger = logging.getLogger(__name__)

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"


def _parse_reactions(raw: object) -> tuple[ReactionRecord, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"reactions endpoint response was not a JSON list: {type(raw)!r}")
    return tuple(ReactionRecord(reaction_id=item["id"], content=item["content"]) for item in raw)


class GitHubReactionFetcher:
    """``memory.store.ReactionFetcher`` implementation backed by GitHub's
    REST API. Constructed once at the composition root
    (``apps/worker/main.py``) with the same ``GitHubInstallationTokenProvider``
    every other GitHub adapter here already shares.
    """

    def __init__(
        self,
        *,
        token_provider: GitHubInstallationTokenProvider,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        concurrency: int = DEFAULT_FAN_OUT_CONCURRENCY,
    ) -> None:
        self._token_provider = token_provider
        self._github_api_base_url = github_api_base_url
        self._timeout = timeout
        self._max_retry_attempts = max_retry_attempts
        self._client = client
        self._concurrency = concurrency

    async def fetch_reactions(
        self, *, installation_id: int, repo_full_name: str, comment_ids: Sequence[int]
    ) -> dict[int, tuple[ReactionRecord, ...]]:
        comment_ids_tuple = tuple(comment_ids)
        if not comment_ids_tuple:
            return {}

        owner, repo = split_owner_repo(repo_full_name)
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def _fetch_one(
            client: httpx.AsyncClient, comment_id: int
        ) -> tuple[ReactionRecord, ...]:
            async with semaphore:
                response = await request_with_retry(
                    client,
                    "GET",
                    f"{self._github_api_base_url}/repos/{owner}/{repo}"
                    f"/pulls/comments/{comment_id}/reactions?per_page=100",
                    headers=headers,
                    max_attempts=self._max_retry_attempts,
                )
            return _parse_reactions(response.json())

        async with client_context(self._client, timeout=self._timeout) as client:
            tasks = [
                asyncio.ensure_future(_fetch_one(client, comment_id))
                for comment_id in comment_ids_tuple
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        reactions_by_comment: dict[int, tuple[ReactionRecord, ...]] = {}
        for comment_id, result in zip(comment_ids_tuple, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "reaction fetch failed for one comment; this poll's other comments are "
                    "unaffected",
                    extra={"comment_id": comment_id, "error": str(result)},
                )
                continue
            reactions_by_comment[comment_id] = result
        return reactions_by_comment


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[ReactionFetcher] = GitHubReactionFetcher
