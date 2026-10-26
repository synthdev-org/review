"""adapter.local.redaction — input-side redaction: secrets never reach a prompt (US-L15).

Server mode redacts only LLM-sourced *output*; this is net-new. ``RedactingDiffFetcher`` wraps the
real ``DiffFetcher`` so the one place every prompt's diff text comes from is screened, whatever
steps consume it. It screens the **whole** diff before the size ceiling truncates anything, so a
secret cannot be cut in two and escape the ruleset (truncation then operates on already-redacted
text). It covers added, removed and context lines, hunk-header text and file paths.

One batched scan per run (``SecretLineFinder``, implemented over Gitleaks): the file-count and byte
caps are checked here, before the scanner starts, and the scanner has an overall deadline. Any
failure raises ``RedactionFailedError`` and nothing is sent. This module is deliberately not
wrapped in ``suppress_telemetry_errors`` (ADR-0012 exempts telemetry recording only), and there is
no switch that turns it off. Coverage is the Gitleaks ruleset only: a bespoke secret can pass.

Files on the path deny-list (``.env*``, private keys, credential stores) keep their entry but lose
their patch, and their content is withheld from analyzers too (a matched-line snippet would
reach the model).
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from adapter.local.exceptions import RedactionFailedError
from findings.redact import REDACTED_PATH_PREFIX
from ingestion.diff import Diff, DiffFetcher

REDACTION_MARKER = "[REDACTED]"
_HUNK_HEADER = re.compile(r"^(@@[^@]*@@)(.*)$")

# Matched against the file's base name, lower-cased.
_DENIED_NAMES = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".htpasswd",
    "*.tfvars",
    "terraform.tfstate*",
)


def is_denied_path(path: str) -> bool:
    """Whether ``path`` names a file whose content is never sent (the example files that hold no
    secrets by convention, such as ``.env.example``, are not exempted: err on the side of not
    sending)."""
    base = posixpath.basename(path.replace("\\", "/")).lower()
    return any(fnmatch.fnmatchcase(base, pattern) for pattern in _DENIED_NAMES)


class SecretLineFinder(Protocol):
    """Which lines of each text hold a secret: one batched scan. Defined here, at the consumer;
    ``adapter/analyzers/gitleaks.py`` implements it. Raises on any failure. The texts are
    attacker-controlled, so an implementation must not let an inline allow marker exempt a line
    (``GitleaksSecretLineFinder`` defuses ``gitleaks:allow`` itself)."""

    async def secret_lines(self, texts: Sequence[str]) -> list[frozenset[int]]: ...


@dataclass(frozen=True)
class RedactionReport:
    """What redaction did, for the run summary. Counts and the (redacted) names of files whose
    content was withheld; never content."""

    redacted_lines: int
    denied_paths: tuple[str, ...]


_SCAN_PREAMBLE = "review-scan-preamble\n"


def _text_lines(scanned_lines: Iterable[int]) -> frozenset[int]:
    """Scanner line numbers (1-based, with ``_SCAN_PREAMBLE`` as line 1) to 1-based line numbers of
    the text that was handed in. The one place that convention lives."""
    return frozenset(n - 1 for n in scanned_lines if n > 1)


def _scan_text(patch: str) -> str:
    """The patch as the scanner should see it: each line without its diff marker (so rules that
    anchor on the start of a line still match) and hunk headers reduced to their trailing context
    text. One scanned line per patch line, so line numbers map back one to one."""
    lines = []
    for line in patch.split("\n"):
        header = _HUNK_HEADER.match(line)
        lines.append(header.group(2) if header else line[1:])
    return "\n".join(lines)


def _redact_patch(patch: str, flagged: frozenset[int]) -> str:
    """Replace each flagged line, keeping its diff marker (or, for a hunk header, the header)
    so the hunk still parses and line numbers do not shift."""
    lines = patch.split("\n")
    for number in flagged:
        if not 1 <= number <= len(lines):
            continue
        line = lines[number - 1]
        header = _HUNK_HEADER.match(line)
        lines[number - 1] = (
            f"{header.group(1)} {REDACTION_MARKER}" if header else line[:1] + REDACTION_MARKER
        )
    return "\n".join(lines)


def _one_line(path: str) -> str:
    return path.replace("\n", " ").replace("\r", " ")


class RedactingDiffFetcher:
    """``DiffFetcher`` decorator that screens everything it returns. The first successful
    ``fetch`` is remembered: the scan runs once per run, however many steps ask."""

    def __init__(
        self,
        inner: DiffFetcher,
        finder: SecretLineFinder,
        *,
        max_files: int,
        max_bytes: int,
    ) -> None:
        self._inner = inner
        self._finder = finder
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._original: Diff | None = None
        self._redacted: Diff | None = None
        self._fetch_lock = asyncio.Lock()
        self._original_path: dict[str, str] = {}
        # Original paths whose content is withheld: a deny-listed name, or a rename away from one.
        self._denied: frozenset[str] = frozenset()
        self.report: RedactionReport | None = None

    @property
    def diff(self) -> Diff | None:
        """The redacted diff handed to the pipeline (``None`` before the first fetch)."""
        return self._redacted

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        async with self._fetch_lock:  # one scan even if two steps ask at once
            if self._redacted is not None:
                return self._redacted
            original = await self._inner.fetch(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
            )
            self._redacted = await self._redact(original)
            self._original = original
            return self._redacted

    async def _redact(self, diff: Diff) -> Diff:
        # A rename away from a denied file (.env -> x.txt) carries its content too.
        denied = {
            f.path
            for f in diff.files
            if is_denied_path(f.path) or (f.previous_path and is_denied_path(f.previous_path))
        }
        self._denied = frozenset(denied)
        # Index 0: every path (and rename source) one per line; then one text per sent patch.
        names = [name for f in diff.files for name in (f.path, f.previous_path) if name is not None]
        sent = [f for f in diff.files if f.patch is not None and f.path not in denied]
        texts = [
            "\n".join(_one_line(n) for n in names),
            *(_scan_text(f.patch) for f in sent if f.patch is not None),
        ]
        size = sum(len(t.encode("utf-8")) for t in texts)
        # Before the scanner starts: a crafted PR cannot make this run unbounded work.
        if len(texts) - 1 > self._max_files:
            raise RedactionFailedError(
                reason=f"the diff has {len(texts) - 1} files, over the {self._max_files}-file cap"
            )
        if size > self._max_bytes:
            raise RedactionFailedError(
                reason=f"the diff is {size} bytes, over the {self._max_bytes}-byte cap"
            )
        try:
            # The scanner skips a file whose first bytes look like an archive/document, and the
            # first line is attacker-shaped (hunk context, path): lead with a fixed benign line.
            raw = await self._finder.secret_lines([_SCAN_PREAMBLE + t for t in texts])
            flagged = [_text_lines(lines) for lines in raw]
        except Exception as exc:
            raise RedactionFailedError(
                reason=f"the secret scan failed ({type(exc).__name__})"
            ) from exc
        if len(flagged) != len(texts):
            raise RedactionFailedError(reason="the secret scan returned an incomplete answer")

        replacement = self._path_replacements(names, flagged[0])
        patches = {
            f.path: _redact_patch(f.patch, flagged[i + 1]) for i, f in enumerate(sent) if f.patch
        }
        redacted_lines = len(flagged[0]) + sum(len(lines) for lines in flagged[1:])
        files = []
        for f in diff.files:
            path = replacement.get(f.path, f.path)
            self._original_path[path] = f.path
            files.append(
                f.model_copy(
                    update={
                        "path": path,
                        "previous_path": replacement.get(f.previous_path, f.previous_path)
                        if f.previous_path is not None
                        else None,
                        "patch": None if f.path in denied else patches.get(f.path, f.patch),
                    }
                )
            )
        self.report = RedactionReport(
            redacted_lines=redacted_lines,
            denied_paths=tuple(replacement.get(p, p) for p in sorted(denied)),
        )
        return Diff(files=tuple(files))

    @staticmethod
    def _path_replacements(names: list[str], flagged: frozenset[int]) -> dict[str, str]:
        """A flagged path becomes ``[REDACTED-PATH-n]`` (unique, so two files never collide)."""
        return {
            names[number - 1]: f"{REDACTED_PATH_PREFIX}{number}]"
            for number in sorted(flagged)
            if 1 <= number <= len(names)
        }

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        """File content for local analyzers; keyed by the path the pipeline knows, which is the
        redacted one. Deny-listed files are never returned: analyzers (Semgrep) quote matched
        source lines into finding messages, which reach the prompt and the posted comment."""
        if self._original is None:
            raise RedactionFailedError(reason="content was requested before the diff was screened")
        wanted = [
            original
            for original in (self._original_path.get(p, p) for p in paths)
            if original not in self._denied and not is_denied_path(original)
        ]
        content = await self._inner.fetch_content(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            diff=self._original,
            paths=wanted,
        )
        shown = {original: path for path, original in self._original_path.items()}
        return {shown.get(path, path): data for path, data in content.items()}


async def redact_texts(finder: SecretLineFinder, texts: Sequence[str]) -> tuple[list[str], int]:
    """The same screening the diff gets, for text that re-enters a prompt or leaves in a query (MCP
    queries and responses): one batched scan, every flagged line replaced by the redaction marker.
    Returns the screened texts and the number of lines replaced. Fails closed: any scanner failure
    raises ``RedactionFailedError`` and the caller must not use the texts."""
    if not texts:
        return [], 0
    scanned = [_SCAN_PREAMBLE + text for text in texts]
    try:
        raw = await finder.secret_lines(scanned)
    except Exception as exc:
        raise RedactionFailedError(reason=f"the secret scan failed ({type(exc).__name__})") from exc
    if len(raw) != len(texts):
        raise RedactionFailedError(reason="the secret scan returned an incomplete answer")
    result: list[str] = []
    count = 0
    for text, lines in zip(texts, raw, strict=True):
        flagged = _text_lines(lines)
        parts = text.split("\n")
        for number in flagged:
            if 1 <= number <= len(parts):
                parts[number - 1] = REDACTION_MARKER
        count += len(flagged)
        result.append("\n".join(parts))
    return result, count


# Runtime confirmation that this class satisfies the Protocol it is built to implement.
_: type[DiffFetcher] = RedactingDiffFetcher
