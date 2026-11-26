"""Unit tests for review.diff_classify (pure, no I/O)."""

from __future__ import annotations

import pytest

from ingestion.diff import Diff, DiffFile
from review.diff_batching import plan_batches
from review.diff_classify import (
    ShrinkConfig,
    ShrinkNote,
    classify_path,
    is_move_only,
    render_shrink_note,
    risk_by_path,
    risk_score,
    shrink_diff,
)


def _f(path: str, patch: str | None = "@@ -1 +1 @@\n-a\n+b") -> DiffFile:
    return DiffFile(path=path, status="modified", patch=patch)


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("review/x.py", "source"),
        ("web/app.tsx", "source"),
        ("review/test_x.py", "test"),
        ("pkg/x_test.go", "test"),
        ("web/a.spec.ts", "test"),
        ("tests/helper.py", "test"),
        ("docs/notes/p.md", "docs"),
        ("README.rst", "docs"),
        ("uv.lock", "lockfile"),
        ("a/package-lock.json", "lockfile"),
        ("static/app.min.js", "generated"),
        ("vendor/lib/x.go", "generated"),
        ("testdata/cassettes/v1.json", "data"),
        ("tests/fixtures/a.yaml", "data"),
        ("x/__snapshots__/a.snap", "data"),
        ("logs/run.jsonl", "data"),
        ("golden/out.json", "data"),
        ("tests/fixtures/helper.py", "test"),
        ("Dockerfile", "other"),
        ("config/app.json", "other"),
    ],
)
def test_classify_path(path: str, kind: str) -> None:
    assert classify_path(path) == kind


def test_move_only() -> None:
    assert is_move_only("@@ -1,2 +1,2 @@\n-a\n-b\n+b\n+a")
    assert is_move_only("@@\n-  x = 1\n+x = 1")  # indentation-only
    assert not is_move_only("@@\n-a\n+b")
    assert not is_move_only("@@\n-a\n-a\n+a")
    assert not is_move_only("@@\n context")
    assert not is_move_only(None)
    assert not is_move_only("")


def test_risk_table_and_ordering() -> None:
    big = "@@ -1 +1 @@\n" + "\n".join(f"-a{i}\n+b{i}" for i in range(300))
    scores = {
        k: risk_score(_f(p, big))
        for k, p in {
            "source": "a.py",
            "test": "test_a.py",
            "other": "Dockerfile",
            "docs": "a.md",
            "data": "x/fixtures/a.json",
            "lock": "uv.lock",
        }.items()
    }
    assert scores == {
        "source": 11.0,
        "test": 7.0,
        "other": 6.0,
        "docs": 4.0,
        "data": 2.0,
        "lock": 1.0,
    }


def test_risk_move_and_size_scaling() -> None:
    assert risk_score(_f("a.py", "@@\n-a\n+a")) == pytest.approx(10 * 0.3 + 2 / 200)
    assert risk_score(_f("a.py", "@@\n-a\n+b")) == pytest.approx(10 + 2 / 200)
    assert risk_score(_f("a.py", None)) == 10.0
    assert risk_score(_f("a.py", "@@\n-a\n+b")) > risk_score(_f("a.py", "@@\n-a\n+a"))


def test_risk_large_config_json_is_data() -> None:
    patch = "@@\n" + "+x\n" * 5000
    assert risk_score(_f("config/big.json", patch)) < 3
    assert risk_score(_f("package.json", patch)) > 5


def test_risk_by_path_is_deterministic() -> None:
    d = Diff(files=(_f("a.py"), _f("b.md")))
    first, second = risk_by_path(d), risk_by_path(d)
    assert first == second
    assert list(risk_by_path(d)) == ["a.py", "b.md"]


# ----- shrink -------------------------------------------------------------


def _hunk(n_del: int, n_add: int = 0, ctx: int = 0) -> str:
    return f"@@ -1,{n_del + ctx} +1,{n_add + ctx} @@\n" + "\n".join(
        [f"-d{i}" for i in range(n_del)] + [f"+a{i}" for i in range(n_add)] + ["c"] * ctx
    )


