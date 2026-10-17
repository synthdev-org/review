"""Unit tests for review.changed_lines (US-A3)."""

from __future__ import annotations

from findings.models import Finding
from review.changed_lines import changed_lines, filter_to_changed_lines

_PATCH = """@@ -1,3 +1,4 @@
 import os
-old = 1
+new = 1
+extra = 2
 keep = 3
@@ -10,2 +11,3 @@ def f():
 a = 1
+b = 2
 c = 3
\\ No newline at end of file
"""


def test_changed_lines_collects_added_and_modified_new_file_lines() -> None:
    assert changed_lines(_PATCH) == frozenset({2, 3, 12})


def test_changed_lines_no_patch_is_empty() -> None:
    assert changed_lines(None) == frozenset()
    assert changed_lines("") == frozenset()


def _finding(file: str, line: int | None) -> Finding:
    return Finding(file=file, line=line, severity="low", message="m", source="ruff")


def test_filter_keeps_changed_drops_untouched_unlined_and_patchless() -> None:
    findings = [
        _finding("a.py", 2),  # changed
        _finding("a.py", 1),  # pre-existing line
        _finding("a.py", None),  # no line
        _finding("big.py", 1),  # file has no patch
    ]
    kept, filtered = filter_to_changed_lines(findings, {"a.py": _PATCH, "big.py": None})
    assert [(f.file, f.line) for f in kept] == [("a.py", 2)]
    assert filtered == 3


def test_changed_lines_hunk_header_variants_and_multiple_hunks() -> None:
    patch = "@@ -0,0 +1 @@\n+only\n@@ -5 +7,2 @@\n-x\n+y\n+z\n"
    assert changed_lines(patch) == frozenset({1, 7, 8})


def test_changed_lines_ignores_content_before_first_hunk_and_deletions() -> None:
    patch = "diff --git a/f b/f\n+++ b/f\n@@ -1,3 +1,1 @@\n-a\n-b\n c\n"
    assert changed_lines(patch) == frozenset()


def test_changed_lines_added_line_that_looks_like_a_header_is_still_added() -> None:
    patch = "@@ -1 +1,2 @@\n a\n+@@ -9 +9 @@\n"
    assert changed_lines(patch) == frozenset({2})


def test_filter_with_no_findings_or_unknown_file() -> None:
    assert filter_to_changed_lines([], {}) == ([], 0)
    kept, filtered = filter_to_changed_lines([_finding("missing.py", 1)], {})
    assert (kept, filtered) == ([], 1)
