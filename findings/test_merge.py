"""Unit tests for findings.merge — pure dedupe/combine logic (US6), per
docs/rules/testing.md's "new domain logic a story touches still needs unit
tests too" rule. No real I/O.
"""

from __future__ import annotations

from findings.merge import POSTED_FINDINGS_CEILING, merge_findings
from findings.models import Finding, Severity, Source


def _finding(
    *,
    file: str = "app.py",
    line: int | None = 10,
    message: str = "problem",
    source: Source = "llm",
    severity: Severity = "medium",
    context_ref_ids: tuple[int, ...] | None = None,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
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
    # All findings share the same severity/source here, so the US-V8 sort
    # is a no-op tie across the board — original order survives as the
    # tiebreak, keeping the first `POSTED_FINDINGS_CEILING`, in order.
    assert result.findings == findings[:POSTED_FINDINGS_CEILING]


# --- US-V8: severity/source-trust ordering once the ceiling truncates ------


def test_ceiling_truncation_promotes_a_higher_severity_finding_ahead_of_lower_ones() -> None:
    """A single `critical` finding, even though it's the very last item in
    the deduped list, must survive the ceiling ahead of a `low`-severity
    finding that would otherwise have been kept by plain position — US-V8's
    "top-N ordered first by severity" rule."""
    low_severity_findings = [
        _finding(line=i, message=f"low priority issue {i}", severity="low", source="llm")
        for i in range(POSTED_FINDINGS_CEILING)
    ]
    critical_finding = _finding(
        line=POSTED_FINDINGS_CEILING,
        message="critical issue arriving last",
        severity="critical",
        source="semgrep",
    )
    deduped = [*low_severity_findings, critical_finding]

    result = merge_findings(deduped)

    assert len(result.findings) == POSTED_FINDINGS_CEILING
    assert result.dropped_by_ceiling_count == 1
    # The critical finding is promoted to the front despite arriving last;
    # the lowest-ranked `low`-severity finding (the last one in original
    # order) is the one dropped to make room.
    assert result.findings[0] == critical_finding
    assert result.findings[1:] == low_severity_findings[:-1]


def test_ceiling_truncation_prefers_non_llm_source_ahead_of_llm_at_equal_severity() -> None:
    """At equal severity, a static-analysis (`semgrep`) finding outranks an
    `llm`-sourced one, even when it arrives last in the deduped list —
    US-V8's "static-analysis-sourced findings ahead of verified-LLM-sourced
    findings at equal severity" rule."""
    llm_findings = [
        _finding(line=i, message=f"llm-sourced issue {i}", severity="medium", source="llm")
        for i in range(POSTED_FINDINGS_CEILING)
    ]
    static_finding = _finding(
        line=POSTED_FINDINGS_CEILING,
        message="static-analysis issue arriving last",
        severity="medium",
        source="semgrep",
    )
    deduped = [*llm_findings, static_finding]

    result = merge_findings(deduped)

    assert len(result.findings) == POSTED_FINDINGS_CEILING
    assert result.dropped_by_ceiling_count == 1
    assert result.findings[0] == static_finding
    # The last `llm`-sourced finding in original order is the one dropped.
    assert result.findings[1:] == llm_findings[:-1]


def test_ceiling_truncation_falls_back_to_original_order_for_equal_severity_and_source() -> None:
    """With severity and source both tied, `sorted()`'s own stability keeps
    original relative order as the final tiebreak — the third criterion
    US-V8 states."""
    findings = [
        _finding(line=i, message=f"distinct unrelated issue number {i}", severity="high")
        for i in range(POSTED_FINDINGS_CEILING + 1)
    ]

    result = merge_findings(findings)

    assert result.findings == findings[:POSTED_FINDINGS_CEILING]


def test_under_ceiling_mixed_severity_list_is_completely_unaffected_by_the_sort() -> None:
    """US-V8's sort only ever runs once truncation actually happens — a
    deduped list under the ceiling, even with wildly mixed severities and
    sources in an order that is *not* severity-sorted, must come back in
    exactly its own original order, not merely contain the same set of
    findings."""
    findings = [
        _finding(line=1, message="a low severity issue", severity="low", source="llm"),
        _finding(line=2, message="a critical issue", severity="critical", source="llm"),
        _finding(line=3, message="a medium severity issue", severity="medium", source="semgrep"),
        _finding(line=4, message="a high severity issue", severity="high", source="llm"),
        _finding(line=5, message="an info severity issue", severity="info", source="semgrep"),
    ]

    result = merge_findings(findings)

    assert result.findings == findings


# --- Phase 3 (v2): root-cause dedupe, tier-aware ordering -------------------

_DELETE_MSG = "Missing authorization check on the delete endpoint allows any user to delete"


def _llm(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "file": "a.py",
        "line": 1,
        "severity": "medium",
        "message": _DELETE_MSG,
        "source": "llm",
    }
    defaults.update(overrides)
    return Finding.model_validate(defaults)


def test_v1_never_merges_across_locations() -> None:
    result = merge_findings([_llm(file="a.py"), _llm(file="b.py")])

    assert len(result.findings) == 2
    assert result.root_cause_merges == []


def test_root_cause_merge_keeps_highest_severity_and_tier_and_notes_locations() -> None:
    first = _llm(file="a.py", line=3, severity="low", tier="nit")
    second = _llm(file="b.py", line=9, severity="high", tier="blocker")

    result = merge_findings([first, second], tiered=True)

    assert len(result.findings) == 1
    merged = result.findings[0]
    assert (merged.file, merged.severity, merged.tier) == ("b.py", "high", "blocker")
    assert merged.other_locations == ("a.py:3",)
    assert result.root_cause_merges == [("b.py:9", "a.py:3")]  # (winner, loser)


def test_root_cause_merge_records_winner_then_loser_when_the_first_occurrence_wins() -> None:
    first = _llm(file="a.py", line=3, severity="high")
    second = _llm(file="b.py", line=9, severity="low")

    result = merge_findings([first, second], tiered=True)

    assert result.root_cause_merges == [("a.py:3", "b.py:9")]


def test_root_cause_merge_takes_text_from_highest_severity_member() -> None:
    medium = _llm(file="a.py", severity="medium", scenario="medium scenario")
    critical = _llm(
        file="b.py",
        severity="critical",
        scenario="critical scenario",
        message=_DELETE_MSG + " (critical)",
    )

    merged = merge_findings([medium, critical], tiered=True).findings[0]

    assert (merged.severity, merged.scenario) == ("critical", "critical scenario")
    assert merged.message.endswith("(critical)")
    assert merged.other_locations == ("a.py:1",)


def test_root_cause_merge_tie_keeps_first_and_carries_context_refs() -> None:
    first = _llm(file="a.py", severity="high")
    second = _llm(file="b.py", severity="high", context_ref_ids=[1])

    merged = merge_findings([first, second], tiered=True).findings[0]

    assert merged.file == "a.py"
    assert merged.other_locations == ("b.py:1",)
    assert merged.context_ref_ids == second.context_ref_ids


def test_distinct_defects_sharing_boilerplate_do_not_merge() -> None:
    a = _llm(message="Unchecked None: user.email is lowercased without a null check in signup")
    b = _llm(
        file="b.py",
        message="Unchecked None: order.owner is lowercased without a null check in checkout",
    )

    assert len(merge_findings([a, b], tiered=True).findings) == 2


def test_messages_differing_only_in_a_code_identifier_do_not_merge() -> None:
    msg = "Missing null check on `{}` before dereferencing it in the request handler path"
    a = _llm(file="a.py", message=msg.format("user"))
    b = _llm(file="b.py", message=msg.format("users"))

    assert len(merge_findings([a, b], tiered=True).findings) == 2


def test_same_tokens_in_different_files_still_merge() -> None:
    msg = "Missing null check on `user` before dereferencing it in the request handler path"

    result = merge_findings(
        [_llm(file="a.py", message=msg), _llm(file="b.py", message=msg)], tiered=True
    )

    assert len(result.findings) == 1


def test_root_cause_merge_requires_same_rule_id_and_category_and_llm_source() -> None:
    assert len(merge_findings([_llm(), _llm(file="b.py", rule_id="R1")], tiered=True).findings) == 2
    assert (
        len(merge_findings([_llm(), _llm(file="b.py", category="smell")], tiered=True).findings)
        == 2
    )
    static = [_llm(source="semgrep"), _llm(file="b.py", source="semgrep")]
    assert len(merge_findings(static, tiered=True).findings) == 2


def test_tiered_orders_nits_last_and_none_tier_counts_as_should_fix() -> None:
    nit = _llm(file="n.py", message="nit text", tier="nit", severity="critical")
    none_tier = _llm(file="m.py", message="other text entirely", tier=None, severity="low")
    blocker = _llm(file="b.py", message="third distinct text", tier="blocker", severity="low")

    result = merge_findings([nit, none_tier, blocker], tiered=True)

    assert [f.file for f in result.findings] == ["b.py", "m.py", "n.py"]


def test_all_nits_over_the_ceiling_are_cut_first_by_tier() -> None:
    nits = [
        _llm(file=f"n{i}.py", rule_id=f"N{i}", tier="nit", severity="critical")
        for i in range(POSTED_FINDINGS_CEILING)
    ]
    should_fix = [
        _llm(file="s.py", message="the one real problem", tier="should_fix", severity="low")
    ]

    result = merge_findings([*nits, *should_fix], tiered=True)

    assert result.dropped_by_ceiling_count == 1
    assert result.findings[0].file == "s.py"
    assert [f.tier for f in result.dropped_by_ceiling] == ["nit"]


def test_mixed_tier_overflow_drops_lowest_tier_first() -> None:
    blockers = [
        _llm(file=f"b{i}.py", rule_id=f"B{i}", tier="blocker")
        for i in range(POSTED_FINDINGS_CEILING - 1)
    ]
    extra = [
        _llm(file="s1.py", message="should fix one", tier="should_fix"),
        _llm(file="n1.py", message="a nit zzzzzz", tier="nit"),
        _llm(file="s2.py", message="should fix two xx", tier="should_fix"),
    ]

    result = merge_findings([*blockers, *extra], tiered=True)

    assert {f.file for f in result.dropped_by_ceiling} == {"n1.py", "s2.py"}


def test_root_cause_merge_unions_other_locations_when_the_newcomer_wins() -> None:
    first = _llm(file="a.py", line=3, severity="low", tier="nit").model_copy(
        update={"other_locations": ("c.py:1", "b.py:9")}
    )
    second = _llm(file="b.py", line=9, severity="high", tier="blocker").model_copy(
        update={"other_locations": ("d.py:2", "c.py:1")}
    )

    merged = merge_findings([first, second], tiered=True).findings[0]

    assert (merged.file, merged.line) == ("b.py", 9)
    assert merged.other_locations == ("c.py:1", "d.py:2", "a.py:3")  # no dupes, not own b.py:9
