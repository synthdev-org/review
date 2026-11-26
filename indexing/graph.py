"""indexing.graph — the call/import graph extractor (US5).

Turns a single file's already-parsed Tree-sitter ``Tree`` (``ChunkResult.tree`` —
never a second parse of the same file) into a ``GraphResult``: one ``GraphNode`` per
function/method/class/interface/type declaration found (per
``rag-techniques.md``'s call/import-graph rationale), and one ``GraphEdge``
per call expression or import/use statement whose target is a syntactically
resolvable name.

**No LLM involved, per ADR-0007** and no per-language module-resolution
engine, per ``spec-indexing.md``'s own "name-based heuristic matching"
decision (no Go module-graph resolution, no Composer autoload resolution,
no TypeScript tsconfig path-alias resolution) — an edge's
``target_symbol_name`` is exactly the identifier text a call/import
expression names, nothing more.

``extract_graph`` is pure (no I/O, no logging): it walks ``tree`` and
returns a ``GraphResult``, raising ``GraphExtractionError`` on failure —
never caught here, per architecture.md's exception-type rule; the
pipeline applies US10's per-file isolation across the whole per-file chain,
exactly the way it already does for ``ChunkParseError``.

**What "matching" means in this module, decided explicitly rather than left
ambiguous (``spec-indexing.md``'s Unit E
traceability entry calls this "pure, no I/O" and requires the
Given/When/Then's "no match is not an error, just no edge created" clause
to hold as a *unit*-tested behavior — a real, load-bearing design decision,
not a stylistic one):** this pure, single-file function has no visibility
into any other file's or the repo's stored symbol table (`extract_graph`
takes only ``language``/``path``/``tree`` — no repository dependency, per
``spec-indexing.md``'s Unit E: "Depends on: Unit C's parse
trees" — nothing else). Checking whether a call/import target *actually
matches* some other node's symbol name — same-file or repo-wide — is
therefore not a check this function can perform today; the "repo's own
indexed symbols" `spec-indexing.md`'s in-scope bullet describes are, by
construction, only knowable once the index storage exists, and that
existence-check is deferred to a future consumer's query (per US7's own
text: a dangling edge "is resolved as 'no match' by any future query the
same way a never-matched edge is per US5" — i.e. existence-matching is a
*query-time* concept for both stories, not a write-time filter). What
*this* function's "matching" instead means, and what genuinely is pure,
no-I/O, unit-testable logic: recognizing which call/import expressions
have a *syntactically resolvable* target name at all (a plain identifier,
a `obj.method()`/`$obj->method()`/`pkg.Func()` member reference, a named
import specifier) versus ones that don't (a dynamic/computed call target,
a wildcard `import * as X` with no single bound symbol, a call through an
expression with no fixed name) — the latter produce **no edge**, which is
the concrete, testable shape "no match is not an error, just no edge
created" takes here, using each unit test's own fixture source as
the "in-memory symbol set". Every
syntactically-resolvable target becomes a **candidate** edge unconditionally, regardless of
whether that name happens to match any node (in this file or otherwise);
the storage schema is deliberately built for exactly this (a plain,
non-FK ``target_symbol_name`` column, explicitly "per US5/US7's dangling
reference design").

**Accepted limitation, named rather than left implicit:** an import/call
expression that has no enclosing function/method definition (a module-level
`initialize();` call, or any import statement — imports are always
module-level in all three languages) is attributed to a synthetic
per-file "module" node (``MODULE_SYMBOL_NAME``, always present in
``GraphResult.nodes``) rather than dropped — `index_graph_edge`'s
``source_node_id`` is a real FK, so every edge needs a real node
to hang off, and a file's own top-level scope is exactly that node for
import statements, which have no other natural owner. PHP's
``object_creation_expression`` (`new Widget()`) is **not** treated as a
call — out of scope here ("call expressions and import
statements" only), a real, symmetric,
consciously accepted limitation like ``chunk.py``'s similarly-named gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from tree_sitter import Node, Tree

# `_is_function_valued_variable_declarator`/`_symbol_name` (imported as
# `_definition_symbol_name` below for readability at its call site) are
# imported, not duplicated, unlike the structural definition-type sets
# below — both are pure, no-I/O Tree-sitter node predicates with no
# chunk.py-specific state, both modules need the identical grammar-level
# resolution, and both live in this same package, so this is a straight
# reuse, not a second copy to keep in sync by hand (see this module's own
# structural-type-sets comment below for the opposite call: those *are*
# duplicated, since chunking's and graph-extraction's own type sets
# genuinely diverge — every definition here becomes its own node,
# container and member alike, unlike chunk.py's own container/member
# split).
from indexing.chunk import ChunkLanguage, _is_function_valued_variable_declarator
from indexing.chunk import _symbol_name as _definition_symbol_name

# The synthetic node representing a file's own top-level/module scope —
# see this module's own "Accepted limitation" docstring section. Not a
# legal identifier in any of the three supported languages, so it cannot
# collide with a real definition's symbol name.
MODULE_SYMBOL_NAME = "<module>"


class GraphExtractionError(Exception):
    """Raised by ``extract_graph`` when ``tree`` cannot be turned into a
    graph — an internal tree-walk failure, or a ``language`` this module
    has no per-language type mapping for (a genuine config/consistency
    bug, e.g. ``ChunkLanguage`` gaining a value this module wasn't updated
    for). Never caught inside this module, per architecture.md's
    exception-type rule; the pipeline is the one place US10's
    per-file isolation is applied, exactly like ``ChunkParseError``.
    """

    def __init__(self, *, file_path: str, language: str, detail: str) -> None:
        super().__init__(f"{file_path}: could not extract graph ({language}): {detail}")
        self.file_path = file_path
        self.language = language
        self.detail = detail


class GraphNode(BaseModel):
    """One function/method/class/interface/type-declaration definition
    found in a file, per US5 — including the synthetic per-file module
    node (``symbol_name == MODULE_SYMBOL_NAME``) every ``GraphResult``
    carries alongside its real definitions.

    ``repo_id`` is deliberately not populated by ``extract_graph`` itself,
    the same way ``Chunk.repo_id`` isn't populated by ``chunk_file``, and
    stays ``None`` end to end — the pipeline passes the job's own
    ``repo_id`` straight through to ``IndexRepository.upsert_file`` as an
    explicit argument instead of setting it here (see
    ``indexing/repository.py``'s own docstring for why).
    """

    model_config = ConfigDict(frozen=True)

    repo_id: int | None = None
    file_path: str
    symbol_name: str
    language: ChunkLanguage


class GraphEdge(BaseModel):
    """One candidate call/import reference from ``source_node`` to
    whatever (if anything) is named ``target_symbol_name``, per US5 — see
    this module's own docstring for what "candidate" and "matching" mean
    in this module specifically.
    """

    model_config = ConfigDict(frozen=True)

    source_node: GraphNode
    target_symbol_name: str
    is_import: bool = False  # from an import/use statement, not a call expression


class GraphResult(BaseModel):
    """``extract_graph``'s return value: every definition found in the
    file (always including the synthetic module node) and every candidate
    edge extracted from its call/import expressions.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


# ---------------------------------------------------------------------------
# Structural definition node types, per language — deliberately duplicated
# from (not imported from) ``indexing/chunk.py``'s own leaf/container type
# sets: architecture.md's pipeline-stage rule ("no stage reaching into
# another stage's internals") applies at the module level within this
# package the same way it already applies across packages —
# chunking and graph-extraction are two distinct pipeline stages sharing a
# package, not one stage's internals reused by the other. Unlike
# ``chunk.py``'s split (a container becomes its own chunk only when it has
# no inner leaf member), every definition here — container *and* member —
# becomes its own ``GraphNode``: a call graph needs both "this method is
# called" and "this class is referenced" to be independently representable.
# ---------------------------------------------------------------------------

_TS_DEFINITION_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
        "type_alias_declaration",
        "enum_declaration",
        "class_declaration",
        "interface_declaration",
    }
)

