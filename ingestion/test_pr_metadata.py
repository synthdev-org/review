"""
Unit tests for ``ingestion.pr_metadata`` (product-knowledge Phase 2).

Covers US-PK2 capture and US-PK20 fork + prefix gates. Pure, no I/O.
"""

from __future__ import annotations

import pytest

from ingestion.pr_metadata import (
    HEAD_REF_MAX_CHARS,
    MAX_ISSUE_KEYS,
    TITLE_MAX_CHARS,
    extract_issue_keys,
    filter_issue_keys,
    is_fork_pr,
    normalize_head_ref,
    normalize_title,
)


def test_normalize_title_collapses_whitespace_and_caps() -> None:
    assert normalize_title("  a \n\t b  ") == "a b"
    assert normalize_title("x" * 1000) == "x" * TITLE_MAX_CHARS
    assert normalize_title("   ") is None
    assert normalize_title(None) is None


def test_normalize_head_ref_caps_and_blanks() -> None:
    assert normalize_head_ref(" feat/x ") == "feat/x"
    assert normalize_head_ref("y" * 999) == "y" * HEAD_REF_MAX_CHARS
    assert normalize_head_ref("  ") is None
    assert normalize_head_ref(None) is None


def test_extracts_keys_from_title_branch_and_body_in_order() -> None:
    keys = extract_issue_keys("Fix PROJ-9 cache", "bugfix/ABC-3-cache", "Closes #41")
    assert keys == ("PROJ-9", "ABC-3", "#41")


def test_keys_are_deduplicated_and_order_stable() -> None:
    keys = extract_issue_keys("PROJ-9 and #1 and PROJ-9", "PROJ-9", "#1 then PROJ-2")
    assert keys == ("PROJ-9", "#1", "PROJ-2")


def test_keys_capped_at_ten() -> None:
    title = " ".join(f"K{i:02d}-{i}" for i in range(30))
    keys = extract_issue_keys(title, None, None)
    assert len(keys) == MAX_ISSUE_KEYS
    assert keys[0] == "K00-0"


def test_non_keys_are_not_matched() -> None:
    assert extract_issue_keys("sha-256 utf-8 x-1 a1b2c3-4 url/#frag&#39;", None, None) == ()


def test_body_text_never_appears_in_output() -> None:
    keys = extract_issue_keys(None, None, "private paragraph with PROJ-5 inside")
    assert keys == ("PROJ-5",)
    assert all("private" not in key for key in keys)


def test_all_none_gives_empty() -> None:
    assert extract_issue_keys(None, None, None) == ()


def test_filter_issue_keys_by_prefix() -> None:
    keys = ("PROJ-9", "HR-77", "#41", "PROJX-1")
    assert filter_issue_keys(keys, ["PROJ"]) == ("PROJ-9",)
    assert filter_issue_keys(keys, ["PROJ-", "HR"]) == ("PROJ-9", "HR-77")
    assert filter_issue_keys(keys, []) == keys


@pytest.mark.parametrize(
    ("head", "fork", "base", "expected"),
    [
        ("acme/widgets", False, "acme/widgets", False),
        ("acme/widgets", None, "ACME/Widgets", False),
        ("mallory/widgets", False, "acme/widgets", True),
        ("acme/widgets", True, "acme/widgets", True),
        (None, None, "acme/widgets", True),
        ("acme/widgets", False, None, True),
    ],
)
def test_is_fork_pr_matrix_fails_closed(
    head: str | None, fork: bool | None, base: str | None, expected: bool
) -> None:
    assert (
        is_fork_pr(head_repo_full_name=head, head_repo_fork=fork, base_repo_full_name=base)
        is expected
    )
