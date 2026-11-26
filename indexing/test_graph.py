"""Tests for indexing.graph — US5 (call/import graph extraction). Unit
tier, per testing.md's own pyramid rule (same note as
``indexing/test_chunk.py``) — pure logic, no I/O, "matching" against each
fixture's own in-memory symbol set (see ``indexing/graph.py``'s own
docstring for what "matching" means at this stage).
"""

from __future__ import annotations

from typing import cast

import pytest
from tree_sitter import Tree

from indexing.chunk import ChunkLanguage, chunk_file
from indexing.graph import (
    MODULE_SYMBOL_NAME,
    GraphEdge,
    GraphExtractionError,
    GraphNode,
    extract_graph,
)

# ---------------------------------------------------------------------------
# US5 — TypeScript: definitions become nodes; calls/imports become edges,
# sourced from their enclosing definition (or the synthetic module node for
# top-level calls and every import, which has no enclosing definition).
# ---------------------------------------------------------------------------

_TS_SOURCE = b"""\
import { Logger } from "./logger";
import Foo from "./foo";
import * as NS from "./ns";

function helper() {
  return 1;
}

function add(a, b) {
  const x = helper();
  const y = Math.max(a, b);
  return x + y;
}

class Widget {
  greet() {
    return helper() + this.other();
  }

  other() {
    return 1;
  }
}

interface Shape {
  area(): number;
}

initialize();
"""


def _parse(language: ChunkLanguage, path: str, source: bytes) -> Tree:
    return chunk_file(language, path, source).tree


def _targets_from(edges: tuple[GraphEdge, ...], source_symbol_name: str) -> set[str]:
    return {e.target_symbol_name for e in edges if e.source_node.symbol_name == source_symbol_name}


def test_typescript_definitions_become_nodes() -> None:
    tree = _parse("typescript", "src/app.ts", _TS_SOURCE)
    result = extract_graph("typescript", "src/app.ts", tree)

    symbol_names = {node.symbol_name for node in result.nodes}
    assert symbol_names == {
        MODULE_SYMBOL_NAME,
        "helper",
        "add",
        "Widget",
        "greet",
        "other",
        "Shape",
    }


def test_typescript_call_inside_function_is_sourced_from_that_function() -> None:
    tree = _parse("typescript", "src/app.ts", _TS_SOURCE)
    result = extract_graph("typescript", "src/app.ts", tree)

    add_edges = [e for e in result.edges if e.source_node.symbol_name == "add"]
    assert {e.target_symbol_name for e in add_edges} == {"helper", "max"}


def test_typescript_member_call_uses_property_name_as_target() -> None:
    tree = _parse("typescript", "src/app.ts", _TS_SOURCE)
    result = extract_graph("typescript", "src/app.ts", tree)

    greet_edges = [e for e in result.edges if e.source_node.symbol_name == "greet"]
    assert {e.target_symbol_name for e in greet_edges} == {"helper", "other"}


