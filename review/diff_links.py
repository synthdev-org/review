"""Builds name-based import/call links among a PR's changed files, for
``review.diff_batching``. Pure CPU, no I/O: the caller supplies file bytes and
wraps this in ``asyncio.to_thread``. Per ADR-0007 there is no module
resolution, so edges are noisy; generic and ambiguous names are filtered here.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from indexing.chunk import ChunkParseError, chunk_file, language_for_path
from indexing.graph import MODULE_SYMBOL_NAME, GraphExtractionError, extract_graph
from review.diff_batching import CALL_WEIGHT, IMPORT_WEIGHT

GENERIC_SYMBOL_NAMES: frozenset[str] = frozenset(
    {
        "main", "init", "__init__", "index", "get", "set", "run", "new", "add", "open",
        "close", "read", "write", "load", "save", "list", "update", "delete", "create",
        "start", "stop", "test", "setup", "handle", "apply", "call", "build", "parse",
        "print", "println", "len", "str", "error", "name", "type", "data", "value",
        "String", "Error", "Println", "Printf", "Sprintf", "Errorf", "Close", "Get", "Set",
    }
)  # fmt: skip
MIN_SYMBOL_NAME_LENGTH: int = 3
MAX_DEFINING_FILES: int = 3  # defined in more changed files = ambiguous, ignored

_PACKAGE_FILE_STEMS = frozenset({"__init__", "index"})


class FileLinksResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    links: dict[str, dict[str, float]]
    unparsed: tuple[str, ...]


def module_aliases(path: str) -> frozenset[str]:
    """Names other files may use to import ``path``: its stem, the parent
    directory for package entry files (``__init__``/``index``), and for Go
    (where a package is a directory) the parent directory name.
    """
    stem, ext = posixpath.splitext(posixpath.basename(path))
    parent = posixpath.basename(posixpath.dirname(path))
    aliases = {stem}
    if stem in _PACKAGE_FILE_STEMS or ext == ".go":
        aliases.add(parent)
    return frozenset(a for a in aliases if a)


def _parse_file(path: str, content: bytes) -> tuple[set[str], set[tuple[str, bool]]] | None:
    """(defined names, wanted (target, is_import)) or None if unparseable."""
    language = language_for_path(path)
    if language is None:
        return None
    try:
        graph = extract_graph(language, path, chunk_file(language, path, content).tree)
    except (ChunkParseError, GraphExtractionError):
        return None
    names = {n.symbol_name for n in graph.nodes if n.symbol_name != MODULE_SYMBOL_NAME}
    return names | module_aliases(path), {(e.target_symbol_name, e.is_import) for e in graph.edges}


def _link_usable(name: str, definers: set[str], defined_count: int) -> bool:
    return not (
        name in GENERIC_SYMBOL_NAMES
        or len(name) < MIN_SYMBOL_NAME_LENGTH
        or not definers
        or defined_count > MAX_DEFINING_FILES
    )


def build_file_links(contents: Mapping[str, bytes]) -> FileLinksResult:
    """Link changed files A -> B when A imports a module B defines
    (``IMPORT_WEIGHT``) or calls a symbol B defines (``CALL_WEIGHT``). A file
    that cannot be parsed is listed in ``unparsed`` and simply has no links.
    """
    defined: dict[str, set[str]] = {}  # name -> files defining it
    wanted: dict[str, set[tuple[str, bool]]] = {}  # file -> {(target, is_import)}
    unparsed: list[str] = []
    for path, content in contents.items():
        parsed = _parse_file(path, content)
        if parsed is None:
            unparsed.append(path)
            continue
        for name in parsed[0]:
            defined.setdefault(name, set()).add(path)
        wanted[path] = parsed[1]

    links: dict[str, dict[str, float]] = {}
    for src, targets in wanted.items():
        for name, is_import in targets:
            definers = defined.get(name, set()) - {src}
            if not _link_usable(name, definers, len(defined.get(name, ()))):
                continue
            weight = IMPORT_WEIGHT if is_import else CALL_WEIGHT
            for dst in definers:
                links.setdefault(src, {}).setdefault(dst, 0.0)
                links[src][dst] += weight
    return FileLinksResult(links=links, unparsed=tuple(unparsed))
