"""adapter.github.git_data — shared, low-level GitHub Git Data API (Trees +
Blobs) helpers, extracted out of ``adapter/github/diff.py``'s previously
inlined logic per the indexing delivery plan's Phase 2 section ("Extract a
shared blob-fetch helper", corrected during an independent reuse review
which found the original draft's reuse claim didn't hold up: none of this
was actually factored into an importable helper before this phase).

Two things are shared here, each used by both ``adapter/github/diff.py``
(``GitHubDiffFetcher``, PR-diff-scoped fetch) and ``adapter/github/tree.py``
(``GitHubTreeFetcher``, indexing's arbitrary/full-tree-scoped fetch, Phase 2)
— genuinely the same GitHub API calls and response shapes, reused rather
than duplicated:

- ``fetch_tree_listing``: the one ``GET .../git/trees/{sha}?recursive=1``
  call and its ``tree``/``truncated`` response-body parsing. ``diff.py``
  already made this exact call for its own changed-file mode/blob-sha
  cross-reference; ``tree.py``'s full-listing fetch (US8) reuses it rather
  than issuing a second, independent implementation.
- ``fetch_blobs``: the symlink (``120000``)/submodule (``160000``) mode
  guard, the ~100MB per-blob size ceiling check, and the
  semaphore-bounded concurrent ``GET .../git/blobs/{sha}`` fetch — fails
  the whole call (cancelling every other in-flight fetch) if even one
  requested blob's fetch fails, rather than silently returning a partial
  ``dict[str, bytes]`` with failing paths missing: a silently incomplete
  content set is a worse failure mode than a job that fails visibly and
  retries.

Both route through ``adapter/github/http.py``'s shared
``request_with_retry`` — the same "one shared retry/backoff +
redacted-logging helper every ``adapter.github`` GitHub API call routes
through" every other call in this package already uses — and raise the
shared ``GitHubGitDataFetchError`` family (``adapter/github/exceptions.py``).
``diff.py`` translates these into its own pre-existing
``GitHubDiffFetchUnreachableError``/``GitHubDiffFetchMalformedResponseError``
at its own call sites, so its exception contract is unchanged by this
extraction; ``tree.py`` (a new adapter with no pre-existing contract to
preserve) raises them directly.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Sequence
from typing import Any, NamedTuple

import httpx

from adapter.github.exceptions import (
    GitHubBlobTooLargeError,
    GitHubGitDataMalformedResponseError,
    GitHubGitDataUnreachableError,
    GitHubPermanentAPIError,
    GitHubRetryExhaustedError,
)
from adapter.github.http import request_with_retry

logger = logging.getLogger(__name__)

# The Git Blobs API's documented ceiling is ~100MB; a blob at or beyond
# this is treated as fatal (never fetched) rather than fetched and found to
# fail.
BLOB_SIZE_CEILING_BYTES = 100_000_000
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubGitDataMalformedResponseError(detail=str(exc)) from exc


async def _request_or_unreachable(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    max_attempts: int,
) -> httpx.Response:
    try:
        return await request_with_retry(
            client, method, url, headers=headers, max_attempts=max_attempts
        )
    except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
        raise GitHubGitDataUnreachableError(detail=str(exc)) from exc


async def fetch_tree_listing(
    client: httpx.AsyncClient,
    *,
    github_api_base_url: str,
    owner: str,
    repo: str,
    headers: dict[str, str],
    commit_sha: str,
    max_retry_attempts: int,
) -> tuple[list[dict[str, Any]], bool]:
    """The one ``GET /repos/{owner}/{repo}/git/trees/{commit_sha}?recursive=1``
    call, returning the raw ``tree`` entry list and the response's own
    ``truncated`` flag — shared by ``diff.py``'s changed-file mode/blob-sha
    cross-reference and ``tree.py``'s full-listing fetch (US8).

    Raises ``GitHubGitDataUnreachableError``/``GitHubGitDataMalformedResponseError``
    on failure — never a raw ``httpx``/transport exception or a KeyError
    from an unexpected response shape.
    """
    response = await _request_or_unreachable(
        client,
        "GET",
        f"{github_api_base_url}/repos/{owner}/{repo}/git/trees/{commit_sha}?recursive=1",
        headers=headers,
        max_attempts=max_retry_attempts,
    )
    data = _parse_json(response)
    try:
        tree_entries = data["tree"]
        truncated = bool(data["truncated"])
    except (KeyError, TypeError) as exc:
        raise GitHubGitDataMalformedResponseError(detail=str(exc)) from exc
    if not isinstance(tree_entries, list):
        raise GitHubGitDataMalformedResponseError(
            detail="tree response 'tree' field was not a list"
        )
    return tree_entries, truncated


class BlobFetchEntry(NamedTuple):
    """One path to (maybe) fetch content for — the shared shape both
    ``diff.py``'s ``DiffFile``-derived entries and ``tree.py``'s
    ``RepoTreeEntry``-derived entries are adapted to before calling
    ``fetch_blobs``, decoupling this shared helper from either caller's own
    domain model.
    """

    path: str
    mode: str | None
    blob_sha: str | None
    size: int | None


async def fetch_blobs(
    client: httpx.AsyncClient,
    *,
    github_api_base_url: str,
    owner: str,
    repo: str,
    headers: dict[str, str],
    entries: Sequence[BlobFetchEntry],
    concurrency: int,
    max_retry_attempts: int,
) -> dict[str, bytes]:
    """Fetch content for every entry in ``entries`` via the Git Blobs API,
    keyed by path. Blobs are requested by ``blob_sha`` (never by path,
    sidestepping any need to URL-encode a caller-controlled filename into the
    request) and the decoded content is reused for every path sharing a sha.

    A ``120000`` (symlink) or ``160000`` (submodule) mode entry, or an
    entry with no ``blob_sha`` (a removed file, or a caller error), is
    skipped entirely — no blob fetched, logged at ``info``. A surviving
    entry's ``size`` must be known: ``None`` is treated the same as
    at/over the ceiling (``GitHubGitDataMalformedResponseError``) rather
    than silently bypassing the check, since the Git Trees API's own
    contract only omits ``size`` for non-blob entries (already filtered
    above by mode) — a blob-mode entry with no ``size`` is an unexpected
    response shape, not a safe-to-fetch unknown. A surviving entry at/over
    ``BLOB_SIZE_CEILING_BYTES`` is fatal (``GitHubBlobTooLargeError``),
    checked against the caller-supplied ``size`` *before* attempting the
    blob call — never a silently skipped/empty file.

    Entries sharing the same ``blob_sha`` (byte-identical content, e.g.
    duplicated files or a byte-identical rename) are fetched exactly once
    and the decoded content is reused for every path — never one redundant
    GitHub API call per duplicate path.

    Bounded to ``concurrency`` concurrent in-flight blob fetches (one per
    distinct ``blob_sha``, not per path). If any fetch fails, every other
    in-flight fetch is cancelled and the failure propagates — a partial
    result ``dict`` is never returned.
    """
    paths_by_blob_sha: dict[str, list[str]] = {}
    for entry in entries:
        if entry.blob_sha is None:
            # Not present in the caller's own source (caller error) or a
            # removed file (no blob_sha) — nothing to fetch either way.
            continue
        if entry.mode in (SYMLINK_MODE, SUBMODULE_MODE):
            logger.info(
                "materialization excluded: symlink or submodule entry",
                extra={"path": entry.path, "mode": entry.mode},
            )
            continue
        if entry.size is None:
            raise GitHubGitDataMalformedResponseError(
                detail=f"{entry.path}: tree entry has no size; cannot enforce the blob size ceiling"
            )
        if entry.size >= BLOB_SIZE_CEILING_BYTES:
            logger.error(
                "blob fetch: blob at/over the Git Blobs API size ceiling",
                extra={"path": entry.path, "blob_size_bytes": entry.size},
            )
            raise GitHubBlobTooLargeError(
                detail=(
                    f"{entry.path} is {entry.size} bytes, at/over the ~100MB Git Blobs API ceiling"
                )
            )
        paths_by_blob_sha.setdefault(entry.blob_sha, []).append(entry.path)

    semaphore = asyncio.Semaphore(concurrency)
    result: dict[str, bytes] = {}

    async def _fetch_one(blob_sha: str, paths: list[str]) -> None:
        async with semaphore:
            response = await _request_or_unreachable(
                client,
                "GET",
                f"{github_api_base_url}/repos/{owner}/{repo}/git/blobs/{blob_sha}",
                headers=headers,
                max_attempts=max_retry_attempts,
            )
        data = _parse_json(response)
        try:
            content_b64 = data["content"]
            content = base64.b64decode(content_b64)
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubGitDataMalformedResponseError(detail=str(exc)) from exc
        for path in paths:
            result[path] = content

    tasks = [
        asyncio.create_task(_fetch_one(blob_sha, paths))
        for blob_sha, paths in paths_by_blob_sha.items()
    ]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return result
