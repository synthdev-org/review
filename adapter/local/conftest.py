"""Shared test helpers for adapter.local and the local CLI: throwaway real git repositories.

Plain helpers (not fixtures) so ``apps/local_cli``'s tests can import them too, the same way they
import ``adapter.llm.conftest``. Tests that need the ``git`` binary skip cleanly, with the reason,
when it is not installed (the ``review-dev`` image has none: install it, for example
``apt-get install -y git``, to run them).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="the git binary is not installed (apt-get install -y git to run these tests)",
)

_GIT_ENV = {
    # Isolate from the developer's own git configuration.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}


class Repo:
    """A real git repository in a temporary directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **_GIT_ENV},
        )
        return result.stdout.strip()

    def write(self, relative_path: str, text: str) -> None:
        target = self.path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


def init_repo(path: Path) -> Repo:
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo(path)
    repo.git("init", "-q", "-b", "main")
    return repo


BASE_APP = "def total(items):\n    return sum(items)\n"
FEATURE_APP = (
    "def total(items):\n"
    "    result = 0\n"
    "    for item in items:\n"
    "        result += item\n"
    "    return result\n"
)


def feature_branch_repo(path: Path) -> Repo:
    """``main`` with one commit, and a ``feature`` branch (checked out) that rewrites
    ``app.py``. The usual "my branch against main" shape."""
    repo = init_repo(path)
    repo.write("app.py", BASE_APP)
    repo.write("README.md", "# demo\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", FEATURE_APP)
    repo.commit("rewrite total")
    return repo


def detached_head_repo(path: Path) -> Repo:
    repo = feature_branch_repo(path)
    repo.git("checkout", "-q", "--detach", "HEAD")
    return repo


def ci_merge_ref_repo(path: Path) -> Repo:
    """What a CI checkout of a pull request is: a detached merge commit of the PR branch into a
    base that has moved on."""
    repo = feature_branch_repo(path)
    repo.git("checkout", "-q", "main")
    repo.write("other.py", "x = 1\n")
    repo.commit("main moves on")
    repo.git("checkout", "-q", "--detach", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge pull request", "feature")
    return repo


def shallow_clone_without_base(origin: Repo, destination: Path) -> Repo:
    """The actions/checkout default: one commit of the PR branch and no base branch at all."""
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "feature",
            f"file://{origin.path}",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    return Repo(destination)
