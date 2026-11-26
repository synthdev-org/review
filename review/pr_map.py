"""review.pr_map — a deterministic per-file summary of a PR (path, status,
``+a/-d``, changed symbols), rendered as a shared header in every batch's
prompt so a batch knows what the other batches contain.

No LLM call: changed symbols are a lookup against ``indexing.chunk``'s
tree-sitter chunks (``GraphNode`` carries no line numbers, and ``chunk_file``
is the same parse ``extract_graph`` uses). A lookup adds no cost and no
failure mode; a call would add both without an accuracy gain. Any file that
cannot be parsed simply has no symbols.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from indexing.chunk import ChunkParseError, chunk_file, language_for_path
from ingestion.diff import Diff
from review.diff_prompt import INERT_REFERENCE_DATA_FRAMING

_HUNK_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NEWLINES = re.compile(r"[\r\n]+")


class PrMapEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    status: str
    added: int
    deleted: int
    symbols: tuple[str, ...]
    """Chunk symbols whose head line range holds an added line, in file order."""


def _scan_patch(patch: str | None) -> tuple[int, int, list[int]]:
    """``(added, deleted, new-side line numbers of added lines)``."""
    added: list[int] = []
    deleted = 0
    new_line = 0
    in_hunk = False
    for raw in (patch or "").split("\n"):
        hunk = _HUNK_START.match(raw)
        if hunk:
            in_hunk, new_line = True, int(hunk.group(1))
        elif not in_hunk or raw.startswith("\\"):
            continue
        elif raw.startswith("+"):
            added.append(new_line)
            new_line += 1
        elif raw.startswith("-"):
            deleted += 1
        else:
            new_line += 1
    return len(added), deleted, added


def _changed_symbols(
    path: str, content: bytes | None, added_lines: Sequence[int], limit: int
) -> tuple[str, ...]:
    language = language_for_path(path)
    if language is None or content is None or not added_lines:
        return ()
    try:
        chunks = chunk_file(language, path, content).chunks
    except ChunkParseError:
        return ()
    named = sorted(
        (c.line_range[0], c.symbol_name)
        for c in chunks
        if c.symbol_name
        and c.line_range
        and any(c.line_range[0] <= n <= c.line_range[1] for n in added_lines)
    )
    seen: dict[str, None] = {}
    for _, name in named:
        seen.setdefault(name)
    return tuple(seen)[:limit]


def build_pr_map(
    diff: Diff, contents: Mapping[str, bytes], *, max_symbols_per_file: int = 8
) -> tuple[PrMapEntry, ...]:
    """One entry per diff file, in diff order. Removed and unparseable files
    have ``symbols=()``."""
    entries = []
    for f in diff.files:
        added, deleted, added_lines = _scan_patch(f.patch)
        symbols = (
            ()
            if f.status == "removed"
            else _changed_symbols(f.path, contents.get(f.path), added_lines, max_symbols_per_file)
        )
        entries.append(
            PrMapEntry(path=f.path, status=f.status, added=added, deleted=deleted, symbols=symbols)
        )
    return tuple(entries)


def _one_line(text: str) -> str:
    # Paths and symbols are untrusted: never let one forge an extra map line.
    return _NEWLINES.sub(" ", text)


def render_pr_map(
    entries: Sequence[PrMapEntry],
    batches: Sequence[Sequence[str]],
    *,
    current: int,
    unreviewed: Collection[str] = (),
    max_bytes: int,
) -> str:
    """The prompt section for batch ``current`` (0-based): ``""`` when there
    are no entries (or nothing fits), else ``"\\n\\n"`` + the inert framing +
    one line per file, truncated at ``max_bytes`` with ``... and N more files``."""
    if not entries:
        return ""
    batch_of = {p: i for i, paths in enumerate(batches) for p in paths}
    skipped = set(unreviewed)
    header = (
        f"\n\nPR map: every changed file in this PR, {INERT_REFERENCE_DATA_FRAMING}. "
        "It shows what the other batches contain; review only the diff above.\n"
    )
    lines = []
    for e in entries:
        if e.path in skipped or e.path not in batch_of:
            label = "not reviewed"
        elif batch_of[e.path] == current:
            label = "this batch"
        else:
            label = f"batch {batch_of[e.path] + 1}"
        symbols = f": {', '.join(_one_line(s) for s in e.symbols)}" if e.symbols else ""
        lines.append(f"{_one_line(e.path)} ({e.status} +{e.added}/-{e.deleted}) [{label}]{symbols}")

    def size(parts: Sequence[str]) -> int:
        return len(header.encode("utf-8")) + len("\n".join(parts).encode("utf-8"))

    if size(lines) <= max_bytes:
        return header + "\n".join(lines)
    kept = list(lines)
    while kept:
        kept.pop()
        tail = [*kept, f"... and {len(lines) - len(kept)} more files"]
        if size(tail) <= max_bytes:
            return header + "\n".join(tail)
    return ""