def test_nothing_to_shrink_is_same_object() -> None:
    d = Diff(files=(_f("a.py"), _f("b.md"), _f("c.json", None)))
    r = shrink_diff(d)
    assert r.diff is d and r.notes == ()


def test_data_stub_threshold_edges() -> None:
    at = "@@\n" + "x" * (8000 - 3)
    assert len(at.encode()) == 8000
    d = Diff(files=(_f("testdata/c.json", at),))
    assert shrink_diff(d).diff is d  # exactly at the threshold: kept
    over = _f("testdata/c.json", at + "y")
    r = shrink_diff(Diff(files=(over,)))
    assert r.diff.files[0].patch == "(data file: +0 -0 lines, 8001 bytes)"
    assert r.notes == (
        ShrinkNote(path="testdata/c.json", reason="data", original_bytes=8001, kept_bytes=36),
    )


def test_data_counts_lines_and_outside_segment_config_exempt() -> None:
    patch = "@@\n" + "+x" * 10 + "\n+y\n-z\n" + "q" * 8000
    r = shrink_diff(Diff(files=(_f("logs/a.jsonl", patch), _f("data/big.json", patch))))
    assert [n.path for n in r.notes] == ["logs/a.jsonl", "data/big.json"]
    assert r.diff.files[0].patch is not None and "+2 -1 lines" in r.diff.files[0].patch
    cfg = shrink_diff(Diff(files=(_f("package.json", patch), _f("x.schema.json", patch))))
    assert cfg.notes == ()


def test_small_data_untouched_and_no_patch_untouched() -> None:
    d = Diff(files=(_f("testdata/a.json", "@@\n+1"), _f("testdata/b.json", None)))
    assert shrink_diff(d).diff is d


def test_deletion_collapse_edges() -> None:
    p19, p20 = _hunk(19), _hunk(20)
    r = shrink_diff(Diff(files=(_f("a.py", p19), _f("b.py", p20))))
    assert r.diff.files[0].patch == p19
    assert r.diff.files[1].patch == "@@ -1,20 +1,0 @@\n(deleted 20 lines; not shown)"
    assert [(n.path, n.reason) for n in r.notes] == [("b.py", "deletion")]


def test_mixed_hunk_not_collapsed_and_per_hunk() -> None:
    mixed = _hunk(30, n_add=1)
    ctx = _hunk(30, ctx=1)
    two = _hunk(25) + "\n" + _hunk(2, n_add=1)
    r = shrink_diff(Diff(files=(_f("m.py", mixed), _f("c.py", ctx), _f("t.py", two))))
    assert r.diff.files[0].patch == mixed and r.diff.files[1].patch == ctx
    out = r.diff.files[2].patch or ""
    assert "(deleted 25 lines; not shown)" in out and "+a0" in out and "-d1" in out


def test_deletion_with_no_newline_marker_collapses() -> None:
    p = _hunk(20) + "\n\\ No newline at end of file"
    r = shrink_diff(Diff(files=(_f("a.py", p),)))
    assert r.notes[0].reason == "deletion"


def test_prose_trim_edges_and_line_boundary() -> None:
    line = "+" + "w" * 99  # 100 bytes with newline
    body = "@@ -0,0 +1,200 @@\n" + "\n".join([line] * 200)
    big = _f("docs/x.md", body)
    r = shrink_diff(Diff(files=(big,)))
    note = r.notes[0]
    out = r.diff.files[0].patch or ""
    assert note.reason == "prose" and note.original_bytes == len(body.encode())
    assert out.endswith("bytes not shown)")
    kept = out.rsplit("\n", 1)[0]
    assert len(kept.encode()) <= 4000 and all(
        ln == line or ln.startswith("@@") for ln in kept.split("\n")
    )
    assert note.kept_bytes == len(out.encode())
    small = _f("docs/y.md", "@@\n" + "+" + "w" * 11_990)
    assert shrink_diff(Diff(files=(small,))).notes == ()


