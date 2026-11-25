from __future__ import annotations

from ingestion.commits import CommitInfo
from review.commit_slices import locate_at_head, plan_commit_slices


def _chain(*shas: str, root: str = "base") -> list[CommitInfo]:
    parents = [root, *shas[:-1]]
    return [CommitInfo(sha=s, parent_shas=(p,)) for s, p in zip(shas, parents, strict=True)]


def test_packs_consecutive_commits_up_to_budget() -> None:
    plan = plan_commit_slices(
        _chain("a", "b", "c", "d"),
        {"a": 40, "b": 40, "c": 40, "d": 10},
        max_bytes=100,
        max_slices=9,
    )
    assert [(s.base_sha, s.head_sha, s.shas, s.estimated_bytes) for s in plan.slices] == [
        ("base", "b", ("a", "b"), 80),
        ("b", "d", ("c", "d"), 50),
    ]
    assert plan.skipped_commits == () and plan.dropped_commits == ()


def test_oversize_commit_is_its_own_slice() -> None:
    plan = plan_commit_slices(
        _chain("a", "b", "c"), {"a": 10, "b": 500, "c": 10}, max_bytes=100, max_slices=9
    )
    assert [s.shas for s in plan.slices] == [("a",), ("b",), ("c",)]
    assert plan.slices[1].estimated_bytes == 500


def test_missing_size_counts_as_zero() -> None:
    plan = plan_commit_slices(_chain("a", "b"), {}, max_bytes=1, max_slices=9)
    assert [s.shas for s in plan.slices] == [("a", "b")]


def test_merge_and_root_commits_are_skipped_and_break_contiguity() -> None:
    commits = [
        CommitInfo(sha="r", parent_shas=()),
        CommitInfo(sha="a", parent_shas=("r",)),
        CommitInfo(sha="m", parent_shas=("a", "x")),
        CommitInfo(sha="b", parent_shas=("m",)),
    ]
    plan = plan_commit_slices(commits, {}, max_bytes=100, max_slices=9)
    assert plan.skipped_commits == ("r", "m")
    assert [(s.base_sha, s.shas) for s in plan.slices] == [("r", ("a",)), ("m", ("b",))]


def test_non_linear_parent_starts_new_slice() -> None:
    commits = [
        CommitInfo(sha="a", parent_shas=("base",)),
        CommitInfo(sha="b", parent_shas=("other",)),
    ]
    plan = plan_commit_slices(commits, {}, max_bytes=100, max_slices=9)
    assert [s.shas for s in plan.slices] == [("a",), ("b",)]


def test_max_slices_drops_later_commits_and_lists_them() -> None:
    plan = plan_commit_slices(
        _chain("a", "b", "c", "d"), dict.fromkeys("abcd", 60), max_bytes=100, max_slices=2
    )
    assert [s.shas for s in plan.slices] == [("a",), ("b",)]
    assert plan.dropped_commits == ("c", "d")


def test_zero_slices_and_empty_input() -> None:
    assert plan_commit_slices([], {}, max_bytes=10, max_slices=3).slices == ()
    plan = plan_commit_slices(_chain("a"), {}, max_bytes=10, max_slices=0)
    assert plan.slices == () and plan.dropped_commits == ("a",)


def test_planning_is_deterministic() -> None:
    commits = _chain("a", "b", "c")
    sizes = {"a": 5, "b": 90, "c": 5}
    assert plan_commit_slices(commits, sizes, max_bytes=50, max_slices=5) == plan_commit_slices(
        commits, sizes, max_bytes=50, max_slices=5
    )


_PATCH = (
    "@@ -1,2 +10,4 @@\n"
    " context_line_here()\n"
    "-removed_line_gone()\n"
    "+    value = compute_total(items)\n"
    "+    x = 1\n"
    "\\ No newline at end of file\n"
    " tail_context_line()\n"
)


def test_head_missing_means_gone() -> None:
    assert not locate_at_head(line=11, slice_patch=_PATCH, head_content=None).present


def test_file_level_cases_are_present_without_line() -> None:
    head = b"anything\n"
    for line, patch in ((None, _PATCH), (11, None), (99, _PATCH)):
        m = locate_at_head(line=line, slice_patch=patch, head_content=head)
        assert m.present and m.line is None


def test_trivial_line_is_file_level() -> None:
    m = locate_at_head(line=12, slice_patch=_PATCH, head_content=b"x = 1\n")
    assert m.present and m.line is None


def test_finds_moved_line_ignoring_indentation() -> None:
    head = b"a\n" * 49 + b"        value = compute_total(items)\n"
    m = locate_at_head(line=11, slice_patch=_PATCH, head_content=head)
    assert m.present and m.line == 50


def test_nearest_duplicate_wins_and_tie_prefers_earlier() -> None:
    text = b"value = compute_total(items)\n"
    # line 11 is the finding's line: lines 1 and 20 -> 20 is nearer
    head = text + b"filler\n" * 18 + text
    assert locate_at_head(line=11, slice_patch=_PATCH, head_content=head).line == 20
    # lines 1 and 21 are equally far (10): the earlier one wins
    head = text + b"filler\n" * 19 + text
    assert locate_at_head(line=11, slice_patch=_PATCH, head_content=head).line == 1


def test_line_removed_at_head_is_gone() -> None:
    m = locate_at_head(line=11, slice_patch=_PATCH, head_content=b"unrelated\ncode\n")
    assert not m.present and m.line is None


def test_non_utf8_head_does_not_raise() -> None:
    m = locate_at_head(
        line=11, slice_patch=_PATCH, head_content=b"\xff\xfe\nvalue = compute_total(items)\n"
    )
    assert m.present and m.line == 2


def test_locate_at_head_uses_git_line_numbers() -> None:
    patch = "@@ -1,1 +4,1 @@\n+    target_line = compute()"
    head = b"a\n\x0c\nb\n    target_line = compute()\n"
    got = locate_at_head(line=4, slice_patch=patch, head_content=head)
    assert got.present and got.line == 4
