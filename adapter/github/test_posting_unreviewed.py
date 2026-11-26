"""Unit tests for the "Not reviewed" section of the review body (US-B6)."""

from __future__ import annotations

from adapter.github.posting import _review_body


def test_body_is_unchanged_without_unreviewed_files() -> None:
    assert _review_body([], 0) == _review_body([], 0, None, ())
    assert "Not reviewed" not in _review_body([], 0)


def test_body_lists_unreviewed_files_escaped() -> None:
    body = _review_body([], 0, None, ("a/b_c.py", "[x](http://evil)"))

    assert "Not reviewed" in body
    assert "a/b" in body
    assert "[x](http://evil)" not in body  # escaped, not a live link


def test_body_caps_the_list_at_fifty_paths() -> None:
    body = _review_body([], 0, None, tuple(f"f{i}.py" for i in range(60)))

    assert "f49" in body and "f50.py" not in body
    assert "and 10 more" in body
