"""indexing.clone — the clone-detection fingerprint engine (Phase 10, Unit A),
per ``docs/specs/spec-clone-detection.md`` and
``docs/plans/plan-phase-10.md``'s Phase 1.

Pure, no I/O, zero infra imports (``docs/rules/architecture.md``): turns one
file's already-parsed Tree-sitter ``Tree`` into a *normalized token stream*,
samples that stream into *winnowing fingerprints* (Schleimer, Wilkerson &
Aiken, "Winnowing: Local Algorithms for Document Fingerprinting"), and later
matches two such streams back into verified clone regions. No LLM, network or
database is involved (ADR-0007).

**Normalization (Type-2 clones).** Comments and whitespace are dropped, every
identifier collapses to one token class, every literal (string, number,
template, regex …) collapses to another, and keywords, operators and
punctuation keep their text — so a copy with renamed variables or edited
constants still matches, while a change of structure does not. A *named* leaf
node whose type no table classifies raises ``CloneIndexError`` rather than
being silently normalized wrongly; ``test_clone.py``'s completeness test pins
the tables against the fixture corpus.

**Winnowing guarantee.** Gram size ``GRAM_TOKENS`` (25) and window ``WINDOW``
(76): any run of at least ``GUARANTEE_TOKENS`` (100) normalized tokens that
appears in two files shares at least one fingerprint, while only about
``2 / (WINDOW + 1)`` of positions are stored. The guarantee is baked into
stored data, so ``clone_detection_min_tokens`` can be *raised* freely but
never lowered below ``GUARANTEE_TOKENS`` without bumping
``CLONE_FINGERPRINT_VERSION`` and re-fingerprinting (plan Assumption 1).

**Collision safety (US-C9).** A window hash only nominates candidate
positions; ``match_regions`` extends and trims every candidate to equality of
the per-token hashes, so a window-hash collision can never yield a region.

**Determinism.** Every hash is derived with ``zlib.crc32`` and integer
arithmetic — never Python's per-process salted ``hash()`` — so values written
by one Worker are comparable by another (``test_clone.py`` pins golden
values).
"""

from __future__ import annotations

import fnmatch
import posixpath
import zlib
from bisect import bisect_right
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from tree_sitter import Node, Tree

from indexing.chunk import ChunkLanguage
from indexing.prefilter import is_indexable_path

GRAM_TOKENS = 25
"""Tokens per hashed gram (``K`` in the winnowing paper)."""

WINDOW = 76
"""Consecutive grams per winnowing window (``w`` in the paper)."""

GUARANTEE_TOKENS = GRAM_TOKENS + WINDOW - 1
"""Shortest duplicated run (100 tokens) the stored fingerprints guarantee to
find; also the lowest ``clone_detection_min_tokens`` the platform accepts."""

CLONE_FINGERPRINT_VERSION = 1
"""Bump when normalization, ``GRAM_TOKENS`` or ``WINDOW`` change: a repo whose
stored version is lower is re-fingerprinted by its next default-branch index
job (the backfill, plan Assumption 5)."""

_MODULUS = (1 << 61) - 1
_BASE = 0x9E3779B97F4A7C15 % _MODULUS
_BASE_POW = pow(_BASE, GRAM_TOKENS - 1, _MODULUS)

_IDENTIFIER_TOKEN = zlib.crc32(b"\x00ID")
_LITERAL_TOKEN = zlib.crc32(b"\x00LIT")


class CloneIndexError(Exception):
    """Raised by ``tokenize`` when a file cannot be turned into a token
    stream (an unclassified named leaf node, or an unexpected tree-walk
    failure). Never caught inside this module, per architecture.md's
    exception-type rule; the indexing pipeline isolates it per file and the
    review step isolates it per PR file.
    """

    def __init__(self, *, file_path: str, language: str, detail: str) -> None:
        super().__init__(
            f"{file_path}: could not tokenize for clone detection ({language}): {detail}"
        )
        self.file_path = file_path
        self.language = language
        self.detail = detail


