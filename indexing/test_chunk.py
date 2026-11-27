"""Tests for indexing.chunk — US3 (structural chunking) and US10's raising
behavior (not yet the "doesn't fail the whole job" isolation, which is
the pipeline's). Unit tier, per testing.md's own pyramid rule — ``indexing`` is a
domain package, no ADR-0005 exemption needed.
"""

from __future__ import annotations

import pytest

from indexing.chunk import (
    CHUNK_TOKEN_BUDGET,
    Chunk,
    ChunkParseError,
    chunk_file,
    language_for_path,
)

# ---------------------------------------------------------------------------
# language_for_path — extension -> ChunkLanguage mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.ts", "typescript"),
        ("src/App.tsx", "tsx"),
        ("src/Widget.php", "php"),
        ("cmd/main.go", "go"),
        ("README.md", None),
        ("scripts/build.py", "python"),
        ("scripts/build.sh", None),
    ],
)
def test_language_for_path(path: str, expected: str | None) -> None:
    assert language_for_path(path) == expected


# ---------------------------------------------------------------------------
# US3 — TypeScript: function/class/interface/type-alias boundaries, with
# enclosing context (class signature, imports) re-attached.
# ---------------------------------------------------------------------------

_TS_SOURCE = b"""\
import { Logger } from "./logger";
import { Foo } from "./foo";

export function add(a: number, b: number): number {
  return a + b;
}

export class Widget {
  private name: string;

  constructor(name: string) {
    this.name = name;
  }

  greet(): string {
    return "hi " + this.name;
  }
}

export interface Shape {
  area(): number;
}

export type Point = { x: number; y: number };
"""


def test_typescript_splits_at_function_class_interface_type_boundaries() -> None:
    result = chunk_file("typescript", "src/app.ts", _TS_SOURCE)

    symbol_names = {chunk.symbol_name for chunk in result.chunks}
    assert symbol_names == {"add", "constructor", "greet", "Shape", "Point"}


def test_typescript_method_chunk_reattaches_enclosing_class_and_imports() -> None:
    result = chunk_file("typescript", "src/app.ts", _TS_SOURCE)

    greet_chunk = next(c for c in result.chunks if c.symbol_name == "greet")

    assert "class Widget" in greet_chunk.content
    assert 'import { Logger } from "./logger";' in greet_chunk.content
    assert 'import { Foo } from "./foo";' in greet_chunk.content
    assert "return" in greet_chunk.content


def test_typescript_function_chunk_is_not_duplicated_inside_class_chunk() -> None:
    result = chunk_file("typescript", "src/app.ts", _TS_SOURCE)

    add_chunks = [c for c in result.chunks if c.symbol_name == "add"]
    assert len(add_chunks) == 1


def test_typescript_chunk_result_carries_the_parsed_tree() -> None:
    result = chunk_file("typescript", "src/app.ts", _TS_SOURCE)

    assert result.tree.root_node.type == "program"
    assert not result.tree.root_node.has_error


def test_typescript_byte_range_matches_original_content() -> None:
    result = chunk_file("typescript", "src/app.ts", _TS_SOURCE)

    add_chunk = next(c for c in result.chunks if c.symbol_name == "add")
    start, end = add_chunk.byte_range
    assert _TS_SOURCE[start:end].decode("utf-8") in add_chunk.content


def test_typescript_const_arrow_function_export_becomes_its_own_chunk() -> None:
    # `const Button = () => {...}` (the idiomatic React-component/arrow-
    # function-export style) is a function declaration in every way that
    # matters here, even though it's bound via a variable declarator
    # rather than the `function` keyword — it must not be silently
    # dropped from every chunk the way a genuinely non-function top-level
    # `const X = 5` still is (this module's own "Accepted limitation").
    source = b"""\
function helper(): number {
  return 1;
}

export const Button = (): number => {
  return helper();
};

const notAFunction = 5;
"""
    result = chunk_file("typescript", "src/app.ts", source)

    symbol_names = {chunk.symbol_name for chunk in result.chunks}
    assert "Button" in symbol_names
    assert "notAFunction" not in symbol_names

    button_chunk = next(c for c in result.chunks if c.symbol_name == "Button")
    assert "helper()" in button_chunk.content


