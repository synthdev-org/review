"""Unit tests for review.diff_batching (pure, no I/O)."""

from __future__ import annotations

from review.diff_batching import (
    HUB_MIN_DEGREE,
    IMPORT_WEIGHT,
    SEPARATOR_BYTES,
    paired_source,
    plan_batches,
)

MAX = 100


def _plan(paths: list[str], size: int | dict[str, int], links=None, *, chunks: int = 8):  # type: ignore[no-untyped-def]
    sizes = size if isinstance(size, dict) else dict.fromkeys(paths, size)
    return plan_batches(paths, sizes, links or {}, max_bytes=MAX, max_chunks=chunks)


def test_empty_diff() -> None:
    plan = _plan([], 0)
    assert plan.batches == () and plan.dropped == ()


def test_single_file_and_fits_is_one_identical_batch() -> None:
    paths = ["z.py", "a.py", "m/b.py"]
    assert _plan(["a.py"], 10).batches == (("a.py",),)
    # 3 * 32 + 2 separators = 98 <= 100: exactly fits, order kept.
    assert _plan(paths, 32).batches == (tuple(paths),)


def test_one_byte_over_splits() -> None:
    assert len(_plan(["a.py", "b.py", "c.py"], 33).batches) == 2  # 99 + 2 seps > 100


def test_oversized_single_file_gets_own_batch() -> None:
    plan = _plan(["a.py", "big.py", "c.py"], {"a.py": 10, "big.py": 500, "c.py": 10})
    assert plan.batches == (("a.py", "c.py"), ("big.py",))


def test_linked_files_share_a_batch() -> None:
    paths = ["a/x.py", "b/y.py", "a/z.py", "b/w.py"]
    links = {"a/x.py": {"b/y.py": IMPORT_WEIGHT}}
    plan = _plan(paths, 40, links)
    assert ("a/x.py", "b/y.py") in plan.batches


def test_links_fold_in_either_direction() -> None:
    paths = ["a/x.py", "b/y.py", "a/z.py", "b/w.py"]
    plan = _plan(paths, 40, {"b/y.py": {"a/x.py": 1.0}})
    assert ("a/x.py", "b/y.py") in plan.batches


def test_hub_does_not_pull_files_together() -> None:
    leaves = [f"d{i}/leaf{i}.py" for i in range(HUB_MIN_DEGREE)]
    paths = ["hub/core.py", *leaves]
    links = {leaf: {"hub/core.py": IMPORT_WEIGHT} for leaf in leaves}
    plan = _plan(paths, 30, links)  # all in one component would be 270 bytes
    assert all(len(b) <= 3 for b in plan.batches)
    assert sum(len(b) for b in plan.batches) == len(paths)


def test_oversize_component_splits_on_weakest_edge() -> None:
    paths = ["a/a.py", "a/b.py", "a/c.py", "a/d.py"]
    links = {"a/a.py": {"a/b.py": 5.0}, "a/b.py": {"a/c.py": 1.0}, "a/c.py": {"a/d.py": 5.0}}
    plan = _plan(paths, 45, links)  # one component = 188 bytes; pairs fit
    assert plan.batches == (("a/a.py", "a/b.py"), ("a/c.py", "a/d.py"))


def test_unlinked_files_group_by_directory() -> None:
    paths = ["a/1.py", "b/2.py", "a/3.py", "b/4.py"]
    plan = _plan(paths, 45)
    assert plan.batches == (("a/1.py", "a/3.py"), ("b/2.py", "b/4.py"))


def test_test_file_stays_with_source_even_across_other_links() -> None:
    paths = ["pkg/foo.py", "x/other.py", "pkg/test_foo.py", "y/more.py"]
    plan = _plan(paths, 45)
    assert ("pkg/foo.py", "pkg/test_foo.py") in plan.batches


def test_cap_drops_tail_batches_in_diff_order() -> None:
    paths = ["a.py", "b.py", "c.py"]
    plan = _plan(paths, 60, chunks=2)
    assert plan.batches == (("a.py",), ("b.py",))
    assert plan.dropped == ("c.py",)


def test_deterministic_and_covers_every_path_once() -> None:
    paths = [f"d{i % 3}/f{i}.py" for i in range(12)]
    links = {paths[0]: {paths[5]: 2.0}, paths[3]: {paths[7]: 2.0}}
    first = _plan(paths, 30, links, chunks=99)
    assert first == _plan(paths, 30, links, chunks=99)
    flat = [p for b in first.batches for p in b]
    assert sorted(flat) == sorted(paths)
    assert all(sum(30 + SEPARATOR_BYTES for _ in b) - SEPARATOR_BYTES <= MAX for b in first.batches)
    assert list(first.batches) == sorted(first.batches, key=lambda b: paths.index(b[0]))


def test_links_to_unknown_paths_are_ignored() -> None:
    plan = _plan(["a.py", "b.py", "c.py"], 40, {"a.py": {"ghost.py": 9.0, "a.py": 9.0}})
    assert sum(len(b) for b in plan.batches) == 3