# ---------------------------------------------------------------------------
# Per-grammar node-type tables — verified against the installed grammars'
# real node kinds and pinned by test_clone.py's completeness test (plan
# Assumption 16), not assumed from documentation.
# ---------------------------------------------------------------------------

_COMMENT_TYPES: Mapping[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset({"comment", "html_comment", "hash_bang_line"}),
    "tsx": frozenset({"comment", "html_comment", "hash_bang_line"}),
    "php": frozenset({"comment"}),
    "go": frozenset({"comment"}),
}

# Whole-node literals: emitted as ONE literal token and never descended into,
# so strings of different length or escape content still match (Type-2).
_ATOMIC_LITERAL_TYPES: Mapping[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset(
        {
            "string",
            "template_string",
            "template_literal_type",
            "regex",
            "number",
            "jsx_text",
            "html_character_reference",
        }
    ),
    "tsx": frozenset(
        {
            "string",
            "template_string",
            "template_literal_type",
            "regex",
            "number",
            "jsx_text",
            "html_character_reference",
        }
    ),
    "php": frozenset(
        {"string", "encapsed_string", "heredoc", "nowdoc", "integer", "float", "text"}
    ),
    "go": frozenset(
        {
            "interpreted_string_literal",
            "raw_string_literal",
            "int_literal",
            "float_literal",
            "imaginary_literal",
            "rune_literal",
        }
    ),
}

_IDENTIFIER_TYPES: Mapping[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset(
        {
            "identifier",
            "property_identifier",
            "type_identifier",
            "private_property_identifier",
            "shorthand_property_identifier",
            "shorthand_property_identifier_pattern",
            "statement_identifier",
        }
    ),
    "tsx": frozenset(
        {
            "identifier",
            "property_identifier",
            "type_identifier",
            "private_property_identifier",
            "shorthand_property_identifier",
            "shorthand_property_identifier_pattern",
            "statement_identifier",
        }
    ),
    "php": frozenset({"name"}),
    "go": frozenset(
        {
            "identifier",
            "field_identifier",
            "type_identifier",
            "package_identifier",
            "label_name",
            "blank_identifier",
        }
    ),
}

# Named leaf nodes that are keywords or fixed values: kept as their own text.
_KEEP_TEXT_LEAF_TYPES: Mapping[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset(
        {"this", "super", "true", "false", "null", "undefined", "predefined_type", "import"}
    ),
    "tsx": frozenset(
        {"this", "super", "true", "false", "null", "undefined", "predefined_type", "import"}
    ),
    "php": frozenset(
        {"boolean", "null", "primitive_type", "bottom_type", "cast_type", "php_tag", "php_end_tag"}
    ),
    "go": frozenset({"true", "false", "nil", "iota"}),
}

# Statements whose tokens are "just imports": a clone consisting only of these
# is boilerplate, not duplication (US-C8).
_IMPORT_TYPES: Mapping[ChunkLanguage, frozenset[str]] = {
    "typescript": frozenset({"import_statement"}),
    "tsx": frozenset({"import_statement"}),
    "php": frozenset(
        {
            "namespace_use_declaration",
            "require_expression",
            "require_once_expression",
            "include_expression",
            "include_once_expression",
        }
    ),
    "go": frozenset({"import_declaration"}),
}


@dataclass(frozen=True, slots=True)
class NormalizedTokens:
    """One file's normalized token stream. ``hashes`` are signed 32-bit
    per-token hashes (they fit a Postgres ``integer[]``); ``lines`` is each
    token's 1-based source line; ``in_import`` flags tokens inside an import
    statement. ``in_import`` is review-time only (it is not persisted: the
    stored side of a clone equals the query side token for token, so the
    query side's flags suffice).
    """

    hashes: tuple[int, ...]
    lines: tuple[int, ...]
    in_import: tuple[bool, ...]


class Fingerprint(BaseModel):
    """One sampled window hash: a signed-63-bit ``hash`` of the gram that
    starts at token ``token_offset``."""

    model_config = ConfigDict(frozen=True)

    hash: int
    token_offset: int