def test_tsx_extension_uses_tsx_grammar_and_still_splits_functions() -> None:
    source = b"""\
export function Button(): string {
  return "<button />";
}
"""
    result = chunk_file("tsx", "src/Button.tsx", source)

    assert {c.symbol_name for c in result.chunks} == {"Button"}


# ---------------------------------------------------------------------------
# US3 — PHP: function/class/interface/trait boundaries.
# ---------------------------------------------------------------------------

_PHP_SOURCE = b"""<?php

namespace App;

use App\\Bar;

function add($a, $b) {
    return $a + $b;
}

class Widget {
    private $name;

    public function __construct($name) {
        $this->name = $name;
    }

    public function greet() {
        return "hi " . $this->name;
    }
}

interface Shape {
    public function area();
}

trait Greetable {
    public function hello() {
        return 1;
    }
}
"""


def test_php_splits_at_function_method_interface_trait_boundaries() -> None:
    result = chunk_file("php", "src/Widget.php", _PHP_SOURCE)

    symbol_names = {chunk.symbol_name for chunk in result.chunks}
    assert symbol_names == {"add", "__construct", "greet", "area", "hello"}


def test_php_method_chunk_reattaches_enclosing_class_and_use_statement() -> None:
    result = chunk_file("php", "src/Widget.php", _PHP_SOURCE)

    greet_chunk = next(c for c in result.chunks if c.symbol_name == "greet")

    assert "class Widget" in greet_chunk.content
    assert "use App\\Bar;" in greet_chunk.content


def test_php_interface_method_with_no_body_still_chunks() -> None:
    result = chunk_file("php", "src/Widget.php", _PHP_SOURCE)

    area_chunk = next(c for c in result.chunks if c.symbol_name == "area")
    assert "function area" in area_chunk.content


# ---------------------------------------------------------------------------
# US3 — Go: function/method/type-declaration boundaries.
# ---------------------------------------------------------------------------

_GO_SOURCE = b"""\
package main

import "fmt"

func Add(a, b int) int {
	return a + b
}

type Widget struct {
	Name string
}

func (w *Widget) Greet() string {
	return "hi " + w.Name
}

type Shape interface {
	Area() float64
}
"""


def test_go_splits_at_function_method_type_boundaries() -> None:
    result = chunk_file("go", "cmd/main.go", _GO_SOURCE)

    symbol_names = {chunk.symbol_name for chunk in result.chunks}
    assert symbol_names == {"Add", "Widget", "Greet", "Shape"}


def test_go_method_chunk_has_no_enclosing_container_but_keeps_receiver() -> None:
    result = chunk_file("go", "cmd/main.go", _GO_SOURCE)

    greet_chunk = next(c for c in result.chunks if c.symbol_name == "Greet")
    assert "func (w *Widget) Greet" in greet_chunk.content


def test_go_type_declaration_symbol_name_resolved_from_type_spec() -> None:
    result = chunk_file("go", "cmd/main.go", _GO_SOURCE)

    widget_chunk = next(c for c in result.chunks if c.symbol_name == "Widget")
    assert "struct" in widget_chunk.content


def test_go_grouped_type_declaration_splits_into_one_chunk_per_type_spec() -> None:
    # `type (\n\tA struct{...}\n\tB struct{...}\n)` wraps both specs in one
    # `type_declaration` node — each must still become its own separately
    # named chunk, never one chunk mis-labeled under only the first type's
    # name with the second type's own content silently unreachable by
    # symbol-name lookup.
    source = b"""\
package main

type (
	A struct {
		X int
	}
	B struct {
		Y int
	}
)
"""
    result = chunk_file("go", "cmd/main.go", source)

    symbol_names = {chunk.symbol_name for chunk in result.chunks}
    assert {"A", "B"} <= symbol_names

    a_chunk = next(c for c in result.chunks if c.symbol_name == "A")
    b_chunk = next(c for c in result.chunks if c.symbol_name == "B")
    assert "X int" in a_chunk.content
    assert "Y int" not in a_chunk.content
    assert "Y int" in b_chunk.content
    assert "X int" not in b_chunk.content


# ---------------------------------------------------------------------------
# Fallback: a file with no matched structural boundaries still gets chunked
# (accepted-limitation fallback, see this module's own docstring).
# ---------------------------------------------------------------------------


