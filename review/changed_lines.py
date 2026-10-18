"""review.changed_lines — pure unified-diff hunk parsing and changed-line
filtering for lint-class analyzers (spec US-A3)."""

from __future__ import annotations

import re
from collections.abc import Mapping

from findings.models import Finding

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_lines(patch: str | None) -> frozenset[int]:
    """New-file line numbers added or modified by ``patch``. ``None`` (GitHub
    omits patches for large/binary changes) yields the empty set."""
    if not patch:
        return frozenset()
    lines: set[int] = set()
    current: int | None = None
    for raw in patch.splitlines():
        header = _HUNK_HEADER.match(raw)
        if header:
            current = int(header.group(1))
        elif current is None or raw.startswith("\\"):
            continue
        elif raw.startswith("+"):
            lines.add(current)
            current += 1
        elif not raw.startswith("-"):
            current += 1
    return frozenset(lines)


def hunk_line_ranges(patch: str | None) -> tuple[range, ...]:
    """Each hunk's contiguous new-file line span (context and added lines),
    so a fix range can be confined to one hunk. Pure-deletion hunks (no
    new-side lines) are omitted."""
    if not patch:
        return ()
    spans: list[range] = []
    start: int | None = None
    count = 0
    for raw in patch.splitlines():
        header = _HUNK_HEADER.match(raw)
        if header:
            if start is not None and count:
                spans.append(range(start, start + count))
            start, count = int(header.group(1)), 0
        elif start is None or raw.startswith(("\\", "-")):
            continue
        else:
            count += 1
    if start is not None and count:
        spans.append(range(start, start + count))
    return tuple(spans)


def filter_to_changed_lines(
    findings: list[Finding], patches: Mapping[str, str | None]
) -> tuple[list[Finding], int]:
    """Keep findings whose line is in an added/modified hunk line of their
    file's patch; returns ``(kept, filtered_count)``. A finding with no line,
    or for a file with no patch, is dropped rather than posted file-wide."""
    cache: dict[str, frozenset[int]] = {}
    kept: list[Finding] = []
    for finding in findings:
        if finding.file not in cache:
            cache[finding.file] = changed_lines(patches.get(finding.file))
        if finding.line is not None and finding.line in cache[finding.file]:
            kept.append(finding)
    return kept, len(findings) - len(kept)
