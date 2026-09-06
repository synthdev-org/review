"""indexing.chunk — the AST-aware structural chunker (US3), per the plan's
Phase 3 section (Unit C).

Parses a single TypeScript/TSX, PHP, or Go file with Tree-sitter and splits
it into ``Chunk``s at real syntactic boundaries (function/method/class/
interface/type-declaration), per ``rag-techniques.md`` §1's cAST
rationale — never a naive fixed-line/character window. Each chunk is
re-packed toward the ~512-token/64-token-overlap budget (the plan's
Assumption 2's carried-forward default) and has its enclosing context
(surrounding class/interface signature, the file's own import statements)
re-attached, so a chunk read in isolation is still self-contained.

**No LLM involved, per ADR-0007** ("indexing stays LLM-free"): "context
re-attachment" here means literally splicing in the enclosing declaration's
own header text and the file's own import statement text — never an
LLM-generated summary (`rag-techniques.md` §1's "prefix fusion" variant,
which ADR-0007 explicitly defers).

``chunk_file`` is pure (no I/O, no logging — a domain package function
callable straight from a unit test): parses ``content`` and returns a
``ChunkResult`` carrying both the produced chunks *and* the parsed
Tree-sitter ``Tree`` together, since Phase 5's graph extraction
(``indexing/graph.py``) reuses this exact parse rather than re-parsing the
same file a second time per push. Raises ``ChunkParseError`` on failure —
never caught here, per architecture.md's exception-type rule; Phase 7's
pipeline applies US10's per-file isolation across the whole per-file chain,
not just this stage.

**Accepted limitation, named rather than left implicit:** only content
inside a matched declaration boundary becomes a chunk — top-level
statements outside any function/method/class/interface/type declaration
(a bare module-level `const X = 5` in TypeScript, a top-level `var`/`const`
block in Go, a loose top-level PHP statement) are not separately chunked.
This matches US3's own Given/When/Then, which names function/method/class/
interface/type-declaration boundaries specifically, not "every top-level
statement." A file with *no* matched declarations at all (e.g. a pure
constants/config file) still produces coverage: it falls back to
token-budget-packing the whole file as a single symbol-less unit, so a
valid file is never silently dropped from the index.
"""

from __future__ import annotations

import math
import posixpath
from dataclasses import dataclass
from typing import Literal

import tree_sitter_go as _tsgo
import tree_sitter_php as _tsphp
import tree_sitter_typescript as _tstypescript
from pydantic import BaseModel, ConfigDict
from tree_sitter import Language, Node, Parser, Tree

ChunkLanguage = Literal["typescript", "tsx", "php", "go"]

# Carried forward from the plan's Assumption 2 (spec-indexing.md's own
# "Open assumptions pending validation" #2) — not tuned against real
# retrieval quality yet, a starting default.
CHUNK_TOKEN_BUDGET = 512
CHUNK_TOKEN_OVERLAP = 64

# No tokenizer dependency exists yet at this phase (Phase 4 adds fastembed,
# which owns the real tokenizer for the embedding model) — token counts here
# are a deliberate, documented approximation (~4 characters/token, a common
# rule of thumb for English/code text), not the embedding model's own count.
# Re-packing against the real tokenizer, if the approximation ever proves too
# coarse, is a Phase 4+ refinement, not required for this phase's own
# Acceptance criteria.
_CHARS_PER_TOKEN_ESTIMATE = 4


class ChunkParseError(Exception):
    """Raised by ``chunk_file`` when ``content`` cannot be turned into
    chunks — invalid UTF-8 content, or an unexpected failure from the
    underlying Tree-sitter parse/tree-walk. Never caught inside this
    module, per architecture.md's exception-type rule; Phase 7's pipeline
    is the one place US10's per-file isolation is applied.
    """

    def __init__(self, *, file_path: str, language: str, detail: str) -> None:
        super().__init__(f"{file_path}: could not chunk ({language}): {detail}")
        self.file_path = file_path
        self.language = language
        self.detail = detail


