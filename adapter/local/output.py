"""adapter.local.output — the ``--output`` file: validated before any model call, written safely.

``--output`` is a command-line flag only and is never read from repository config, so a pull
request cannot choose a path to write. The path is checked first (``check_output_path``) and
written last (``write_output``): a symlink is refused, a file git tracks is never overwritten, and
the file or its directory must be writable, so a run does not spend a model call on findings it
cannot keep.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from adapter.local.git import run_git

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # absent on Windows


class OutputFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


_SUFFIXES = {
    ".md": OutputFormat.MARKDOWN,
    ".markdown": OutputFormat.MARKDOWN,
    ".json": OutputFormat.JSON,
}


class OutputPathError(Exception):
    """The ``--output`` path cannot be used; the message says why and is safe to print."""


def _refuse_symlinked_directory(repo_dir: Path, path: Path) -> None:
    """Refuse a path whose directory part goes through a symlink into the repository:
    ``link/README.md`` with ``link -> .`` would otherwise reach a tracked file that neither the
    last-component symlink check nor ``git ls-files`` (which sees the unresolved path)
    can recognise. A link that lands outside the repository (macOS ``/tmp``) cannot reach
    one and is allowed."""
    lexical = Path(os.path.normpath(path.absolute())).parent
    resolved = lexical.resolve()
    repo_lexical = Path(os.path.normpath(repo_dir.absolute()))
    # A symlinked repo root itself (macOS ``/tmp``) is not a link inside the repository.
    expected = (
        repo_dir.resolve() / lexical.relative_to(repo_lexical)
        if lexical.is_relative_to(repo_lexical)
        else lexical
    )
    if resolved != expected and resolved.is_relative_to(repo_dir.resolve()):
        raise OutputPathError(
            f"{path} is reached through a symbolic link; refusing to write through it"
        )


def _check_filesystem(repo_dir: Path, path: Path) -> None:
    """The blocking half of ``check_output_path``: symlink, type and permission checks."""
    if path.is_symlink():
        raise OutputPathError(f"{path} is a symbolic link; refusing to write through it")
    _refuse_symlinked_directory(repo_dir, path)
    if path.exists():
        if not path.is_file():
            raise OutputPathError(f"{path} exists and is not a regular file")
        if not os.access(path, os.W_OK):
            raise OutputPathError(f"{path} is not writable")
    else:
        parent = path.parent
        if not parent.is_dir():
            raise OutputPathError(f"the directory {parent} does not exist")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise OutputPathError(f"the directory {parent} is not writable")


async def check_output_path(
    repo_dir: Path, path: Path, *, environ: Mapping[str, str]
) -> OutputFormat:
    """Return the format ``path`` selects, or raise ``OutputPathError``. Raises
    ``adapter.local.exceptions.GitCommandError`` if git cannot answer the tracked-file question."""
    output_format = _SUFFIXES.get(path.suffix.lower())
    if output_format is None:
        raise OutputPathError(f"{path} must end in .md, .markdown or .json")
    # Blocking stat/access calls: off the event loop.
    await asyncio.to_thread(_check_filesystem, repo_dir, path)
    # `--error-unmatch` exits 0 only for a path git tracks (in HEAD or staged). Outside a
    # repository it fails, which correctly means "not tracked".
    returncode, _ = await run_git(
        repo_dir,
        "ls-files",
        "--error-unmatch",
        "--",
        f":(literal){path.absolute()}",
        environ=environ,
    )
    if returncode == 0:
        raise OutputPathError(
            f"{path} is tracked by git; refusing to overwrite it (choose another path)"
        )
    return output_format


def write_output(repo_dir: Path, path: Path, text: str) -> None:
    """Write ``text`` to ``path``. ``O_NOFOLLOW`` re-checks the symlink rule at the moment of the
    write, since the check above ran before a long model call."""
    _refuse_symlinked_directory(repo_dir, path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def discard_stale_output(path: Path) -> None:
    """Remove an ``--output`` file left by an earlier run, so a consumer that reads it after a
    failed run cannot mistake old findings for this run's. ``unlink`` never follows a symlink;
    a file that cannot be removed is left (the run is already failing, and says why)."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