_DEFINITION_TYPES: dict[ChunkLanguage, frozenset[str]] = {
    "typescript": _TS_DEFINITION_TYPES,
    "tsx": _TS_DEFINITION_TYPES,
    "php": frozenset(
        {
            "function_definition",
            "method_declaration",
            "enum_declaration",
            "class_declaration",
            "interface_declaration",
            "trait_declaration",
        }
    ),
    # `type_declaration` is deliberately absent here — see `indexing.chunk`'s
    # own `_LEAF_TYPES["go"]` docstring comment on why it's special-cased in
    # `_walk` below instead of matched generically here.
    "go": frozenset({"function_declaration", "method_declaration"}),
    "python": frozenset({"function_definition", "class_definition"}),
}

# Call-expression node types, per language — see this module's own
# "Accepted limitation" docstring section for what's deliberately excluded
# (PHP's `object_creation_expression`).
_CALL_TYPES: dict[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset({"call_expression"}),
    "tsx": frozenset({"call_expression"}),
    "php": frozenset({"function_call_expression", "member_call_expression"}),
    "go": frozenset({"call_expression"}),
    "python": frozenset({"call"}),
}

# Import-expression node types, per language. Go's is intentionally the
# innermost `import_spec` node (not `import_declaration`) — a single
# parenthesized `import (...)` block holds one `import_spec` per imported
# package, and the tree-walk below finds each regardless of nesting depth
# either way; anchoring on `import_spec` directly means the target
# extraction below never needs to special-case the parenthesized-vs-bare
# `import "fmt"` grammar shapes separately.
_IMPORT_TYPES: dict[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset({"import_statement"}),
    "tsx": frozenset({"import_statement"}),
    "php": frozenset({"namespace_use_declaration"}),
    "go": frozenset({"import_spec"}),
    "python": frozenset({"import_statement", "import_from_statement"}),
}


