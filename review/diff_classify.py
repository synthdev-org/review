"""Pure diff classification, risk scoring and input shrinking (no I/O).

``shrink_diff`` stubs data/fixture patches, collapses deletion-only hunks and
trims long prose, so the prompt carries less low-value text. The caller lists
every ``ShrinkNote`` as not-reviewed-in-detail; nothing is silently dropped.
Imports ``ingestion.diff`` only and never raises on odd input (no I/O at all).
"""

from __future__ import annotations

import posixpath
from collections import Counter
from fnmatch import fnmatch
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ingestion.diff import Diff, DiffFile

FileKind = Literal["source", "test", "docs", "data", "lockfile", "generated", "other"]
ShrinkReason = Literal["data", "deletion", "prose"]

_BASE_RISK: dict[FileKind, float] = {
    "source": 10.0,
    "test": 6.0,
    "other": 5.0,
    "docs": 3.0,
    "data": 1.0,
    "generated": 0.0,
    "lockfile": 0.0,
}
_LOCKFILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.sum",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "Pipfile.lock",
    }
)
_GENERATED_GLOBS = (
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
)
_GENERATED_SEGMENTS = frozenset({"vendor", "node_modules", "dist", "third_party"})
_DATA_SEGMENTS = frozenset(
    {
        "testdata",
        "fixtures",
        "cassettes",
        "__snapshots__",
        "snapshots",
        "golden",
        "baseline",
        "baselines",
    }
)
_DATA_EXTS = frozenset({".jsonl", ".ndjson", ".csv", ".tsv", ".snap", ".har"})
_DATA_UNDER_SEGMENT_EXTS = frozenset({".json", ".yaml", ".yml"})
_SIZED_DATA_EXTS = frozenset({".json", ".yaml", ".yml", ".toml"})
_CONFIG_GLOBS = ("package.json", "tsconfig*.json", "pyproject.toml", "*.schema.json")
_DOC_EXTS = frozenset({".md", ".rst", ".adoc", ".txt"})
_SOURCE_EXTS = frozenset(
    {
        ".py",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".kt",
        ".rb",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".php",
        ".swift",
        ".scala",
        ".sh",
        ".sql",
        ".vue",
    }
)
_TEST_SEGMENTS = frozenset({"tests", "test", "__tests__", "e2e"})
_DEFAULT_STUB_MIN = 8_000


def _is_test(path: str, base: str) -> bool:
    return (
        any(seg in _TEST_SEGMENTS for seg in path.split("/")[:-1])
        or base.startswith("test_")
        or base == "conftest.py"
        or any(fnmatch(base, g) for g in ("*_test.*", "*.test.*", "*.spec.*"))
    )


def _is_generated(base: str, segments: set[str]) -> bool:
    return bool(segments & _GENERATED_SEGMENTS) or any(fnmatch(base, g) for g in _GENERATED_GLOBS)


def _is_data(ext: str, segments: set[str]) -> bool:
    return ext in _DATA_EXTS or bool(segments & _DATA_SEGMENTS and ext in _DATA_UNDER_SEGMENT_EXTS)


def classify_path(path: str) -> FileKind:
    """Kind of a changed file from its path alone (size-based data is handled
    by ``shrink_diff``/``risk_score``, which also see the patch).
    """
    base = posixpath.basename(path)
    ext = posixpath.splitext(base)[1].lower()
    segments = set(path.split("/")[:-1])
    if base in _LOCKFILES:
        return "lockfile"
    if _is_generated(base, segments):
        return "generated"
    if _is_data(ext, segments):
        return "data"
    if _is_test(path, base):
        return "test"
    if ext in _DOC_EXTS:
        return "docs"
    return "source" if ext in _SOURCE_EXTS else "other"


def _patch_bytes(patch: str | None) -> int:
    return len(patch.encode("utf-8", "replace")) if patch else 0


def _kind(file: DiffFile, stub_min: int) -> FileKind:
    kind = classify_path(file.path)
    base = posixpath.basename(file.path)
    if (
        kind == "other"
        and posixpath.splitext(base)[1].lower() in _SIZED_DATA_EXTS
        and _patch_bytes(file.patch) > stub_min
        and not any(fnmatch(base, g) for g in _CONFIG_GLOBS)
    ):
        return "data"
    return kind


