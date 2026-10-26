"""Tests for the --output file rules: the path is validated before any model call and written
without following a symlink. The tracked-file rule needs a real repository, so those tests are
integration tests and need ``git`` on PATH."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adapter.local.conftest import feature_branch_repo, init_repo, requires_git
from adapter.local.output import (
    OutputFormat,
    OutputPathError,
    check_output_path,
    discard_stale_output,
    write_output,
)


@pytest.mark.integration
@requires_git
class TestCheckOutputPath:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("r.md", OutputFormat.MARKDOWN),
            ("r.MARKDOWN", OutputFormat.MARKDOWN),
            ("r.json", OutputFormat.JSON),
        ],
    )
    async def test_the_extension_selects_the_format(
        self, tmp_path: Path, name: str, expected: OutputFormat
    ) -> None:
        repo = init_repo(tmp_path / "repo")

        assert await check_output_path(repo.path, repo.path / name, environ=os.environ) is expected

    @pytest.mark.asyncio
    async def test_an_unknown_extension_is_refused(self, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "repo")

        with pytest.raises(OutputPathError, match=r"\.md, \.markdown or \.json"):
            await check_output_path(repo.path, repo.path / "r.txt", environ=os.environ)

    @pytest.mark.asyncio
    async def test_a_path_outside_any_repository_is_allowed(self, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "repo")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        assert (
            await check_output_path(repo.path, elsewhere / "r.md", environ=os.environ)
            is OutputFormat.MARKDOWN
        )

    @pytest.mark.asyncio
    async def test_a_dangling_symlink_is_refused(self, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "repo")
        link = repo.path / "r.md"
        link.symlink_to(tmp_path / "does-not-exist")

        with pytest.raises(OutputPathError, match="symbolic link"):
            await check_output_path(repo.path, link, environ=os.environ)

    @pytest.mark.asyncio
    async def test_a_path_through_a_symlinked_directory_is_refused(self, tmp_path: Path) -> None:
        repo = feature_branch_repo(tmp_path / "repo")
        repo.write("README.md", "tracked")
        repo.git("add", "README.md")
        (repo.path / "link").symlink_to(".")

        with pytest.raises(OutputPathError, match="symbolic link"):
            await check_output_path(repo.path, repo.path / "link" / "README.md", environ=os.environ)

    @pytest.mark.asyncio
    async def test_glob_characters_in_the_name_are_literal(self, tmp_path: Path) -> None:
        repo = feature_branch_repo(tmp_path / "repo")
        repo.write("a.md", "tracked")
        repo.git("add", "a.md")

        assert (
            await check_output_path(repo.path, repo.path / "*.md", environ=os.environ)
            is OutputFormat.MARKDOWN
        )

    @pytest.mark.asyncio
    async def test_a_staged_but_uncommitted_file_counts_as_tracked(self, tmp_path: Path) -> None:
        repo = feature_branch_repo(tmp_path / "repo")
        repo.write("new.md", "x")
        repo.git("add", "new.md")

        with pytest.raises(OutputPathError, match="tracked by git"):
            await check_output_path(repo.path, repo.path / "new.md", environ=os.environ)


def test_write_output_replaces_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "r.md"
    target.write_text("old, longer content")

    write_output(tmp_path, target, "new")

    assert target.read_text() == "new"


def test_write_output_will_not_follow_a_symlink_swapped_in_after_the_check(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    link = tmp_path / "r.md"
    link.symlink_to(victim)

    with pytest.raises(OSError):
        write_output(tmp_path, link, "overwritten")

    assert victim.read_text() == "precious"
    assert os.path.islink(link)


def test_write_output_will_not_write_through_a_symlinked_directory(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")

    with pytest.raises(OutputPathError):
        write_output(tmp_path, tmp_path / "link" / "r.md", "x")

    assert not (tmp_path / "real" / "r.md").exists()


def test_discard_stale_output_removes_an_old_file_and_tolerates_a_missing_one(
    tmp_path: Path,
) -> None:
    target = tmp_path / "r.json"
    target.write_text("old findings")

    discard_stale_output(target)
    discard_stale_output(target)

    assert not target.exists()


def test_discard_stale_output_removes_a_symlink_not_its_target(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("keep")
    link = tmp_path / "r.md"
    link.symlink_to(real)

    discard_stale_output(link)

    assert not link.is_symlink() and real.read_text() == "keep"
