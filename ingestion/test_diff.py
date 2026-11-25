"""Unit tests for ``ingestion.diff.commentable_lines`` — pure logic deciding
which new-side lines GitHub accepts an inline comment on (a wrong answer makes
GitHub 422 the whole review, losing every finding)."""

from __future__ import annotations

import pytest

from ingestion.diff import Diff, DiffFile, commentable_lines


def _lines(patch: str | None) -> frozenset[int]:
    diff = Diff(files=(DiffFile(path="f.py", status="modified", patch=patch),))
    return commentable_lines(diff)["f.py"]


def test_added_and_context_lines_are_commentable() -> None:
    assert _lines("@@ -1,2 +1,3 @@\n a\n+b\n c") == {1, 2, 3}


def test_removed_lines_do_not_advance_the_new_side_counter() -> None:
    # new-side: ctx=1, (removed), added=2
    assert _lines("@@ -1,3 +1,2 @@\n ctx\n-gone\n+new") == {1, 2}


def test_multiple_hunks_reset_the_counter_and_leave_the_gap_uncommentable() -> None:
    patch = "@@ -1,1 +1,1 @@\n a\n@@ -10,2 +20,2 @@\n x\n+y"
    assert _lines(patch) == {1, 20, 21}


def test_no_newline_marker_is_not_a_line() -> None:
    assert _lines("@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new") == {1}


def test_hunk_header_without_counts_is_parsed() -> None:
    assert _lines("@@ -5 +7 @@\n+only") == {7}


@pytest.mark.parametrize("patch", [None, ""])
def test_missing_or_empty_patch_has_no_commentable_lines(patch: str | None) -> None:
    assert _lines(patch) == frozenset()


def test_pure_deletion_hunk_has_no_commentable_lines() -> None:
    assert _lines("@@ -1,2 +0,0 @@\n-a\n-b") == frozenset()


def test_lines_before_the_first_hunk_header_are_ignored() -> None:
    assert _lines("garbage\n@@ -1 +1 @@\n+a") == {1}


def test_every_file_gets_an_entry() -> None:
    diff = Diff(
        files=(
            DiffFile(path="a.py", status="added", patch="@@ -0,0 +1 @@\n+x"),
            DiffFile(path="b.bin", status="modified", patch=None),
        )
    )
    assert commentable_lines(diff) == {"a.py": frozenset({1}), "b.bin": frozenset()}
