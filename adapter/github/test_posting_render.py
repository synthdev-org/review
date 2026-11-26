"""Unit tests for the pure rendering helpers in ``adapter.github.posting``."""

# No HTTP and no GitHub. The publisher's wire behavior is covered by the
# respx integration tests alongside.

from __future__ import annotations

import logging

import pytest

from adapter.github import posting
from adapter.github.exceptions import GitHubPostingMalformedRequestError
from findings.markdown import escape_markdown
from findings.models import Finding


def _finding(
    message: str = "boom",
    *,
    file: str = "a.py",
    line: int | None = 3,
    severity: str = "high",
    **extra: object,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        message=message,
        source="llm",
        **extra,
    )


# --- _finding_body ----------------------------------------------------------------


def test_finding_body_escapes_untrusted_text_and_shows_the_source() -> None:
    body = posting._finding_body(_finding("<img src=x> **bold**", rule_id="r_1"))

    assert body.startswith("**[high]** ")
    assert "<img" not in body and "**bold**" not in body  # markup in the message is neutralized
    assert "_source: llm_" in body
    assert "(r" in body  # rule id is appended, not wrapped in raw backticks
    assert "Scenario" not in body


def test_finding_body_v2_adds_scenario_other_locations_and_agent_prompt() -> None:
    finding = _finding(
        scenario="when x is None", other_locations=("b.py:4", "c.py:9"), citation_markdown="**Ref**"
    )

    v1 = posting._finding_body(finding)
    v2 = posting._finding_body(finding, v2=True)

    assert "Scenario" not in v1 and "Also at" not in v1
    assert "**Scenario:** when x is None" in v2
    assert "_Also at: b\\.py:4, c\\.py:9_" in v2
    assert v2.count("**Ref**") == 1 and v1.count("**Ref**") == 1
    assert len(v2) > len(v1)


def test_finding_body_v2_truncates_an_oversize_comment_but_v1_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    long = _finding("word " * 10_000)

    with caplog.at_level(logging.WARNING, logger=posting.logger.name):
        v2 = posting._finding_body(long, v2=True)
    v1 = posting._finding_body(long)

    assert len(v2) < posting._COMMENT_HARD_LIMIT + 100 and v2.endswith("_(truncated)_")
    assert len(v1) > posting._COMMENT_HARD_LIMIT  # v1 stays byte-identical
    assert any("finding body truncated" in r.message for r in caplog.records)


def test_review_comments_anchor_each_finding_at_its_line() -> None:
    comments = posting._review_comments([_finding(line=3), _finding(file="b.py", line=8)])

    assert [(c["path"], c["line"]) for c in comments] == [("a.py", 3), ("b.py", 8)]
    assert all(c["body"].startswith("**[high]**") for c in comments)


# --- collapsed / unreviewed sections --------------------------------------------------


def test_collapsed_section_lists_each_finding_and_caps_the_list() -> None:
    items = [
        _finding(f"m{i}\nsecond line", file=f"f{i}.py", line=None if i == 0 else i)
        for i in range(52)
    ]

    section = posting._collapsed_section(items)

    lines = section.split("\n")
    assert lines[0] == "<details><summary>52 more finding(s)</summary>"
    assert lines[-1] == "</details>"
    assert "- ...and 2 more not listed." in section
    assert section.count("\n- **") == posting._COLLAPSED_MAX_ITEMS
    assert "second line" in section and "m0 second line" in section  # newlines flattened
    assert "**f0\\.py**" in section and "**f1\\.py:1**" in section  # line is shown only when known


def test_unreviewed_section_escapes_paths_and_caps_the_list() -> None:
    paths = tuple(f"dir/*file{i}*.py" for i in range(53))

    section = posting._unreviewed_section(paths)

    assert section.startswith("Not reviewed (")
    assert section.count("\n- ") == 51  # 50 paths + the "and N more" line
    assert section.endswith("- and 3 more")
    assert "*file0*" not in section  # markdown emphasis in a path is escaped


# --- _review_body -----------------------------------------------------------------------


def test_review_body_with_no_extras_is_just_the_summary() -> None:
    assert posting._review_body([], 2) == escape_markdown("Automated review found 2 finding(s).")


