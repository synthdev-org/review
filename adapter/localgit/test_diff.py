from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adapter.localgit.diff import LocalGitDiffFetcher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "keep.py").write_text("a = 1\n")
    (tmp_path / "gone.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "feat")
    (tmp_path / "keep.py").write_text("a = 2\n")
    (tmp_path / "new.py").write_text("b = 1\n")
    (tmp_path / "gone.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change")
    return tmp_path


async def test_fetch_reports_status_patch_and_blob_metadata(repo: Path) -> None:
    fetcher = LocalGitDiffFetcher(repo, "main", "feat")

    diff = await fetcher.fetch(
        installation_id=0, repo_full_name="l/r", pr_number=0, head_sha="feat"
    )

    files = {f.path: f for f in diff.files}
    assert {p: f.status for p, f in files.items()} == {
        "keep.py": "modified",
        "new.py": "added",
        "gone.py": "removed",
    }
    assert files["keep.py"].patch is not None
    assert files["keep.py"].patch.startswith("@@")
    assert "+a = 2" in files["keep.py"].patch
    assert files["keep.py"].mode == "100644"
    assert files["keep.py"].blob_size_bytes == len("a = 2\n")
    assert files["gone.py"].mode is None


async def test_fetch_content_returns_head_blobs(repo: Path) -> None:
    fetcher = LocalGitDiffFetcher(repo, "main", "feat")
    diff = await fetcher.fetch(
        installation_id=0, repo_full_name="l/r", pr_number=0, head_sha="feat"
    )

    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="l/r", diff=diff, paths=["keep.py", "new.py"]
    )

    assert content == {"keep.py": b"a = 2\n", "new.py": b"b = 1\n"}
