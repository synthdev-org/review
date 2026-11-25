"""Unit + integration-level tests for ``review.diff_expand`` (US-X3)."""

from __future__ import annotations

import pytest

from ingestion.diff import Diff, DiffFile
from review.diff_expand import ExpandConfig, expand_diff, expand_patch, expandable_paths

HEAD = "\n".join(
    [
        "import os",  # 1
        "",  # 2
        "def alpha(x):",  # 3
        "    a = 1",  # 4
        "    b = 2",  # 5
        "    c = x + a",  # 6
        "    d = c + b",  # 7
        "    return d",  # 8
        "",  # 9
        "def beta(y):",  # 10
        "    e = y",  # 11
        "    f = e * 2",  # 12
        "    g = f + 1",  # 13
        "    return g",  # 14
        "",
    ]
)
HEAD_B = HEAD.encode()


def _fixture_patch() -> str:
    # new lines 5..8: b = 2 / c = x + a (added) / d / return d
    return "@@ -5,3 +5,4 @@\n     b = 2\n+    c = x + a\n     d = c + b\n     return d"


def test_expands_to_enclosing_symbol_and_rewrites_header() -> None:
    out = expand_patch("m.py", _fixture_patch(), HEAD_B)
    lines = out.split("\n")
    # symbol alpha starts line 3; hunk new_start 5 -> 2 before lines (3,4)
    assert lines[0] == "@@ -3,5 +3,6 @@ alpha"
    assert lines[1] == " def alpha(x):"
    assert lines[2] == "     a = 1"
    assert "+    c = x + a" in lines
    assert out != _fixture_patch()


def test_existing_section_text_is_kept() -> None:
    patch = (
        "@@ -5,3 +5,4 @@ def alpha(x):\n     b = 2\n+    c = x + a\n     d = c + b\n     return d"
    )
    assert expand_patch("m.py", patch, HEAD_B).startswith("@@ -3,5 +3,6 @@ def alpha(x):")


def test_context_after_stops_at_symbol_end() -> None:
    patch = "@@ -3,2 +3,3 @@\n def alpha(x):\n+    z = 0\n     a = 1"
    out = expand_patch("m.py", patch, HEAD_B)
    # alpha ends at line 8 and context_after=20 must not leak into beta
    assert "def beta" not in out
    assert out.rstrip("\n").endswith("     return d")


def test_context_limits_are_honoured() -> None:
    out = expand_patch(
        "m.py", _fixture_patch(), HEAD_B, ExpandConfig(context_before=1, context_after=0)
    )
    assert out.startswith("@@ -4,4 +4,5 @@ alpha")


def test_clamps_across_neighbouring_hunks_no_line_twice() -> None:
    head = "\n".join(["def f():"] + [f"    x{i} = {i}" for i in range(2, 21)]) + "\n"
    patch = (
        "@@ -3,1 +3,2 @@\n     x2 = 2\n+    NEW1 = 1\n@@ -9,1 +10,2 @@\n     x9 = 9\n+    NEW2 = 2"
    )
    # make the second hunk's first new line match head line 10
    patch = patch.replace("@@ -9,1 +10,2 @@\n     x9 = 9", "@@ -9,1 +10,2 @@\n     x10 = 10")
    patch = patch.replace("@@ -3,1 +3,2 @@\n     x2 = 2", "@@ -3,1 +3,2 @@\n     x3 = 3")
    out = expand_patch("m.py", patch, head.encode())
    body = [ln for ln in out.split("\n") if not ln.startswith(("@@", "+"))]
    assert len(body) == len(set(body))  # no head line rendered twice
    assert out.count("@@ -") == 2


def test_stale_head_content_returns_original() -> None:
    stale = HEAD.replace("b = 2", "b = 999").encode()
    assert expand_patch("m.py", _fixture_patch(), stale) == _fixture_patch()


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("notes.txt", HEAD_B),  # unsupported language
        ("m.py", b"\xff\xfe\x00"),  # not UTF-8
        ("m.py", b""),  # head empty -> mismatch
    ],
)
def test_degradation_returns_original(path: str, content: bytes) -> None:
    assert expand_patch(path, _fixture_patch(), content) == _fixture_patch()


