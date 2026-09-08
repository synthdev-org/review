"""Unit tests for review.verification — per
docs/plans/plan-deliver-verification-pipeline.md's Phase 3 Tests section.

Isolated from any I/O (per ADR-0005 and docs/rules/testing.md's pyramid):
``_VerificationVerdictItem`` parsing/dropping-malformed-items logic in
isolation (``_parse_verdicts``), plus the fail-closed verdict-correlation
cases Assumption 5 requires (``_correlate_verdicts``) — a missing entry, a
duplicate index (first occurrence wins, logged), and an out-of-range index
(ignored, logged), each proven not to silently produce a "kept" verdict for
the affected finding.
"""

from __future__ import annotations

import json
import logging

import pytest

from review.verification import (
    VerificationResponseParseError,
    _correlate_verdicts,
    _parse_verdicts,
    _VerificationVerdictItem,
)

# --- _parse_verdicts: per-item tolerant parsing ------------------------------


def _verdict_json(
    index: int,
    *,
    valid: bool = True,
    actionable: bool = True,
    specific: bool = True,
    valuable: bool = True,
) -> dict[str, object]:
    return {
        "index": index,
        "valid": valid,
        "actionable": actionable,
        "specific": specific,
        "valuable": valuable,
    }


def test_parse_verdicts_returns_one_item_per_well_formed_verdict() -> None:
    content = json.dumps({"verdicts": [_verdict_json(0), _verdict_json(1, valid=False)]})

    verdicts = _parse_verdicts(content, job_id="job-1")

    assert len(verdicts) == 2
    assert verdicts[0] == _VerificationVerdictItem(**_verdict_json(0))
    assert verdicts[1].valid is False


def test_parse_verdicts_drops_one_malformed_item_not_the_whole_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = json.dumps(
        {
            "verdicts": [
                _verdict_json(0),
                {
                    "index": 1,
                    "valid": "not-a-bool",
                    "actionable": True,
                    "specific": True,
                    "valuable": True,
                },
                _verdict_json(2),
            ]
        }
    )

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        verdicts = _parse_verdicts(content, job_id="job-1")

    assert [v.index for v in verdicts] == [0, 2]
    dropped = [r for r in caplog.records if "dropping one malformed verdict" in r.message]
    assert len(dropped) == 1
    assert dropped[0].job_id == "job-1"  # type: ignore[attr-defined]
    assert dropped[0].verdict_position == 1  # type: ignore[attr-defined]


def test_parse_verdicts_missing_index_field_is_dropped_as_malformed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = json.dumps(
        {"verdicts": [{"valid": True, "actionable": True, "specific": True, "valuable": True}]}
    )

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        verdicts = _parse_verdicts(content, job_id="job-1")

    assert verdicts == []


def test_parse_verdicts_non_json_content_raises_verification_response_parse_error() -> None:
    with pytest.raises(VerificationResponseParseError):
        _parse_verdicts("not valid json {{{", job_id="job-1")


def test_parse_verdicts_missing_top_level_verdicts_key_raises_parse_error() -> None:
    with pytest.raises(VerificationResponseParseError):
        _parse_verdicts(json.dumps({"not_verdicts": []}), job_id="job-1")


def test_parse_verdicts_verdicts_not_a_list_raises_parse_error() -> None:
    with pytest.raises(VerificationResponseParseError):
        _parse_verdicts(json.dumps({"verdicts": "nope"}), job_id="job-1")


def test_parse_verdicts_empty_verdicts_list_is_not_an_error() -> None:
    assert _parse_verdicts(json.dumps({"verdicts": []}), job_id="job-1") == []


# --- _correlate_verdicts: fail-closed correlation (Assumption 5) ------------


def test_correlate_verdicts_missing_entry_leaves_that_index_unmatched() -> None:
    """A verdict list missing an entry for one candidate finding (index 1
    out of 2 candidates) must never produce a verdict for it — the caller's
    own ``verdict_by_index.get(1)`` returns ``None``, which
    ``VerificationStep.run`` treats as rejected, not a crash or a silent
    'verified'."""
    verdicts = [_VerificationVerdictItem(**_verdict_json(0))]

    verdict_by_index = _correlate_verdicts(verdicts, candidate_count=2, job_id="job-1")

    assert 0 in verdict_by_index
    assert 1 not in verdict_by_index


def test_correlate_verdicts_duplicate_index_keeps_first_occurrence_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _VerificationVerdictItem(**_verdict_json(0, valid=True))
    second = _VerificationVerdictItem(**_verdict_json(0, valid=False))

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        verdict_by_index = _correlate_verdicts([first, second], candidate_count=1, job_id="job-1")

    assert verdict_by_index[0] is first
    dup_warnings = [r for r in caplog.records if "duplicate verdict index" in r.message]
    assert len(dup_warnings) == 1
    assert dup_warnings[0].job_id == "job-1"  # type: ignore[attr-defined]
    assert dup_warnings[0].index == 0  # type: ignore[attr-defined]


def test_correlate_verdicts_out_of_range_index_is_ignored_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    in_range = _VerificationVerdictItem(**_verdict_json(0))
    out_of_range = _VerificationVerdictItem(**_verdict_json(5))

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        verdict_by_index = _correlate_verdicts(
            [in_range, out_of_range], candidate_count=1, job_id="job-1"
        )

    assert list(verdict_by_index) == [0]
    oor_warnings = [r for r in caplog.records if "out-of-range verdict index" in r.message]
    assert len(oor_warnings) == 1
    assert oor_warnings[0].job_id == "job-1"  # type: ignore[attr-defined]
    assert oor_warnings[0].index == 5  # type: ignore[attr-defined]


def test_correlate_verdicts_negative_index_is_out_of_range() -> None:
    verdict = _VerificationVerdictItem(**_verdict_json(-1))

    verdict_by_index = _correlate_verdicts([verdict], candidate_count=1, job_id="job-1")

    assert verdict_by_index == {}
