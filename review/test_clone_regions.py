"""Unit tests for review.clone_regions (pure): the new-code test, merging,
grouping, the per-job cap, the anchor line and the message wording
(plan-phase-10 Phase 4; US-C4, US-C5, US-C10)."""

from __future__ import annotations

import re

from review.clone_regions import RegionMatch, is_new_code, plan_findings


def _match(
    start: int = 1,
    end: int = 20,
    *,
    file: str = "src/new.ts",
    other: str = "src/old.ts",
    other_start: int = 100,
    other_end: int | None = None,
) -> RegionMatch:
    return RegionMatch(
        file=file,
        start_line=start,
        end_line=end,
        other_path=other,
        other_start_line=other_start,
        other_end_line=other_end if other_end is not None else other_start + (end - start),
    )


def _all(file: str, start: int, end: int) -> dict[str, frozenset[int]]:
    return {file: frozenset(range(start, end + 1))}


def test_exactly_half_new_is_dropped_more_than_half_is_kept() -> None:
    added = frozenset(range(1, 6))  # lines 1..5 of 1..10

    assert is_new_code(1, 10, added) is False
    assert is_new_code(1, 10, added | {6}) is True
    assert is_new_code(1, 10, frozenset()) is False


def test_single_match_becomes_one_low_severity_duplication_finding() -> None:
    plan = plan_findings([_match(5, 24)], _all("src/new.ts", 5, 24), max_findings=5)

    (finding,) = plan.findings
    assert (finding.source, finding.rule_id, finding.severity) == (
        "clone_detection",
        "duplication",
        "low",
    )
    assert finding.file == "src/new.ts"
    assert finding.line == 5
    assert finding.message == (
        "Duplicates 20 lines of `src/old.ts` (lines 100\u2013119); consider extracting shared code."
    )
    assert plan.cap_dropped_count == 0


def test_anchor_is_the_first_added_line_of_the_region() -> None:
    plan = plan_findings([_match(1, 20)], {"src/new.ts": frozenset(range(8, 21))}, max_findings=5)

    assert plan.findings[0].line == 8


def test_several_locations_report_the_largest_then_first_path_plus_n_more() -> None:
    matches = [
        _match(1, 20, other="src/b.ts", other_start=10),
        _match(1, 20, other="src/a.ts", other_start=40),
        _match(1, 12, other="src/c.ts", other_start=70),
    ]

    plan = plan_findings(matches, _all("src/new.ts", 1, 20), max_findings=5)

    (finding,) = plan.findings
    assert "`src/a.ts` (lines 40\u201359) and 2 more;" in finding.message


def test_overlapping_and_adjacent_regions_in_one_file_merge_into_one_finding() -> None:
    matches = [_match(1, 20, other="src/a.ts"), _match(21, 40, other="src/a.ts", other_start=300)]

    plan = plan_findings(matches, _all("src/new.ts", 1, 40), max_findings=5)

    assert len(plan.findings) == 1


def test_a_gap_between_regions_keeps_them_separate() -> None:
    matches = [_match(1, 20), _match(30, 50, other_start=300)]

    plan = plan_findings(matches, _all("src/new.ts", 1, 50), max_findings=5)

    assert len(plan.findings) == 2


def test_cap_keeps_the_largest_regions_and_counts_the_rest() -> None:
    matches = [_match(i * 100, i * 100 + 9 + i, other_start=i * 1000) for i in range(1, 9)]
    added = {"src/new.ts": frozenset(range(0, 1000))}

    plan = plan_findings(matches, added, max_findings=5)

    assert len(plan.findings) == 5
    assert plan.cap_dropped_count == 3
    assert {f.line for f in plan.findings} == {400, 500, 600, 700, 800}


def test_message_has_only_paths_and_integers() -> None:
    plan = plan_findings(
        [_match(1, 30, other="src/billing/invoice.ts")], _all("src/new.ts", 1, 30), max_findings=5
    )

    message = plan.findings[0].message
    assert re.fullmatch(
        r"Duplicates \d+ lines of `[^`]+` \(lines \d+\u2013\d+\); "
        r"consider extracting shared code\.",
        message,
    )


def test_no_matches_yields_no_findings() -> None:
    assert plan_findings([], {}, max_findings=5).findings == ()


def test_hostile_path_in_message_is_neutralised_by_the_posting_escape() -> None:
    from adapter.github.posting import _finding_body

    hostile = "src/a`b\n# [x](https://evil.example)<img>.ts"
    plan = plan_findings([_match(1, 20, other=hostile)], _all("src/new.ts", 1, 20), max_findings=5)
    (finding,) = plan.findings

    body = _finding_body(finding)

    assert "`" not in re.sub(r"\\`", "", body)  # every backtick is backslash-escaped
    assert "<" not in body and "\n# " not in body
    assert "src/a\\`b" in body and "\\[x\\]\\(https://evil\\.example\\)&lt;img&gt;" in body
