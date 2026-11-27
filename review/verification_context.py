"""Bounded head-version file text for the verification judge.

Pure and deterministic: no I/O. ``VerificationStep`` fetches head contents
and hands them here; every degradation (binary file, undecodable bytes,
parse failure, unsupported language) falls back to a plain line window or to
no entry for that file, never an exception.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from findings.models import Finding
from indexing.chunk import ChunkParseError, chunk_file, language_for_path
from review.diff_prompt import INERT_REFERENCE_DATA_FRAMING

_DATA_SEGMENTS = frozenset(
    {
        "testdata",
        "fixtures",
        "cassettes",
        "snapshots",
        "__snapshots__",
        "golden",
        "baseline",
        "baselines",
    }
)
_DATA_EXTENSIONS = frozenset({".jsonl", ".ndjson", ".csv", ".tsv", ".snap", ".har"})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_GAP = "   ..."
_ONE_LINE = re.compile(r"[\r\n]+")


def _git_lines(text: str) -> list[str]:
    """Lines as git and GitHub number them: only ``\n`` breaks a line (not form feed or U+2028)."""
    lines = [ln.removesuffix("\r") for ln in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


class FileContextConfig(BaseModel):
    max_bytes_per_file: int = 24_000
    surrounding_lines: int = 40
    max_total_bytes: int = 80_000


def _is_excluded(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    if name.startswith((".env", "id_rsa", "id_ed25519")) or name.endswith(_SECRET_SUFFIXES):
        return True
    if any(segment in _DATA_SEGMENTS for segment in parts[:-1]):
        return True
    return posixpath.splitext(name)[1] in _DATA_EXTENSIONS


def finding_context_paths(findings: Sequence[Finding]) -> tuple[str, ...]:
    """Unique cited paths in first-appearance order, minus secret-shaped
    and data-kind paths.
    """
    seen: dict[str, None] = {}
    for finding in findings:
        if finding.file and not _is_excluded(finding.file):
            seen.setdefault(finding.file)
    return tuple(seen)


def _decode(raw: bytes) -> str | None:
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _symbol_ranges(path: str, raw: bytes) -> list[tuple[int, int]]:
    """Line ranges of named chunks; empty on an unsupported language or a parse failure."""
    language = language_for_path(path)
    if language is None:
        return []
    try:
        chunks = chunk_file(language, path, raw).chunks
    except ChunkParseError:
        return []
    return [c.line_range for c in chunks if c.symbol_name and c.line_range]


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _render(lines: list[str], ranges: list[tuple[int, int]]) -> str:
    out: list[str] = []
    previous_end = 0
    for start, end in ranges:
        if start > previous_end + 1:
            out.append(_GAP)
        out.extend(f"{n:4d}| {lines[n - 1]}" for n in range(start, end + 1))
        previous_end = end
    if previous_end < len(lines):
        out.append(_GAP)
    return "\n".join(out)


def _windows(
    lines: list[str],
    cited: Sequence[int | None],
    symbols: list[tuple[int, int]],
    config: FileContextConfig,
    *,
    with_symbols: bool,
) -> list[tuple[int, int]]:
    total = len(lines)
    surround = config.surrounding_lines
    ranges: list[tuple[int, int]] = []
    for line in cited:
        if line is None:
            ranges.append((1, min(total, 2 * surround + 1)))
            continue
        line = min(max(line, 1), total)
        ranges.append((max(1, line - surround), min(total, line + surround)))
        if with_symbols:
            holding = [r for r in symbols if r[0] <= line <= r[1]]
            if holding:
                ranges.append(min(holding, key=lambda r: r[1] - r[0]))
    return _merge(ranges)


def _fit(text: str, limit: int) -> str:
    """Truncate whole lines so the text fits ``limit`` bytes."""
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        used += len(line.encode()) + 1
        if used > limit:
            kept.append(_GAP)
            break
        kept.append(line)
    return "\n".join(kept)


def _file_text(
    path: str, raw: bytes, cited: Sequence[int | None], config: FileContextConfig
) -> str | None:
    content = _decode(raw)
    if content is None:
        return None
    lines = _git_lines(content)
    if not lines:
        return None
    if len(raw) <= config.max_bytes_per_file:
        return _render(lines, [(1, len(lines))])
    symbols = _symbol_ranges(path, raw)
    text = ""
    for with_symbols in (True, False):
        text = _render(lines, _windows(lines, cited, symbols, config, with_symbols=with_symbols))
        if len(text.encode()) <= config.max_bytes_per_file:
            return text
    return _fit(text, config.max_bytes_per_file)


def build_file_contexts(
    findings: Sequence[Finding],
    head_files: Mapping[str, bytes],
    config: FileContextConfig | None = None,
) -> dict[str, str]:
    """Map each cited path to numbered head text, within the configured caps.

    A file that fits ``max_bytes_per_file`` is included whole; a larger one
    gets the smallest enclosing named symbol plus ``surrounding_lines`` either
    side of each cited line (merged, gaps shown as ``...``). Adding files
    stops at the first one that would exceed ``max_total_bytes``.
    """
    config = config or FileContextConfig()
    contexts: dict[str, str] = {}
    total = 0
    for path in finding_context_paths(findings):
        raw = head_files.get(path)
        if raw is None:
            continue
        cited = [f.line for f in findings if f.file == path]
        text = _file_text(path, raw, cited, config)
        if text is None:
            continue
        size = len(text.encode())
        if total + size > config.max_total_bytes:
            break
        contexts[path] = text
        total += size
    return contexts


def render_file_context_section(contexts: Mapping[str, str]) -> str:
    """Delimited inert-reference block for the judge prompt, ``""`` when empty."""
    if not contexts:
        return ""
    blocks = "\n".join(f"--- {_ONE_LINE.sub(' ', path)}\n{text}" for path, text in contexts.items())
    return (
        "\n\nCurrent (head) version of the files the candidate findings cite ("
        f"{INERT_REFERENCE_DATA_FRAMING}). A finding is invalid if this code refutes it:\n"
        f"{blocks}"
    )
