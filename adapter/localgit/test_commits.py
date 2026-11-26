from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adapter.localgit.commits import LocalGitCommitSource
from ingestion.commits import CommitsUnavailableError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _commit(tmp_path, "base.py", "a = 1\n", "base")
    _git(tmp_path, "checkout", "-b", "feat")
    return tmp_path


async def _list(src: LocalGitCommitSource):  # type: ignore[no-untyped-def]
    return await src.list_commits(
        installation_id=0, repo_full_name="l/r", pr_number=0, head_sha="feat"
    )


async def test_list_commits_oldest_first_with_parents(repo: Path) -> None:
    base = _git(repo, "rev-parse", "main")
    c1 = _commit(repo, "one.py", "x = 1\n", "first commit")
    c2 = _commit(repo, "two.py", "y = 2\n", "second commit")

    commits = await _list(LocalGitCommitSource(repo, "main", "feat"))

    assert [(c.sha, c.parent_shas, c.title) for c in commits] == [
        (c1, (base,), "first commit"),
        (c2, (c1,), "second commit"),
    ]


async def test_merge_commit_has_two_parents(repo: Path) -> None:
    _commit(repo, "one.py", "x = 1\n", "feat work")
    _git(repo, "checkout", "main")
    _commit(repo, "side.py", "s = 1\n", "main work")
    _git(repo, "checkout", "feat")
    _git(repo, "merge", "--no-ff", "-m", "merge main", "main")

    commits = await _list(LocalGitCommitSource(repo, "main", "feat"))

    assert [len(c.parent_shas) for c in commits if c.title == "merge main"] == [2]


async def test_no_commits_in_range_is_empty(repo: Path) -> None:
    assert await _list(LocalGitCommitSource(repo, "main", "feat")) == ()


async def test_unknown_ref_raises_commits_unavailable(repo: Path) -> None:
    with pytest.raises(CommitsUnavailableError):
        await _list(LocalGitCommitSource(repo, "main", "nope"))


async def test_fetch_range_diff_covers_only_the_range(repo: Path) -> None:
    c1 = _commit(repo, "one.py", "x = 1\n", "first")
    c2 = _commit(repo, "two.py", "y = 2\n", "second")

    diff = await LocalGitCommitSource(repo, "main", "feat").fetch_range_diff(
        installation_id=0, repo_full_name="l/r", base_sha=c1, head_sha=c2
    )

    assert [(f.path, f.status) for f in diff.files] == [("two.py", "added")]
    assert diff.files[0].patch is not None and "+y = 2" in diff.files[0].patch


async def test_fetch_range_diff_bad_sha_raises_commits_unavailable(repo: Path) -> None:
    with pytest.raises(CommitsUnavailableError):
        await LocalGitCommitSource(repo, "main", "feat").fetch_range_diff(
            installation_id=0, repo_full_name="l/r", base_sha="deadbeef", head_sha="cafebabe"
        )


async def test_a_ref_starting_with_dash_is_not_taken_for_an_option(repo: Path) -> None:
    with pytest.raises(CommitsUnavailableError):
        await _list(LocalGitCommitSource(repo, "--output=/tmp/should-not-exist", "feat"))
