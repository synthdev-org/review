"""adapter.github.tree — the concrete ``indexing.tree.RepoTreeFetcher``
implementation: fetches a repo's full file tree at a commit (the full-run
and full-tree-fallback case, US8), and — for an arbitrary, non-diff-scoped
path set — real file content, via GitHub's REST API. Per the indexing
delivery plan's Phase 2 section (Unit B).

Reuses, rather than reimplements, the same Git Trees/Blobs API call/parse
logic ``adapter/github/diff.py`` already has: both route through the shared
``adapter/github/git_data.py`` helpers (``fetch_tree_listing``/
``fetch_blobs``) — genuine reuse of the exact call/parse logic, per that
phase's own "reuses, not reimplements, the tree-listing call itself"
finding, not a second independent implementation of either.

**Trees API truncation, addressed explicitly:** GitHub's Git Trees API
truncates its response at 100,000 entries/7MB and sets ``"truncated": true``
on the payload when it does. ``diff.py``'s existing changed-file-only use
of this endpoint never approaches that ceiling, but this module's full-repo
listing (up to this plan's own 20,000-file full-run ceiling) is far more
exposed to it — ``fetch_tree`` checks the response's own ``truncated``
field and raises ``indexing.tree.RepoTreeTruncatedError`` rather than
silently returning a partial tree. Any ``GitHubGitDataFetchError`` the
underlying ``fetch_tree_listing`` call raises for a more ordinary reason
(unreachable, or a malformed response) is likewise caught and re-raised as
the domain-defined ``indexing.tree.RepoTreeFetchError`` — never a raw
adapter-level exception type, for the same zero-infra-imports reason
``fetch_content``'s own translation below exists.

**Directory entries are not files:** the Git Trees API's recursive listing
includes ``"type": "tree"`` entries for directories, which carry no
``blob_sha``/``size`` a ``RepoTreeEntry`` could represent — these are
filtered out of ``fetch_tree``'s return value entirely, never included as
zero-value/placeholder entries.

**Symlink/submodule content-fetch guard, shared with ``diff.py``:**
``fetch_content`` skips a ``120000`` (symlink) or ``160000`` (submodule)
mode entry entirely (no blob fetched) via the shared
``adapter/github/git_data.py`` helper's own guard — the same one
``GitHubDiffFetcher`` already applies, now shared rather than a gap unique
to this tree-scoped fetch (an arbitrary-path fetch without this guard could
issue wasted/failing Blob API calls against non-file tree entries).

**Partial-fetch-failure semantics:** if ``fetch_content``'s underlying blob
fetch fails for even one path in a requested set, the whole call raises
(via the shared helper's own cancel-on-failure behavior) rather than
silently returning a partial ``dict[str, bytes]`` with failing paths
missing — a silently incomplete content set is a worse failure mode (an
incomplete index that looks complete) than a job that fails visibly and
retries. Every ``adapter.github.exceptions.GitHubGitDataFetchError``
subclass ``fetch_blobs`` can raise for this call (unreachable, malformed
response, or an oversized blob) is caught here and re-raised as the one
domain-defined ``indexing.tree.RepoTreeContentFetchError`` — never a raw
adapter-level exception type, which ``indexing/pipeline.py`` (a domain
package) may not import, per ``docs/rules/architecture.md``'s
zero-infra-imports rule.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import GitHubGitDataFetchError, GitHubGitDataMalformedResponseError
from adapter.github.git_data import BlobFetchEntry, fetch_blobs, fetch_tree_listing
from adapter.github.http import auth_headers, client_context, split_owner_repo
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)

logger = logging.getLogger(__name__)

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
_DEFAULT_BLOB_FETCH_CONCURRENCY = 10
_TREE_TYPE = "tree"


def _split_owner_repo(repo_full_name: str) -> tuple[str, str]:
    """Wraps ``adapter/github/http.py``'s shared ``split_owner_repo`` in
    this module's own typed error instead of an unhandled ``ValueError`` —
    mirrors ``adapter/github/diff.py``'s own identical helper.
    """
    try:
        return split_owner_repo(repo_full_name)
    except ValueError as exc:
        raise GitHubGitDataMalformedResponseError(detail=str(exc)) from exc


def _to_repo_tree_entry(raw_entry: dict[str, Any]) -> RepoTreeEntry | None:
    """Converts one raw Git Trees API entry into a ``RepoTreeEntry``, or
    ``None`` for a directory (``"type": "tree"``) entry — not a file, so
    nothing this domain model can represent.
    """
    try:
        entry_type = raw_entry["type"]
        path = raw_entry["path"]
        mode = raw_entry["mode"]
    except (KeyError, TypeError) as exc:
        raise GitHubGitDataMalformedResponseError(detail=str(exc)) from exc
    if entry_type == _TREE_TYPE:
        return None
    try:
        blob_sha = raw_entry["sha"]
    except (KeyError, TypeError) as exc:
        raise GitHubGitDataMalformedResponseError(detail=str(exc)) from exc
    return RepoTreeEntry(path=path, mode=mode, blob_sha=blob_sha, size=raw_entry.get("size"))


class GitHubTreeFetcher:
    """``indexing.tree.RepoTreeFetcher`` implementation backed by GitHub's
    REST API. Constructed once at the composition root
    (``apps/worker/main.py``, Phase 7) with a shared
    ``GitHubInstallationTokenProvider`` — mirrors
    ``adapter.github.diff.GitHubDiffFetcher``'s own construction shape (a
    single long-lived Worker process handles many different
    installations'/repos' jobs, so every call takes the job's own
    ``installation_id``/``repo_full_name`` explicitly).
    """

    def __init__(
        self,
        *,
        token_provider: GitHubInstallationTokenProvider,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        blob_fetch_concurrency: int = _DEFAULT_BLOB_FETCH_CONCURRENCY,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._github_api_base_url = github_api_base_url
        self._timeout = timeout
        self._max_retry_attempts = max_retry_attempts
        self._blob_fetch_concurrency = blob_fetch_concurrency
        # Optional shared, long-lived client (constructed once at the
        # composition root). See adapter/github/http.py's `client_context`.
        self._client = client

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        owner, repo = _split_owner_repo(repo_full_name)

        try:
            async with client_context(self._client, timeout=self._timeout) as client:
                raw_entries, truncated = await fetch_tree_listing(
                    client,
                    github_api_base_url=self._github_api_base_url,
                    owner=owner,
                    repo=repo,
                    headers=headers,
                    commit_sha=commit_sha,
                    max_retry_attempts=self._max_retry_attempts,
                )
        except GitHubGitDataFetchError as exc:
            raise RepoTreeFetchError(
                repo_full_name=repo_full_name, commit_sha=commit_sha, detail=str(exc)
            ) from exc

        if truncated:
            logger.error(
                "tree fetch: Git Trees API response was truncated",
                extra={"repo_full_name": repo_full_name, "commit_sha": commit_sha},
            )
            raise RepoTreeTruncatedError(repo_full_name=repo_full_name, commit_sha=commit_sha)

        entries = [_to_repo_tree_entry(raw_entry) for raw_entry in raw_entries]
        return tuple(entry for entry in entries if entry is not None)

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        owner, repo = _split_owner_repo(repo_full_name)
        entries = [
            BlobFetchEntry(
                path=entry.path, mode=entry.mode, blob_sha=entry.blob_sha, size=entry.size
            )
            for entry in paths_with_blob_sha
        ]

        try:
            async with client_context(self._client, timeout=self._timeout) as client:
                return await fetch_blobs(
                    client,
                    github_api_base_url=self._github_api_base_url,
                    owner=owner,
                    repo=repo,
                    headers=headers,
                    entries=entries,
                    concurrency=self._blob_fetch_concurrency,
                    max_retry_attempts=self._max_retry_attempts,
                )
        except GitHubGitDataFetchError as exc:
            raise RepoTreeContentFetchError(repo_full_name=repo_full_name, detail=str(exc)) from exc


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[RepoTreeFetcher] = GitHubTreeFetcher
