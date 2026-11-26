"""indexing.tree — the ``RepoTreeEntry`` data model and the
``RepoTreeFetcher`` Protocol operating over it.

**Placement — ``indexing/tree.py``, not ``ingestion/tree.py``:**
[ADR-0006](../docs/adr/0006-protocol-defined-at-data-model.md)'s exception
for defining a Protocol away from its consumer only applies with more than
one real consumer sharing an arbitrary "first consumer" home; this
Protocol has exactly one named consumer (``indexing/pipeline.py``),
so ADR-0006's own default rule — defined at the consumer — applies
unmodified.

``indexing`` has zero infra imports (no ``httpx``/``sqlalchemy``/etc.), per
``docs/rules/architecture.md``'s domain-package rule — ``adapter/github/tree.py``
is the one concrete implementation, wired in only at the
composition root (``apps/worker/main.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class RepoTreeEntry(BaseModel):
    """One file or submodule-gitlink entry (directory/``tree``-type entries
    are excluded — they aren't files) from a repo's full Git tree listing
    at one commit.

    ``mode`` is the real octal git mode (``100644``/``100755``/``120000``/
    ``160000``) — the same field ``ingestion.diff.DiffFile.mode`` sources
    from the same Git Trees API — so a symlink (``120000``) or submodule
    (``160000``) entry is distinguishable before any content is fetched for
    it (see ``adapter/github/tree.py``'s content-fetch guard, shared with
    ``adapter/github/diff.py`` via ``adapter/github/git_data.py``).

    ``size`` is ``None`` for a submodule (``160000``) gitlink entry — the
    Git Trees API's own ``size`` field only applies to blob (file) entries,
    never to a commit-type gitlink reference.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    mode: str
    blob_sha: str
    size: int | None = None


class RepoTreeTruncatedError(Exception):
    """Raised when GitHub's Git Trees API response is truncated (that
    API's own 100,000-entry/7MB ceiling) — part of
    ``RepoTreeFetcher.fetch_tree``'s failure contract.

    ``ingestion.diff.DiffFetcher``'s changed-file-only use of this same
    endpoint never approaches that ceiling, but ``RepoTreeFetcher.fetch_tree``'s
    full-repo listing (up to the 20,000-file full-run ceiling) is far more exposed to it. Raised
    rather than silently returning a partial tree — a caller
    (``indexing/pipeline.py``) must treat this the same as any other
    file-set-resolution failure for that job (logged, job marked
    ``FAILED``), never a silently incomplete index.
    """

    def __init__(self, *, repo_full_name: str, commit_sha: str) -> None:
        super().__init__(f"Git Trees API response for {repo_full_name}@{commit_sha} was truncated")
        self.repo_full_name = repo_full_name
        self.commit_sha = commit_sha


class RepoTreeFetchError(Exception):
    """Raised when ``RepoTreeFetcher.fetch_tree`` fails to reach the Git
    Trees API or gets back a malformed response — part of that method's
    own failure contract, alongside ``RepoTreeTruncatedError``. Treated the
    same as ``RepoTreeTruncatedError`` by every caller: a connectivity
    hiccup fetching a repo's tree (the current commit, or, in US8's
    full-tree-diff fallback, the previously-indexed commit, which may
    no longer be resolvable after a force-push/history rewrite) is exactly
    as job-fatal as a truncated response, never a silently-swallowed
    "genuine bug" that leaves the job stuck ``CLAIMED`` forever instead of
    cleanly marked ``FAILED`` and naturally retried on the next push.
    """

    def __init__(self, *, repo_full_name: str, commit_sha: str, detail: str) -> None:
        super().__init__(f"tree fetch for {repo_full_name}@{commit_sha} failed: {detail}")
        self.repo_full_name = repo_full_name
        self.commit_sha = commit_sha
        self.detail = detail


class RepoTreeContentFetchError(Exception):
    """Raised when ``RepoTreeFetcher.fetch_content`` fails to fetch content
    for the requested path set — part of that method's own failure
    contract. The underlying fetch fails the *whole* call rather than
    returning a silently partial result (a deliberate choice — see
    ``adapter/github/tree.py``'s own docstring), so a caller
    (``indexing/pipeline.py``) that wants a bad file among many to not
    block the rest of a push must catch this one typed exception around
    the batch and treat every requested path as skipped-for-this-push,
    never a raw adapter-level exception type this domain package may not
    import.
    """

    def __init__(self, *, repo_full_name: str, detail: str) -> None:
        super().__init__(f"content fetch for {repo_full_name} failed: {detail}")
        self.repo_full_name = repo_full_name
        self.detail = detail


class RepoTreeFetcher(Protocol):
    """Fetches a repo's full file tree at one commit, and (for an
    arbitrary, non-diff-scoped path set) real file content. Implemented by
    ``adapter/github/tree.py``'s ``GitHubTreeFetcher``.

    Justified as a domain-defined Protocol regardless of implementation
    count, ``indexing/pipeline.py`` (this Protocol's one consumer) is a
    domain package and may not import ``adapter/github/tree.py``
    (which imports ``httpx``) directly — the
    actual, sufficient justification is ``architecture.md``'s own
    zero-infra-imports domain-boundary rule, independent of whether a
    second implementation of this Protocol ever ships.
    """

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        """Fetch every file's real git mode/blob sha/size at
        ``commit_sha`` (one Git Trees API call, recursive) — the full-run
        and full-tree-fallback (US8) case. Raises ``RepoTreeTruncatedError``
        if the underlying Git Trees API response was truncated, rather than
        returning a silently partial tuple, or ``RepoTreeFetchError`` if the
        call couldn't be completed at all (unreachable, or a malformed
        response).
        """
        ...

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        """Fetch real file content for an arbitrary (not diff-scoped) set
        of ``RepoTreeEntry`` items via the Git Blobs API, keyed by each
        entry's ``path`` (a blob shared by several paths is fetched once and
        reused). A symlink (``120000``) or
        submodule (``160000``) entry is skipped entirely (no blob fetched),
        even if it's present in ``paths_with_blob_sha``. Raises
        ``RepoTreeContentFetchError`` for the whole call (never a silently
        partial result) if even one requested blob's fetch fails.
        """
        ...
