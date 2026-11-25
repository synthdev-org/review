"""Unit tests for review.verification_context (US-X1 window selection, caps,
secret-path exclusion; US-X2 degradation to no entry). One integration-marked
test chains the three public functions over a real tree-sitter parse."""

from __future__ import annotations

import pytest

from findings.models import Finding
from review.diff_prompt import INERT_REFERENCE_DATA_FRAMING
from review.verification_context import (
    FileContextConfig,
    build_file_contexts,
    finding_context_paths,
    render_file_context_section,
)


def _f(path: str, line: int | None = 1) -> Finding:
    return Finding(file=path, line=line, severity="high", message="m", source="llm")


def _py(n: int) -> bytes:
    return "".join(f"x{i} = {i}\n" for i in range(1, n + 1)).encode()


def test_paths_unique_in_first_appearance_order() -> None:
    got = finding_context_paths([_f("b.py"), _f("a.py"), _f("b.py", 9)])
    assert got == ("b.py", "a.py")


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "cfg/.env.local",
        "k/server.pem",
        "k/tls.KEY",
        "home/id_rsa",
        "home/id_rsa.pub",
        "review/testdata/eval/v1.json",
        "tests/fixtures/a.py",
        "x/cassettes/c.yaml",
        "data/rows.jsonl",
        "a/b.csv",
        "t/x.snap",
    ],
)
def test_excluded_paths(path: str) -> None:
    assert finding_context_paths([_f(path)]) == ()
    assert build_file_contexts([_f(path)], {path: b"secret\n"}) == {}


def test_regular_json_and_empty_inputs() -> None:
    assert finding_context_paths([_f("package.json")]) == ("package.json",)
    assert finding_context_paths([]) == ()
    assert build_file_contexts([], {"a.py": b"x\n"}) == {}


def test_small_file_included_whole_with_numbers() -> None:
    out = build_file_contexts([_f("a.py", 2)], {"a.py": b"one\ntwo\r\nthree\n"})
    assert out == {"a.py": "   1| one\n   2| two\n   3| three"}


def test_head_file_absent_or_not_cited_is_skipped() -> None:
    out = build_file_contexts([_f("a.py")], {"b.py": b"x\n"})
    assert out == {}


def test_one_entry_for_several_findings() -> None:
    out = build_file_contexts([_f("a.py", 1), _f("a.py", 2)], {"a.py": b"x\ny\n"})
    assert list(out) == ["a.py"]


@pytest.mark.parametrize("raw", [b"a\x00b\n", b"\xff\xfe\xfa", b""])
def test_binary_undecodable_or_empty_skipped(raw: bytes) -> None:
    assert build_file_contexts([_f("a.py")], {"a.py": raw}) == {}


def test_large_file_gets_window_with_gaps() -> None:
    cfg = FileContextConfig(max_bytes_per_file=100, surrounding_lines=2)
    out = build_file_contexts([_f("n.txt", 50)], {"n.txt": _py(100)}, cfg)["n.txt"]
    lines = out.split("\n")
    assert lines[0] == "   ..."
    assert lines[-1] == "   ..."
    assert [ln[:4].strip() for ln in lines[1:-1]] == ["48", "49", "50", "51", "52"]
    assert "x50 = 50" in out
    assert "x1 =" not in out


def test_distant_findings_get_separate_windows_adjacent_merge() -> None:
    cfg = FileContextConfig(max_bytes_per_file=1000, surrounding_lines=1)
    out = build_file_contexts(
        [_f("n.txt", 10), _f("n.txt", 12), _f("n.txt", 80)], {"n.txt": _py(1000)}, cfg
    )["n.txt"]
    numbered = [ln for ln in out.split("\n") if ln != "   ..."]
    assert [int(ln.split("|")[0]) for ln in numbered] == [9, 10, 11, 12, 13, 79, 80, 81]
    assert out.count("   ...") == 3


def test_line_none_gives_file_head_and_out_of_range_clamped() -> None:
    cfg = FileContextConfig(max_bytes_per_file=1000, surrounding_lines=2)
    head = build_file_contexts([_f("n.txt", None)], {"n.txt": _py(1000)}, cfg)["n.txt"]
    assert head.startswith("   1| x1 = 1") and "   5| x5 = 5" in head and "   6|" not in head
    far = build_file_contexts([_f("n.txt", 9999)], {"n.txt": _py(1000)}, cfg)["n.txt"]
    assert "1000| x1000 = 1000" in far
    low = build_file_contexts([_f("n.txt", -3)], {"n.txt": _py(1000)}, cfg)["n.txt"]
    assert low.startswith("   1| x1")