def test_paired_source_conventions() -> None:
    paths = ["p/foo.py", "p/test_foo.py", "p/bar.py", "p/bar_test.py"]
    assert paired_source("p/test_foo.py", paths) == "p/foo.py"
    assert paired_source("p/bar_test.py", paths) == "p/bar.py"
    assert paired_source("p/foo.py", paths) is None
    assert paired_source("w/foo.test.ts", ["w/foo.ts"]) == "w/foo.ts"
    assert paired_source("w/foo.spec.tsx", ["w/foo.tsx"]) == "w/foo.tsx"
    assert paired_source("w/foo.test.tsx", ["w/foo.ts"]) == "w/foo.ts"
    assert paired_source("g/foo_test.go", ["g/foo.go"]) == "g/foo.go"


def test_paired_source_basename_fallback_and_ambiguity() -> None:
    assert paired_source("tests/test_foo.py", ["src/foo.py"]) == "src/foo.py"
    assert paired_source("tests/test_foo.py", ["a/foo.py", "b/foo.py"]) is None
    assert paired_source("tests/test_foo.py", ["src/other.py"]) is None


# ----- risk ordering -------------------------------------------------------


def test_risk_none_equals_old_plan_and_fits_short_circuit() -> None:
    paths = ["z/a.py", "a/b.py", "m/c.py", "a/d.py"]
    sizes = dict.fromkeys(paths, 40)
    old = plan_batches(paths, sizes, {}, max_bytes=MAX, max_chunks=8)
    assert old == plan_batches(paths, sizes, {}, max_bytes=MAX, max_chunks=8, risk=None)
    fits = plan_batches(
        paths, dict.fromkeys(paths, 10), {}, max_bytes=MAX, max_chunks=1, risk={"z/a.py": 1}
    )
    assert fits.batches == (tuple(paths),)


def test_risk_orders_batches_and_cap_drops_lowest() -> None:
    paths = ["a.md", "b.json", "c.py", "d.py"]
    risk = {"a.md": 3.0, "b.json": 1.0, "c.py": 10.0, "d.py": 10.0}
    plan = plan_batches(paths, dict.fromkeys(paths, 60), {}, max_bytes=MAX, max_chunks=2, risk=risk)
    kept = {p for b in plan.batches for p in b}
    assert {"c.py", "d.py"} <= kept
    assert "b.json" in plan.dropped
    assert list(plan.dropped) == [p for p in paths if p in plan.dropped]  # diff order


def test_risk_keeps_diff_order_inside_batch_and_is_deterministic() -> None:
    paths = ["a.py", "b.py", "c.py", "d.py"]
    risk = {"a.py": 1.0, "b.py": 5.0, "c.py": 5.0, "d.py": 1.0}
    sizes = dict.fromkeys(paths, 40)
    one = plan_batches(paths, sizes, {}, max_bytes=MAX, max_chunks=8, risk=risk)
    two = plan_batches(paths, sizes, {}, max_bytes=MAX, max_chunks=8, risk=dict(risk))
    assert one == two
    assert one.batches[0] == ("b.py", "c.py")  # riskiest batch first, diff order within
    assert all(list(b) == sorted(b, key=paths.index) for b in one.batches)


def test_risk_ties_break_by_first_index_and_missing_counts_zero() -> None:
    paths = ["a.py", "b.py", "c.py"]
    plan = plan_batches(
        paths, dict.fromkeys(paths, 60), {}, max_bytes=MAX, max_chunks=8, risk={"c.py": 1.0}
    )
    assert plan.batches[0] == ("c.py",)
    assert plan.batches[1:] == (("a.py",), ("b.py",))
    tied = plan_batches(paths, dict.fromkeys(paths, 60), {}, max_bytes=MAX, max_chunks=8, risk={})
    assert tied.batches == (("a.py",), ("b.py",), ("c.py",))


def test_risk_group_ranked_by_riskiest_member_keeps_linked_group_together() -> None:
    paths = ["a/x.py", "b/y.py", "c/z.py"]
    links = {"a/x.py": {"b/y.py": IMPORT_WEIGHT}}
    risk = {"a/x.py": 4.0, "b/y.py": 2.0, "c/z.py": 3.0}
    plan = plan_batches(
        paths, dict.fromkeys(paths, 40), links, max_bytes=MAX, max_chunks=1, risk=risk
    )
    assert plan.batches == (("a/x.py", "b/y.py"),) and plan.dropped == ("c/z.py",)


def test_one_big_risky_file_outranks_a_bin_of_many_low_risk_files() -> None:
    # US-X8: the cap drops docs before source, whatever the docs' summed risk.
    docs = [f"d{i}.md" for i in range(9)]
    paths = ["src/big.py", *docs]
    sizes = {"src/big.py": 90, **dict.fromkeys(docs, 20)}
    risk = {"src/big.py": 10.0, **dict.fromkeys(docs, 3.0)}  # docs sum to 27 > 10
    plan = plan_batches(paths, sizes, {}, max_bytes=MAX, max_chunks=1, risk=risk)
    assert plan.batches == (("src/big.py",),)
