"""Unit tests for review.fix_validation (US-AF4, AF5, AF8, AF10) over the
real parsers and the fixture corpus in review/testdata/fix/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from findings.models import Suggestion
from review.fix_validation import (
    _TREE_SITTER_EXTENSIONS,
    RejectionReason,
    apply_suggestion,
    check_range,
    check_replacement,
    check_syntax,
    is_supported_path,
    range_text,
    syntax_error_count,
)

_CORPUS = Path(__file__).parent / "testdata" / "fix"
_CASES: dict[str, dict[str, Any]] = json.loads((_CORPUS / "cases.json").read_text())


def _suggestion(start: int, end: int, replacement: str = "x") -> Suggestion:
    return Suggestion(start_line=start, end_line=end, replacement=replacement)


# --- check_range (US-AF4) ---------------------------------------------------

_HUNKS = (range(10, 20), range(30, 40))


def test_range_inside_one_hunk_is_accepted() -> None:
    assert check_range(_suggestion(11, 13), 12, _HUNKS, 20) is None


def test_single_line_range_is_accepted() -> None:
    assert check_range(_suggestion(12, 12), 12, _HUNKS, 20) is None


@pytest.mark.parametrize(
    ("start", "end", "line"),
    [
        (18, 32, 19),  # spans two hunks
        (5, 12, 11),  # starts before the hunk
        (18, 20, 19),  # ends one past the hunk
        (11, 13, 14),  # finding outside the range
        (13, 11, 12),  # inverted
        (0, 3, 1),  # not a file line
    ],
)
def test_range_violations_are_out_of_range(start: int, end: int, line: int) -> None:
    assert check_range(_suggestion(start, end), line, _HUNKS, 20) is RejectionReason.OUT_OF_RANGE


def test_range_over_the_cap_is_out_of_range() -> None:
    assert check_range(_suggestion(10, 14), 12, _HUNKS, 4) is RejectionReason.OUT_OF_RANGE
    assert check_range(_suggestion(10, 13), 12, _HUNKS, 4) is None


# --- apply_suggestion / range_text -----------------------------------------


def test_line_numbers_follow_newlines_only() -> None:
    content = "a\x0cb\nc\nd\n"
    assert apply_suggestion(content, _suggestion(2, 2, "X")) == "a\x0cb\nX\nd\n"
    assert range_text(content, _suggestion(1, 1)) == "a\x0cb\n"
    assert check_replacement("c\n", "a\x0cb\nc", 2) is None
    assert check_replacement("c\n", "a\x0cb\nc\nd", 2) is RejectionReason.OVERSIZED


def test_apply_replaces_the_inclusive_range() -> None:
    assert apply_suggestion("a\nb\nc\n", _suggestion(2, 2, "B")) == "a\nB\nc\n"
    assert apply_suggestion("a\nb\nc\n", _suggestion(1, 2, "X\nY\nZ\n")) == "X\nY\nZ\nc\n"


def test_apply_empty_replacement_deletes_the_range() -> None:
    assert apply_suggestion("a\nb\nc\n", _suggestion(2, 3, "")) == "a\n"


def test_apply_blank_line_replacement_is_not_a_deletion() -> None:
    assert apply_suggestion("a\nb\nc\n", _suggestion(2, 2, "\n")) == "a\n\nc\n"


def test_apply_keeps_a_missing_final_newline() -> None:
    assert apply_suggestion("a\nb", _suggestion(2, 2, "B")) == "a\nB"


def test_apply_outside_the_file_raises() -> None:
    with pytest.raises(ValueError):
        apply_suggestion("a\n", _suggestion(1, 2))


def test_range_text_returns_original_lines() -> None:
    assert range_text("a\nb\nc\n", _suggestion(2, 3)) == "b\nc\n"


# --- check_replacement (US-AF8) ---------------------------------------------


def test_replacement_text_rules() -> None:
    assert check_replacement("b\n", "B", 40) is None
    assert check_replacement("b\n", "", 40) is None  # deletion
    assert check_replacement("b\n", "b", 40) is RejectionReason.NOOP
    assert check_replacement("b\n", "b\n", 40) is RejectionReason.NOOP
    assert check_replacement("", "", 40) is RejectionReason.NOOP
    assert check_replacement("b\n", "a\r\nb", 40) is RejectionReason.INVALID_TEXT
    assert check_replacement("b\n", "a\x00", 40) is RejectionReason.INVALID_TEXT
    assert check_replacement("b\n", "1\n2\n3\n", 2) is RejectionReason.OVERSIZED
    assert check_replacement("b\n", "1\n2\n", 2) is None


def test_rejection_reasons_are_the_closed_metric_vocabulary() -> None:
    assert {reason.value for reason in RejectionReason} == {
        "syntax_error",
        "reanalysis_failed",
        "rubric_failed",
        "noop",
        "redaction",
        "oversized",
        "out_of_range",
        "invalid_text",
        "overlap",
        "parse_error",
        "timeout",
        "fetch_failed",
    }


# --- syntax gate (US-AF5), per language over the corpus --------------------


def _read(language: str, name: str) -> str:
    return str((_CORPUS / language / _CASES[language][name]).read_text())


def _patched(language: str, source: str, case: str) -> str:
    return apply_suggestion(source, Suggestion(**_CASES[language][case]))


@pytest.mark.parametrize("language", sorted(_CASES))
def test_clean_patch_is_accepted(language: str) -> None:
    original = _read(language, "original")
    path = _CASES[language]["original"]

    for case in ("valid", "deletion"):
        assert check_syntax(path, original, _patched(language, original, case)) is None


@pytest.mark.parametrize("language", sorted(_CASES))
def test_syntax_breaking_patch_is_rejected(language: str) -> None:
    original = _read(language, "original")
    path = _CASES[language]["original"]

    result = check_syntax(path, original, _patched(language, original, "broken"))

    assert result is RejectionReason.SYNTAX_ERROR


@pytest.mark.parametrize("language", sorted(_CASES))
def test_preexisting_error_outside_the_range_is_tolerated(language: str) -> None:
    original = _read(language, "preexisting")
    path = _CASES[language]["preexisting"]
    assert syntax_error_count(path, original) != 0

    clean = check_syntax(path, original, _patched(language, original, "valid"))
    broken = check_syntax(path, original, _patched(language, original, "broken"))

    # Python cannot count errors, so a file that already fails to parse is
    # rejected outright (conservative narrowing, plan Assumption 2).
    assert clean is (RejectionReason.SYNTAX_ERROR if language == "python" else None)
    assert broken is RejectionReason.SYNTAX_ERROR


def test_corpus_covers_every_gate_language() -> None:
    corpus_extensions = {Path(case["original"]).suffix for case in _CASES.values()}

    assert corpus_extensions == {*_TREE_SITTER_EXTENSIONS, ".py"} - {".jsx", ".mjs", ".cjs"}
    for language, case in _CASES.items():
        assert {"original", "preexisting", "valid", "broken", "deletion"} <= case.keys(), language
        assert (_CORPUS / language / case["original"]).is_file()
        assert (_CORPUS / language / case["preexisting"]).is_file()


def test_javascript_variants_use_the_tsx_grammar() -> None:
    for suffix in (".js", ".jsx", ".mjs", ".cjs"):
        assert syntax_error_count(f"a{suffix}", "const a = <b />;\n") == 0
        assert syntax_error_count(f"a{suffix}", "const = ;\n") != 0


def test_unsupported_path_has_no_count_and_fails_closed() -> None:
    assert syntax_error_count("notes.txt", "anything") is None
    assert not is_supported_path("notes.txt")
    assert is_supported_path("pkg/Main.GO")
    assert check_syntax("notes.txt", "a", "b") is RejectionReason.SYNTAX_ERROR