class CloneFileIndex(BaseModel):
    """Everything persisted per file: the per-token hashes (so a match can be
    verified without refetching code), the cumulative line map, and the
    sampled fingerprints. ``line_first_token[k]`` is the number of tokens that
    sit on lines ``1 .. k+1`` — i.e. the index of the first token of line
    ``k + 2`` — so token ``i`` lives on line
    ``bisect_right(line_first_token, i) + 1``.
    """

    model_config = ConfigDict(frozen=True)

    file_path: str
    language: str
    token_hashes: tuple[int, ...]
    line_first_token: tuple[int, ...]
    fingerprints: tuple[Fingerprint, ...]


class CloneRegion(BaseModel):
    """One verified duplicated run between a query file and a stored file.
    Token spans are half-open; line ranges are 1-based and inclusive."""

    model_config = ConfigDict(frozen=True)

    stored_file_path: str
    query_token_start: int
    query_token_end: int
    stored_token_start: int
    stored_token_end: int
    query_start_line: int
    query_end_line: int
    stored_start_line: int
    stored_end_line: int

    @property
    def token_count(self) -> int:
        return self.query_token_end - self.query_token_start

    @property
    def line_count(self) -> int:
        return self.query_end_line - self.query_start_line + 1


# ---------------------------------------------------------------------------
# Tokenizing
# ---------------------------------------------------------------------------


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= (1 << 31) else value


def tokenize(language: ChunkLanguage, path: str, tree: Tree) -> NormalizedTokens:
    """Walk ``tree``'s leaves and return the file's ``NormalizedTokens`` — see
    this module's docstring for the normalization. Raises ``CloneIndexError``
    on an unclassified named leaf or any unexpected failure."""

    comment_types = _COMMENT_TYPES[language]
    atomic_types = _ATOMIC_LITERAL_TYPES[language]
    identifier_types = _IDENTIFIER_TYPES[language]
    keep_text_types = _KEEP_TEXT_LEAF_TYPES[language]
    import_types = _IMPORT_TYPES[language]

    hashes: list[int] = []
    lines: list[int] = []
    in_import: list[bool] = []

    def emit(token_hash: int, node: Node, importing: bool) -> None:
        hashes.append(_signed32(token_hash))
        lines.append(node.start_point.row + 1)
        in_import.append(importing)

    try:
        stack: list[tuple[Node, bool]] = [(tree.root_node, False)]
        while stack:
            node, importing = stack.pop()
            node_type = node.type
            if node_type in comment_types:
                continue
            if node_type in atomic_types:
                emit(_LITERAL_TOKEN, node, importing)
                continue
            if node_type in identifier_types and node.child_count == 0:
                emit(_IDENTIFIER_TOKEN, node, importing)
                continue
            if node.child_count:
                child_importing = importing or node_type in import_types
                stack.extend((child, child_importing) for child in reversed(node.children))
                continue
            # A leaf that is neither a comment, a literal nor an identifier.
            if node.start_byte == node.end_byte:
                continue  # zero-width (an empty file's root, a missing/automatic token)
            if not node.is_named or node_type in keep_text_types:
                emit(zlib.crc32(node.text or b""), node, importing)
                continue
            raise CloneIndexError(
                file_path=path,
                language=language,
                detail=f"unclassified named leaf node type {node_type!r}",
            )
    except CloneIndexError:
        raise
    except Exception as exc:
        raise CloneIndexError(file_path=path, language=language, detail=str(exc)) from exc

    return NormalizedTokens(tuple(hashes), tuple(lines), tuple(in_import))


def _line_first_token(lines: Sequence[int]) -> tuple[int, ...]:
    """Cumulative token counts per line, for ``lines`` (non-decreasing)."""
    if not lines:
        return ()
    last_line = lines[-1]
    counts = [0] * last_line
    for line in lines:
        counts[line - 1] += 1
    running = 0
    cumulative: list[int] = []
    for count in counts:
        running += count
        cumulative.append(running)
    return tuple(cumulative)