class Chunk(BaseModel):
    """One self-contained, token-budget-packed unit of a file's content,
    per the plan's Phase 3 section.

    ``repo_id`` is deliberately not populated by ``chunk_file`` itself
    (that pure function only ever sees one file's ``content``, never a job's
    repo identity), and stays ``None`` end to end — Phase 7's pipeline
    passes the job's own ``repo_id`` straight through to
    ``IndexRepository.upsert_file`` as an explicit argument instead of
    setting it here (see ``indexing/repository.py``'s own docstring for
    why). ``path``/``language`` are different: those *are* populated, by
    ``chunk_file``'s own arguments of the same name.
    """

    model_config = ConfigDict(frozen=True)

    repo_id: int | None = None
    file_path: str
    language: ChunkLanguage
    symbol_name: str | None
    byte_range: tuple[int, int]
    line_range: tuple[int, int] | None = None
    """1-indexed ``(start_line, end_line)``, inclusive both ends — computed
    once, uniformly, by ``_line_range`` below from the chunk's own final
    ``byte_range`` against the *full* file content, per
    docs/plans/plan-deliver-context-builder.md's Assumption 5. ``None`` only
    for a ``Chunk`` constructed without it (e.g. directly in a test, or a
    row indexed before this field's migration and never re-indexed since —
    see ``adapter/storage/models.py``'s ``IndexChunkRow``) — every chunk
    ``chunk_file`` itself produces always populates this.
    """
    content: str
    chunk_index: int


