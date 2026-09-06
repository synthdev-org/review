"""Tests for review.context_symbols — US1/US10, per
docs/plans/plan-deliver-context-builder.md's Phase 1 section and
docs/adr/0005-pure-no-io-logic-unit-tested.md's scope extension for this
phase. Unit tier: ``resolve_changed_symbols`` performs no I/O of its own —
it is handed already-fetched ``pre_content``/``post_content`` dicts and only
does CPU-bound Tree-sitter parsing (dispatched via ``asyncio.to_thread``).
``fetch_diff_content`` (real I/O) is deliberately not unit-tested here — see
that function's own docstring for why.
"""

from __future__ import annotations

import logging

import pytest

from ingestion.diff import Diff, DiffFile
from review.context_symbols import (
    MAX_CHANGED_CHUNKS_PER_JOB,
    MAX_CHANGED_SYMBOLS_PER_JOB,
    _line_range_to_byte_range,
    resolve_changed_symbols,
)

pytestmark = pytest.mark.asyncio


def _diff(*files: DiffFile) -> Diff:
    return Diff(files=tuple(files))


# ---------------------------------------------------------------------------
# `_line_range_to_byte_range` — `\n`-only line splitting, never a bare `\r`.
# ---------------------------------------------------------------------------


async def test_line_range_to_byte_range_normal_newlines_unaffected() -> None:
    """Plain `\\n` and `\\r\\n` line endings must resolve identically to
    before this fix (`\\r\\n` still counts as one line break, since `\\r`
    stays attached to the preceding line's bytes either way)."""

    content = b"line one\nline two\nline three\n"
    assert _line_range_to_byte_range(content, 1, 1) == (0, len(b"line one\n"))
    assert _line_range_to_byte_range(content, 2, 1) == (
        len(b"line one\n"),
        len(b"line one\nline two\n"),
    )

    crlf_content = b"line one\r\nline two\r\nline three\r\n"
    assert _line_range_to_byte_range(crlf_content, 1, 1) == (0, len(b"line one\r\n"))
    assert _line_range_to_byte_range(crlf_content, 2, 1) == (
        len(b"line one\r\n"),
        len(b"line one\r\nline two\r\n"),
    )


async def test_line_range_to_byte_range_bare_cr_is_not_a_line_break() -> None:
    """A bare `\\r` (old Mac-style line ending, no `\\n`) must never
    fragment a line on its own — only `\\n` delimits lines, matching
    `indexing/chunk.py`'s `_line_range` newline-counting and a unified
    diff's own `\\n`-delimited hunk-header line count. Before this fix,
    `bytes.splitlines(keepends=True)` treated the bare `\\r` here as its own
    line break, splitting "line one\\rline two" into two lines instead of
    one."""

    content = b"line one\rline two\nline three\n"
    # Only one `\n`-delimited line boundary precedes "line three" — the bare
    # `\r` inside the first line must not count as a second one.
    assert _line_range_to_byte_range(content, 1, 1) == (0, len(b"line one\rline two\n"))
    assert _line_range_to_byte_range(content, 2, 1) == (
        len(b"line one\rline two\n"),
        len(b"line one\rline two\nline three\n"),
    )


async def test_line_range_to_byte_range_trailing_newline_not_counted_as_extra_line() -> None:
    """Content ending in `\\n` has no phantom trailing line. A naive
    `content.split(b"\\n")` produces a trailing empty-string artifact after
    the final separator (e.g. `b"a\\nb\\n".split(b"\\n") == [b"a", b"b",
    b""]`) that must NOT be counted as an extra real line — regression test
    for a bug where that artifact inflated the line count by one, making an
    out-of-range `start_line` (one past the real last line) incorrectly
    resolve instead of returning `None`."""

    content = b"line one\nline two\n"  # exactly 2 real lines

    # The true last line still resolves.
    assert _line_range_to_byte_range(content, 2, 1) == (
        len(b"line one\n"),
        len(b"line one\nline two\n"),
    )
    # One past the true last line must be rejected as out-of-range, not
    # resolve against the phantom trailing empty-string artifact.
    assert _line_range_to_byte_range(content, 3, 1) is None


# ---------------------------------------------------------------------------
# TypeScript — a single-line change inside one function resolves to it.
# ---------------------------------------------------------------------------

_TS_PRE = b"""\
import { Logger } from "./logger";

export function add(a: number, b: number): number {
  return a + b;
}

export class Widget {
  greet(): string {
    return "hi";
  }
}
"""

_TS_POST = b"""\
import { Logger } from "./logger";

export function add(a: number, b: number): number {
  return a + b + 1;
}

export class Widget {
  greet(): string {
    return "hi";
  }
}
"""