PY = (
    b"import os\n\n\n"
    b"def alpha():\n    a = 1\n    b = 2\n    c = 3\n    return a\n\n\n"
    b"def beta():\n    d = 4\n    e = 5\n    f = 6\n    g = 7\n    return d\n"
)


def test_enclosing_symbol_included_beyond_surrounding() -> None:
    cfg = FileContextConfig(max_bytes_per_file=134, surrounding_lines=0)
    out = build_file_contexts([_f("m.py", 13)], {"m.py": PY}, cfg)["m.py"]
    assert "def beta():" in out and "return d" in out
    assert "def alpha" not in out


def test_oversized_symbol_falls_back_to_surrounding_lines() -> None:
    cfg = FileContextConfig(max_bytes_per_file=120, surrounding_lines=1)
    out = build_file_contexts([_f("m.py", 13)], {"m.py": PY}, cfg)["m.py"]
    assert len(out.encode()) <= 120
    assert "e = 5" in out


def test_unsupported_language_and_syntax_garbage_use_plain_window() -> None:
    cfg = FileContextConfig(max_bytes_per_file=60, surrounding_lines=1)
    for path in ("notes.txt", "broken.py"):
        out = build_file_contexts(
            [_f(path, 30)], {path: _py(60) if path == "notes.txt" else b"def (((\n" * 20}, cfg
        )
        assert path in out and "..." in out[path]


def test_per_file_cap_truncates_even_a_single_window() -> None:
    cfg = FileContextConfig(max_bytes_per_file=60, surrounding_lines=40)
    out = build_file_contexts([_f("n.txt", 50)], {"n.txt": _py(200)}, cfg)["n.txt"]
    assert len(out.encode()) <= 60 + len("   ...")
    assert out.endswith("   ...")


def test_total_cap_stops_adding_files() -> None:
    cfg = FileContextConfig(max_total_bytes=60)
    files = {"a.py": b"a = 1\n" * 4, "b.py": b"b = 1\n" * 8, "c.py": b"c\n"}
    out = build_file_contexts([_f("a.py"), _f("b.py"), _f("c.py")], files, cfg)
    assert list(out) == ["a.py"]


def test_deterministic() -> None:
    fs = [_f("m.py", 13), _f("a.py", 1)]
    files = {"m.py": PY, "a.py": b"x\n"}
    cfg = FileContextConfig(max_bytes_per_file=100, surrounding_lines=1)
    assert build_file_contexts(fs, files, cfg) == build_file_contexts(fs, files, cfg)


def test_section_empty_and_framed() -> None:
    assert render_file_context_section({}) == ""
    section = render_file_context_section({"a.py": "   1| x", "b.py": "   1| y"})
    assert section.startswith("\n\nCurrent (head) version of the files")
    assert INERT_REFERENCE_DATA_FRAMING in section
    assert (
        "A finding is invalid if this code refutes it:\n--- a.py\n   1| x\n--- b.py\n   1| y"
        in section
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_module_level_pytestmark_visible_outside_the_cited_hunk() -> None:
    """US-X1: a head file whose module-level marker sits far from the cited
    line still shows it, because the whole small file is included."""
    src = (
        b"import pytest\n\npytestmark = pytest.mark.integration\n\n\n"
        b"async def test_x():\n    assert True\n"
    )
    findings = [_f("tests/test_a.py", 6), _f(".env", 1)]
    contexts = build_file_contexts(findings, {"tests/test_a.py": src, ".env": b"TOKEN=1\n"})
    section = render_file_context_section(contexts)
    assert "pytestmark = pytest.mark.integration" in section
    assert "TOKEN" not in section and section.count("--- ") == 1


@pytest.mark.parametrize(
    "path",
    [
        "a/__snapshots__/x.py",
        "a/baselines/x.py",
        "t/rows.ndjson",
        "t/rows.tsv",
        "t/trace.har",
        "k/cert.p12",
        "k/cert.pfx",
        "home/id_ed25519",
        "home/id_ed25519.pub",
    ],
)
def test_more_data_and_secret_paths_excluded(path: str) -> None:
    assert finding_context_paths([_f(path)]) == ()


def test_line_numbers_follow_git_not_unicode_line_breaks() -> None:
    src = "a = 1\n\x0c\nb = 2\nTARGET = 3\nc = 4\n\u2028d = 5\n".encode()
    out = build_file_contexts([_f("x.py", 4)], {"x.py": src})["x.py"]
    assert "   4| TARGET = 3" in out
    assert "   6| \u2028d = 5" in out


def test_newline_in_path_cannot_forge_a_block_header() -> None:
    out = render_file_context_section({"a.py\n--- /etc/passwd": "   1| x"})
    assert "\n--- /etc/passwd" not in out
