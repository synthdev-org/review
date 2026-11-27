"""adapter.github.diff — the concrete ``ingestion.diff.DiffFetcher``
implementation: fetches a PR's changed-file list, real git mode/blob
metadata, and (for a pre-filtered subset) actual file content, via
GitHub's REST API.

**Two calls for the changed-file list/metadata, not a per-file Contents-API
call:**

1. ``GET /repos/{owner}/{repo}/pulls/{pull_number}/files`` — paginated (via
   the response's own ``Link`` header, followed to completion; a
   single-page call would silently lose coverage of any PR touching more
   files than one page) — gives path/status/patch per changed file.
2. **One** ``GET /repos/{owner}/{repo}/git/trees/{head_sha}?recursive=1``
   call (the Git Trees API) — gives every path's real git ``mode``, blob
   ``sha``, and blob ``size`` in a single response, cross-referenced by
   path against (1)'s file list. This is the actual octal git mode the
   symlink/submodule guard needs — not a derived value from a
   differently-shaped field — and the same response's ``size`` field is
   what populates ``DiffFile.blob_size_bytes``, so pre-filter's
   aggregate-bytes ceiling has a byte figure to check against without
   fetching content first.

Real file **content** is deliberately not fetched by ``fetch()`` — see
``fetch_content`` below, called only after ``review/prefilter.py`` has
narrowed the path set, so a per-file Git Blobs API call is never spent on a
file pre-filter would have excluded anyway.

**Coverage-limit / cost-bounding guards:**

- The Files-endpoint caps at 3,000 files per PR regardless of pagination —
  the number of files actually returned is compared against the PR
  object's own ``changed_files`` count; a mismatch is fatal
  (``GitHubFileCountMismatchError``), never a silent truncation, since an
  attacker could otherwise pad a PR past the cap to push files past
  review entirely.
- The Git Trees API truncates at 100,000 entries / 7MB with no pagination
  available. Fatal only when a changed-file path has **no** matching Trees
  entry to resolve its mode (``GitHubTreeEntryMissingError``) — never
  merely because the response's own ``truncated: true`` flag is set while
  every changed-file path still resolved (the common case for an ordinary
  PR against a large monorepo). Silently defaulting a missing entry to
  "regular file" would let a truncated response's missing symlink/submodule
  entries bypass the mode-based materialization guard below.
- The Git Blobs API has a documented ~100MB per-blob size ceiling.
  ``fetch_content`` checks each surviving file's already-known
  ``blob_size_bytes`` (from the Trees API response fetched during
  ``fetch()``) against this ceiling *before* attempting the blob call —
  deterministic and doesn't depend on interpreting whatever specific error
  shape GitHub's own oversized-blob response takes. At/over the ceiling is
  fatal (``GitHubBlobTooLargeError``), never a silently skipped/empty file,
  which would let an oversized file hide a secret or vulnerable pattern
  from this platform's own static-analysis/LLM coverage.

**Materialization safety — the mode-based symlink/submodule guard:**
``fetch_content`` checks each entry's real git ``mode`` before fetching its
blob content at all: a ``120000`` (symlink) or ``160000`` (submodule) entry
is skipped entirely — no blob fetched, logged at ``info`` as excluded — a
``100644``/``100755`` (regular file/executable) entry is fetched normally.
Blob fetches are keyed by ``blob_sha``, never by path, which sidesteps any
need to URL-encode a PR-controlled filename into the fetch request —
materialization-time path handling (writing the fetched bytes to a local
temp directory) is a separate concern, handled by
``adapter/analyzers/subprocess_runner.py``, not here.

Up to ``blob_fetch_concurrency`` (default 10) Blob fetches run concurrently,
bounded by a stdlib ``asyncio.Semaphore`` — matching the restraint shown
elsewhere for GitHub API usage, avoiding both unacceptable
sequential latency and the risk of tripping GitHub's own secondary rate
limits.

**Shared with ``adapter/github/tree.py``:**
the Git Trees API listing call/parse and the mode-guard/size-ceiling/
concurrency-bounded Git Blobs API fetch described above are no longer
inlined here alone — both now route through
``adapter/github/git_data.py``'s ``fetch_tree_listing``/``fetch_blobs``,
shared with the new ``GitHubTreeFetcher`` (indexing's arbitrary/full-tree
content fetch) rather than duplicated. The three exceptions this class's
own call sites raise (``GitHubDiffFetchUnreachableError``,
``GitHubDiffFetchMalformedResponseError``, translated from the shared
helper's own failures; ``GitHubBlobTooLargeError``, raised by the shared
helper directly and never translated) are still exactly these same three
types — no call site of this class needs to change. Their *class
hierarchy* did shift, though: ``GitHubBlobTooLargeError`` is now a
``GitHubGitDataFetchError`` subclass (shared with ``GitHubTreeFetcher``),
not a ``GitHubDiffFetchError`` one — a hypothetical ``except
GitHubDiffFetchError`` written to catch every failure this class can
raise as one family would no longer catch it (nothing in this codebase
does that today).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import (
    GitHubBlobTooLargeError,
    GitHubDiffFetchMalformedResponseError,
    GitHubDiffFetchUnreachableError,
    GitHubFileCountMismatchError,
    GitHubGitDataMalformedResponseError,
    GitHubGitDataUnreachableError,
    GitHubPermanentAPIError,
    GitHubRetryExhaustedError,
    GitHubTreeEntryMissingError,
)
from adapter.github.git_data import BlobFetchEntry, fetch_blobs, fetch_tree_listing
from adapter.github.http import (
    DEFAULT_FAN_OUT_CONCURRENCY,
    auth_headers,
    client_context,
    request_with_retry,
)
from ingestion.diff import (
    Diff,
    DiffContentMalformedError,
    DiffContentUnavailableError,
    DiffFetcher,
    DiffFile,
)
from tenancy.resolution import split_owner_repo

logger = logging.getLogger(__name__)

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
# Reuses `adapter/github/http.py`'s shared `DEFAULT_FAN_OUT_CONCURRENCY`
# rather than its own
# independent `10` — see that constant's own docstring for the full "one
# shared value, not three duplicated `10`s" reasoning.
_DEFAULT_BLOB_FETCH_CONCURRENCY = DEFAULT_FAN_OUT_CONCURRENCY
_REMOVED_STATUS = "removed"


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubDiffFetchMalformedResponseError(detail=str(exc)) from exc


def _split_owner_repo(repo_full_name: str) -> tuple[str, str]:
    """Wraps ``adapter/github/http.py``'s shared ``split_owner_repo`` in
    this module's own typed error instead of an unhandled ``ValueError`` —
    ``adapter/github/schemas.py`` types ``repository.full_name`` as a bare
    ``str`` with no format constraint, so a malformed/non-standard delivery
    must fail through this module's typed-exception contract, not crash the
    caller.
    """
    try:
        return split_owner_repo(repo_full_name)
    except ValueError as exc:
        raise GitHubDiffFetchMalformedResponseError(detail=str(exc)) from exc


def _verify_changed_files_count(
    pr_response: httpx.Response,
    raw_files: list[dict[str, Any]],
    *,
    repo_full_name: str,
    pr_number: int,
) -> None:
    pr_data = _parse_json(pr_response)
    try:
        changed_files_count = int(pr_data["changed_files"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubDiffFetchMalformedResponseError(detail=str(exc)) from exc
    if len(raw_files) != changed_files_count:
        logger.error(
            "diff fetch: files-endpoint count mismatch (possible truncation/bypass)",
            extra={
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "returned_count": len(raw_files),
                "changed_files_count": changed_files_count,
            },
        )
        raise GitHubFileCountMismatchError(
            detail=(
                f"files-endpoint returned {len(raw_files)} files but the PR object "
                f"reports changed_files={changed_files_count}"
            )
        )


def _build_diff_files(
    raw_files: list[dict[str, Any]],
    tree_by_path: dict[str, dict[str, Any]],
    *,
    repo_full_name: str,
    pr_number: int,
) -> list[DiffFile]:
    diff_files: list[DiffFile] = []
    for raw_file in raw_files:
        try:
            path = raw_file["filename"]
            status = raw_file["status"]
        except (KeyError, TypeError) as exc:
            raise GitHubDiffFetchMalformedResponseError(detail=str(exc)) from exc
        patch = raw_file.get("patch")

        if status == _REMOVED_STATUS:
            # No longer exists at head_sha's tree — nothing to resolve.
            diff_files.append(DiffFile(path=path, status=status, patch=patch))
            continue

        entry = tree_by_path.get(path)
        if entry is None:
            logger.error(
                "diff fetch: changed file has no matching tree entry (possible truncation/bypass)",
                extra={"repo_full_name": repo_full_name, "pr_number": pr_number, "path": path},
            )
            raise GitHubTreeEntryMissingError(
                detail=f"changed file {path!r} has no matching Git Trees API entry"
            )
        diff_files.append(
            DiffFile(
                path=path,
                status=status,
                patch=patch,
                mode=entry.get("mode"),
                blob_sha=entry.get("sha"),
                blob_size_bytes=entry.get("size"),
            )
        )
    return diff_files


class GitHubDiffFetcher:
    """``ingestion.diff.DiffFetcher`` implementation backed by GitHub's
    REST API. Constructed once at the composition root
    (``apps/worker/main.py``) with a shared ``GitHubInstallationTokenProvider``
    — a single long-lived Worker process handles many different
    installations'/repos' jobs over its lifetime, so every call takes the
    job's own ``installation_id``/``repo_full_name`` explicitly rather than
    binding this instance to one repo at construction time.
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
        # composition root — apps/worker/main.py). Defaults to None so
        # existing callers/tests keep the prior per-call `httpx.AsyncClient`
        # behavior unchanged. See adapter/github/http.py's `client_context`.
        self._client = client

    async def _request_or_unreachable(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Shared ``request_with_retry`` call + exception translation used by
        every GitHub API call this fetcher makes: a permanent API error or an
        exhausted retry budget both become the one
        ``GitHubDiffFetchUnreachableError`` this class's own Protocol
        contract raises for "GitHub was unreachable", regardless of which
        specific endpoint failed.
        """
        try:
            return await request_with_retry(
                client, method, url, headers=headers, max_attempts=self._max_retry_attempts
            )
        except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
            raise GitHubDiffFetchUnreachableError(detail=str(exc)) from exc

    async def _fetch_tree_entries(
        self, client: httpx.AsyncClient, headers: dict[str, str], owner: str, repo: str, sha: str
    ) -> list[dict[str, Any]]:
        """Wraps ``adapter/github/git_data.py``'s shared ``fetch_tree_listing``
        in this class's own pre-existing exception contract
        (``GitHubDiffFetchUnreachableError``/``GitHubDiffFetchMalformedResponseError``)
        — the ``truncated`` flag it also returns is not consulted here:
        unlike ``adapter/github/tree.py``'s full-listing fetch,
        ``GitHubDiffFetcher`` only ever cross-references specific
        already-known changed-file paths, so a truncated response is only
        fatal if one of *those* paths is unresolvable (``GitHubTreeEntryMissingError``
        below), never merely because the flag itself is set.
        """
        try:
            tree_entries, _truncated = await fetch_tree_listing(
                client,
                github_api_base_url=self._github_api_base_url,
                owner=owner,
                repo=repo,
                headers=headers,
                commit_sha=sha,
                max_retry_attempts=self._max_retry_attempts,
            )
        except GitHubGitDataUnreachableError as exc:
            raise GitHubDiffFetchUnreachableError(detail=str(exc)) from exc
        except GitHubGitDataMalformedResponseError as exc:
            raise GitHubDiffFetchMalformedResponseError(detail=str(exc)) from exc
        return tree_entries

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        owner, repo = _split_owner_repo(repo_full_name)

        async with client_context(self._client, timeout=self._timeout) as client:
            # The PR-metadata GET (only consulted below, for
            # `changed_files_count`), the paginated files-list fetch, and
            # the Git Trees fetch are three independent GitHub API
            # round-trips — none needs another's result (the tree fetch
            # only needs `head_sha`, already known; the PR-metadata result
            # isn't used until after this line) — so all three run
            # concurrently instead of paying the PR-metadata round-trip's
            # latency serially before the other two even start.
            pr_response, raw_files, tree_entries = await asyncio.gather(
                self._request_or_unreachable(
                    client,
                    "GET",
                    f"{self._github_api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}",
                    headers=headers,
                ),
                self._fetch_all_file_pages(client, headers, owner, repo, pr_number),
                self._fetch_tree_entries(client, headers, owner, repo, head_sha),
            )
            _verify_changed_files_count(
                pr_response, raw_files, repo_full_name=repo_full_name, pr_number=pr_number
            )

            tree_by_path: dict[str, dict[str, Any]] = {
                entry["path"]: entry for entry in tree_entries if "path" in entry
            }

        diff_files = _build_diff_files(
            raw_files, tree_by_path, repo_full_name=repo_full_name, pr_number=pr_number
        )
        return Diff(files=tuple(diff_files))

    async def _fetch_all_file_pages(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        url: str | None = (
            f"{self._github_api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100"
        )
        while url is not None:
            response = await self._request_or_unreachable(client, "GET", url, headers=headers)
            page = _parse_json(response)
            if not isinstance(page, list):
                raise GitHubDiffFetchMalformedResponseError(
                    detail="files endpoint response was not a JSON list"
                )
            files.extend(page)
            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
        return files

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        """Delegates the actual mode-guard/size-ceiling/concurrency-bounded
        fetch to ``adapter/github/git_data.py``'s shared ``fetch_blobs`` —
        see the module docstring's "Shared with adapter/github/tree.py"
        section. Any path not present in ``diff.files`` is silently
        excluded before calling the shared helper (a caller error — same
        behavior as before this extraction), matching ``BlobFetchEntry``'s
        precondition that every entry it's given is real.

        **Domain-boundary translation:** every
        ``GitHubDiffFetchError`` this method's own call sites raise —
        translated from the shared ``adapter.github.git_data`` helpers'
        failures, exactly as before — is re-raised one further level as the
        domain-owned ``ingestion.diff.DiffContentMalformedError``/
        ``DiffContentUnavailableError``, per that Protocol's own updated
        failure contract: ``ingestion.diff.DiffFetcher`` is a domain
        Protocol, and a domain-package caller (the
        ``ContextBuilderStep``) must never catch an adapter-owned exception
        type by name, per architecture.md's "domain never imports adapter
        code" rule. ``GitHubBlobTooLargeError`` (a ``GitHubGitDataFetchError``
        *sibling* of ``GitHubDiffFetchError``, never translated into one —
        see this module's own docstring) gets its own separate ``except``
        clause into the same ``DiffContentUnavailableError``, rather than a
        single shared base-class catch that could never actually catch it.
        """
        try:
            return await self._fetch_content_untranslated(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                diff=diff,
                paths=paths,
            )
        except GitHubDiffFetchMalformedResponseError as err:
            raise DiffContentMalformedError(repo_full_name=repo_full_name, detail=str(err)) from err
        except GitHubDiffFetchUnreachableError as err:
            raise DiffContentUnavailableError(
                repo_full_name=repo_full_name, detail=str(err)
            ) from err
        except GitHubBlobTooLargeError as err:
            raise DiffContentUnavailableError(
                repo_full_name=repo_full_name, detail=str(err)
            ) from err

    async def _fetch_content_untranslated(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        """Token exchange plus the shared ``fetch_blobs`` call, with the
        git-data failures translated to ``GitHubDiffFetchError`` subtypes;
        ``fetch_content`` re-raises those as the domain-owned errors.

        **Named, accepted residual:** the token-exchange call below
        (``self._token_provider.get_token``) sits outside this translation
        — a ``GitHubTokenExchangeError`` still propagates untranslated, the
        same pre-existing gap ``indexing.tree.RepoTreeFetcher`` already has
        today, not something this translation is in scope to close.
        ``review/static_analysis.py`` already calls this exact
        method too and is unaffected in behavior by this change — it
        catches none of these exception types by name today, relying on
        ``review/pipeline.py``'s generic per-step exception handling — but
        the exception *type* now surfacing in its own logs/error tracking
        for this path changes silently, named here rather than left
        unacknowledged.
        """
        token = await self._token_provider.get_token(
            installation_id=installation_id, repo_full_name=repo_full_name
        )
        headers = auth_headers(token)
        owner, repo = _split_owner_repo(repo_full_name)
        entries = _blob_entries(diff, paths)

        async with client_context(self._client, timeout=self._timeout) as client:
            try:
                return await fetch_blobs(
                    client,
                    github_api_base_url=self._github_api_base_url,
                    owner_repo=(owner, repo),
                    headers=headers,
                    entries=entries,
                    concurrency=self._blob_fetch_concurrency,
                    max_retry_attempts=self._max_retry_attempts,
                )
            except GitHubGitDataUnreachableError as exc:
                raise GitHubDiffFetchUnreachableError(detail=str(exc)) from exc
            except GitHubGitDataMalformedResponseError as exc:
                raise GitHubDiffFetchMalformedResponseError(detail=str(exc)) from exc


def _blob_entries(diff: Diff, paths: Sequence[str]) -> list[BlobFetchEntry]:
    """``BlobFetchEntry`` per requested path present in ``diff.files``;
    absent paths are silently excluded (a caller error)."""
    by_path = {file.path: file for file in diff.files}
    return [
        BlobFetchEntry(
            path=path,
            mode=by_path[path].mode,
            blob_sha=by_path[path].blob_sha,
            size=by_path[path].blob_size_bytes,
        )
        for path in paths
        if path in by_path
    ]


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[DiffFetcher] = GitHubDiffFetcher
