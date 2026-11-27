"""review.diff_expand — render each diff hunk with its enclosing symbol's
surrounding head lines, so the reviewer does not see bare ``+``/``-`` lines.

Pure and offline: the caller fetches head contents (``Mapping[path, bytes]``);
this module only parses (``indexing.chunk``'s tree-sitter chunker) and
rewrites patch text. Every failure (unsupported language, parse error, decode
error, stale head content, malformed patch, size cap) returns the original
patch unchanged — expansion never fails a review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from indexing.chunk import ChunkParseError, chunk_file, language_for_path
from ingestion.diff import Diff

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_EXPANDABLE_STATUSES = frozenset({"modified", "renamed"})


class ExpandConfig(BaseModel):
    context_before: int = 40
    """Max unchanged lines added above a hunk."""
    context_after: int = 20
    """Max unchanged lines added below a hunk."""
    max_bytes_per_file: int = 16_000
    """Expanded patch above this many UTF-8 bytes: the original is returned."""


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    body: tuple[str, ...]

    @property
    def new_end(self) -> int:
        return self.new_start + self.new_count - 1

    @property
    def first_change(self) -> int:
        """New-side line of the first +/- line; ``new_start`` is only leading context."""
        line = self.new_start
        for b in self.body:
            if b[0] in "+-":
                return line
            line += b[0] == " "
        return self.new_start


def expandable_paths(diff: Diff) -> list[str]:
    """Files whose patch can be expanded: modified/renamed, patch present,
    language supported by the chunker.
    """
    return [
        f.path
        for f in diff.files
        if f.status in _EXPANDABLE_STATUSES and f.patch and language_for_path(f.path) is not None
    ]


def _parse_hunks(lines: list[str]) -> tuple[list[str], list[_Hunk]] | None:
    """``(preamble, hunks)`` or ``None`` when the patch is not trustworthy
    (no hunk, a body line without a prefix, or counts disagreeing with the
    header).
    """
    preamble: list[str] = []
    hunks: list[_Hunk] = []
    header: re.Match[str] | None = None
    body: list[str] = []

    def close() -> bool:
        if header is None:
            return True
        old_n = sum(1 for b in body if b[0] in " -")
        new_n = sum(1 for b in body if b[0] in " +")
        old_count = 1 if header.group(2) is None else int(header.group(2))
        new_count = 1 if header.group(4) is None else int(header.group(4))
        if (old_n, new_n) != (old_count, new_count):
            return False
        hunks.append(
            _Hunk(
                int(header.group(1)),
                old_count,
                int(header.group(3)),
                new_count,
                header.group(5),
                tuple(body),
            )
        )
        return True

    for line in lines:
        match = _HUNK_HEADER.match(line)
        if match:
            if not close():
                return None
            header, body = match, []
        elif header is None:
            preamble.append(line)
        elif line[:1] in (" ", "+", "-", "\\"):
            body.append(line)
        else:
            return None
    if not close() or not hunks:
        return None
    return preamble, hunks


def _enclosing(
    chunks_by_span: list[tuple[int, int, str]], line: int
) -> tuple[int, int, str] | None:
    best: tuple[int, int, str] | None = None
    for start, end, name in chunks_by_span:
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end, name)
    return best


def _head_lines_and_spans(
    path: str, head_content: bytes
) -> tuple[list[str], list[tuple[int, int, str]]] | None:
    """Head lines and named chunk spans, or ``None`` when the language is
    unsupported or head cannot be parsed.
    """
    language = language_for_path(path)
    if language is None:
        return None
    try:
        head_lines = head_content.decode("utf-8").split("\n")
        if head_lines and head_lines[-1] == "":
            head_lines.pop()
        result = chunk_file(language, path, head_content)
    except (UnicodeDecodeError, ChunkParseError):
        return None
    spans = [
        (c.line_range[0], c.line_range[1], c.symbol_name)
        for c in result.chunks
        if c.symbol_name and c.line_range
    ]
    return head_lines, spans


def _matches_head(hunk: _Hunk, head_lines: list[str]) -> bool:
    """False when the hunk's first new-side line disagrees with head (stale or wrong ref)."""
    first_new = next((b for b in hunk.body if b[0] in " +"), None)
    if first_new is None or hunk.new_count <= 0:
        return True
    at = hunk.new_start - 1
    return at < len(head_lines) and head_lines[at] == first_new[1:]


