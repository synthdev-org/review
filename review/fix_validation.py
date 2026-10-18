"""review.fix_validation — pure validation of a generated single-range fix
(docs/specs/spec-auto-fix.md US-AF4, AF5, AF8): range bounds, in-memory patch
application, replacement text rules and the syntax gate. No I/O, no infra
imports; a helper of the ``fix_generation`` step with no ``Step`` of its own.

**The syntax gate's language map is local on purpose.**
``indexing.chunk.language_for_path`` governs *indexing* scope and must not
start covering JavaScript, so this module maps the same extension sets
``review.file_matchers`` routes analyzers by. JavaScript parses with the
``tsx`` grammar (a superset, so it may accept some invalid JavaScript; the
originating analyzer's re-analysis, which parses as JavaScript, catches
that). Python uses the stdlib parser, which gives no error count, so there
the original must parse and the patched file must parse.
"""

from __future__ import annotations

import ast
import posixpath
import re
from collections.abc import Sequence
from enum import StrEnum

from findings.models import Suggestion
from indexing.chunk import ChunkLanguage, parse_file


class RejectionReason(StrEnum):
    """Why a fix was not shown. The values are the closed label set of the
    ``review.fix_generation`` outcome metric (docs/plans/plan-phase-11.md
    Assumption 13); the step adds ``accepted`` alongside them."""

    SYNTAX_ERROR = "syntax_error"
    REANALYSIS_FAILED = "reanalysis_failed"
    RUBRIC_FAILED = "rubric_failed"
    NOOP = "noop"
    REDACTION = "redaction"
    OVERSIZED = "oversized"
    OUT_OF_RANGE = "out_of_range"
    INVALID_TEXT = "invalid_text"
    OVERLAP = "overlap"
    PARSE_ERROR = "parse_error"
    TIMEOUT = "timeout"
    FETCH_FAILED = "fetch_failed"


_TREE_SITTER_EXTENSIONS: dict[str, ChunkLanguage] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "tsx",
    ".jsx": "tsx",
    ".mjs": "tsx",
    ".cjs": "tsx",
    ".php": "php",
    ".go": "go",
}
_PYTHON_EXTENSION = ".py"


def _extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def is_supported_path(path: str) -> bool:
    """Whether the syntax gate can judge ``path`` (otherwise it is never a fix
    candidate)."""
    extension = _extension(path)
    return extension == _PYTHON_EXTENSION or extension in _TREE_SITTER_EXTENSIONS


def check_range(
    suggestion: Suggestion,
    finding_line: int,
    hunks: Sequence[range],
    max_range_lines: int,
) -> RejectionReason | None:
    """US-AF4: ordered, within the size cap, containing the finding's line,
    and lying wholly inside one hunk's new-file lines."""
    start, end = suggestion.start_line, suggestion.end_line
    if start < 1 or start > end or end - start + 1 > max_range_lines:
        return RejectionReason.OUT_OF_RANGE
    if not start <= finding_line <= end:
        return RejectionReason.OUT_OF_RANGE
    if not any(hunk.start <= start and end < hunk.stop for hunk in hunks):
        return RejectionReason.OUT_OF_RANGE
    return None


def _replacement_text(replacement: str) -> str:
    """The exact line text a replacement stands for: empty is a deletion, else
    the lines with exactly one terminating newline (matching how
    ``findings.markdown.suggestion_block`` renders it)."""
    if not replacement:
        return ""
    return replacement if replacement.endswith("\n") else replacement + "\n"


def split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only (keeping it), as GitHub numbers lines; ``str.splitlines``
    would also split on form feed, U+2028 and friends."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def range_text(content: str, suggestion: Suggestion) -> str:
    """The original text of the suggestion's line range (with line endings)."""
    lines = split_lines(content)
    return "".join(lines[suggestion.start_line - 1 : suggestion.end_line])


def apply_suggestion(content: str, suggestion: Suggestion) -> str:
    """Patch ``content`` in memory. The range must lie within the file."""
    lines = split_lines(content)
    if suggestion.start_line < 1 or suggestion.end_line > len(lines):
        raise ValueError(
            f"range {suggestion.start_line}-{suggestion.end_line} is outside a "
            f"{len(lines)}-line file"
        )
    new_text = _replacement_text(suggestion.replacement)
    # A range ending a file with no final newline keeps that property.
    if new_text and suggestion.end_line == len(lines) and not lines[-1].endswith("\n"):
        new_text = new_text[:-1]
    return (
        "".join(lines[: suggestion.start_line - 1])
        + new_text
        + "".join(lines[suggestion.end_line :])
    )


def check_replacement(
    original_range_text: str, replacement: str, max_lines: int
) -> RejectionReason | None:
    """US-AF8 text rules: no carriage return or NUL, within the size cap, and
    not equal to what is already there."""
    if "\r" in replacement or "\x00" in replacement:
        return RejectionReason.INVALID_TEXT
    if len(split_lines(replacement)) > max_lines:
        return RejectionReason.OVERSIZED
    if split_lines(_replacement_text(replacement)) == split_lines(original_range_text):
        return RejectionReason.NOOP
    return None


def syntax_error_count(path: str, text: str) -> int | None:
    """Number of syntax errors in ``text``: Tree-sitter ``ERROR``/``MISSING``
    nodes, or 0/1 for Python (parses or not). ``None`` for an unsupported
    path."""
    extension = _extension(path)
    if extension == _PYTHON_EXTENSION:
        try:
            ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return 1
        return 0
    language = _TREE_SITTER_EXTENSIONS.get(extension)
    if language is None:
        return None
    root = parse_file(language, path, text.encode("utf-8", errors="replace")).root_node
    if not root.has_error:
        return 0
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
        # A subtree without errors cannot contain any; skip it.
        stack.extend(child for child in node.children if child.has_error or child.is_missing)
    return count


def check_syntax(path: str, original: str, patched: str) -> RejectionReason | None:
    """US-AF5: the patch must not add syntax errors. Python additionally
    requires the original to parse (its parser cannot count errors); an
    unsupported path fails closed."""
    before = syntax_error_count(path, original)
    after = syntax_error_count(path, patched)
    if before is None or after is None:
        return RejectionReason.SYNTAX_ERROR
    if _extension(path) == _PYTHON_EXTENSION and before:
        return RejectionReason.SYNTAX_ERROR
    return RejectionReason.SYNTAX_ERROR if after > before else None
