"""adapter.github.repo_config — the concrete
``review.static_analysis.RepoConfigFetcher``: fetches allow-listed analyzer
config files (e.g. ``ruff.toml``) from the repository root at the PR head
commit, per docs/plans/plan-phase-9.md Phase 2.

Reuses ``GitHubTreeFetcher`` (any ``indexing.tree.RepoTreeFetcher``) for both
the tree listing and the blob fetch. Only exact root-level names from the
caller's allow-list are ever fetched — never a path a config or the PR names.
Symlinks/submodules, oversize files and files beyond the count cap are
skipped (logged); a truncated tree or any fetch failure raises
``RepoConfigFetchError`` so the step degrades to default profiles.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from adapter.github.exceptions import GitHubAdapterError
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)
from review.static_analysis import RepoConfigFetcher, RepoConfigFetchError

logger = logging.getLogger(__name__)

_REGULAR_FILE_MODES = frozenset({"100644", "100755"})


class GitHubRepoConfigFetcher:
    def __init__(self, tree_fetcher: RepoTreeFetcher) -> None:
        self._tree_fetcher = tree_fetcher

    async def fetch_config_files(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        commit_sha: str,
        names: Collection[str],
        max_file_bytes: int,
        max_files: int,
    ) -> dict[str, bytes]:
        try:
            tree = await self._tree_fetcher.fetch_tree(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                commit_sha=commit_sha,
            )
        except RepoTreeTruncatedError as exc:
            raise RepoConfigFetchError("tree_truncated") from exc
        except (RepoTreeFetchError, GitHubAdapterError) as exc:
            raise RepoConfigFetchError("tree_fetch_failed") from exc

        wanted = frozenset(names)
        selected: list[RepoTreeEntry] = []
        for entry in sorted(tree, key=lambda e: e.path):
            base = entry.path.rsplit("/", 1)[-1]
            if base not in wanted:
                continue
            if entry.path != base:
                logger.info(
                    "nested analyzer config ignored (repo root only)",
                    extra={"repo_full_name": repo_full_name, "config_path": entry.path},
                )
                continue
            if entry.mode not in _REGULAR_FILE_MODES:
                _skipped(repo_full_name, entry.path, "not_a_regular_file")
            elif entry.size is None or entry.size > max_file_bytes:
                _skipped(repo_full_name, entry.path, "file_too_large")
            elif len(selected) >= max_files:
                _skipped(repo_full_name, entry.path, "too_many_files")
            else:
                selected.append(entry)
        if not selected:
            return {}

        try:
            content = await self._tree_fetcher.fetch_content(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                paths_with_blob_sha=selected,
            )
        except (RepoTreeContentFetchError, GitHubAdapterError) as exc:
            raise RepoConfigFetchError("content_fetch_failed") from exc

        # Defense in depth: the tree-reported size is advisory.
        files: dict[str, bytes] = {}
        for path, data in content.items():
            if len(data) > max_file_bytes:
                _skipped(repo_full_name, path, "file_too_large")
            else:
                files[path] = data
        return files


def _skipped(repo_full_name: str, path: str, reason: str) -> None:
    logger.warning(
        "analyzer config file skipped",
        extra={
            "repo_full_name": repo_full_name,
            "config_path": path,
            "config_skip_reason": reason,
        },
    )


_: type[RepoConfigFetcher] = GitHubRepoConfigFetcher