def test_review_body_v1_lists_file_level_findings_and_unreviewed_files() -> None:
    body = posting._review_body(
        [_finding("note", file="x.py", line=None)], 1, unreviewed_files=("big.bin",)
    )

    assert "Findings not attributable to a specific line:" in body
    assert "- **x\\.py**: **[high]** note" in body
    assert "Not reviewed (" in body and "- big\\.bin" in body
    assert "<details>" not in body  # collapsed is None => v1 layout


def test_review_body_v2_moves_overflowing_file_level_findings_into_the_collapsed_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    big = [_finding("w " * 5_000, file=f"f{i}.py", line=None) for i in range(3)]
    small = _finding("tiny", file="small.py", line=None)

    with caplog.at_level(logging.WARNING, logger=posting.logger.name):
        body = posting._review_body([*big, small], 4, collapsed=[])

    assert "- **small\\.py**:" in body
    assert (
        "- **f0\\.py**:" in body and "f1\\.py" in body.split("<details>")[1]
    )  # a smaller later finding still fits the budget
    assert "<details><summary>" in body  # the overflowed ones are folded, not dropped
    assert any("over the review-body budget" in r.message for r in caplog.records)
    assert len(body) <= posting._BODY_HARD_LIMIT + 100


def test_review_body_hard_truncates_as_a_last_resort(caplog: pytest.LogCaptureFixture) -> None:
    paths = tuple("p" * 200 for _ in range(50))
    findings = [_finding("m " * 100, file="a.py", line=None) for _ in range(400)]

    with caplog.at_level(logging.WARNING, logger=posting.logger.name):
        body = posting._review_body(findings, 400, unreviewed_files=paths)

    assert body.endswith("_(truncated)_")
    assert any("hard-truncated" in r.message for r in caplog.records)


# --- _indent_under_bullet / fences / truncation -------------------------------------------


def test_indent_under_bullet_indents_text_lines_but_not_blank_ones() -> None:
    assert posting._indent_under_bullet("a\n\nb") == "  a\n\n  b"


@pytest.mark.parametrize(
    ("text", "fence", "details"),
    [
        pytest.param("plain", None, 0, id="nothing-open"),
        pytest.param("```py\ncode", ("", "```"), 0, id="open-fence"),
        pytest.param("```\ncode\n```", None, 0, id="closed-fence"),
        pytest.param("````\n```\nx", ("", "````"), 0, id="shorter-fence-does-not-close"),
        pytest.param("```a`b\nx", None, 0, id="backtick-in-info-string-is-not-an-opener"),
        pytest.param("<details>\n<details>\n</details>", None, 1, id="open-details"),
        pytest.param("```\n<details>\n```", None, 0, id="details-in-code-ignored"),
        pytest.param("</details>", None, 0, id="stray-closer-clamped-to-zero"),
    ],
)
def test_scan_fences_reports_what_is_left_open(
    text: str, fence: tuple[str, str] | None, details: int
) -> None:
    assert posting._scan_fences(text) == (fence, details)


def test_truncate_body_cuts_at_a_line_boundary_and_reopens_nothing() -> None:
    body = "line one\nline two\nline three"

    assert posting._truncate_body(body, 12) == "line one\n\n_(truncated)_"


def test_truncate_body_keeps_a_cut_that_lands_exactly_on_a_newline() -> None:
    assert posting._truncate_body("abc\ndef", 3) == "abc\n\n_(truncated)_"


def test_truncate_body_never_ends_on_a_lone_backslash() -> None:
    cut = posting._truncate_body("abcdef\\gh", 7)  # long unbroken line, cut after the backslash

    assert cut == "abcdef\n\n_(truncated)_"


def test_truncate_body_keeps_an_even_run_of_backslashes() -> None:
    assert posting._truncate_body("ab\\\\cd", 4) == "ab\\\\\n\n_(truncated)_"


def test_truncate_body_closes_an_open_fence_and_details() -> None:
    body = "<details>\n```\ncode line\nmore code that is cut"

    cut = posting._truncate_body(body, 25)

    assert cut == "<details>\n```\ncode line\n```\n\n_(truncated)_\n</details>"


# --- _split_owner_repo --------------------------------------------------------------------


def test_split_owner_repo_splits_a_full_name() -> None:
    assert posting._split_owner_repo("acme/widgets") == ("acme", "widgets")


def test_split_owner_repo_wraps_a_malformed_name_in_the_typed_error() -> None:
    with pytest.raises(GitHubPostingMalformedRequestError):
        posting._split_owner_repo("no-slash")