# ---------------------------------------------------------------------------
# Winnowing
# ---------------------------------------------------------------------------


def _gram_hashes(token_hashes: Sequence[int]) -> list[int]:
    """Rolling polynomial hash of every ``GRAM_TOKENS``-token gram."""
    count = len(token_hashes) - GRAM_TOKENS + 1
    if count <= 0:
        return []
    values = [(token & 0xFFFFFFFF) + 1 for token in token_hashes]
    current = 0
    for value in values[:GRAM_TOKENS]:
        current = (current * _BASE + value) % _MODULUS
    grams = [current]
    for index in range(1, count):
        current = (current - values[index - 1] * _BASE_POW) % _MODULUS
        current = (current * _BASE + values[index + GRAM_TOKENS - 1]) % _MODULUS
        grams.append(current)
    return grams


def fingerprints(token_hashes: Sequence[int]) -> tuple[Fingerprint, ...]:
    """Winnow ``token_hashes`` into sampled fingerprints. A stream shorter than
    ``GUARANTEE_TOKENS`` yields none — it cannot contain a run the platform
    would report."""
    grams = _gram_hashes(token_hashes)
    if len(grams) < WINDOW:
        return ()
    selected: list[Fingerprint] = []
    last_index = -1
    window: deque[int] = deque()  # gram indices, hashes strictly increasing; rightmost min wins
    for index, gram in enumerate(grams):
        while window and grams[window[-1]] >= gram:
            window.pop()
        window.append(index)
        if window[0] <= index - WINDOW:
            window.popleft()
        if index >= WINDOW - 1:
            minimum = window[0]
            if minimum != last_index:
                selected.append(Fingerprint(hash=grams[minimum], token_offset=minimum))
                last_index = minimum
    return tuple(selected)


def build_clone_index(
    language: ChunkLanguage, path: str, tree: Tree
) -> tuple[CloneFileIndex, NormalizedTokens]:
    """Tokenize ``tree`` and build the persisted ``CloneFileIndex`` for it,
    returning the (non-persisted) ``NormalizedTokens`` too for callers that
    need ``in_import``. Raises ``CloneIndexError``."""
    tokens = tokenize(language, path, tree)
    index = CloneFileIndex(
        file_path=path,
        language=language,
        token_hashes=tokens.hashes,
        line_first_token=_line_first_token(tokens.lines),
        fingerprints=fingerprints(tokens.hashes),
    )
    return index, tokens


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _line_of(index: CloneFileIndex, token: int) -> int:
    return bisect_right(index.line_first_token, token) + 1


