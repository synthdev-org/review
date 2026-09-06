"""Unit tests for findings.merge — pure dedupe/combine logic (US6), per
docs/rules/testing.md's "new domain logic a story touches still needs unit
tests too" rule. No real I/O.
"""

from __future__ import annotations

from findings.merge import POSTED_FINDINGS_CEILING, merge_findings
from findings.models import Finding, Source


def _finding(
    *,
    file: str = "app.py",
    line: int | None = 10,
    message: str = "problem",
    source: Source = "llm",
    context_ref_ids: tuple[int, ...] | None = None,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity="medium",
        message=message,
        source=source,
        context_ref_ids=context_ref_ids,
    )


def test_empty_input_returns_empty_output() -> None:
    result = merge_findings([])

    assert result.findings == []
    assert result.dropped_by_ceiling_count == 0


def test_clear_duplicate_on_same_file_and_line_collapses_to_one() -> None:
    static_finding = _finding(message="SQL injection risk here", source="semgrep")
    llm_finding = _finding(message="SQL injection risk detected here", source="llm")

    result = merge_findings([static_finding, llm_finding])

    assert len(result.findings) == 1
    # The first occurrence wins — the static-analysis finding, per this
    # module's own docstring ("deterministic first pass as the trust
    # anchor" design rationale).
    assert result.findings[0] is static_finding


def test_deduped_llm_findings_context_ref_ids_carries_forward_to_the_kept_finding() -> None:
    """A static-analysis finding (kept, no `context_ref_ids`) deduped
    against an LLM finding of the same defect (dropped, but citation-
    bearing) must not silently lose that citation data — `review.output`'s
    `_apply_citations` pass runs *after* `merge_findings`, so a dropped
    `context_ref_ids` here can never be recovered downstream. The kept
    finding's own `message`/`severity`/`source` still come from the
    static-analysis finding, unchanged."""
    static_finding = _finding(message="SQL injection risk here", source="semgrep")
    llm_finding = _finding(
        message="SQL injection risk detected here", source="llm", context_ref_ids=(0, 2)
    )

    result = merge_findings([static_finding, llm_finding])

    assert len(result.findings) == 1
    kept = result.findings[0]
    assert kept.message == static_finding.message
    assert kept.source == static_finding.source
    assert kept.context_ref_ids == (0, 2)


def test_kept_findings_own_context_ref_ids_are_not_overwritten_by_a_later_duplicate() -> None:
    """When the *kept* (first-seen) finding already carries `context_ref_ids`
    of its own, a later-arriving duplicate's `context_ref_ids` must not
    overwrite it — "first non-`None` value wins", not "last duplicate
    wins", consistent with the rest of this module's own "first occurrence
    wins" precedence."""
    first_llm_finding = _finding(
        message="SQL injection risk here", source="llm", context_ref_ids=(1,)
    )
    second_llm_finding = _finding(
        message="SQL injection risk detected here", source="llm", context_ref_ids=(2, 3)
    )

    result = merge_findings([first_llm_finding, second_llm_finding])

    assert len(result.findings) == 1
    assert result.findings[0].context_ref_ids == (1,)


def test_three_way_duplicate_chain_still_honors_first_non_none_wins() -> None:
    """`_dedupe`'s own bucket write-back (updating the stored bucket entry
    to the *merged* finding, not just `deduped`) is load-bearing for a
    3-way duplicate chain, not dead code: without it, the third finding's
    "is the kept finding's own `context_ref_ids` still `None`?" check would
    read a *stale* bucket entry from before the second finding's merge,
    incorrectly conclude the kept finding still has no citation, and
    overwrite the second finding's own `context_ref_ids` instead of
    leaving it alone."""
    first_finding = _finding(message="SQL injection risk here", source="semgrep")
    second_finding = _finding(
        message="SQL injection risk detected here", source="llm", context_ref_ids=(1,)
    )
    third_finding = _finding(
        message="possible SQL injection risk here", source="llm", context_ref_ids=(2, 3)
    )

    result = merge_findings([first_finding, second_finding, third_finding])

    assert len(result.findings) == 1
    kept = result.findings[0]
    assert kept.message == first_finding.message
    assert kept.context_ref_ids == (1,)


def test_two_distinct_findings_on_the_same_line_stay_separate() -> None:
    finding_a = _finding(message="unused import")
    finding_b = _finding(message="possible null pointer dereference in handler")

    result = merge_findings([finding_a, finding_b])

    assert result.findings == [finding_a, finding_b]


def test_findings_on_different_lines_never_dedupe_even_with_identical_message() -> None:
    finding_a = _finding(line=1, message="identical text")
    finding_b = _finding(line=2, message="identical text")

    result = merge_findings([finding_a, finding_b])

    assert result.findings == [finding_a, finding_b]


def test_two_file_level_findings_none_line_can_still_dedupe() -> None:
    finding_a = _finding(line=None, message="file-level issue with this module")
    finding_b = _finding(line=None, message="file-level issue in this module")

    result = merge_findings([finding_a, finding_b])

    assert len(result.findings) == 1


# --- Posted-findings ceiling, boundary cases -------------------------------


def _distinct_findings(count: int) -> list[Finding]:
    # Each on its own line with a wholly distinct message — never a
    # dedupe-eligible pair — so the ceiling, not dedupe, is what's under
    # test here.
    return [_finding(line=i, message=f"distinct unrelated issue number {i}") for i in range(count)]


def test_merged_output_at_exactly_the_ceiling_passes_through_unmodified() -> None:
    findings = _distinct_findings(POSTED_FINDINGS_CEILING)

    result = merge_findings(findings)

    assert len(result.findings) == POSTED_FINDINGS_CEILING
    assert result.dropped_by_ceiling_count == 0


def test_merged_output_one_over_the_ceiling_drops_exactly_the_excess() -> None:
    findings = _distinct_findings(POSTED_FINDINGS_CEILING + 1)

    result = merge_findings(findings)

    assert len(result.findings) == POSTED_FINDINGS_CEILING
    assert result.dropped_by_ceiling_count == 1
    # The kept findings are the first `POSTED_FINDINGS_CEILING`, in order —
    # the same "flag, don't silently reorder/truncate coverage" posture
    # every other ceiling in this plan keeps.
    assert result.findings == findings[:POSTED_FINDINGS_CEILING]