@dataclass(frozen=True)
class _WalkState:
    """Everything the recursive tree-walk below needs, bundled once per
    ``extract_graph`` call so ``_walk`` itself only ever carries the two
    things that actually change per recursive step (the current AST
    ``Node`` and its enclosing ``GraphNode``)."""

    language: ChunkLanguage
    path: str
    definition_types: frozenset[str]
    call_types: frozenset[str]
    import_types: frozenset[str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]


def extract_graph(language: ChunkLanguage, path: str, tree: Tree) -> GraphResult:
    """Extract every definition (as a ``GraphNode``) and every candidate
    call/import reference (as a ``GraphEdge``) from ``tree`` — one file's
    already-parsed Tree-sitter tree, at ``path``, in ``language``. See
    this module's own docstring for the full design. Raises
    ``GraphExtractionError`` on failure.
    """

    try:
        module_node = GraphNode(file_path=path, symbol_name=MODULE_SYMBOL_NAME, language=language)
        state = _WalkState(
            language=language,
            path=path,
            definition_types=_DEFINITION_TYPES[language],
            call_types=_CALL_TYPES[language],
            import_types=_IMPORT_TYPES[language],
            nodes=[module_node],
            edges=[],
        )

        _walk(tree.root_node, enclosing=module_node, state=state)
    except GraphExtractionError:
        raise
    except Exception as exc:
        raise GraphExtractionError(file_path=path, language=language, detail=str(exc)) from exc

    return GraphResult(nodes=tuple(state.nodes), edges=tuple(state.edges))


def _walk(node: Node, *, enclosing: GraphNode, state: _WalkState) -> None:
    for child in node.children:
        if state.language == "go" and child.type == "type_declaration":
            # One `type_declaration` can group multiple `type_spec`
            # children (`type (\n\tA struct{...}\n\tB struct{...}\n)`) —
            # see `indexing.chunk`'s own matching comment. Each becomes its
            # own node, keyed to its own `type_spec`, never one node
            # mis-labeled under only the first type's name.
            for spec in child.children:
                if spec.type != "type_spec":
                    continue
                symbol_name = _definition_symbol_name(spec)
                if symbol_name is not None:
                    definition_node = GraphNode(
                        file_path=state.path, symbol_name=symbol_name, language=state.language
                    )
                    state.nodes.append(definition_node)
                    _walk(spec, enclosing=definition_node, state=state)
            continue

        # `const Button = () => {...}` — see `indexing.chunk`'s own
        # docstring on why this counts as a function declaration, the same
        # as any `state.definition_types` match, even though it's bound
        # via a variable declarator rather than the `function` keyword.
        if child.type in state.definition_types or (
            state.language in ("typescript", "tsx")
            and child.type == "variable_declarator"
            and _is_function_valued_variable_declarator(child)
        ):
            symbol_name = _definition_symbol_name(child)
            if symbol_name is not None:
                definition_node = GraphNode(
                    file_path=state.path, symbol_name=symbol_name, language=state.language
                )
                state.nodes.append(definition_node)
                _walk(child, enclosing=definition_node, state=state)
                continue
            # Grammar edge case: a matched definition type with no
            # resolvable name (verified against the installed grammars,
            # no known real case for the type sets above) — walk its
            # children under the current enclosing scope rather than
            # dropping them, same defensive spirit as chunk.py's own
            # `_symbol_name` fallback.

        if child.type in state.call_types:
            target = _call_target(child, state.language)
            if target is not None:
                state.edges.append(GraphEdge(source_node=enclosing, target_symbol_name=target))
        elif child.type in state.import_types:
            for target in _import_targets(child, state.language):
                state.edges.append(
                    GraphEdge(source_node=enclosing, target_symbol_name=target, is_import=True)
                )

        _walk(child, enclosing=enclosing, state=state)