def test_file_with_no_declarations_falls_back_to_whole_file_chunk() -> None:
    source = b"package config\n\nconst MaxRetries = 3\n"

    result = chunk_file("go", "cmd/config.go", source)

    assert len(result.chunks) == 1
    assert result.chunks[0].symbol_name is None
    assert "MaxRetries" in result.chunks[0].content


def test_empty_file_produces_zero_chunks() -> None:
    result = chunk_file("go", "cmd/empty.go", b"")

    assert result.chunks == ()


# ---------------------------------------------------------------------------
# Token-budget packing with overlap — a large function is split into
# multiple chunks, each within budget, with overlapping trailing lines.
# ---------------------------------------------------------------------------


def test_large_function_is_packed_into_multiple_chunks_within_budget() -> None:
    # ~200 lines, each comfortably over the char-per-token estimate needed
    # to blow past CHUNK_TOKEN_BUDGET for the whole function in one chunk.
    body_lines = "\n".join(f"    x{i} := {i}  // padding padding padding" for i in range(200))
    source = f"""package main

func Big() int {{
{body_lines}
    return 0
}}
""".encode()

    result = chunk_file("go", "cmd/big.go", source)

    big_chunks = [c for c in result.chunks if c.symbol_name == "Big"]
    assert len(big_chunks) > 1
    for chunk in big_chunks:
        # A generous margin over the nominal budget: the char/4 estimate is
        # approximate, and every window also carries the (small, here-empty)
        # prefix, so this only checks packing actually bounds chunk size
        # rather than asserting the estimate is exact.
        assert len(chunk.content) < (CHUNK_TOKEN_BUDGET * 4) * 2


def test_consecutive_packed_chunks_overlap() -> None:
    body_lines = "\n".join(f"    x{i} := {i}  // padding padding padding" for i in range(200))
    source = f"""package main

func Big() int {{
{body_lines}
    return 0
}}
""".encode()

    result = chunk_file("go", "cmd/big.go", source)
    big_chunks = [c for c in result.chunks if c.symbol_name == "Big"]

    first_tail = big_chunks[0].content.strip().splitlines()[-1]
    second_head_lines = big_chunks[1].content.strip().splitlines()
    assert first_tail in second_head_lines


def test_oversized_boundary_line_is_not_duplicated_via_overlap() -> None:
    """Regression test: the overlap-window backtracking loop used to
    unconditionally absorb the line at a packed window's own boundary into
    the next window's overlap even when that single line's own token
    estimate already exceeded `CHUNK_TOKEN_OVERLAP` by itself — silently
    duplicating a disproportionately large line into the next chunk far
    beyond the intended overlap size. A boundary line long enough to blow
    the overlap budget on its own (but not the whole per-chunk budget) must
    land in exactly one packed chunk, never two.
    """
    small_line = "    y := 1  // padding padding pad\n"
    huge_line = '    z := "' + ("x" * 1200) + '"\n'
    # 22 leading small lines lands `huge_line` exactly at the first packed
    # window's own boundary for this module's token-estimate/budget/overlap
    # constants — found by probing `chunk_file`'s actual packing boundaries
    # directly, not derived analytically, since the enclosing function's
    # own header/return lines also count toward the running token total.
    body_lines = small_line * 22 + huge_line + small_line * 60
    source = f"""package main

func Big() int {{
{body_lines}
    return 0
}}
""".encode()

    result = chunk_file("go", "cmd/big.go", source)

    big_chunks = [c for c in result.chunks if c.symbol_name == "Big"]
    assert len(big_chunks) > 1
    huge_line_occurrences = sum(1 for c in big_chunks if huge_line.strip() in c.content)
    assert huge_line_occurrences == 1


def test_chunk_index_is_sequential_per_file() -> None:
    result = chunk_file("typescript", "src/app.ts", _TS_SOURCE)

    indices = [chunk.chunk_index for chunk in result.chunks]
    assert indices == list(range(len(indices)))


# ---------------------------------------------------------------------------
# line_range: computed uniformly by counting newlines in the *full*
# file content up to each end of the chunk's own already-correct,
# file-absolute byte_range. Round seven/eight added these two cases
# specifically because three prior computation attempts each would have
# passed a naive single-window, no-trailing-newline test while still being
# wrong for exactly these cases.
# ---------------------------------------------------------------------------


