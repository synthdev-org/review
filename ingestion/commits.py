"""ingestion.commits — commit-listing model and the ``CommitSource`` Protocol
the optional commit-by-commit review mode consumes.

Kept separate from ``ingestion.diff.DiffFetcher`` on purpose: adding methods
there would break every existing implementation. ``adapter/github/commits.py``
and ``adapter/localgit/commits.py`` implement it; wired only at the
composition root.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ingestion.diff import Diff


class CommitInfo(BaseModel):
    """One commit of a PR. ``parent_shas`` is empty for a root commit and has
    two or more entries for a merge commit.
    """

    model_config = ConfigDict(frozen=True)

    sha: str
    parent_shas: tuple[str, ...]
    title: str = ""


class CommitsUnavailableError(Exception):
    """Commit listing or a range diff could not be obtained (host unreachable,
    retries exhausted, malformed response). Domain-owned so callers can
    degrade without importing an adapter exception type.
    """

    def __init__(self, *, repo_full_name: str, detail: str) -> None:
        super().__init__(f"commits for {repo_full_name} unavailable: {detail}")
        self.repo_full_name = repo_full_name
        self.detail = detail


class CommitSource(Protocol):
    async def list_commits(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> tuple[CommitInfo, ...]:
        """The PR's commits, oldest first."""
        ...

    async def fetch_range_diff(
        self, *, installation_id: int, repo_full_name: str, base_sha: str, head_sha: str
    ) -> Diff:
        """Diff of ``base_sha...head_sha``. Only path/status/patch are
        guaranteed; mode/blob fields may be ``None``.

        Raises ``CommitsUnavailableError`` on failure.
        """
        ...
