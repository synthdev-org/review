"""Tests for review.diff_prompt.render_diff/truncate_diff_text, moved here
unchanged from ``review/test_review_generation_step.py``'s own equivalent
boundary cases (per ADR-0005); these pure functions' sole home now that both
``review/intent_summary.py`` and ``review/review_generation.py`` share them.

Pure-function unit tests, no I/O, per testing.md's Unit tier.
"""

from __future__ import annotations

from ingestion.diff import Diff, DiffFile
from review.diff_prompt import render_diff, truncate_diff_text


def _diff_of_patch_byte_length(length: int) -> Diff:
    """A single-file ``Diff`` whose patch text is ``length`` repeated ``x``
    bytes."""
    return Diff(files=(DiffFile(path="app.py", status="modified", patch="x" * length),))


# --- render_diff -------------------------------------------------------------


def test_render_diff_renders_multiple_files_in_order() -> None:
    diff = Diff(
        files=(
            DiffFile(path="a.py", status="modified", patch="@@ -1 +1 @@\n-old\n+new\n"),
            DiffFile(path="b.py", status="added", patch="@@ -0,0 +1 @@\n+new file\n"),
        )
    )

    rendered = render_diff(diff)

    a_index = rendered.index("--- a.py (modified)")
    b_index = rendered.index("--- b.py (added)")
    assert a_index < b_index
    assert "-old" in rendered
    assert "+new" in rendered
    assert "+new file" in rendered


def test_render_diff_file_with_no_patch_renders_placeholder() -> None:
    diff = Diff(files=(DiffFile(path="binary.png", status="modified", patch=None),))

    rendered = render_diff(diff)

    assert "--- binary.png (modified)" in rendered
    assert "(no patch available)" in rendered


# --- truncate_diff_text boundary cases ---------------------------------------


def test_diff_exactly_at_ceiling_is_not_truncated() -> None:
    diff = _diff_of_patch_byte_length(1_000)
    rendered = render_diff(diff)
    rendered_bytes = len(rendered.encode("utf-8"))

    truncated_text, original_bytes, final_bytes = truncate_diff_text(rendered, rendered_bytes)

    assert truncated_text == rendered
    assert original_bytes == rendered_bytes
    assert final_bytes == rendered_bytes


def test_diff_one_byte_over_ceiling_is_truncated_with_correct_byte_counts() -> None:
    diff = _diff_of_patch_byte_length(1_000)
    rendered = render_diff(diff)
    rendered_bytes = len(rendered.encode("utf-8"))

    truncated_text, original_bytes, final_bytes = truncate_diff_text(rendered, rendered_bytes - 1)

    assert truncated_text != rendered
    assert original_bytes == rendered_bytes
    assert final_bytes == rendered_bytes - 1
    assert len(truncated_text.encode("utf-8")) == final_bytes


def test_diff_comfortably_under_ceiling_is_not_truncated() -> None:
    diff = _diff_of_patch_byte_length(10)
    rendered = render_diff(diff)

    truncated_text, original_bytes, final_bytes = truncate_diff_text(rendered, 1_000)

    assert truncated_text == rendered
    assert original_bytes == final_bytes
    assert original_bytes < 1_000