def test_second_window_line_range_starts_well_after_line_one() -> None:
    """A multi-window declaration's *second* window must get a line_range
    starting well after line 1, matching the real file's own line numbers —
    not the coincidentally-correct line 1 a single-window case would pass
    even with a broken computation.
    """
    body_lines = "\n".join(f"    x{i} := {i}  // padding padding padding" for i in range(200))
    source = f"""package main

func Big() int {{
{body_lines}
    return 0
}}
""".encode()

    result = chunk_file("go", "cmd/big.go", source)
    big_chunks = [c for c in result.chunks if c.symbol_name == "Big"]
    assert len(big_chunks) > 1

    second = big_chunks[1]
    assert second.line_range is not None
    start_line, end_line = second.line_range
    assert start_line > 1

    # Independently verify against the real file's own line numbers — by
    # reconstructing the byte range implied by (start_line, end_line) via a
    # plain splitlines-based slice (not this module's own `_line_range`
    # formula under test) and checking it equals the chunk's actual
    # byte_range slice of the source.
    file_lines = source.splitlines(keepends=True)
    reconstructed = b"".join(file_lines[start_line - 1 : end_line])
    assert source[second.byte_range[0] : second.byte_range[1]] == reconstructed


def test_whole_file_chunk_with_trailing_newline_end_line_is_not_off_by_one() -> None:
    """A whole-file fallback chunk (no matched declarations) of a fixture
    that *ends with a trailing newline* — the near-universal case for real
    source files — must get an end_line equal to the file's real last line,
    not one past it (the exact `byte_range[1] - 1` off-by-one this
    computation must get right).
    """
    source = b"package config\n\nconst MaxRetries = 3\n"

    result = chunk_file("go", "cmd/config.go", source)

    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.line_range is not None
    # Three real lines ("package config", "", "const MaxRetries = 3") — the
    # trailing "\n" doesn't imply a fourth, empty line.
    assert chunk.line_range == (1, 3)


# ---------------------------------------------------------------------------
# US10 — one file's parse failure raises a typed error (not yet the "rest
# of the job still proceeds" isolation, which is the pipeline's own test).
# ---------------------------------------------------------------------------


def test_invalid_utf8_content_raises_chunk_parse_error() -> None:
    invalid_utf8 = b"\xff\xfe\x00 not valid utf-8 \x80\x81"

    with pytest.raises(ChunkParseError) as excinfo:
        chunk_file("go", "cmd/bad.go", invalid_utf8)

    assert excinfo.value.file_path == "cmd/bad.go"
    assert excinfo.value.language == "go"


def test_chunk_parse_error_is_never_swallowed_by_chunk_file() -> None:
    # chunk_file itself never catches its own ChunkParseError — it's the
    # caller's (the pipeline's) job to isolate a per-file failure, per this
    # module's own docstring.
    with pytest.raises(ChunkParseError):
        chunk_file("php", "src/bad.php", b"\xff\xfe not valid utf-8")


# ---------------------------------------------------------------------------
# Chunk model itself
# ---------------------------------------------------------------------------


def test_chunk_defaults_repo_id_to_none() -> None:
    result = chunk_file("go", "cmd/main.go", _GO_SOURCE)

    assert all(chunk.repo_id is None for chunk in result.chunks)


def test_chunk_is_frozen() -> None:
    chunk = Chunk(
        file_path="a.go",
        language="go",
        symbol_name="Foo",
        byte_range=(0, 1),
        content="x",
        chunk_index=0,
    )
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own frozen-model error type
        chunk.chunk_index = 1  # type: ignore[misc]


_PY_SOURCE = b"""\
import os
from a.b import c

def helper():
    return 1

class Widget:
    def greet(self):
        return helper()
"""


def test_python_splits_functions_and_methods_with_class_and_imports_attached() -> None:
    result = chunk_file("python", "pkg/widget.py", _PY_SOURCE)

    assert {c.symbol_name for c in result.chunks} == {"helper", "greet"}
    greet_chunk = next(c for c in result.chunks if c.symbol_name == "greet")
    assert "class Widget:" in greet_chunk.content
    assert "{" not in greet_chunk.content
    assert "from a.b import c" in greet_chunk.content