def _widen_hunk(
    hunk: _Hunk,
    symbol: tuple[int, int, str],
    head_lines: list[str],
    covered: int,
    next_start: int,
    config: ExpandConfig,
) -> tuple[list[str], int]:
    """Output lines for ``hunk`` widened to ``symbol``, and the new ``covered``."""
    sym_start, sym_end, name = symbol
    before_from = max(sym_start, hunk.new_start - config.context_before, covered + 1, 1)
    after_to = min(sym_end, hunk.new_end + config.context_after, next_start - 1, len(head_lines))
    before = [" " + head_lines[n - 1] for n in range(before_from, hunk.new_start)]
    after = [" " + head_lines[n - 1] for n in range(hunk.new_end + 1, after_to + 1)]
    old_first = hunk.old_start + (1 if hunk.old_count == 0 else 0)
    header = _header(
        hunk,
        old_first - len(before) if hunk.old_count + len(before) + len(after) else hunk.old_start,
        hunk.old_count + len(before) + len(after),
        hunk.new_start - len(before),
        hunk.new_count + len(before) + len(after),
        name if not hunk.section.strip() else None,
    )
    return [header, *before, *hunk.body, *after], max(covered, hunk.new_end + len(after))


def _patch_lines(patch: str) -> list[str]:
    return patch.rstrip("\n").split("\n")


def _render_hunk(
    hunk: _Hunk,
    spans: list[tuple[int, int, str]],
    head_lines: list[str],
    covered: int,
    next_start: int,
    config: ExpandConfig,
) -> tuple[list[str], int]:
    """Output lines for ``hunk`` (widened when inside a symbol) and the new ``covered``."""
    symbol = _enclosing(spans, hunk.first_change) if hunk.new_count > 0 else None
    if symbol is not None:
        return _widen_hunk(hunk, symbol, head_lines, covered, next_start, config)
    header = _header(hunk, hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count)
    return [header, *hunk.body], max(covered, hunk.new_end)


def expand_patch(
    path: str, patch: str, head_content: bytes, config: ExpandConfig | None = None
) -> str:
    """``patch`` with each hunk widened to its enclosing symbol's nearby head
    lines (prefixed ``" "``), ``@@`` counts rewritten. The original ``patch``
    on any failure or when nothing changes.
    """
    config = config or ExpandConfig()
    loaded = _head_lines_and_spans(path, head_content)
    if loaded is None:
        return patch
    head_lines, spans = loaded

    trailing_newline = patch.endswith("\n")
    parsed = _parse_hunks(_patch_lines(patch))
    if parsed is None:
        return patch
    preamble, hunks = parsed

    out = list(preamble)
    covered = 0  # last head line already shown by an earlier hunk
    for index, hunk in enumerate(hunks):
        if not _matches_head(hunk, head_lines):
            return patch
        next_start = hunks[index + 1].new_start if index + 1 < len(hunks) else len(head_lines) + 1
        lines, covered = _render_hunk(hunk, spans, head_lines, covered, next_start, config)
        out.extend(lines)

    expanded = "\n".join(out) + ("\n" if trailing_newline else "")
    if expanded == patch or len(expanded.encode("utf-8")) > config.max_bytes_per_file:
        return patch
    return expanded


def _header(
    hunk: _Hunk,
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    symbol: str | None = None,
) -> str:
    section = f" {symbol}" if symbol else hunk.section
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{section}"


def expand_diff(
    diff: Diff, contents: Mapping[str, bytes], config: ExpandConfig | None = None
) -> Diff:
    """``diff`` with expandable files' patches expanded from ``contents``;
    the same object when nothing changed.
    """
    wanted = set(expandable_paths(diff))
    files = []
    changed = False
    for f in diff.files:
        head = contents.get(f.path) if f.path in wanted else None
        patch = expand_patch(f.path, f.patch or "", head, config) if head is not None else None
        if patch is not None and patch != f.patch:
            f = f.model_copy(update={"patch": patch})
            changed = True
        files.append(f)
    return Diff(files=tuple(files)) if changed else diff