def _node_text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


def _call_target(node: Node, language: ChunkLanguage) -> str | None:
    if language in ("typescript", "tsx"):
        return _ts_call_target(node)
    if language == "php":
        return _php_call_target(node)
    if language == "python":
        return _python_call_target(node)
    return _go_call_target(node)


def _ts_call_target(node: Node) -> str | None:
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return _node_text(function_node)
    if function_node.type == "member_expression":
        property_node = function_node.child_by_field_name("property")
        if property_node is not None and property_node.type in (
            "property_identifier",
            "private_property_identifier",
        ):
            return _node_text(property_node)
    return None


def _php_call_target(node: Node) -> str | None:
    if node.type == "function_call_expression":
        function_node = node.child_by_field_name("function")
        if function_node is not None and function_node.type == "name":
            return _node_text(function_node)
        return None
    if node.type == "member_call_expression":
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.type == "name":
            return _node_text(name_node)
        return None
    return None


def _python_call_target(node: Node) -> str | None:
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return _node_text(function_node)
    if function_node.type == "attribute":
        attribute_node = function_node.child_by_field_name("attribute")
        if attribute_node is not None:
            return _node_text(attribute_node)
    return None


def _go_call_target(node: Node) -> str | None:
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return _node_text(function_node)
    if function_node.type == "selector_expression":
        field_node = function_node.child_by_field_name("field")
        if field_node is not None and field_node.type == "field_identifier":
            return _node_text(field_node)
    return None


def _import_targets(node: Node, language: ChunkLanguage) -> list[str]:
    if language in ("typescript", "tsx"):
        return _ts_import_targets(node)
    if language == "php":
        return _php_import_targets(node)
    if language == "python":
        return _python_import_targets(node)
    return _go_import_target(node)


def _ts_import_targets(node: Node) -> list[str]:
    clause = next((c for c in node.children if c.type == "import_clause"), None)
    if clause is None:
        return []

    targets: list[str] = []
    for child in clause.children:
        if child.type == "identifier":
            # `import Foo from "./foo"` — a default import's local binding
            # name, heuristically treated as the target symbol name (no
            # module-resolution engine, per spec-indexing.md's decision).
            targets.append(_node_text(child))
        elif child.type == "named_imports":
            for specifier in child.children:
                if specifier.type != "import_specifier":
                    continue
                # `import { Foo as Bar } from "./mod"` resolves to the real
                # exported name `Foo`, never the local alias `Bar` — the
                # same "matching happens against the real symbol name, not
                # the local alias" choice `_php_use_clause_target` already
                # makes for PHP's own `use X as Y` aliasing (see that
                # function's own call site/test for why: `Bar` is a
                # binding local to this file, meaningless as a target for
                # matching against some other file's own real definition
                # node). Go's `_go_import_target` looks like a
                # counter-example but isn't: a Go import alias becomes the
                # actual call-site qualifier everywhere in the file
                # (`s.Trim()` for `import s "strings"`), so the alias *is*
                # the only name any call in this file could ever reference
                # a package member through — unlike a TS/PHP named import,
                # whose call sites already resolve to whichever bare local
                # name is in scope regardless of what this edge records.
                name_node = specifier.child_by_field_name("name")
                if name_node is not None:
                    targets.append(_node_text(name_node))
        # `namespace_import` (`import * as Bar from "./bar"`) binds the
        # whole module, not one named symbol — no resolvable target, per
        # this module's own "no match is not an error" design.
    return targets