class ChunkResult(BaseModel):
    """``chunk_file``'s return value — the produced chunks *and* the parsed
    Tree-sitter ``Tree``, carried together so Phase 5's graph extraction
    doesn't re-parse the same file a second time per push (see this
    module's own docstring).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    chunks: tuple[Chunk, ...]
    tree: Tree


# ---------------------------------------------------------------------------
# Language setup — one compiled ``Language`` per supported dialect, built
# once at import time (Tree-sitter's own recommended pattern; compiling a
# grammar is comparatively expensive, parsing with it is cheap).
# ---------------------------------------------------------------------------

_LANGUAGES: dict[ChunkLanguage, Language] = {
    "typescript": Language(_tstypescript.language_typescript()),
    "tsx": Language(_tstypescript.language_tsx()),
    "php": Language(_tsphp.language_php()),
    "go": Language(_tsgo.language()),
}

_EXTENSION_LANGUAGE: dict[str, ChunkLanguage] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
    ".go": "go",
}


def language_for_path(path: str) -> ChunkLanguage | None:
    """Map a file's extension to the ``ChunkLanguage`` ``chunk_file``
    expects, or ``None`` if the extension isn't in this phase's scope
    (``.ts``/``.tsx``/``.php``/``.go``, per US9). Shared by
    ``indexing/prefilter.py``'s extension filter and Phase 7's pipeline,
    which both need the same extension→language mapping.
    """

    _, extension = posixpath.splitext(path)
    return _EXTENSION_LANGUAGE.get(extension.lower())


# ---------------------------------------------------------------------------
# Structural boundary node types, per language — the Tree-sitter grammar
# node types that mark a function/method/class/interface/type-declaration
# boundary, per US3. Verified against the actual installed grammar
# versions' real parse trees, not assumed from documentation.
#
# "Leaf" types become their own chunk directly. "Container" types
# (class/interface/trait) are split by their own inner leaf members
# (methods) when they have any — the container's own header becomes each
# method's enclosing context (see ``_container_prefix``) rather than a
# separate whole-container chunk — and fall back to one whole-container
# chunk only when no inner leaf member exists (e.g. a fields-only
# interface).
# ---------------------------------------------------------------------------

_TS_LEAF_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
        "type_alias_declaration",
        "enum_declaration",
    }
)
_TS_CONTAINER_TYPES = frozenset({"class_declaration", "interface_declaration"})

_LEAF_TYPES: dict[ChunkLanguage, frozenset[str]] = {
    "typescript": _TS_LEAF_TYPES,
    "tsx": _TS_LEAF_TYPES,
    "php": frozenset({"function_definition", "method_declaration", "enum_declaration"}),
    # `type_declaration` is deliberately absent here — Go's own `type (...)`
    # syntax lets one `type_declaration` node group multiple `type_spec`
    # children (e.g. `type (\n\tA struct{...}\n\tB struct{...}\n)`), so it's
    # special-cased in `_collect_units` below to split into one unit per
    # `type_spec` instead of matching generically here (which would treat
    # an entire multi-type group as a single, mis-labeled unit).
    "go": frozenset({"function_declaration", "method_declaration"}),
}

_CONTAINER_TYPES: dict[ChunkLanguage, frozenset[str]] = {
    "typescript": _TS_CONTAINER_TYPES,
    "tsx": _TS_CONTAINER_TYPES,
    "php": frozenset({"class_declaration", "interface_declaration", "trait_declaration"}),
    "go": frozenset(),
}

# `const Button = (): string => {...}` / `const helper = function() {...}` —
# a function bound to a name via a variable declarator, not the `function`
# keyword — is a function declaration in every way that matters here (the
# idiomatic React-component/arrow-function-export style), so it's matched
# as its own leaf unit the same as `function_declaration` — never silently
# dropped from every chunk the way a genuinely non-function top-level
# `const X = 5` still is (this module's own "Accepted limitation" section).
_TS_FUNCTION_VALUE_TYPES = frozenset({"arrow_function", "function_expression"})


def _is_function_valued_variable_declarator(node: Node) -> bool:
    value = node.child_by_field_name("value")
    return value is not None and value.type in _TS_FUNCTION_VALUE_TYPES


_IMPORT_TYPES: dict[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset({"import_statement"}),
    "tsx": frozenset({"import_statement"}),
    "php": frozenset({"namespace_use_declaration"}),
    "go": frozenset({"import_declaration"}),
}


@dataclass(frozen=True)
class _Unit:
    """One chunkable declaration found by ``_collect_units``: the
    Tree-sitter node itself, its resolved symbol name (``None`` if the
    grammar exposes no ``name`` field for it), and the enclosing
    class/interface/trait node whose header should be re-attached as
    context (``None`` if this unit is already a whole container, or has no
    enclosing container at all)."""

    node: Node
    symbol_name: str | None
    enclosing: Node | None


def chunk_file(language: ChunkLanguage, path: str, content: bytes) -> ChunkResult:
    """Parse ``content`` (one file's raw bytes, at ``path``, in
    ``language``) and split it into ``Chunk``s at real syntactic
    boundaries — see this module's own docstring for the full design.
    Raises ``ChunkParseError`` on failure.
    """

    try:
        # Validated once, up front, so an invalid-UTF-8 file fails
        # deterministically here rather than deep inside tree-walking —
        # every subsequent decode in this module uses errors="replace" as a
        # defense-in-depth fallback only, not as the primary UTF-8 check.
        _ = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChunkParseError(
            file_path=path, language=language, detail=f"invalid UTF-8 content: {exc}"
        ) from exc

    try:
        parser = Parser(_LANGUAGES[language])
        tree = parser.parse(content)
        chunks = _build_chunks(tree.root_node, language=language, path=path, content=content)
    except ChunkParseError:
        raise
    except Exception as exc:
        raise ChunkParseError(file_path=path, language=language, detail=str(exc)) from exc

    return ChunkResult(chunks=chunks, tree=tree)


def _line_range(content: bytes, byte_range: tuple[int, int]) -> tuple[int, int]:
    """Computes a chunk's 1-indexed ``(start_line, end_line)`` by counting
    newlines in the *full* file ``content`` up to each end of its own
    already-correct, file-absolute ``byte_range`` — per
    docs/plans/plan-deliver-context-builder.md's Assumption 5, **not** by
    reusing any chunking-path-specific internal state (``Node.start_point``/
    ``end_point``, or ``_pack()``'s own per-window line-span bookkeeping):
    three prior attempts at exactly this computation were each
    independently found wrong by multiple reviewers across several rounds,
    because none of that internal state ever tracks the declaration's own
    starting *file* line, only offsets into the declaration's own extracted
    text.

    ``byte_range`` follows this module's existing Python-slice convention
    (``[start, end)``, exclusive end — see ``content[node.start_byte:node.end_byte]``
    elsewhere in this module), so ``byte_range[1]`` is one byte *past* the
    chunk's actual last included byte. Counting newlines up to
    ``byte_range[1] - 1`` (the real last included byte), not
    ``byte_range[1]``, avoids silently counting one extra line whenever that
    last byte is itself a trailing ``\\n`` — the normal case both for a
    ``_pack_whole_file()`` chunk of a file with a trailing newline
    (near-universal for real source files) and for every non-final window of
    a multi-window ``_pack()`` split (each window boundary lands on a line
    boundary).
    """

    start_line = content[: byte_range[0]].count(b"\n") + 1
    end_line = content[: byte_range[1] - 1].count(b"\n") + 1
    return start_line, end_line


def _build_chunks(
    root: Node, *, language: ChunkLanguage, path: str, content: bytes
) -> tuple[Chunk, ...]:
    units = _collect_units(root, language)
    if not units:
        return _pack_whole_file(language=language, path=path, content=content)

    imports_prefix = _collect_imports_prefix(root, language, content)

    chunks: list[Chunk] = []
    index = 0
    for unit in units:
        node = unit.node
        node_text = content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

        prefix = imports_prefix
        if unit.enclosing is not None:
            prefix += _container_prefix(unit.enclosing, content)

        for window_text, start_byte, end_byte in _pack(
            prefix, node_text, node.start_byte, CHUNK_TOKEN_BUDGET, CHUNK_TOKEN_OVERLAP
        ):
            chunks.append(
                Chunk(
                    file_path=path,
                    language=language,
                    symbol_name=unit.symbol_name,
                    byte_range=(start_byte, end_byte),
                    line_range=_line_range(content, (start_byte, end_byte)),
                    content=window_text,
                    chunk_index=index,
                )
            )
            index += 1

    return tuple(chunks)


def _pack_whole_file(*, language: ChunkLanguage, path: str, content: bytes) -> tuple[Chunk, ...]:
    """Fallback for a file with zero matched structural boundaries (see
    this module's own "Accepted limitation" docstring section): the whole
    file is packed as a single symbol-less unit, so a valid file is never
    silently dropped from the index."""

    text = content.decode("utf-8", errors="replace")
    if not text.strip():
        return ()

    chunks: list[Chunk] = []
    for index, (window_text, start_byte, end_byte) in enumerate(
        _pack("", text, 0, CHUNK_TOKEN_BUDGET, CHUNK_TOKEN_OVERLAP)
    ):
        chunks.append(
            Chunk(
                file_path=path,
                language=language,
                symbol_name=None,
                byte_range=(start_byte, end_byte),
                line_range=_line_range(content, (start_byte, end_byte)),
                content=window_text,
                chunk_index=index,
            )
        )
    return tuple(chunks)


def _collect_units(root: Node, language: ChunkLanguage) -> list[_Unit]:
    leaf_types = _LEAF_TYPES[language]
    container_types = _CONTAINER_TYPES[language]
    units: list[_Unit] = []

    def walk(node: Node, enclosing: Node | None) -> None:
        for child in node.children:
            if language == "go" and child.type == "type_declaration":
                # One `type_declaration` can group multiple `type_spec`
                # children (`type (\n\tA struct{...}\n\tB struct{...}\n)`) —
                # each becomes its own unit, keyed to its own `type_spec`
                # node (which has its own `name` field), never the whole
                # group mis-labeled under only its first type's name.
                for spec in child.children:
                    if spec.type != "type_spec":
                        continue
                    units.append(
                        _Unit(node=spec, symbol_name=_symbol_name(spec), enclosing=enclosing)
                    )
                continue
            if child.type in leaf_types or (
                language in ("typescript", "tsx")
                and child.type == "variable_declarator"
                and _is_function_valued_variable_declarator(child)
            ):
                units.append(
                    _Unit(node=child, symbol_name=_symbol_name(child), enclosing=enclosing)
                )
            elif child.type in container_types:
                found_before = len(units)
                walk(child, child)
                if len(units) == found_before:
                    # No inner leaf member (e.g. a fields-only interface) —
                    # the whole container becomes its own chunk instead.
                    units.append(_Unit(node=child, symbol_name=_symbol_name(child), enclosing=None))
            else:
                walk(child, enclosing)

    walk(root, None)
    return units


def _symbol_name(node: Node) -> str | None:
    # Go's own `type_spec` (never the outer, possibly multi-spec
    # `type_declaration` — see `_collect_units`'s own Go special-case) has
    # a `name` field directly, the same as every other matched node here.
    name_node = node.child_by_field_name("name")
    if name_node is not None and name_node.text is not None:
        return name_node.text.decode("utf-8", errors="replace")

    return None


def _container_prefix(container: Node, content: bytes) -> str:
    """The enclosing class/interface/trait's own header text (everything
    up to its body's opening brace), re-attached ahead of one of its
    member chunks so that chunk reads as self-contained in isolation, per
    US3's "enclosing context ... re-attached" requirement."""

    body = container.child_by_field_name("body")
    header_end = body.start_byte if body is not None else container.end_byte
    header_text = content[container.start_byte : header_end].decode("utf-8", errors="replace")
    return header_text.rstrip() + " {\n"


def _collect_imports_prefix(root: Node, language: ChunkLanguage, content: bytes) -> str:
    """Every top-level import/use statement's own raw text, re-attached
    ahead of each chunk, per US3's "relevant imports re-attached"
    requirement."""

    import_types = _IMPORT_TYPES[language]
    found: list[Node] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if child.type in import_types:
                found.append(child)
            else:
                walk(child)

    walk(root)
    if not found:
        return ""

    lines = [
        content[node.start_byte : node.end_byte].decode("utf-8", errors="replace") for node in found
    ]
    return "\n".join(lines) + "\n\n"


def _estimate_tokens(text: str) -> int:
    """A deliberate approximation — see this module's own
    ``_CHARS_PER_TOKEN_ESTIMATE`` docstring comment for why no real
    tokenizer is used at this phase."""

    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN_ESTIMATE))