def test_prose_keep_policy_leaves_prose() -> None:
    body = "@@\n" + "+" + "w" * 20_000
    d = Diff(files=(_f("spec.md", body), _f("testdata/c.json", body)))
    r = shrink_diff(d, ShrinkConfig(prose_policy="keep"))
    assert r.diff.files[0].patch == body
    assert [n.reason for n in r.notes] == ["data"]


def test_whole_file_prose_deletion_collapses_not_trims() -> None:
    r = shrink_diff(Diff(files=(_f("docs/notes/p.md", _hunk(5000)),)))
    assert r.notes[0].reason == "deletion" and r.notes[0].kept_bytes < 100


def test_prose_trim_multibyte_safe() -> None:
    body = "@@\n" + "\n".join(["+é" * 100] * 200)
    r = shrink_diff(Diff(files=(_f("a.md", body),)))
    assert r.notes[0].reason == "prose"


def test_shrink_preserves_order_and_other_fields() -> None:
    a = DiffFile(path="a.py", status="added", mode="100644", blob_sha="s", blob_size_bytes=3)
    d = Diff(files=(a, _f("testdata/c.json", "@@\n" + "x" * 9000), _f("z.py")))
    r = shrink_diff(d)
    assert [f.path for f in r.diff.files] == ["a.py", "testdata/c.json", "z.py"]
    assert r.diff.files[0] is a
    assert r.diff.files[1].status == "modified"
    assert shrink_diff(d) == r  # deterministic


def test_render_note() -> None:
    n = ShrinkNote(path="a.json", reason="data", original_bytes=9, kept_bytes=1)
    assert render_shrink_note(n) == "a.json (data file stubbed; not reviewed in detail)"
    for reason in ("deletion", "prose"):
        text = render_shrink_note(
            ShrinkNote(path="p", reason=reason, original_bytes=1, kept_bytes=0)
        )
        assert text.startswith("p (") and text.endswith("; not reviewed in detail)")


# ----- integration of risk with batching (no step wiring) -----------------


def test_cap_drops_lowest_risk_first_and_lists_them() -> None:
    files = {
        "data/x/fixtures/c.json": "@@\n+x",
        "docs/a.md": "@@\n+x",
        "tests/test_a.py": "@@\n+x",
        "src/a.py": "@@\n-a\n+b",
        "src/b.py": "@@\n-a\n+b",
    }
    diff = Diff(files=tuple(_f(p, patch) for p, patch in files.items()))
    paths = [f.path for f in diff.files]
    sizes = dict.fromkeys(paths, 60)
    plan = plan_batches(paths, sizes, {}, max_bytes=130, max_chunks=2, risk=risk_by_path(diff))
    kept = {p for b in plan.batches for p in b}
    assert kept == {"src/a.py", "src/b.py", "tests/test_a.py", "docs/a.md"} - set(plan.dropped)
    scores = risk_by_path(diff)
    assert min(scores[p] for p in kept) >= max(scores[p] for p in plan.dropped)
    assert "data/x/fixtures/c.json" in plan.dropped
    assert {"src/a.py", "src/b.py"} <= kept


def test_dashed_comment_lines_are_counted_not_taken_for_headers() -> None:
    # A deleted SQL/Lua comment "-- x" renders as "--- x" in a headerless patch.
    assert is_move_only("@@ -1,2 +1,2 @@\n--- a comment\n+-- a comment\n")
    assert not is_move_only("@@ -1 +1 @@\n--- a comment\n++++ b\n")
    stub = (
        shrink_diff(
            Diff(files=(_f("db/data.csv", "@@ -1 +1 @@\n--- c\n+" + "x" * 9000),)),
        )
        .diff.files[0]
        .patch
    )
    assert stub is not None and "+1 -1 lines" in stub


def test_pure_rename_ranks_below_tests_and_logic() -> None:
    rename = DiffFile(path="a/new.py", status="renamed", patch=None)
    logic = DiffFile(
        path="tests/test_x.py",
        status="modified",
        patch="@@ -1 +1 @@\n" + "".join(f"-a{i}\n+b{i}\n" for i in range(100)),
    )
    assert risk_score(rename) < risk_score(logic)