def _php_use_clause_target(clause: Node) -> str | None:
    name_node: Node | None = None
    for child in clause.children:
        if child.type == "qualified_name":
            segments = [gc for gc in child.children if gc.type == "name"]
            if segments:
                name_node = segments[-1]
        elif child.type == "name" and name_node is None:
            # A bare `use Foo;` clause's own name, or (only reached if no
            # `qualified_name` sibling precedes it) an alias — the
            # `qualified_name` branch above always wins when both are
            # present, since it appears first in the grammar's child
            # order.
            name_node = child

    return _node_text(name_node) if name_node is not None else None


def _php_import_targets(node: Node) -> list[str]:
    targets: list[str] = []
    for child in node.children:
        if child.type == "namespace_use_clause":
            target = _php_use_clause_target(child)
            if target is not None:
                targets.append(target)
        elif child.type == "namespace_use_group":
            # `use App\Models\{Foo, Bar as B};` — a grouped-use import
            # nests its own `namespace_use_clause` children inside this
            # `namespace_use_group` node, one level deeper than the flat
            # `use Foo;`/`use App\Foo;` case above.
            for grouped_clause in child.children:
                if grouped_clause.type != "namespace_use_clause":
                    continue
                target = _php_use_clause_target(grouped_clause)
                if target is not None:
                    targets.append(target)
    return targets


def _python_dotted_last(node: Node) -> str | None:
    # `a.b.c` -> "c"; an `aliased_import` resolves to its real name, never
    # the alias (same choice as TS/PHP above).
    if node.type == "aliased_import":
        inner = node.child_by_field_name("name")
        return _python_dotted_last(inner) if inner is not None else None
    if node.type != "dotted_name":
        return None
    parts = [c for c in node.children if c.type == "identifier"]
    return _node_text(parts[-1]) if parts else None


def _python_import_targets(node: Node) -> list[str]:
    names = [_python_dotted_last(n) for n in node.children_by_field_name("name")]
    targets = [n for n in names if n is not None]
    if node.type == "import_statement":
        return targets
    # `from a.b import c` also targets the module's own last segment; a
    # relative module (`.m`) holds its dotted_name inside `relative_import`.
    module = node.child_by_field_name("module_name")
    if module is not None:
        if module.type == "relative_import":
            module = next((c for c in module.children if c.type == "dotted_name"), None)
        module_target = _python_dotted_last(module) if module is not None else None
        if module_target is not None:
            targets.insert(0, module_target)
    return targets


_GO_MAJOR_VERSION_SUFFIX = re.compile(r"v[0-9]+")


def _go_import_target(node: Node) -> list[str]:
    # An explicit local alias (`import s "strings"`) is the name code
    # actually references, so it takes precedence over the path-derived
    # default package name (`strings`) when present.
    alias_node = node.child_by_field_name("name")
    if alias_node is not None and alias_node.type in ("blank_identifier", "dot"):
        # `import _ "pkg"` (side-effect-only) binds no identifier any code
        # can reference; `import . "pkg"` binds every one of its exported
        # identifiers unqualified (`Join(...)`, never `pkg.Join(...)`) —
        # neither leaves a `pkg`-qualified reference anywhere in this file,
        # so falling through to the path-derived name here would
        # manufacture a call-graph edge to a symbol name nothing in this
        # file could possibly call that way. No edge at all is the correct
        # result, the same "no match is not an error" design this module's
        # own docstring already states for unresolved call targets.
        return []
    if alias_node is not None and alias_node.type == "package_identifier":
        return [_node_text(alias_node)]

    path_node = node.child_by_field_name("path")
    if path_node is None:
        return []

    raw = _node_text(path_node).strip('"')
    if not raw:
        return []

    segments = raw.split("/")
    # Go's own module major-version-suffix convention (v2+): an import
    # path ending in a bare `/vN` segment names the package by the segment
    # *before* the suffix, not the suffix itself — `.../bar/v2` imports
    # package `bar`, addressed in code as `bar.Func()`, never `v2.Func()`.
    if len(segments) > 1 and _GO_MAJOR_VERSION_SUFFIX.fullmatch(segments[-1]):
        segments = segments[:-1]

    return [segments[-1]]