def _pack(
    prefix: str, body: str, body_start_byte: int, budget: int, overlap: int
) -> list[tuple[str, int, int]]:
    """Pack ``body`` (one declaration's own text, starting at
    ``body_start_byte`` in the original file) into one or more windows,
    each within ``budget`` tokens once ``prefix`` (already-decided
    enclosing context) is accounted for, with ``overlap`` tokens of
    trailing context carried into the next window. Returns
    ``(full_chunk_text, start_byte, end_byte)`` tuples — ``start_byte``/
    ``end_byte`` describe ``body``'s own contribution to that window only
    (``prefix`` is synthesized context, not a byte range in the file)."""

    prefix_tokens = _estimate_tokens(prefix) if prefix else 0
    body_budget = max(budget - prefix_tokens, 1)

    if _estimate_tokens(body) <= body_budget:
        return [(prefix + body, body_start_byte, body_start_byte + len(body.encode("utf-8")))]

    lines = body.splitlines(keepends=True)
    line_byte_lengths = [len(line.encode("utf-8")) for line in lines]
    cumulative_bytes = [0]
    for length in line_byte_lengths:
        cumulative_bytes.append(cumulative_bytes[-1] + length)

    windows: list[tuple[str, int, int]] = []
    start = 0
    line_count = len(lines)
    while start < line_count:
        tokens = 0
        end = start
        while end < line_count:
            line_tokens = _estimate_tokens(lines[end])
            if tokens > 0 and tokens + line_tokens > body_budget:
                break
            tokens += line_tokens
            end += 1

        window_text = "".join(lines[start:end])
        window_start_byte = body_start_byte + cumulative_bytes[start]
        window_end_byte = body_start_byte + cumulative_bytes[end]
        windows.append((prefix + window_text, window_start_byte, window_end_byte))

        if end >= line_count:
            break

        # Unlike the forward-packing loop above, a candidate overlap line
        # that alone exceeds `overlap` is never force-included: skipping it
        # (falling back to zero overlap for this window transition) is
        # always a safe, valid outcome here — `start = max(back, start +
        # 1)` below already guarantees forward progress regardless of
        # whether any line was backed into the overlap — whereas the
        # forward-packing loop's own single-oversized-line case has no such
        # safe fallback (a window can't be empty).
        back = end
        overlap_tokens = 0
        while back > start:
            line_tokens = _estimate_tokens(lines[back - 1])
            if overlap_tokens + line_tokens > overlap:
                break
            overlap_tokens += line_tokens
            back -= 1
        start = max(back, start + 1)

    return windows
