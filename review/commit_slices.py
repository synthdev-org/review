"""review.commit_slices — pure planning helpers for the optional
commit-by-commit review mode: group a PR's commits into consecutive slices
that each fit a byte budget, and check whether a finding's line is still
present at head. No I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from ingestion.commits import CommitInfo

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TRIVIAL_LINE_CHARS = 8


def _git_lines(text: str) -> list[str]:
    """Lines as git and GitHub number them: only ``\n`` breaks a line (not form feed or U+2028)."""
    lines = [ln.removesuffix("\r") for ln in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


class CommitSlice(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_sha: str
    head_sha: str
    shas: tuple[str, ...]
    estimated_bytes: int


class CommitPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    slices: tuple[CommitSlice, ...]
    skipped_commits: tuple[str, ...]
    """Merge/root commits: they have no single parent to diff against."""
    dropped_commits: tuple[str, ...]
    """Commits in slices beyond ``max_slices``."""


def plan_commit_slices(
    commits: Sequence[CommitInfo],
    sizes: Mapping[str, int],
    *,
    max_bytes: int,
    max_slices: int,
) -> CommitPlan:
    """Greedily pack consecutive commits (oldest first) into slices of at most
    ``max_bytes``. A slice also ends where the next commit's parent is not the
    previous commit. A single oversize commit becomes its own slice."""
    groups: list[list[CommitInfo]] = []
    skipped: list[str] = []
    current: list[CommitInfo] = []
    current_bytes = 0
    for commit in commits:
        if len(commit.parent_shas) != 1:
            skipped.append(commit.sha)
            current = []
            continue
        size = sizes.get(commit.sha, 0)
        contiguous = bool(current) and commit.parent_shas[0] == current[-1].sha
        if contiguous and current_bytes + size <= max_bytes:
            current.append(commit)
            current_bytes += size
            continue
        current = [commit]
        current_bytes = size
        groups.append(current)

    slices = tuple(
        CommitSlice(
            base_sha=g[0].parent_shas[0],
            head_sha=g[-1].sha,
            shas=tuple(c.sha for c in g),
            estimated_bytes=sum(sizes.get(c.sha, 0) for c in g),
        )
        for g in groups
    )
    kept = slices[: max(max_slices, 0)]
    dropped = tuple(sha for s in slices[len(kept) :] for sha in s.shas)
    return CommitPlan(slices=kept, skipped_commits=tuple(skipped), dropped_commits=dropped)


class HeadMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    present: bool
    line: int | None = None


def _new_side_lines(patch: str) -> dict[int, str]:
    lines: dict[int, str] = {}
    new_line = 0
    for raw in _git_lines(patch):
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
        elif raw.startswith(("-", "\\")):
            continue
        elif new_line:
            lines[new_line] = raw[1:]
            new_line += 1
    return lines


def locate_at_head(
    *, line: int | None, slice_patch: str | None, head_content: bytes | None
) -> HeadMatch:
    """Is a finding made on a slice still present at head? The finding's line
    text is taken from the slice patch and matched (whitespace-stripped) in the
    head file, nearest line first (earlier line on a tie).

    Missing head file: gone. No line, a line not in the patch, or a trivial
    line (too short to match reliably): present at file level (``line=None``).
    """
    if head_content is None:
        return HeadMatch(present=False)
    if line is None or slice_patch is None:
        return HeadMatch(present=True)
    text = _new_side_lines(slice_patch).get(line, "").strip()
    if len(text) < _TRIVIAL_LINE_CHARS:
        return HeadMatch(present=True)
    head_lines = _git_lines(head_content.decode(errors="replace"))
    candidates = [i for i, ln in enumerate(head_lines, 1) if ln.strip() == text]
    if not candidates:
        return HeadMatch(present=False)
    return HeadMatch(present=True, line=min(candidates, key=lambda i: (abs(i - line), i)))
