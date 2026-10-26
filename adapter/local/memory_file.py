"""adapter.local.memory_file — the repository memory file (US-L12, US-L13; ADR-0016 decision 4).

A dismissed finding stays dismissed because the repository says so, in a tracked file the developer
reviews like any other change. There is no database and no embedding: a finding is dismissed when
its fingerprint (``memory.fingerprint.build_finding_fingerprint``, the same text server mode embeds)
hashes to the fingerprint of an entry. Only model (``llm``) findings are checked: static-analysis
findings pass through verification unchanged (US-V6).

**Format** (default path ``.review/memory.jsonl``, set only by ``REVIEW_LOCAL_MEMORY_FILE``, never
by repository content): JSON Lines. One JSON object per line; blank lines and lines starting with
``#`` are ignored. Fields: ``file`` (path, or ``*`` for any file), ``rule_id`` (string or ``null``),
``message`` (exactly as review printed it, i.e. after secret redaction), optional ``note``. Unknown
fields are rejected. JSON Lines rather than YAML on purpose: the file is attacker-influenced in CI,
and a line-oriented format has no aliases, anchors, tags or nesting to abuse and gives exact line
numbers for errors.

**The parser is bounded**: size, line count, line length and a parse deadline are capped, and every
failure is a ``MemoryFileError`` naming the line (never its content, which may be a secret). A
malformed file is an error, never ignored.

**Where it is read from.** On a developer machine, the working tree (a symlink is refused). In CI,
from the **base** commit through git, never the pull-request head, so a PR cannot dismiss its own
findings (B5d).

**Proposals (US-L13)** are text only: ``propose_rules`` and ``render_proposal`` return a suggested
unified diff of the tracked file. Nothing in this module writes to the repository.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import stat
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adapter.local.exceptions import GitCommandError, MemoryFileError
from adapter.local.git import run_git
from findings.markdown import break_autolinks, sanitize_message, strip_display_text
from memory.fingerprint import build_finding_fingerprint

DEFAULT_MEMORY_FILE = ".review/memory.jsonl"
MEMORY_FILE_MAX_BYTES = 262_144
MEMORY_FILE_MAX_LINES = 2_000
MEMORY_FILE_MAX_LINE_CHARS = 5_000
PARSE_TIMEOUT_SECONDS = 2.0
WILDCARD_FILE = "*"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # absent on Windows

# US-L13: how many distinct files must carry the same dismissal (same rule id and message) before
# review suggests one repository-wide entry instead. Proposed value, to be confirmed by the owner
# (docs/adr/0016-local-mode-composition-root.md, Amendment: memory file).
PROPOSAL_MIN_DISMISSALS = 3


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(min_length=1, max_length=1024)
    rule_id: str | None = Field(default=None, max_length=256)
    message: str = Field(min_length=1, max_length=4000)
    note: str | None = Field(default=None, max_length=500)


@dataclass(frozen=True)
class SuppressedFinding:
    file: str
    rule_id: str | None
    message: str


@dataclass(frozen=True)
class LoadedMemoryFile:
    path: str
    text: str
    entries: tuple[MemoryEntry, ...]


def fingerprint_digest(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _entry_digest(entry: MemoryEntry) -> str:
    return fingerprint_digest(
        build_finding_fingerprint(file=entry.file, rule_id=entry.rule_id, message=entry.message)
    )


def _error(path: str, line: int | None, reason: str) -> MemoryFileError:
    return MemoryFileError(path=path, line=line, reason=reason)


def _field_name(loc: tuple[int | str, ...]) -> str:
    name = ".".join(str(part) for part in loc)
    return name if name in MemoryEntry.model_fields else "an unknown field"


def parse_memory_file(data: bytes, *, path: str) -> LoadedMemoryFile:
    """Parse ``data``; raises ``MemoryFileError`` (with the line number) if anything is wrong."""
    if len(data) > MEMORY_FILE_MAX_BYTES:
        raise _error(path, None, f"larger than the {MEMORY_FILE_MAX_BYTES}-byte limit")
    try:
        text = data.decode("utf-8-sig")  # tolerate the BOM Windows editors add
    except UnicodeDecodeError as exc:
        raise _error(path, data.count(b"\n", 0, exc.start) + 1, "not valid UTF-8") from exc
    lines = text.split("\n")
    if len(lines) > MEMORY_FILE_MAX_LINES:
        raise _error(path, None, f"more than the {MEMORY_FILE_MAX_LINES}-line limit")
    deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS
    entries: list[MemoryEntry] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if time.monotonic() > deadline:
            raise _error(path, number, "parsing took too long")
        if len(line) > MEMORY_FILE_MAX_LINE_CHARS:
            raise _error(path, number, f"longer than {MEMORY_FILE_MAX_LINE_CHARS} characters")
        try:
            value = json.loads(line)
        except (ValueError, RecursionError) as exc:
            reason = exc.msg if isinstance(exc, json.JSONDecodeError) else "too deeply nested"
            raise _error(path, number, f"not valid JSON ({reason})") from exc
        if not isinstance(value, dict):
            raise _error(path, number, "must be a JSON object")
        try:
            entries.append(MemoryEntry.model_validate(value))
        except ValidationError as exc:
            # Known field names and pydantic's rule text only: an unknown key is attacker- or
            # typo-supplied and could be a secret, and pydantic's input echo is not used.
            problems = "; ".join(f"{_field_name(e['loc'])}: {e['msg']}" for e in exc.errors())
            raise _error(path, number, problems) from exc
    return LoadedMemoryFile(path=path, text=text, entries=tuple(entries))


class FileMemoryMatcher:
    """``review.memory_suppression.FingerprintMatcher`` over the entries of one memory file.

    Remembers what it suppressed (``suppressed``) so the run can list it: a dismissal is never
    silent. A wildcard entry (``file`` of ``*``) dismisses its rule id and message in any file.
    """

    def __init__(self, entries: tuple[MemoryEntry, ...]) -> None:
        self._digests = {_entry_digest(entry) for entry in entries}
        self.suppressed: list[SuppressedFinding] = []

    async def is_dismissed(self, *, repo_id: str, fingerprint: str) -> bool:
        # The shared fingerprint format is newline-delimited, so a file path containing a newline
        # cannot be parsed back: the finding is not matched and stays visible (fails safe).
        parts = fingerprint.split("\n", 2)
        if len(parts) != 3:
            return False
        file, rule, message = parts
        rule_id = rule or None
        # An entry is the text as printed, which is the display form of the message.
        stripped = strip_display_text(message)
        candidates = {
            fingerprint_digest(build_finding_fingerprint(file=f, rule_id=rule_id, message=m))
            for f in (file, WILDCARD_FILE)
            for m in (message, stripped, break_autolinks(stripped), sanitize_message(message))
        }
        if candidates.isdisjoint(self._digests):
            return False
        # The same finding is asked about by more than one step: list it once.
        suppressed = SuppressedFinding(file=file, rule_id=rule_id, message=message)
        if suppressed not in self.suppressed:
            self.suppressed.append(suppressed)
        return True


def safe_relative(path: str) -> PurePosixPath:
    posix = PurePosixPath(path)
    if not path or posix.is_absolute() or ".." in posix.parts or "\\" in path or "\0" in path:
        raise _error(path, None, "the configured path must be relative and inside the repository")
    return posix


def read_bounded(
    target: Path, label: str, *, limit: int, follow_symlinks: bool = False
) -> bytes | None:
    """The bytes of the regular file ``target`` (at most ``limit``), or ``None`` if it does not
    exist. The one bounded, no-follow reader every local-mode file read shares; raises
    ``MemoryFileError`` for a non-regular, oversized or unreadable file."""
    try:
        status = target.stat() if follow_symlinks else target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error(label, None, f"could not be read ({exc.strerror or 'I/O error'})") from exc
    try:
        if not stat.S_ISREG(status.st_mode):
            raise _error(label, None, "is not a regular file")
        if status.st_size > limit:
            raise _error(label, None, f"larger than the {limit}-byte limit")
        descriptor = os.open(target, os.O_RDONLY | (0 if follow_symlinks else _NOFOLLOW))
        # A buffered read loops until EOF or the cap; a bare os.read may return a short chunk.
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read(limit + 1)
    except OSError as exc:
        raise _error(label, None, f"could not be read ({exc.strerror or 'I/O error'})") from exc


def read_working_tree(repo_dir: Path, relative: PurePosixPath, label: str) -> bytes | None:
    root = repo_dir.resolve()
    target = root / relative
    if not target.exists() and not target.is_symlink():
        return None
    # `resolve()` follows symlinks; a difference from the lexical path means one was crossed.
    if target.resolve() != Path(os.path.normpath(target)):
        raise _error(label, None, "is reached through a symbolic link; refusing to read it")
    return read_bounded(target, label, limit=MEMORY_FILE_MAX_BYTES)


async def read_from_commit(
    repo_dir: Path,
    commit: str,
    relative: PurePosixPath,
    label: str,
    *,
    environ: Mapping[str, str],
) -> bytes | None:
    code, listing = await run_git(
        repo_dir, "ls-tree", "-z", commit, "--", str(relative), environ=environ
    )
    if code != 0:
        raise GitCommandError(subcommand="ls-tree", detail=f"exit status {code}")
    if not listing:
        return None
    meta, _, name = listing.rstrip(b"\0").partition(b"\t")
    fields = meta.split()
    if name != str(relative).encode() or len(fields) != 3:
        return None
    mode, kind, blob = fields[0], fields[1], fields[2].decode("ascii", "replace")
    if kind != b"blob" or mode == b"120000":
        raise _error(label, None, "is not a regular file on the base branch")
    code, size = await run_git(repo_dir, "cat-file", "-s", blob, environ=environ)
    if code != 0 or not size.strip().isdigit():
        raise GitCommandError(subcommand="cat-file", detail=f"exit status {code}")
    if int(size) > MEMORY_FILE_MAX_BYTES:
        raise _error(label, None, f"larger than the {MEMORY_FILE_MAX_BYTES}-byte limit")
    code, data = await run_git(
        repo_dir,
        "cat-file",
        "blob",
        blob,
        environ=environ,
        max_output_bytes=MEMORY_FILE_MAX_BYTES + 1,
    )
    if code != 0:
        raise GitCommandError(subcommand="cat-file", detail=f"exit status {code}")
    return data


async def load_memory_file(
    repo_dir: Path, path: str, *, base_commit: str | None, environ: Mapping[str, str]
) -> LoadedMemoryFile | None:
    """The memory file at ``path`` (relative to ``repo_dir``), or ``None`` if there is none.

    ``base_commit`` set means CI: the file is read from that commit and the working tree (the
    pull-request head) is never consulted. Raises ``MemoryFileError`` for a bad path, an unusable
    file or a malformed one, and ``GitCommandError`` if git cannot answer.
    """
    relative = safe_relative(path)
    label = str(relative)
    data = (
        await asyncio.to_thread(read_working_tree, repo_dir, relative, label)
        if base_commit is None
        else await read_from_commit(repo_dir, base_commit, relative, label, environ=environ)
    )
    return None if data is None else parse_memory_file(data, path=label)


def propose_rules(entries: tuple[MemoryEntry, ...]) -> tuple[MemoryEntry, ...]:
    """Repository-wide entries worth suggesting: the same rule id and message dismissed for at
    least ``PROPOSAL_MIN_DISMISSALS`` distinct files, with no wildcard entry for it yet."""
    files: dict[tuple[str, str], set[str]] = defaultdict(set)
    has_wildcard: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.rule_id or "", entry.message)
        if entry.file == WILDCARD_FILE:
            has_wildcard.add(key)
        else:
            files[key].add(entry.file)
    return tuple(
        MemoryEntry(
            file=WILDCARD_FILE,
            rule_id=rule_id or None,
            message=message,
            note=f"proposed: dismissed in {len(paths)} files",
        )
        for (rule_id, message), paths in files.items()
        if len(paths) >= PROPOSAL_MIN_DISMISSALS and (rule_id, message) not in has_wildcard
    )


def render_proposal(loaded: LoadedMemoryFile, proposals: tuple[MemoryEntry, ...]) -> str:
    """A unified diff that appends ``proposals`` to the tracked file; ``""`` when there are none.
    It is only ever printed: a human applies it (``git apply``) and commits it."""
    if not proposals:
        return ""
    old = [line + "\n" for line in loaded.text.rstrip("\n").split("\n")] if loaded.text else []
    new = old + [
        json.dumps(p.model_dump(exclude_none=True), ensure_ascii=False) + "\n" for p in proposals
    ]
    return "".join(
        difflib.unified_diff(old, new, fromfile=f"a/{loaded.path}", tofile=f"b/{loaded.path}")
    )


def _describe(item: SuppressedFinding) -> str:
    rule = "" if item.rule_id is None else f" ({strip_display_text(item.rule_id)})"
    return f"{strip_display_text(item.file)}: {strip_display_text(item.message)}{rule}"


def describe_suppressed(items: list[SuppressedFinding]) -> tuple[str, ...]:
    """One plain line per suppressed finding (control characters stripped)."""
    return tuple(_describe(item) for item in items)