@pytest.mark.parametrize(
    "patch",
    ["", "garbage", "@@ -1,1 +1,1 @@\nno-prefix", "@@ -5,3 +5,9 @@\n     b = 2"],
)
def test_malformed_patch_returns_original(patch: str) -> None:
    assert expand_patch("m.py", patch, HEAD_B) == patch


def test_pure_addition_hunk_old_start_is_adjusted() -> None:
    patch = "@@ -5,0 +6,1 @@\n+    c = x + a"
    out = expand_patch("m.py", patch, HEAD_B, ExpandConfig(context_before=2, context_after=1))
    assert out.startswith("@@ -4,3 +4,4 @@ alpha")


def test_deletion_only_hunk_is_left_alone() -> None:
    patch = "@@ -5,1 +4,0 @@\n-    gone = 1"
    assert expand_patch("m.py", patch, HEAD_B) == patch


def test_hunk_outside_any_symbol_is_left_alone() -> None:
    patch = "@@ -1,1 +1,2 @@\n import os\n+import sys"
    assert expand_patch("m.py", patch, HEAD_B) == patch


def test_size_cap_returns_original() -> None:
    assert expand_patch("m.py", _fixture_patch(), HEAD_B, ExpandConfig(max_bytes_per_file=10)) == (
        _fixture_patch()
    )


def test_trailing_newline_and_no_newline_marker_preserved() -> None:
    patch = _fixture_patch() + "\n"
    out = expand_patch("m.py", patch, HEAD_B)
    assert out.endswith("     return d\n")
    marker = "@@ -5,1 +5,1 @@\n-    b = 2\n+    b = 2\n\\ No newline at end of file"
    assert "\\ No newline at end of file" in expand_patch(
        "m.py", marker.replace("-    b = 2\n", "-    old\n"), HEAD_B
    )


def test_deterministic() -> None:
    first = expand_patch("m.py", _fixture_patch(), HEAD_B)
    second = expand_patch("m.py", _fixture_patch(), HEAD_B)
    assert first == second


def _diff(*files: DiffFile) -> Diff:
    return Diff(files=files)


def test_expandable_paths_filters_status_patch_language() -> None:
    p = _fixture_patch()
    diff = _diff(
        DiffFile(path="a.py", status="modified", patch=p),
        DiffFile(path="b.py", status="renamed", patch=p),
        DiffFile(path="c.py", status="added", patch=p),
        DiffFile(path="d.py", status="removed", patch=p),
        DiffFile(path="e.py", status="modified", patch=None),
        DiffFile(path="f.txt", status="modified", patch=p),
    )
    assert expandable_paths(diff) == ["a.py", "b.py"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expand_diff_end_to_end_keeps_untouched_files_and_identity() -> None:
    p = _fixture_patch()
    added = DiffFile(path="new.py", status="added", patch="@@ -0,0 +1,1 @@\n+x")
    diff = _diff(DiffFile(path="a.py", status="modified", patch=p), added)
    out = expand_diff(diff, {"a.py": HEAD_B, "new.py": b"x\n"})
    assert out is not diff
    assert out.files[0].patch != p and out.files[1] is added
    # nothing expandable / no contents / stale -> the very same object
    assert expand_diff(diff, {}) is diff
    assert expand_diff(diff, {"a.py": b"zzz"}) is diff


def test_change_near_top_of_a_symbol_expands_that_symbol() -> None:
    head = "\n".join(
        ["def a():", "    return 1", "", "", "def b(q):", "    z = q * 2", "    y = z"]
        + [f"    w{i} = y" for i in range(8)]
        + ["    return y", ""]
    )
    # git -U3: the hunk starts at the blank line 3, before def b (line 5); change is line 6.
    patch = "@@ -3,6 +3,6 @@\n \n \n def b(q):\n-    z = q\n+    z = q * 2\n     y = z\n     w0 = y"
    out = expand_patch("m.py", patch, head.encode(), ExpandConfig(context_after=10))
    assert out != patch and "     return y" in out
