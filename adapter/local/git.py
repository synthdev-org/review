"""adapter.local.git — the one place local mode runs the ``git`` binary.

Every call is a bounded subprocess (timeout, output cap, kill-and-reap) with a credential-stripped
environment, the same restraint ``adapter/analyzers/subprocess_runner.py`` applies to analyzers.
stderr is discarded, never merged into stdout, so a git warning cannot corrupt a patch; failures
are reported from the exit status by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from adapter.analyzers.exceptions import AnalyzerOutputTooLargeError, AnalyzerTimeoutError
from adapter.analyzers.subprocess_runner import analyzer_env, run_bounded_subprocess
from adapter.local.exceptions import (
    GitCommandError,
    GitRefNotFoundError,
    NoMergeBaseError,
    NotAGitRepositoryError,
)

GIT_TIMEOUT_SECONDS = 30.0
MAX_GIT_OUTPUT_BYTES = 100_000_000


@dataclass(frozen=True)
class ReviewRange:
    """What a ``--base`` ref resolved to. The reviewed diff is ``merge_base_sha..head_sha`` (what a
    pull request shows), not ``base..HEAD``, so commits that landed on the base since the branch
    point are not reported as this branch's changes."""

    base_sha: str
    merge_base_sha: str
    head_sha: str
    has_changes: bool
    # True for ``--staged``: the diff is the index against ``merge_base_sha`` (which is HEAD), so
    # unstaged edits are never part of it.
    staged: bool = False


async def run_git(
    repo_dir: Path,
    *args: str,
    environ: Mapping[str, str],
    stdin: bytes | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
) -> tuple[int, bytes]:
    """Run ``git <args>`` in ``repo_dir``; returns ``(exit status, stdout)``. Raises
    ``GitCommandError`` if git is missing, times out or writes more than ``max_output_bytes``."""
    subcommand = args[0]
    try:
        result = await run_bounded_subprocess(
            ["git", *args],
            cwd=repo_dir,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            env={
                **analyzer_env(environ),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            },
            stdin=stdin,
            isolated=True,
        )
    except AnalyzerTimeoutError as exc:
        raise GitCommandError(subcommand=subcommand, detail=f"timed out after {timeout}s") from exc
    # After the timeout: ``TimeoutError`` is an ``OSError``.
    except OSError as exc:
        raise GitCommandError(
            subcommand=subcommand, detail=f"cannot run git (is it installed and on PATH?): {exc}"
        ) from exc
    except AnalyzerOutputTooLargeError as exc:
        raise GitCommandError(
            subcommand=subcommand, detail=f"output exceeded {max_output_bytes} bytes"
        ) from exc
    return result.returncode, result.stdout


async def git_ok(repo_dir: Path, *args: str, environ: Mapping[str, str]) -> bytes:
    """``run_git`` for a call that must succeed; a non-zero exit raises ``GitCommandError``."""
    returncode, output = await run_git(repo_dir, *args, environ=environ)
    if returncode != 0:
        raise GitCommandError(subcommand=args[0], detail=f"exit status {returncode}")
    return output


async def _resolve_commit(
    repo_dir: Path, ref: str, *, shallow: bool, environ: Mapping[str, str]
) -> str:
    # A leading "-" would be read by git as an option; no ref name needs one.
    if ref.startswith("-"):
        raise GitRefNotFoundError(ref=ref, shallow=shallow)
    returncode, output = await run_git(
        repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", environ=environ
    )
    if returncode != 0:
        raise GitRefNotFoundError(ref=ref, shallow=shallow)
    return output.decode().strip()


async def resolve_staged_range(repo_dir: Path, *, environ: Mapping[str, str]) -> ReviewRange:
    """The staged changes (the index against HEAD). Raises ``NotAGitRepositoryError``,
    ``GitRefNotFoundError`` (no commits yet) or ``GitCommandError``."""
    returncode, _ = await run_git(repo_dir, "rev-parse", "--is-inside-work-tree", environ=environ)
    if returncode != 0:
        raise NotAGitRepositoryError(path=str(repo_dir))
    shallow = (
        await git_ok(repo_dir, "rev-parse", "--is-shallow-repository", environ=environ)
    ).strip() == b"true"
    head_sha = await _resolve_commit(repo_dir, "HEAD", shallow=shallow, environ=environ)
    returncode, _ = await run_git(
        repo_dir, "diff", "--cached", "--quiet", "--no-ext-diff", environ=environ
    )
    if returncode not in (0, 1):
        raise GitCommandError(subcommand="diff", detail=f"exit status {returncode}")
    return ReviewRange(
        base_sha=head_sha,
        merge_base_sha=head_sha,
        head_sha=head_sha,
        has_changes=returncode == 1,
        staged=True,
    )


async def resolve_review_range(
    repo_dir: Path, base: str, *, environ: Mapping[str, str], head: str = "HEAD"
) -> ReviewRange:
    """Resolve ``base`` and ``head`` (default ``HEAD``; a branch, a detached head or a CI merge ref
    all work) to commits, find their merge base, and say whether anything changed between them.
    Raises ``NotAGitRepositoryError``, ``GitRefNotFoundError`` (the missing ref is named; in a
    shallow clone the message says to fetch it), ``NoMergeBaseError`` or ``GitCommandError``."""
    returncode, _ = await run_git(repo_dir, "rev-parse", "--is-inside-work-tree", environ=environ)
    if returncode != 0:
        raise NotAGitRepositoryError(path=str(repo_dir))
    shallow = (
        await git_ok(repo_dir, "rev-parse", "--is-shallow-repository", environ=environ)
    ).strip() == b"true"
    base_sha = await _resolve_commit(repo_dir, base, shallow=shallow, environ=environ)
    head_sha = await _resolve_commit(repo_dir, head, shallow=shallow, environ=environ)
    returncode, output = await run_git(repo_dir, "merge-base", base_sha, head_sha, environ=environ)
    if returncode == 1:
        raise NoMergeBaseError(base=base, shallow=shallow)
    if returncode != 0:
        raise GitCommandError(subcommand="merge-base", detail=f"exit status {returncode}")
    merge_base_sha = output.decode().strip()
    # `diff --quiet` exits 1 when the trees differ.
    returncode, _ = await run_git(
        repo_dir, "diff", "--quiet", "--no-ext-diff", merge_base_sha, head_sha, environ=environ
    )
    if returncode not in (0, 1):
        raise GitCommandError(subcommand="diff", detail=f"exit status {returncode}")
    return ReviewRange(
        base_sha=base_sha,
        merge_base_sha=merge_base_sha,
        head_sha=head_sha,
        has_changes=returncode == 1,
    )


async def resolve_repo_root(directory: Path, *, environ: Mapping[str, str]) -> Path:
    """The top of the work tree ``directory`` is in, so repository-relative files (the memory
    file, the nomination file) are found when the run starts from a subdirectory. Anything that
    is not a work tree is returned unchanged for the range resolution to report."""
    returncode, output = await run_git(directory, "rev-parse", "--show-toplevel", environ=environ)
    top = output.decode("utf-8", errors="replace").strip()
    return Path(top) if returncode == 0 and top else directory
