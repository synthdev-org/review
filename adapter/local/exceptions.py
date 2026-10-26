"""Typed exceptions for adapter.local (architecture.md: specific exception types per concern)."""

from __future__ import annotations


class LocalGitError(Exception):
    """Base class for every error the local git adapter raises."""


class NotAGitRepositoryError(LocalGitError):
    def __init__(self, *, path: str) -> None:
        super().__init__(f"{path} is not inside a git repository")
        self.path = path


class GitRefNotFoundError(LocalGitError):
    """``ref`` does not resolve to a commit. In CI a shallow checkout is the usual cause."""

    def __init__(self, *, ref: str, shallow: bool) -> None:
        hint = (
            " (this is a shallow clone: fetch the base branch, e.g. `git fetch --depth=50 "
            "origin <base>`)"
            if shallow
            else " (give --base a branch, tag or commit that exists locally, e.g. --base main)"
        )
        super().__init__(f"git ref {ref!r} does not exist{hint}")
        self.ref = ref
        self.shallow = shallow


class NoMergeBaseError(LocalGitError):
    def __init__(self, *, base: str, shallow: bool) -> None:
        hint = (
            " (this is a shallow clone: deepen it, e.g. `git fetch --deepen=100`)"
            if shallow
            else ""
        )
        super().__init__(f"{base!r} and HEAD share no common ancestor{hint}")
        self.base = base
        self.shallow = shallow


class GitCommandError(LocalGitError):
    """git could not be run, timed out, produced too much output or exited non-zero."""

    def __init__(self, *, subcommand: str, detail: str) -> None:
        super().__init__(f"git {subcommand} failed: {detail}")
        self.subcommand = subcommand
        self.detail = detail


class IndexUnavailableError(Exception):
    """A step that needs the server's code index or memory store ran in local mode. The wiring
    keeps those steps disabled (plan A3), so reaching this is a configuration bug, not a user
    error."""

    def __init__(self, *, capability: str) -> None:
        super().__init__(f"{capability} is not available in local mode (the step must be disabled)")
        self.capability = capability


class RedactionFailedError(Exception):
    """Input-side redaction could not screen the diff, so nothing may be sent (fail closed).

    ``reason`` is written by this adapter from counts and exception class names only; it never
    holds code, paths or scanner output, because it is printed to the terminal and CI logs.
    """

    def __init__(self, *, reason: str) -> None:
        super().__init__(f"redaction failed, nothing sent: {reason}")
        self.reason = reason


class MemoryFileError(Exception):
    """The repository memory file cannot be used. ``line`` is 1-based, or ``None`` for the file as
    a whole. The message names the path and line and a reason; it never quotes file content, which
    may hold a secret and is printed to terminals and CI logs."""

    def __init__(self, *, path: str, line: int | None, reason: str) -> None:
        where = path if line is None else f"{path}:{line}"
        super().__init__(f"memory file {where}: {reason}")
        self.path = path
        self.line = line
        self.reason = reason