def match_regions(
    query: CloneFileIndex,
    stored: CloneFileIndex,
    *,
    min_tokens: int = GUARANTEE_TOKENS,
    min_lines: int = 10,
) -> tuple[CloneRegion, ...]:
    """Verified duplicated runs between ``query`` and ``stored``.

    Every shared-fingerprint position is a *candidate*: it is verified to
    ``GRAM_TOKENS`` of per-token equality (a window-hash collision dies here,
    US-C9), extended left and right while the per-token hashes stay equal and
    trimmed to exactly that equality, then kept only if the run reaches
    ``min_tokens`` tokens and ``min_lines`` query lines. Hits that fall inside
    an already-extended run on the same diagonal are skipped, so a long run is
    extended once, not once per fingerprint.
    """
    if not query.fingerprints or not stored.fingerprints:
        return ()

    query_tokens = query.token_hashes
    stored_tokens = stored.token_hashes
    stored_by_hash: dict[int, list[int]] = {}
    for fingerprint in stored.fingerprints:
        stored_by_hash.setdefault(fingerprint.hash, []).append(fingerprint.token_offset)

    covered_until: dict[int, int] = {}  # diagonal (query - stored offset) -> query end
    spans: list[tuple[int, int, int, int]] = []
    for fingerprint in sorted(query.fingerprints, key=lambda item: item.token_offset):
        query_offset = fingerprint.token_offset
        for stored_offset in stored_by_hash.get(fingerprint.hash, ()):
            diagonal = query_offset - stored_offset
            if covered_until.get(diagonal, -1) >= query_offset + GRAM_TOKENS:
                continue
            if (
                query_tokens[query_offset : query_offset + GRAM_TOKENS]
                != stored_tokens[stored_offset : stored_offset + GRAM_TOKENS]
            ):
                continue  # window-hash collision: tokens differ
            left = 0
            while (
                query_offset - left > 0
                and stored_offset - left > 0
                and query_tokens[query_offset - left - 1] == stored_tokens[stored_offset - left - 1]
            ):
                left += 1
            right = GRAM_TOKENS
            while (
                query_offset + right < len(query_tokens)
                and stored_offset + right < len(stored_tokens)
                and query_tokens[query_offset + right] == stored_tokens[stored_offset + right]
            ):
                right += 1
            query_start, query_end = query_offset - left, query_offset + right
            stored_start, stored_end = stored_offset - left, stored_offset + right
            covered_until[diagonal] = max(covered_until.get(diagonal, -1), query_end)
            if query_end - query_start < min_tokens:
                continue
            spans.append((query_start, query_end, stored_start, stored_end))

    regions: list[CloneRegion] = []
    for query_start, query_end, stored_start, stored_end in _merge_spans(spans):
        region = CloneRegion(
            stored_file_path=stored.file_path,
            query_token_start=query_start,
            query_token_end=query_end,
            stored_token_start=stored_start,
            stored_token_end=stored_end,
            query_start_line=_line_of(query, query_start),
            query_end_line=_line_of(query, query_end - 1),
            stored_start_line=_line_of(stored, stored_start),
            stored_end_line=_line_of(stored, stored_end - 1),
        )
        if region.line_count >= min_lines:
            regions.append(region)
    return tuple(regions)


def _merge_spans(spans: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Union spans ``(query_start, query_end, stored_start, stored_end)`` that
    overlap or touch on *both* sides, to a fixed point, sorted by start. A
    self-similar file otherwise yields one region per diagonal (US-C8)."""
    merged = sorted(spans)
    changed = True
    while changed:
        changed = False
        result: list[tuple[int, int, int, int]] = []
        for qs, qe, ss, se in merged:
            for position, (mqs, mqe, mss, mse) in enumerate(result):
                if qs <= mqe and mqs <= qe and ss <= mse and mss <= se:
                    result[position] = (min(qs, mqs), max(qe, mqe), min(ss, mss), max(se, mse))
                    changed = True
                    break
            else:
                result.append((qs, qe, ss, se))
        merged = sorted(result)
    return merged


def region_is_import_only(tokens: NormalizedTokens, start: int, end: int) -> bool:
    """Whether every token in ``[start, end)`` sits inside an import / use /
    require statement — boilerplate, not duplication (US-C8)."""
    return end > start and all(tokens.in_import[start:end])


# ---------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------

_TEST_DIRECTORIES = frozenset({"test", "tests", "__tests__"})
_TEST_FILE_PATTERNS = ("*_test.go", "*.test.*", "*.spec.*")


def is_test_path(path: str) -> bool:
    """Whether ``path`` is a test file (``*_test.go``, ``*.test.*``,
    ``*.spec.*``, or under a ``test/``, ``tests/`` or ``__tests__/``
    directory) — never either side of a reported pair (US-C8)."""
    parts = path.split("/")
    if any(part in _TEST_DIRECTORIES for part in parts[:-1]):
        return True
    name = posixpath.basename(path)
    return any(fnmatch.fnmatch(name, pattern) for pattern in _TEST_FILE_PATTERNS)


def is_excluded_path(path: str) -> bool:
    """Whether ``path`` may take part in clone detection at all: it must be an
    indexable path (in-scope language, not generated / vendored / a lockfile —
    ``indexing.prefilter.is_indexable_path``) and not a test file."""
    return not is_indexable_path(path) or is_test_path(path)