_TS_PATCH = "@@ -4,1 +4,1 @@\n-  return a + b;\n+  return a + b + 1;\n"


async def test_typescript_single_line_change_resolves_to_enclosing_function() -> None:
    diff = _diff(DiffFile(path="src/app.ts", status="modified", patch=_TS_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={"src/app.ts": _TS_PRE},
        post_content={"src/app.ts": _TS_POST},
        job_id="job-1",
    )

    assert [c.symbol_name for c in result.pre_change_hits] == ["add"]
    assert [c.symbol_name for c in result.post_change_hits] == ["add"]


# ---------------------------------------------------------------------------
# PHP — same shape, proving the language dispatch isn't TS-only.
# ---------------------------------------------------------------------------

_PHP_PRE = b"""<?php

function addNumbers($a, $b) {
    return $a + $b;
}

class Widget {
    function greet() {
        return "hi";
    }
}
"""

_PHP_POST = b"""<?php

function addNumbers($a, $b) {
    return $a + $b + 1;
}

class Widget {
    function greet() {
        return "hi";
    }
}
"""

_PHP_PATCH = "@@ -4,1 +4,1 @@\n-    return $a + $b;\n+    return $a + $b + 1;\n"


async def test_php_single_line_change_resolves_to_enclosing_function() -> None:
    diff = _diff(DiffFile(path="src/widget.php", status="modified", patch=_PHP_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={"src/widget.php": _PHP_PRE},
        post_content={"src/widget.php": _PHP_POST},
        job_id="job-1",
    )

    assert [c.symbol_name for c in result.pre_change_hits] == ["addNumbers"]
    assert [c.symbol_name for c in result.post_change_hits] == ["addNumbers"]


# ---------------------------------------------------------------------------
# Go — US10: a renamed function resolves under its pre-change name.
# ---------------------------------------------------------------------------

_GO_PRE_RENAME = b"""package main

func oldName() int {
    return 1
}
"""

_GO_POST_RENAME = b"""package main

func newName() int {
    return 1
}
"""

_GO_RENAME_PATCH = "@@ -3,1 +3,1 @@\n-func oldName() int {\n+func newName() int {\n"


async def test_go_renamed_function_pre_change_hit_uses_old_name() -> None:
    diff = _diff(DiffFile(path="cmd/main.go", status="modified", patch=_GO_RENAME_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={"cmd/main.go": _GO_PRE_RENAME},
        post_content={"cmd/main.go": _GO_POST_RENAME},
        job_id="job-1",
    )

    assert [c.symbol_name for c in result.pre_change_hits] == ["oldName"]
    assert [c.symbol_name for c in result.post_change_hits] == ["newName"]


# ---------------------------------------------------------------------------
# US10 — a removed function's pre-change hit still resolves; nothing on the
# post-change side (the function no longer exists).
# ---------------------------------------------------------------------------

_GO_PRE_REMOVED = b"""package main

import "fmt"

func removedFn() {
    fmt.Println("bye")
}
"""

_GO_POST_REMOVED = b"""package main

import "fmt"
"""

_GO_REMOVED_PATCH = '@@ -4,4 +3,0 @@\n-\n-func removedFn() {\n-    fmt.Println("bye")\n-}\n'


async def test_go_removed_function_pre_change_hit_resolves_post_change_does_not() -> None:
    diff = _diff(DiffFile(path="cmd/main.go", status="modified", patch=_GO_REMOVED_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={"cmd/main.go": _GO_PRE_REMOVED},
        post_content={"cmd/main.go": _GO_POST_REMOVED},
        job_id="job-1",
    )

    assert [c.symbol_name for c in result.pre_change_hits] == ["removedFn"]
    assert result.post_change_hits == ()


# ---------------------------------------------------------------------------
# US1 — a hunk that doesn't fall inside exactly one enclosing declaration
# (here: spanning two declarations) resolves to no symbol at all.
# ---------------------------------------------------------------------------

_GO_MULTI_DECL = b"""package main

func First() int {
    return 1
}

func Second() int {
    return 2
}
"""

# Spans from inside First() (line 3) to inside Second() (line 7) — two
# enclosing declarations, not one.
_GO_SPANNING_PATCH = "@@ -3,5 +3,5 @@\n context\n"


async def test_hunk_spanning_two_declarations_resolves_to_no_symbol() -> None:
    diff = _diff(DiffFile(path="cmd/main.go", status="modified", patch=_GO_SPANNING_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={"cmd/main.go": _GO_MULTI_DECL},
        post_content={"cmd/main.go": _GO_MULTI_DECL},
        job_id="job-1",
    )

    assert result.pre_change_hits == ()
    assert result.post_change_hits == ()


# ---------------------------------------------------------------------------
# US1 — an import-only hunk resolves to no symbol.
# ---------------------------------------------------------------------------

_GO_IMPORT_ONLY_PATCH = "@@ -1,1 +1,1 @@\n-package main\n+package main // comment\n"


async def test_import_only_hunk_resolves_to_no_symbol() -> None:
    source = b"""package main

func Foo() int {
    return 1
}
"""
    diff = _diff(DiffFile(path="cmd/main.go", status="modified", patch=_GO_IMPORT_ONLY_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={"cmd/main.go": source},
        post_content={"cmd/main.go": source},
        job_id="job-1",
    )

    assert result.pre_change_hits == ()
    assert result.post_change_hits == ()


# ---------------------------------------------------------------------------
# US8 — an out-of-scope-language file contributes nothing, but doesn't
# block the rest of the diff.
# ---------------------------------------------------------------------------


async def test_out_of_scope_language_file_contributes_nothing_others_unaffected() -> None:
    diff = _diff(
        DiffFile(path="README.md", status="modified", patch="@@ -1,1 +1,1 @@\n-a\n+b\n"),
        DiffFile(path="cmd/main.go", status="modified", patch=_GO_RENAME_PATCH),
    )

    result = await resolve_changed_symbols(
        diff,
        pre_content={
            "README.md": b"a\n",
            "cmd/main.go": _GO_PRE_RENAME,
        },
        post_content={
            "README.md": b"b\n",
            "cmd/main.go": _GO_POST_RENAME,
        },
        job_id="job-1",
    )

    assert [c.symbol_name for c in result.pre_change_hits] == ["oldName"]
    assert [c.symbol_name for c in result.post_change_hits] == ["newName"]


# ---------------------------------------------------------------------------
# A hunk header referencing a line range beyond the fetched content's own
# line count logs a warning distinguishing a real diff/content mismatch
# from the ordinary "empty hunk side" no-symbol case.
# ---------------------------------------------------------------------------

_GO_OUT_OF_RANGE_PATCH = "@@ -10,1 +10,1 @@\n-old\n+new\n"


async def test_hunk_line_range_beyond_content_logs_mismatch_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hunk header claiming line 10 against a 3-line file (e.g. a race
    between the diff being computed and the blob actually fetched) resolves
    to no symbol — the same outcome as any other unresolvable hunk — but,
    unlike a legitimate empty hunk side, must log a `warning` naming the
    mismatch rather than staying silent."""
    source = b"""package main

func Foo() int {
    return 1
}
"""
    diff = _diff(DiffFile(path="cmd/main.go", status="modified", patch=_GO_OUT_OF_RANGE_PATCH))

    with caplog.at_level(logging.WARNING, logger="review.context_symbols"):
        result = await resolve_changed_symbols(
            diff,
            pre_content={"cmd/main.go": source},
            post_content={"cmd/main.go": source},
            job_id="job-1",
        )

    assert result.pre_change_hits == ()
    assert result.post_change_hits == ()
    mismatch_warnings = [r for r in caplog.records if "diff/content mismatch" in r.message]
    assert len(mismatch_warnings) == 2  # pre-change and post-change sides both log
    assert {r.start_line for r in mismatch_warnings} == {10}  # type: ignore[attr-defined]


async def test_import_only_hunk_resolves_to_no_symbol_and_logs_no_mismatch_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ordinary empty-hunk-side case (`line_count <= 0`, e.g. an
    import-only, pure-comment-line hunk) must NOT be confused with a real
    diff/content mismatch — no warning logged, unlike the out-of-range
    case above."""
    source = b"""package main

func Foo() int {
    return 1
}
"""
    diff = _diff(DiffFile(path="cmd/main.go", status="modified", patch=_GO_IMPORT_ONLY_PATCH))

    with caplog.at_level(logging.WARNING, logger="review.context_symbols"):
        result = await resolve_changed_symbols(
            diff,
            pre_content={"cmd/main.go": source},
            post_content={"cmd/main.go": source},
            job_id="job-1",
        )

    assert result.pre_change_hits == ()
    assert result.post_change_hits == ()
    assert not [r for r in caplog.records if "diff/content mismatch" in r.message]


# ---------------------------------------------------------------------------
# US8 — a ChunkParseError for one file doesn't abort the rest of the diff.
# ---------------------------------------------------------------------------


async def test_chunk_parse_error_isolated_per_file(caplog: pytest.LogCaptureFixture) -> None:
    invalid_utf8 = b"\xff\xfe not valid utf-8"
    diff = _diff(
        DiffFile(path="cmd/bad.go", status="modified", patch=_GO_RENAME_PATCH),
        DiffFile(path="cmd/main.go", status="modified", patch=_GO_RENAME_PATCH),
    )

    with caplog.at_level(logging.WARNING, logger="review.context_symbols"):
        result = await resolve_changed_symbols(
            diff,
            pre_content={"cmd/bad.go": invalid_utf8, "cmd/main.go": _GO_PRE_RENAME},
            post_content={"cmd/bad.go": invalid_utf8, "cmd/main.go": _GO_POST_RENAME},
            job_id="job-1",
        )

    assert [c.symbol_name for c in result.pre_change_hits] == ["oldName"]
    assert [c.symbol_name for c in result.post_change_hits] == ["newName"]
    assert any("chunk parse failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# A file absent from pre_content (newly added file) contributes no
# pre-change chunks but still contributes post-change chunks.
# ---------------------------------------------------------------------------


async def test_file_absent_from_pre_content_contributes_no_pre_change_hits() -> None:
    diff = _diff(DiffFile(path="cmd/new.go", status="added", patch=_GO_RENAME_PATCH))

    result = await resolve_changed_symbols(
        diff,
        pre_content={},
        post_content={"cmd/new.go": _GO_POST_RENAME},
        job_id="job-1",
    )

    assert result.pre_change_hits == ()
    assert [c.symbol_name for c in result.post_change_hits] == ["newName"]


# ---------------------------------------------------------------------------
# US9 (Assumption 6) — MAX_CHANGED_SYMBOLS_PER_JOB / MAX_CHANGED_CHUNKS_PER_JOB
# ceilings are enforced, in diff order, with a warning log.
# ---------------------------------------------------------------------------


def _build_go_source(values: list[int]) -> tuple[bytes, dict[int, int]]:
    """Builds a Go source file with one function per entry of ``values``
    (``func Fn{i}() int { return {values[i]} }``), returning the source
    bytes and a ``{index: 1-indexed line number of its own return
    statement}`` map computed directly from the lines actually written —
    not re-derived analytically — so hunk headers built from it are
    guaranteed correct.
    """

    lines: list[str] = []
    return_lines: dict[int, int] = {}
    for i, value in enumerate(values):
        lines.append(f"func Fn{i}() int {{")
        lines.append(f"    return {value}")
        return_lines[i] = len(lines)
        lines.append("}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode(), return_lines


async def test_ceilings_truncate_excess_in_diff_order_and_log_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    count = MAX_CHANGED_SYMBOLS_PER_JOB + 10
    pre_values = list(range(count))
    post_values = [v + 1 for v in pre_values]
    pre_source, return_lines = _build_go_source(pre_values)
    post_source, post_return_lines = _build_go_source(post_values)
    assert return_lines == post_return_lines  # structurally identical, only values differ

    patch = "".join(
        f"@@ -{return_lines[i]},1 +{return_lines[i]},1 @@\n"
        f"-    return {pre_values[i]}\n"
        f"+    return {post_values[i]}\n"
        for i in range(count)
    )
    diff = _diff(DiffFile(path="pkg/many.go", status="modified", patch=patch))

    with caplog.at_level(logging.WARNING, logger="review.context_symbols"):
        result = await resolve_changed_symbols(
            diff,
            pre_content={"pkg/many.go": pre_source},
            post_content={"pkg/many.go": post_source},
            job_id="job-1",
        )

    assert len(result.pre_change_hits) == MAX_CHANGED_SYMBOLS_PER_JOB
    assert len(result.post_change_hits) == MAX_CHANGED_CHUNKS_PER_JOB
    # Diff order preserved: the surviving hits are the first
    # MAX_CHANGED_SYMBOLS_PER_JOB functions, not an arbitrary subset.
    assert [c.symbol_name for c in result.pre_change_hits] == [
        f"Fn{i}" for i in range(MAX_CHANGED_SYMBOLS_PER_JOB)
    ]
    assert any(
        "max_changed_symbols_per_job" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    )
    assert any(
        "max_changed_chunks_per_job" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    )
    # Phase 5's own Observability requirement: ContextBuilderStep's per-run
    # log folds these upstream-ceiling drop counts into its own summary, not
    # just the immediate warning logs above — proving they're returned, not
    # just logged at drop time.
    assert result.pre_dropped_count == 10
    assert result.post_dropped_count == count - MAX_CHANGED_CHUNKS_PER_JOB
