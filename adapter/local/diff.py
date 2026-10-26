"""adapter.local.diff — ``ingestion.diff.DiffFetcher`` over a local git repository.


It only *splits* what git already produced: ``git diff --raw -z`` gives each changed file's path,
status, mode and new blob id (unambiguously, whatever the file name holds), ``git diff --patch``
gives the hunks, and the two are paired by position because both list files in the same order.
``DiffFile.patch`` follows GitHub's shape, the hunks only without the ``diff --git``/``---``/``+++``
header, which is what ``review/changed_lines.py`` and the prompt renderer expect. Hunk parsing
itself already exists in ``review.changed_lines`` and is not repeated here.

Content is read from the git object database by blob id (the same "keyed by ``blob_sha``, never by
path" contract the GitHub fetcher has), and a symlink or submodule entry is never read, reusing the
mode constants of ``adapter/github/git_data.py``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from adapter.local.exceptions import GitCommandError
from adapter.local.git import MAX_GIT_OUTPUT_BYTES, ReviewRange, git_ok, run_git
from ingestion.diff import (
    BLOB_SIZE_CEILING_BYTES,
    SUBMODULE_MODE,
    SYMLINK_MODE,
    Diff,
    DiffContentMalformedError,
    DiffContentUnavailableError,
    DiffFetcher,
    DiffFile,
)

_RAW_META = re.compile(r"^:(\d{6}) (\d{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) ([A-Z])\d*$")
_DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
_STATUS_NAMES = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "R": "renamed",
    "C": "copied",
    "T": "modified",  # a type change (file to symlink); the new mode still flags a symlink
}
_REMOVED = "removed"
# Hash and diff-format options that make the output independent of the developer's git config.
_DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--no-abbrev")


_BATCH_HEADER_ALLOWANCE = 128  # sha, type, size and newlines around one blob


async def _gather_all[T](*awaitables: Awaitable[T]) -> list[T]:
    """``asyncio.gather`` that cancels (and reaps) the siblings when one fails, so a failed git
    call does not leave another subprocess running to its timeout."""
    tasks = [asyncio.ensure_future(a) for a in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass(frozen=True)
class RawEntry:
    """One line of ``git diff --raw -z``."""

    status: str
    new_mode: str
    new_sha: str
    path: str
    previous_path: str | None
    # A type change (file <-> symlink): `git diff --patch` prints it as two sections, a deletion
    # and an addition, where `--raw` prints one record.
    type_change: bool = False


def parse_raw_diff(raw: bytes, *, repo_full_name: str) -> list[RawEntry]:
    """Parse ``git diff --raw -z --no-abbrev`` output (NUL separated: a ``:``-prefixed metadata
    record, then the path, or the old and the new path for a rename or copy)."""
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries: list[RawEntry] = []
    i = 0
    while i < len(tokens):
        meta = _RAW_META.match(tokens[i].decode("utf-8", errors="replace"))
        if meta is None:
            raise DiffContentMalformedError(
                repo_full_name=repo_full_name, detail="unrecognised `git diff --raw` record"
            )
        letter = meta.group(5)
        path_count = 2 if letter in "RC" else 1
        paths = [t.decode("utf-8", errors="replace") for t in tokens[i + 1 : i + 1 + path_count]]
        if letter == "U":
            raise DiffContentMalformedError(
                repo_full_name=repo_full_name,
                detail="the changes include unmerged paths: resolve the conflicts first",
            )
        if len(paths) != path_count or letter not in _STATUS_NAMES:
            raise DiffContentMalformedError(
                repo_full_name=repo_full_name, detail="truncated or unknown `git diff --raw` record"
            )
        entries.append(
            RawEntry(
                status=_STATUS_NAMES[letter],
                new_mode=meta.group(2),
                new_sha=meta.group(4),
                path=paths[-1],
                previous_path=paths[0] if path_count == 2 else None,
                type_change=letter == "T",
            )
        )
        i += 1 + path_count
    return entries


def split_patches(patch_text: str) -> list[str | None]:
    """Split ``git diff --patch`` output into one entry per file: the hunks from the first ``@@``
    on, or ``None`` when the file has none (binary, pure rename, mode-only change). A body line
    always starts with a space, ``+``, ``-`` or ``\\``, so ``diff --git `` at a line start is
    always a file boundary."""
    sections = _DIFF_HEADER.split(patch_text)[1:]
    patches: list[str | None] = []
    for section in sections:
        start = section.find("\n@@")
        patches.append(None if start == -1 else section[start + 1 :].removesuffix("\n"))
    return patches


def _align_patches(entries: Sequence[RawEntry], patches: list[str | None]) -> list[str | None]:
    """One patch per entry: for a type change, the second of its two sections (the new side).
    Returned unchanged if the counts cannot be reconciled, so the caller reports the mismatch."""
    if len(patches) != len(entries) + sum(e.type_change for e in entries):
        return patches
    aligned: list[str | None] = []
    index = 0
    for entry in entries:
        index += 2 if entry.type_change else 1
        aligned.append(patches[index - 1])
    return aligned


def _parse_batch_check(output: bytes, *, repo_full_name: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        parts = line.split(" ")
        if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
            raise DiffContentMalformedError(
                repo_full_name=repo_full_name, detail="unexpected `git cat-file --batch-check` line"
            )
        sizes[parts[0]] = int(parts[2])
    return sizes


class LocalGitDiffFetcher:
    """``DiffFetcher`` for the range ``review_range.merge_base_sha..head_sha`` of ``repo_dir``.

    ``installation_id``, ``repo_full_name`` and ``pr_number`` are part of the Protocol's signature
    and carry no meaning locally (the seed fabricates them).
    """

    def __init__(
        self, repo_dir: Path, review_range: ReviewRange, *, environ: Mapping[str, str]
    ) -> None:
        self._repo_dir = repo_dir
        self._environ = environ
        self._merge_base = review_range.merge_base_sha
        # `git diff --cached <HEAD>` is the index against HEAD; the staged blobs are in the object
        # database already, so content is read the same way as for a committed range.
        self._staged = review_range.staged

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        revisions = ("--cached", self._merge_base) if self._staged else (self._merge_base, head_sha)
        try:
            # Independent reads of the same range: run side by side.
            raw, patch_text = await _gather_all(
                git_ok(
                    self._repo_dir,
                    "diff",
                    "--raw",
                    "-z",
                    *_DIFF_OPTIONS,
                    *revisions,
                    environ=self._environ,
                ),
                git_ok(
                    self._repo_dir,
                    "diff",
                    "--patch",
                    "-U3",
                    *_DIFF_OPTIONS,
                    *revisions,
                    environ=self._environ,
                ),
            )
            entries = parse_raw_diff(raw, repo_full_name=repo_full_name)
            patches = _align_patches(
                entries, split_patches(patch_text.decode("utf-8", errors="replace"))
            )
            if len(entries) != len(patches):
                raise DiffContentMalformedError(
                    repo_full_name=repo_full_name,
                    detail=f"{len(entries)} files in the file list but {len(patches)} in the patch",
                )
            sizes = await self._blob_sizes(entries, repo_full_name=repo_full_name)
        except GitCommandError as exc:
            raise DiffContentUnavailableError(
                repo_full_name=repo_full_name, detail=str(exc)
            ) from exc
        files = []
        for entry, patch in zip(entries, patches, strict=True):
            removed = entry.status == _REMOVED
            files.append(
                DiffFile(
                    path=entry.path,
                    status=entry.status,
                    patch=patch,
                    mode=None if removed else entry.new_mode,
                    blob_sha=None if removed else entry.new_sha,
                    blob_size_bytes=sizes.get(entry.new_sha),
                    previous_path=entry.previous_path,
                )
            )
        return Diff(files=tuple(files))

    async def _blob_sizes(
        self, entries: Sequence[RawEntry], *, repo_full_name: str
    ) -> dict[str, int]:
        """Blob sizes in one ``cat-file --batch-check`` call. A removed file has no new blob and a
        submodule's id is a commit in another repository, so neither is asked for."""
        shas = sorted(
            {e.new_sha for e in entries if e.status != _REMOVED and e.new_mode != SUBMODULE_MODE}
        )
        if not shas:
            return {}
        returncode, output = await run_git(
            self._repo_dir,
            "cat-file",
            "--batch-check",
            environ=self._environ,
            stdin=("\n".join(shas) + "\n").encode(),
        )
        if returncode != 0:
            raise GitCommandError(subcommand="cat-file", detail=f"exit status {returncode}")
        return _parse_batch_check(output, repo_full_name=repo_full_name)

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        """Content keyed by *path*: that is what every consumer (static analysis, clone detection,
        fix generation) indexes it by, and what ``GitHubDiffFetcher`` returns, whatever the
        Protocol's docstring says about blob ids. A blob shared by several paths is read once."""
        wanted = set(paths)
        selected: list[DiffFile] = []
        for file in diff.files:
            if file.path not in wanted or file.blob_sha is None:
                continue
            if file.mode in (SYMLINK_MODE, SUBMODULE_MODE):
                continue
            if (file.blob_size_bytes or 0) >= BLOB_SIZE_CEILING_BYTES:
                raise DiffContentUnavailableError(
                    repo_full_name=repo_full_name,
                    detail=f"{file.path} exceeds the {BLOB_SIZE_CEILING_BYTES} byte blob ceiling",
                )
            selected.append(file)
        sizes = {f.blob_sha: f.blob_size_bytes or 0 for f in selected if f.blob_sha is not None}
        by_blob: dict[str, bytes] = {}
        for batch in _batches(sizes):
            by_blob.update(await self._read_blobs(batch, repo_full_name=repo_full_name))
        return {f.path: by_blob[f.blob_sha] for f in selected if f.blob_sha is not None}

    async def _read_blobs(self, shas: Sequence[str], *, repo_full_name: str) -> dict[str, bytes]:
        """One ``cat-file --batch`` call for ``shas``: ``<sha> blob <size>\\n<bytes>\\n`` each."""
        try:
            returncode, output = await run_git(
                self._repo_dir,
                "cat-file",
                "--batch",
                environ=self._environ,
                stdin=("\n".join(shas) + "\n").encode(),
            )
        except GitCommandError as exc:
            raise DiffContentUnavailableError(
                repo_full_name=repo_full_name, detail=str(exc)
            ) from exc
        if returncode != 0:
            raise DiffContentUnavailableError(
                repo_full_name=repo_full_name, detail=f"cat-file: exit status {returncode}"
            )
        blobs: dict[str, bytes] = {}
        position = 0
        for sha in shas:
            end = output.find(b"\n", position)
            header = output[position:end].split() if end != -1 else []
            if (
                len(header) != 3
                or header[0].decode("ascii", "replace") != sha
                or header[1] != b"blob"
            ):
                raise DiffContentUnavailableError(
                    repo_full_name=repo_full_name, detail=f"cat-file: no blob for {sha}"
                )
            start = end + 1
            try:
                stop = start + int(header[2])
            except ValueError:
                stop = -1
            if stop < start or stop > len(output):
                raise DiffContentUnavailableError(
                    repo_full_name=repo_full_name, detail=f"cat-file: truncated blob {sha}"
                )
            blobs[sha] = output[start:stop]
            position = stop + 1  # the newline git writes after each blob
        return blobs


def _batches(sizes: Mapping[str, int]) -> list[list[str]]:
    """``sizes``' blobs grouped so one ``cat-file --batch`` answer stays under git's output cap."""
    batches: list[list[str]] = [[]]
    total = 0
    for sha, size in sizes.items():
        cost = size + _BATCH_HEADER_ALLOWANCE
        if batches[-1] and total + cost > MAX_GIT_OUTPUT_BYTES:
            batches.append([])
            total = 0
        batches[-1].append(sha)
        total += cost
    return [b for b in batches if b]


# Runtime confirmation that this class satisfies the Protocol it is built to implement.
_: type[DiffFetcher] = LocalGitDiffFetcher