def test_typescript_named_and_default_imports_become_edges_from_module_node() -> None:
    tree = _parse("typescript", "src/app.ts", _TS_SOURCE)
    result = extract_graph("typescript", "src/app.ts", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "Logger" in module_targets
    assert "Foo" in module_targets


def test_typescript_namespace_import_produces_no_edge_not_an_error() -> None:
    tree = _parse("typescript", "src/app.ts", _TS_SOURCE)
    result = extract_graph("typescript", "src/app.ts", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "NS" not in module_targets


def test_typescript_const_arrow_function_export_becomes_its_own_node() -> None:
    # See `indexing.chunk`'s own docstring/test on why this counts as a
    # function declaration even though it's bound via a variable
    # declarator rather than the `function` keyword.
    source = b"""\
function helper() {
  return 1;
}

export const Button = () => {
  return helper();
};
"""
    tree = _parse("typescript", "src/app.ts", source)
    result = extract_graph("typescript", "src/app.ts", tree)

    assert "Button" in {node.symbol_name for node in result.nodes}
    button_edges = _targets_from(result.edges, "Button")
    assert button_edges == {"helper"}


def test_typescript_top_level_call_is_sourced_from_module_node() -> None:
    tree = _parse("typescript", "src/app.ts", _TS_SOURCE)
    result = extract_graph("typescript", "src/app.ts", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "initialize" in module_targets


def test_tsx_extension_still_extracts_graph() -> None:
    source = b"""\
export function Button() {
  return helper();
}

function helper() {
  return 1;
}
"""
    tree = _parse("tsx", "src/Button.tsx", source)
    result = extract_graph("tsx", "src/Button.tsx", tree)

    symbol_names = {node.symbol_name for node in result.nodes}
    assert {"Button", "helper"} <= symbol_names

    button_edges = [e for e in result.edges if e.source_node.symbol_name == "Button"]
    assert {e.target_symbol_name for e in button_edges} == {"helper"}


# ---------------------------------------------------------------------------
# US5 — PHP: function/method/class/interface/trait definitions become
# nodes; function/member calls and `use` imports become edges.
# ---------------------------------------------------------------------------

_PHP_SOURCE = b"""<?php

namespace App;

use App\\Bar;
use App\\Baz as B;
use Foo;

function helper() {
    return 1;
}

function add($a, $b) {
    $x = helper();
    $y = otherFn($a, $b);
    return $x + $y;
}

class Widget {
    public function greet($a, $b) {
        return helper() . $this->other();
    }

    public function other() {
        return 1;
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

initialize();
"""


def test_php_definitions_become_nodes() -> None:
    tree = _parse("php", "src/Widget.php", _PHP_SOURCE)
    result = extract_graph("php", "src/Widget.php", tree)

    symbol_names = {node.symbol_name for node in result.nodes}
    assert symbol_names == {
        MODULE_SYMBOL_NAME,
        "helper",
        "add",
        "Widget",
        "greet",
        "other",
        "Shape",
        "area",
        "Greetable",
        "hello",
    }


def test_php_function_and_member_calls_become_edges() -> None:
    tree = _parse("php", "src/Widget.php", _PHP_SOURCE)
    result = extract_graph("php", "src/Widget.php", tree)

    add_edges = [e for e in result.edges if e.source_node.symbol_name == "add"]
    assert {e.target_symbol_name for e in add_edges} == {"helper", "otherFn"}

    greet_edges = [e for e in result.edges if e.source_node.symbol_name == "greet"]
    assert {e.target_symbol_name for e in greet_edges} == {"helper", "other"}


def test_php_use_imports_become_edges_using_unaliased_name() -> None:
    tree = _parse("php", "src/Widget.php", _PHP_SOURCE)
    result = extract_graph("php", "src/Widget.php", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert {"Bar", "Baz", "Foo", "initialize"} <= module_targets
    # The alias ("B") never appears as a target — matching happens against
    # the real symbol name, not the local alias.
    assert "B" not in module_targets


def test_php_grouped_use_imports_become_edges() -> None:
    # `use App\Models\{Foo, Bar as B};` nests its own clauses one level
    # deeper (inside a `namespace_use_group`) than the flat `use Foo;`
    # case above — both must still produce edges.
    source = b"""<?php

use App\\Models\\{Foo, Bar as B};

initialize();
"""
    tree = _parse("php", "src/Widget.php", source)
    result = extract_graph("php", "src/Widget.php", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert {"Foo", "Bar", "initialize"} <= module_targets
    assert "B" not in module_targets


# ---------------------------------------------------------------------------
# US5 — Go: function/method/type declarations become nodes; calls and
# imports become edges.
# ---------------------------------------------------------------------------

_GO_SOURCE = b"""\
package main

import (
	"fmt"
	s "strings"
)

func helper() int {
	return 1
}

func Add(a, b int) int {
	x := helper()
	y := s.ToUpper("a")
	fmt.Println(x, y)
	return x
}

type Widget struct {
	Name string
}

func (w *Widget) Greet() string {
	return fmt.Sprintf("%d", helper())
}

type Shape interface {
	Area() float64
}

func init() {
	helper()
}
"""


def test_go_definitions_become_nodes() -> None:
    tree = _parse("go", "cmd/main.go", _GO_SOURCE)
    result = extract_graph("go", "cmd/main.go", tree)

    symbol_names = {node.symbol_name for node in result.nodes}
    assert symbol_names == {
        MODULE_SYMBOL_NAME,
        "helper",
        "Add",
        "Widget",
        "Greet",
        "Shape",
        "init",
    }


def test_go_grouped_type_declaration_produces_one_node_per_type_spec() -> None:
    # See indexing/test_chunk.py's identical fixture/reasoning: one
    # `type_declaration` can group multiple `type_spec` children.
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
    tree = _parse("go", "cmd/main.go", source)
    result = extract_graph("go", "cmd/main.go", tree)

    symbol_names = {node.symbol_name for node in result.nodes}
    assert {"A", "B"} <= symbol_names


def test_go_plain_and_selector_calls_become_edges() -> None:
    tree = _parse("go", "cmd/main.go", _GO_SOURCE)
    result = extract_graph("go", "cmd/main.go", tree)

    add_edges = [e for e in result.edges if e.source_node.symbol_name == "Add"]
    assert {e.target_symbol_name for e in add_edges} == {"helper", "ToUpper", "Println"}

    greet_edges = [e for e in result.edges if e.source_node.symbol_name == "Greet"]
    assert {e.target_symbol_name for e in greet_edges} == {"Sprintf", "helper"}

    init_edges = [e for e in result.edges if e.source_node.symbol_name == "init"]
    assert {e.target_symbol_name for e in init_edges} == {"helper"}


def test_go_imports_use_alias_when_present_else_the_package_path_tail() -> None:
    tree = _parse("go", "cmd/main.go", _GO_SOURCE)
    result = extract_graph("go", "cmd/main.go", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "fmt" in module_targets
    assert "s" in module_targets
    assert "strings" not in module_targets


def test_go_blank_import_produces_no_edge_target() -> None:
    # `import _ "pkg"` (side-effect-only) binds no identifier any code can
    # reference — falling through to the path-derived package name here
    # would manufacture an edge to a symbol nothing in the file can
    # actually call.
    source = b"""\
package main

import _ "encoding/json"

func main() {}
"""
    tree = _parse("go", "cmd/main.go", source)
    result = extract_graph("go", "cmd/main.go", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "json" not in module_targets
    assert module_targets == set()


def test_go_dot_import_produces_no_edge_target() -> None:
    # `import . "pkg"` binds every one of `pkg`'s exported identifiers
    # unqualified (`Join(...)`, never `strings.Join(...)`) — falling
    # through to the path-derived package name here would manufacture an
    # edge to a symbol nothing in the file can actually call that way.
    source = b"""\
package main

import . "strings"

func main() {}
"""
    tree = _parse("go", "cmd/main.go", source)
    result = extract_graph("go", "cmd/main.go", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "strings" not in module_targets
    assert module_targets == set()


def test_go_import_strips_major_version_suffix_from_path_derived_name() -> None:
    # Go's own module major-version-suffix convention: `.../bar/v2`
    # imports package `bar` (addressed in code as `bar.Func()`), not a
    # package literally named `v2`.
    source = b"""\
package main

import "example.com/foo/bar/v2"

func main() {
	bar.Func()
}
"""
    tree = _parse("go", "cmd/main.go", source)
    result = extract_graph("go", "cmd/main.go", tree)

    module_targets = _targets_from(result.edges, MODULE_SYMBOL_NAME)
    assert "bar" in module_targets
    assert "v2" not in module_targets


# ---------------------------------------------------------------------------
# GraphResult always carries the synthetic module node, even for a file
# with no definitions/calls/imports at all.
# ---------------------------------------------------------------------------


def test_module_node_always_present_even_with_no_definitions() -> None:
    tree = _parse("go", "cmd/empty.go", b"package main\n")
    result = extract_graph("go", "cmd/empty.go", tree)

    assert [n.symbol_name for n in result.nodes] == [MODULE_SYMBOL_NAME]
    assert result.edges == ()


# ---------------------------------------------------------------------------
# Reuses the chunker's own parsed tree — no second parse, per this module's own
# "Why next" note.
# ---------------------------------------------------------------------------


def test_extract_graph_reuses_chunk_files_own_parsed_tree() -> None:
    chunk_result = chunk_file("go", "cmd/main.go", _GO_SOURCE)

    result = extract_graph("go", "cmd/main.go", chunk_result.tree)

    assert any(node.symbol_name == "Add" for node in result.nodes)


# ---------------------------------------------------------------------------
# US10 — a malformed input raises a typed error, never swallowed here.
# ---------------------------------------------------------------------------


def test_unsupported_language_raises_graph_extraction_error() -> None:
    tree = _parse("go", "cmd/main.go", _GO_SOURCE)

    with pytest.raises(GraphExtractionError) as excinfo:
        extract_graph(cast(ChunkLanguage, "rust"), "cmd/main.go", tree)

    assert excinfo.value.file_path == "cmd/main.go"
    assert excinfo.value.language == "rust"


def test_graph_extraction_error_is_never_swallowed_by_extract_graph() -> None:
    tree = _parse("php", "src/bad.php", _PHP_SOURCE)

    with pytest.raises(GraphExtractionError):
        extract_graph(cast(ChunkLanguage, "rust"), "src/bad.php", tree)


# ---------------------------------------------------------------------------
# Model shape
# ---------------------------------------------------------------------------


def test_graph_node_defaults_repo_id_to_none() -> None:
    tree = _parse("go", "cmd/main.go", _GO_SOURCE)
    result = extract_graph("go", "cmd/main.go", tree)

    assert all(node.repo_id is None for node in result.nodes)


def test_graph_node_is_frozen() -> None:
    node = GraphNode(file_path="a.go", symbol_name="Foo", language="go")
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own frozen-model error type
        node.symbol_name = "Bar"  # type: ignore[misc]


def test_graph_edge_is_frozen() -> None:
    node = GraphNode(file_path="a.go", symbol_name="Foo", language="go")
    edge = GraphEdge(source_node=node, target_symbol_name="Bar")
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own frozen-model error type
        edge.target_symbol_name = "Baz"  # type: ignore[misc]


_PY_SOURCE = b"""\
import a.b.c
import x.y as z
from a.b import c, d as e
from . import rel
from .m import *

def helper():
    return 1

class Widget:
    def run(self, o):
        helper()
        o.save()
        o.x.deep()
        fns[0]()
"""


def test_python_definitions_become_nodes() -> None:
    result = extract_graph("python", "pkg/w.py", _parse("python", "pkg/w.py", _PY_SOURCE))

    assert {n.symbol_name for n in result.nodes} == {MODULE_SYMBOL_NAME, "helper", "Widget", "run"}


def test_python_calls_use_identifier_or_attribute_name() -> None:
    result = extract_graph("python", "pkg/w.py", _parse("python", "pkg/w.py", _PY_SOURCE))

    assert _targets_from(result.edges, "run") == {"helper", "save", "deep"}


def test_python_imports_use_real_names_never_aliases() -> None:
    result = extract_graph("python", "pkg/w.py", _parse("python", "pkg/w.py", _PY_SOURCE))

    assert _targets_from(result.edges, MODULE_SYMBOL_NAME) == {"c", "y", "b", "d", "rel", "m"}
