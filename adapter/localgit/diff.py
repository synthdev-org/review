"""`LocalGitDiffFetcher` — implements `ingestion.diff.DiffFetcher` over a local
git repository: `git diff base...head` for the file list and patches,
`git ls-tree` for mode/size, `git show` for content.

`head_sha`/`pr_number`/`installation_id` are accepted for Protocol
compatibility; the refs come from the constructor, so one instance reviews one
`base...head` range.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from ingestion.diff import Diff, DiffContentUnavailableError, DiffFile

_STATUS = {"A": "added", "M": "modified", "D": "removed", "T": "modified"}
_GIT_TIMEOUT_SECONDS = 60


class LocalGitError(Exception):
    """A `git` invocation failed."""


async def run_git(repo: Path, *args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        proc.kill()
        raise LocalGitError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        raise LocalGitError(f"git {' '.join(args)} failed: {err.decode(errors='replace').strip()}")
    return out


class LocalGitDiffFetcher:
    def __init__(self, repo: Path, base_ref: str, head_ref: str) -> None:
        self._repo = repo
        self._range = f"{base_ref}...{head_ref}"
        self._head = head_ref

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        listing = await run_git(
            self._repo, "diff", "--name-status", "--no-renames", "-z", self._range
        )
        tokens = listing.decode().split("\0")
        entries = [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens) - 1, 2)]
        files = await asyncio.gather(*(self._file(code, path) for code, path in entries))
        return Diff(files=tuple(files))

    async def _file(self, code: str, path: str) -> DiffFile:
        status = _STATUS.get(code[0], "modified")
        patch_raw = await run_git(self._repo, "diff", "--no-renames", self._range, "--", path)
        # Drop git's header; GitHub's `patch` starts at the first hunk.
        text = patch_raw.decode(errors="replace")
        patch = text[text.index("@@") :] if "@@" in text else None
        if status == "removed":
            return DiffFile(path=path, status=status, patch=patch)
        tree = await run_git(self._repo, "ls-tree", "-l", self._head, "--", path)
        # "<mode> blob <sha> <size>\t<path>"
        meta = tree.decode().split("\t")[0].split()
        return DiffFile(
            path=path,
            status=status,
            patch=patch,
            mode=meta[0],
            blob_sha=meta[2],
            blob_size_bytes=int(meta[3]) if meta[3].isdigit() else None,
        )

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        by_path = {f.path: f for f in diff.files}
        wanted = [
            p for p in paths if by_path.get(p) and by_path[p].mode not in ("120000", "160000")
        ]
        try:
            blobs = await asyncio.gather(
                *(run_git(self._repo, "show", f"{self._head}:{p}") for p in wanted)
            )
        except LocalGitError as exc:
            raise DiffContentUnavailableError(
                repo_full_name=repo_full_name, detail=str(exc)
            ) from exc
        return dict(zip(wanted, blobs, strict=True))
