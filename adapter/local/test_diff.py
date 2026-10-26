"""Integration tests for adapter.local.git and adapter.local.diff against real git repositories.

Part of the US-L1 story (`apps/local_cli/test_local_review.py` runs the whole pipeline): the
shapes a developer or a CI job really has - a feature branch, a detached HEAD, a CI merge ref and
a shallow clone where the base ref is missing (the likely real-world "bad base ref"). Needs the
``git`` binary; skips with a reason when it is absent.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from adapter.local.conftest import (
    FEATURE_APP,
    Repo,
    ci_merge_ref_repo,
    detached_head_repo,
    feature_branch_repo,
    init_repo,
    requires_git,
    shallow_clone_without_base,
)
from adapter.local.diff import LocalGitDiffFetcher
from adapter.local.exceptions import (
    GitCommandError,
    GitRefNotFoundError,
    NoMergeBaseError,
    NotAGitRepositoryError,
)
from adapter.local.git import (
    ReviewRange,
    resolve_repo_root,
    resolve_review_range,
    resolve_staged_range,
    run_git,
)
from ingestion.diff import SUBMODULE_MODE, SYMLINK_MODE, Diff, DiffContentUnavailableError

pytestmark = [pytest.mark.integration, requires_git]


async def _fetch(repo: Repo, base: str = "main") -> tuple[Diff, LocalGitDiffFetcher, ReviewRange]:
    review_range = await resolve_review_range(repo.path, base, environ=os.environ)
    fetcher = LocalGitDiffFetcher(repo.path, review_range, environ=os.environ)
    diff = await fetcher.fetch(
        installation_id=0, repo_full_name="local/x", pr_number=0, head_sha=review_range.head_sha
    )
    return diff, fetcher, review_range


@pytest.mark.asyncio
async def test_branch_diff_has_path_status_hunks_mode_and_blob(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    diff, fetcher, review_range = await _fetch(repo)

    assert review_range.has_changes
    [file] = diff.files
    assert (file.path, file.status, file.mode) == ("app.py", "modified", "100644")
    assert file.patch is not None
    assert file.patch.startswith("@@ ")
    assert "+    result = 0" in file.patch
    assert file.blob_sha == repo.git("rev-parse", "HEAD:app.py")
    assert file.blob_size_bytes == len(FEATURE_APP)
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=["app.py"]
    )
    assert content == {"app.py": FEATURE_APP.encode()}


@pytest.mark.asyncio
async def test_diff_is_against_the_merge_base_not_the_base_tip(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    repo.git("checkout", "-q", "main")
    repo.write("other.py", "x = 1\n")
    repo.commit("main moves on")
    repo.git("checkout", "-q", "feature")

    diff, _, _ = await _fetch(repo)

    assert [f.path for f in diff.files] == ["app.py"]


@pytest.mark.asyncio
async def test_added_removed_renamed_symlink_and_binary_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("gone.py", "print('bye')\n")
    repo.write("before.py", "".join(f"line {i}\n" for i in range(30)))
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    (repo.path / "gone.py").unlink()
    repo.git("mv", "before.py", "after.py")
    repo.write("added.py", "x = 1\n")
    os.symlink("added.py", repo.path / "link.py")
    (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    repo.commit("mixed changes")

    diff, fetcher, _ = await _fetch(repo)

    by_path = {f.path: f for f in diff.files}
    assert by_path["gone.py"].status == "removed"
    assert (by_path["gone.py"].mode, by_path["gone.py"].blob_sha) == (None, None)
    assert by_path["after.py"].status == "renamed"
    assert by_path["after.py"].previous_path == "before.py"
    assert by_path["added.py"].status == "added"
    assert by_path["link.py"].mode == SYMLINK_MODE
    assert by_path["blob.bin"].patch is None
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=list(by_path)
    )
    # A removed file has no content and a symlink is never read (the mode guard).
    assert set(content) == {"after.py", "added.py", "blob.bin"}


@pytest.mark.asyncio
async def test_a_submodule_entry_is_listed_but_never_read(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.commit("base")
    sub_commit = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "-b", "feature")
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{sub_commit},vendor/lib")
    repo.git("commit", "-q", "-m", "add gitlink")

    diff, fetcher, _ = await _fetch(repo)

    [file] = diff.files
    assert (file.path, file.mode) == ("vendor/lib", SUBMODULE_MODE)
    assert file.blob_size_bytes is None
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=["vendor/lib"]
    )
    assert content == {}


@pytest.mark.asyncio
async def test_no_changes_against_the_base(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")

    review_range = await resolve_review_range(repo.path, "main", environ=os.environ)

    assert not review_range.has_changes


@pytest.mark.asyncio
async def test_detached_head_works(tmp_path: Path) -> None:
    repo = detached_head_repo(tmp_path / "repo")

    diff, _, _ = await _fetch(repo)

    assert [f.path for f in diff.files] == ["app.py"]


@pytest.mark.asyncio
async def test_ci_merge_ref_reviews_only_the_pull_requests_changes(tmp_path: Path) -> None:
    repo = ci_merge_ref_repo(tmp_path / "repo")

    diff, _, review_range = await _fetch(repo, base="main")

    assert [f.path for f in diff.files] == ["app.py"]
    assert review_range.head_sha == repo.git("rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_missing_base_ref_is_named(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    with pytest.raises(GitRefNotFoundError, match="no-such-branch") as caught:
        await resolve_review_range(repo.path, "no-such-branch", environ=os.environ)

    assert not caught.value.shallow


@pytest.mark.asyncio
async def test_a_base_that_looks_like_an_option_is_not_passed_to_git(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    with pytest.raises(GitRefNotFoundError, match="--output=x"):
        await resolve_review_range(repo.path, "--output=x", environ=os.environ)

    assert not (repo.path / "x").exists()


@pytest.mark.asyncio
async def test_not_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(NotAGitRepositoryError):
        await resolve_review_range(plain, "main", environ=os.environ)


def _clone(origin: Repo, destination: Path, *args: str) -> Repo:
    subprocess.run(
        ["git", "clone", "-q", *args, f"file://{origin.path}", str(destination)],
        check=True,
        capture_output=True,
    )
    return Repo(destination)


@pytest.mark.asyncio
async def test_shallow_clone_missing_the_base_ref_says_to_fetch_it(tmp_path: Path) -> None:
    origin = feature_branch_repo(tmp_path / "origin")

    shallow = shallow_clone_without_base(origin, tmp_path / "shallow")

    with pytest.raises(GitRefNotFoundError, match="origin/main") as caught:
        await resolve_review_range(shallow.path, "origin/main", environ=os.environ)
    assert caught.value.shallow
    assert "shallow" in str(caught.value)


@pytest.mark.asyncio
async def test_shallow_clone_with_no_shared_history_reports_no_merge_base(tmp_path: Path) -> None:
    origin = feature_branch_repo(tmp_path / "origin")
    origin.git("checkout", "-q", "main")
    origin.write("later.py", "y = 2\n")
    origin.commit("main moves on")
    shallow = _clone(origin, tmp_path / "shallow", "--depth", "1", "--branch", "feature")
    shallow.git("fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")

    with pytest.raises(NoMergeBaseError, match="shallow"):
        await resolve_review_range(shallow.path, "origin/main", environ=os.environ)

    shallow.git("fetch", "-q", "--unshallow", "origin")
    assert (await resolve_review_range(shallow.path, "origin/main", environ=os.environ)).has_changes


@pytest.mark.asyncio
async def test_run_git_kills_a_command_that_outlives_its_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    started = time.monotonic()

    with pytest.raises(GitCommandError, match="timed out"):
        await run_git(repo.path, "-c", "alias.nap=!sleep 2", "nap", timeout=0.2, environ=os.environ)

    assert time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_run_git_stops_reading_at_the_output_cap(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    with pytest.raises(GitCommandError, match="exceeded 5 bytes"):
        await run_git(
            repo.path, "cat-file", "blob", "HEAD:app.py", max_output_bytes=5, environ=os.environ
        )


@pytest.mark.asyncio
async def test_fetch_wraps_a_git_failure_as_content_unavailable(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    review_range = await resolve_review_range(repo.path, "main", environ=os.environ)
    fetcher = LocalGitDiffFetcher(repo.path, review_range, environ=os.environ)

    with pytest.raises(DiffContentUnavailableError):
        await fetcher.fetch(
            installation_id=0, repo_full_name="local/x", pr_number=0, head_sha="f" * 40
        )


@pytest.mark.asyncio
async def test_content_is_keyed_by_path_and_identical_files_each_get_it(tmp_path: Path) -> None:
    """Every consumer (static analysis, clone detection, fix generation) looks content up by path,
    as ``GitHubDiffFetcher`` returns it; two paths with the same bytes share one blob."""
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("one.py", "same = 1\n")
    repo.write("two.py", "same = 1\n")
    repo.commit("add twins")

    diff, fetcher, _ = await _fetch(repo)
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=["one.py", "two.py"]
    )

    assert content == {"one.py": b"same = 1\n", "two.py": b"same = 1\n"}


@pytest.mark.asyncio
async def test_staged_diff_holds_only_the_index_against_head_not_unstaged_or_committed_work(
    tmp_path: Path,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")  # app.py is already rewritten and committed
    repo.write("staged.py", "staged = 1\n")
    repo.git("add", "staged.py")
    repo.write("app.py", FEATURE_APP + "# unstaged edit\n")  # modified, never added
    repo.write("untracked.py", "u = 1\n")

    review_range = await resolve_staged_range(repo.path, environ=os.environ)
    fetcher = LocalGitDiffFetcher(repo.path, review_range, environ=os.environ)
    diff = await fetcher.fetch(
        installation_id=0, repo_full_name="local/x", pr_number=0, head_sha=review_range.head_sha
    )

    assert review_range.staged and review_range.has_changes
    assert review_range.merge_base_sha == review_range.head_sha == repo.git("rev-parse", "HEAD")
    assert [(f.path, f.status) for f in diff.files] == [("staged.py", "added")]
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=["staged.py", "app.py"]
    )
    assert content == {"staged.py": b"staged = 1\n"}


@pytest.mark.asyncio
async def test_staged_with_nothing_staged_has_no_changes(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    repo.write("app.py", FEATURE_APP + "# unstaged edit\n")

    review_range = await resolve_staged_range(repo.path, environ=os.environ)

    assert review_range.staged and not review_range.has_changes


def test_an_unmerged_entry_asks_for_the_conflicts_to_be_resolved() -> None:
    from adapter.local.diff import parse_raw_diff
    from ingestion.diff import DiffContentMalformedError

    record = b":000000 000000 " + b"0" * 40 + b" " + b"0" * 40 + b" U\0app.py\0"

    with pytest.raises(DiffContentMalformedError, match="resolve the conflicts"):
        parse_raw_diff(record, repo_full_name="local/x")


@pytest.mark.integration
@requires_git
@pytest.mark.asyncio
async def test_a_run_started_in_a_subdirectory_resolves_to_the_repository_root(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    sub = repo.path / "services" / "api"
    sub.mkdir(parents=True)

    assert (await resolve_repo_root(sub, environ=os.environ)).resolve() == repo.path.resolve()


@pytest.mark.integration
@requires_git
@pytest.mark.asyncio
async def test_a_directory_outside_any_repository_is_returned_unchanged(tmp_path: Path) -> None:
    assert await resolve_repo_root(tmp_path, environ=os.environ) == tmp_path
