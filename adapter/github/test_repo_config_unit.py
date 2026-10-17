"""Unit tests for adapter.github.repo_config's selection logic (allow-list,
root-only, mode, size and count caps) against a fake ``RepoTreeFetcher`` —
no HTTP. The respx cassette suite in ``test_repo_config.py`` covers the wire.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from adapter.github.repo_config import GitHubRepoConfigFetcher
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)
from review.static_analysis import RepoConfigFetchError


class _FakeTree:
    def __init__(
        self,
        entries: Sequence[RepoTreeEntry] = (),
        *,
        tree_error: Exception | None = None,
        content_error: Exception | None = None,
        content: dict[str, bytes] | None = None,
    ) -> None:
        self._entries = tuple(entries)
        self._tree_error = tree_error
        self._content_error = content_error
        self._content = content
        self.requested: list[str] = []

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        if self._tree_error:
            raise self._tree_error
        return self._entries

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        if self._content_error:
            raise self._content_error
        self.requested = [e.path for e in paths_with_blob_sha]
        if self._content is not None:
            return self._content
        return {e.path: b"x" for e in paths_with_blob_sha}


def _entry(path: str, *, mode: str = "100644", size: int | None = 5) -> RepoTreeEntry:
    return RepoTreeEntry(path=path, mode=mode, blob_sha="a" * 40, size=size)


async def _fetch(
    tree: _FakeTree, *, names: set[str] | None = None, max_bytes: int = 100, max_files: int = 2
) -> dict[str, bytes]:
    return await GitHubRepoConfigFetcher(tree).fetch_config_files(
        installation_id=1,
        repo_full_name="a/b",
        commit_sha="c" * 40,
        names=names or {"ruff.toml", "phpstan.neon"},
        max_file_bytes=max_bytes,
        max_files=max_files,
    )


@pytest.mark.asyncio
async def test_only_allow_listed_root_regular_files_are_requested() -> None:
    tree = _FakeTree(
        [
            _entry("ruff.toml"),
            _entry("pkg/ruff.toml"),  # nested: ignored
            _entry("other.toml"),  # not allow-listed
            _entry("phpstan.neon", mode="120000"),  # symlink
        ]
    )
    assert await _fetch(tree) == {"ruff.toml": b"x"}
    assert tree.requested == ["ruff.toml"]


@pytest.mark.asyncio
async def test_size_boundary_is_inclusive_and_unknown_size_is_skipped() -> None:
    tree = _FakeTree([_entry("ruff.toml", size=100), _entry("phpstan.neon", size=None)])
    assert set(await _fetch(tree)) == {"ruff.toml"}
    assert set(await _fetch(_FakeTree([_entry("ruff.toml", size=101)]))) == set()


@pytest.mark.asyncio
async def test_file_count_cap_keeps_first_by_path_order() -> None:
    names = {"a.toml", "b.toml", "c.toml"}
    tree = _FakeTree([_entry("c.toml"), _entry("a.toml"), _entry("b.toml")])
    assert set(await _fetch(tree, names=names, max_files=2)) == {"a.toml", "b.toml"}


@pytest.mark.asyncio
async def test_no_selection_skips_content_fetch() -> None:
    tree = _FakeTree(
        [_entry("README.md")],
        content_error=RepoTreeContentFetchError(repo_full_name="a/b", detail="never"),
    )
    assert await _fetch(tree) == {}


@pytest.mark.asyncio
async def test_returned_content_over_cap_is_dropped_even_if_tree_size_lied() -> None:
    tree = _FakeTree([_entry("ruff.toml")], content={"ruff.toml": b"y" * 101})
    assert await _fetch(tree) == {}


@pytest.mark.parametrize(
    ("tree_kwargs", "reason"),
    [
        (
            {"tree_error": RepoTreeTruncatedError(repo_full_name="a/b", commit_sha="c")},
            "tree_truncated",
        ),
        (
            {"tree_error": RepoTreeFetchError(repo_full_name="a/b", commit_sha="c", detail="d")},
            "tree_fetch_failed",
        ),
        (
            {
                "entries": [_entry("ruff.toml")],
                "content_error": RepoTreeContentFetchError(repo_full_name="a/b", detail="d"),
            },
            "content_fetch_failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_failures_raise_with_reason_and_chain(tree_kwargs: dict, reason: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(RepoConfigFetchError) as info:
        await _fetch(_FakeTree(**tree_kwargs))
    assert info.value.reason == reason
    assert info.value.__cause__ is not None