def _changes(patch: str) -> tuple[list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for line in patch.split("\n"):
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def is_move_only(patch: str | None) -> bool:
    """True when the added lines are the removed lines, reordered or moved."""
    if not patch:
        return False
    added, removed = _changes(patch)
    if not added:
        return False
    return Counter(s.strip() for s in added) == Counter(s.strip() for s in removed)


def risk_score(file: DiffFile) -> float:
    """0.0..11.0: file-kind base (x0.3 for a pure move) plus up to 1 for size of change."""
    base = _BASE_RISK[_kind(file, _DEFAULT_STUB_MIN)]
    if is_move_only(file.patch) or (file.status == "renamed" and not file.patch):
        base *= 0.3
    added, removed = _changes(file.patch or "")
    return base + min(len(added) + len(removed), 200) / 200


def risk_by_path(diff: Diff) -> dict[str, float]:
    return {f.path: risk_score(f) for f in diff.files}


class ShrinkConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    stub_data_min_bytes: int = 8_000
    collapse_deletion_min_lines: int = 20
    prose_max_bytes: int = 12_000
    prose_keep_bytes: int = 4_000
    prose_policy: Literal["trim", "keep"] = "trim"


class ShrinkNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    reason: ShrinkReason
    original_bytes: int
    kept_bytes: int


class ShrinkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    diff: Diff
    notes: tuple[ShrinkNote, ...]


def _collapse_deletions(patch: str, min_lines: int) -> str:
    """Replace each deletion-only hunk of ``min_lines``+ lines by its header and a note."""
    out: list[str] = []
    hunk: list[str] = []

    def flush() -> None:
        body = [ln for ln in hunk[1:] if not ln.startswith("\\")]
        if hunk and len(body) >= min_lines and all(ln.startswith("-") for ln in body):
            out.extend([hunk[0], f"(deleted {len(body)} lines; not shown)"])
        else:
            out.extend(hunk)
        hunk.clear()

    for line in patch.split("\n"):
        if line.startswith("@@ "):
            flush()
        if hunk or line.startswith("@@ "):
            hunk.append(line)
        else:
            out.append(line)
    flush()
    return "\n".join(out)


def _trim_prose(patch: str, keep_bytes: int) -> str:
    kept: list[str] = []
    used = 0
    for line in patch.split("\n"):
        used += len(line.encode("utf-8", "replace")) + 1
        if used > keep_bytes:
            break
        kept.append(line)
    omitted = _patch_bytes(patch) - _patch_bytes("\n".join(kept))
    return "\n".join([*kept, f"(prose truncated; {omitted} bytes not shown)"])


def _shrink_file(file: DiffFile, cfg: ShrinkConfig) -> tuple[DiffFile, ShrinkNote | None]:
    patch = file.patch
    if not patch:
        return file, None
    size = _patch_bytes(patch)
    kind = _kind(file, cfg.stub_data_min_bytes)
    reason: ShrinkReason
    if kind == "data" and size > cfg.stub_data_min_bytes:
        added, removed = _changes(patch)
        new, reason = f"(data file: +{len(added)} -{len(removed)} lines, {size} bytes)", "data"
    else:
        new, reason = _collapse_deletions(patch, cfg.collapse_deletion_min_lines), "deletion"
        if (
            kind == "docs"
            and cfg.prose_policy == "trim"
            and _patch_bytes(new) > cfg.prose_max_bytes
        ):
            new, reason = _trim_prose(new, cfg.prose_keep_bytes), "prose"
    if new == patch:
        return file, None
    note = ShrinkNote(
        path=file.path, reason=reason, original_bytes=size, kept_bytes=_patch_bytes(new)
    )
    return file.model_copy(update={"patch": new}), note


def shrink_diff(diff: Diff, config: ShrinkConfig | None = None) -> ShrinkResult:
    """Shrink ``diff``; ``result.diff is diff`` when nothing changed."""
    cfg = config or ShrinkConfig()
    files: list[DiffFile] = []
    notes: list[ShrinkNote] = []
    for file in diff.files:
        new_file, note = _shrink_file(file, cfg)
        files.append(new_file)
        if note is not None:
            notes.append(note)
    if not notes:
        return ShrinkResult(diff=diff, notes=())
    return ShrinkResult(diff=Diff(files=tuple(files)), notes=tuple(notes))


_REASON_TEXT: dict[ShrinkReason, str] = {
    "data": "data file stubbed",
    "deletion": "deleted lines collapsed",
    "prose": "prose truncated",
}


def render_shrink_note(note: ShrinkNote) -> str:
    return f"{note.path} ({_REASON_TEXT[note.reason]}; not reviewed in detail)"


__all__ = [
    "FileKind",
    "ShrinkConfig",
    "ShrinkNote",
    "ShrinkReason",
    "ShrinkResult",
    "classify_path",
    "is_move_only",
    "render_shrink_note",
    "risk_by_path",
    "risk_score",
    "shrink_diff",
]
