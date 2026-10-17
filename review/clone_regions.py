"""review.clone_regions — the pure half of the ``clone_detection`` step
(``docs/plans/plan-phase-10.md`` Phase 4): the new-code test, merging of
overlapping regions, grouping of several matches into one finding, the
per-job cap and the finding message.

No I/O, zero infra imports. Messages are built from paths and integers only
— never source text from either location (US-C4), so redaction cannot be
bypassed and nothing beyond a path is exposed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from findings.models import Finding

DUPLICATION_RULE_ID = "duplication"
_EN_DASH = "\u2013"


@dataclass(frozen=True, slots=True)
class RegionMatch:
    """One verified duplicated run: lines ``start_line..end_line`` (1-based,
    inclusive) of PR file ``file`` equal lines ``other_start_line..
    other_end_line`` of ``other_path``."""

    file: str
    start_line: int
    end_line: int
    other_path: str
    other_start_line: int
    other_end_line: int

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class ClonePlan:
    """``plan_findings`` result: the findings to emit and how many merged
    regions the per-job cap dropped."""

    findings: tuple[Finding, ...]
    cap_dropped_count: int


def is_new_code(start_line: int, end_line: int, added_lines: frozenset[int]) -> bool:
    """Whether **more than half** of lines ``start_line..end_line`` are in
    ``added_lines`` (US-C5): exactly half is dropped."""
    total = end_line - start_line + 1
    added = sum(1 for line in range(start_line, end_line + 1) if line in added_lines)
    return added * 2 > total


def _cluster(matches: Sequence[RegionMatch]) -> list[list[RegionMatch]]:
    """Group same-file matches whose line ranges overlap or touch (US-C8,
    US-C10), in file then line order."""
    clusters: list[list[RegionMatch]] = []
    cluster_end = 0
    for match in sorted(matches, key=lambda item: (item.file, item.start_line, item.end_line)):
        current = clusters[-1] if clusters else None
        if current and current[0].file == match.file and match.start_line <= cluster_end + 1:
            current.append(match)
            cluster_end = max(cluster_end, match.end_line)
        else:
            clusters.append([match])
            cluster_end = match.end_line
    return clusters


def _finding(cluster: list[RegionMatch], added_lines: frozenset[int]) -> tuple[int, Finding]:
    start = min(match.start_line for match in cluster)
    end = max(match.end_line for match in cluster)
    # Largest match first, then lexicographically first path (US-C4).
    ranked = sorted(cluster, key=lambda m: (-m.line_count, m.other_path, m.other_start_line))
    best = ranked[0]
    others = {(m.other_path, m.other_start_line, m.other_end_line) for m in cluster}
    more = len(others) - 1
    message = (
        f"Duplicates {best.line_count} lines of `{best.other_path}` "
        f"(lines {best.other_start_line}{_EN_DASH}{best.other_end_line})"
        + (f" and {more} more" if more else "")
        + "; consider extracting shared code."
    )
    anchor = next((line for line in range(start, end + 1) if line in added_lines), start)
    finding = Finding(
        file=best.file,
        line=anchor,
        severity="low",
        message=message,
        source="clone_detection",
        rule_id=DUPLICATION_RULE_ID,
    )
    return end - start + 1, finding


def plan_findings(
    matches: Sequence[RegionMatch],
    added_lines_by_file: dict[str, frozenset[int]],
    *,
    max_findings: int,
) -> ClonePlan:
    """Merge, group and cap ``matches`` into findings: one per merged region
    (anchored at its first added line), the largest ``max_findings`` kept."""
    sized = [
        _finding(cluster, added_lines_by_file.get(cluster[0].file, frozenset()))
        for cluster in _cluster(matches)
    ]
    sized.sort(key=lambda item: (-item[0], item[1].file, item[1].line or 0))
    kept = sized[:max_findings]
    return ClonePlan(
        findings=tuple(finding for _, finding in kept), cap_dropped_count=len(sized) - len(kept)
    )
