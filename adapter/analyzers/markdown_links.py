"""adapter.analyzers.markdown_links: a pure-Python ``StaticAnalyzer`` that flags
broken markdown links without an LLM call.

Two checks, both limited to what ``StaticAnalyzer.run`` receives (A5: changed
file bytes plus the diff, never the repo tree or the filesystem):

- a ``#fragment`` link whose heading or explicit anchor is absent from the
  target markdown file (the same file, or another changed one);
- a relative link to a path that the diff **removes** and does not re-add.

Findings are emitted only on lines the PR adds (the posting API 422s an inline
comment anywhere else); a broken link on an untouched line is not reported.

shortcut: a link to a path absent from the diff is not flagged, because
without the tree listing it cannot be told from a link to an unchanged file.
Upgrade by passing the tree listing into ``run`` (widens A5).

Untrusted input: the bound is the control (a thread cannot be cancelled), so
oversize input raises ``AnalyzerOversize`` before any regex runs, and every
regex runs only on a line at most ``_MAX_LINE_CHARS`` long (linear time).
Targets are matched against dict keys only: no filesystem read, no symlink
following; absolute and ``..``-escaping targets are not resolved.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from urllib.parse import unquote

from findings.models import Finding
from ingestion.diff import Diff
from review.static_analysis import AnalyzerOversize, StaticAnalyzer

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_LINE_CHARS = 1024
_MAX_TARGET_CHARS = 200

_LINK = re.compile(r"\[[^\]\n]*\]\(<?((?:[^()\s>]|\([^()\s>]*\))+)>?(?:\s+\"[^\"]*\")?\)")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
_HTML_ANCHOR = re.compile(r"""<a\s[^>]*?(?:name|id)=["']([^"']+)["']""", re.IGNORECASE)
_INLINE_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_NOT_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)
_BACKTICK_RUN = re.compile(r"`+")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _fence(line: str) -> tuple[str, str] | None:
    """Parse a CommonMark fence line as ``(marker run, rest)``, else None."""
    body = line.lstrip(" ")
    if len(line) - len(body) > 3 or body[:1] not in ("`", "~"):
        return None
    run = len(body) - len(body.lstrip(body[0]))
    return (body[:run], body[run:]) if run >= 3 else None


def _atx_text(line: str) -> str | None:
    """Heading text of an ATX heading line (closing ``#`` run removed), else None."""
    body = line.lstrip(" ")
    if len(line) - len(body) > 3:
        return None
    after = body.lstrip("#")
    if not 1 <= len(body) - len(after) <= 6:
        return None
    text = after.lstrip(" \t")
    if len(text) == len(after) or not text:
        return None
    rest = text.rstrip()
    return rest.rstrip("#").rstrip() or rest[:1]


def _blank_code_spans(line: str) -> str:
    """Blank (same length) every code span: a backtick run closed by a run of equal length.

    Linear: one pass records, per run, the next run of the same length.
    """
    runs = list(_BACKTICK_RUN.finditer(line))
    following: list[int | None] = [None] * len(runs)
    last_seen: dict[int, int] = {}
    for i in range(len(runs) - 1, -1, -1):
        size = runs[i].end() - runs[i].start()
        following[i] = last_seen.get(size)
        last_seen[size] = i
    chars = list(line)
    i = 0
    while i < len(runs):
        closer = following[i]
        if closer is None:
            i += 1
            continue
        chars[runs[i].start() : runs[closer].end()] = " " * (runs[closer].end() - runs[i].start())
        i = closer + 1
    return "".join(chars)


def _is_markdown(path: str) -> bool:
    return path.lower().endswith((".md", ".markdown"))


def _closes_fence(line: str, fence: tuple[str, int]) -> bool:
    closer = _fence(line)
    return bool(
        closer and closer[0][0] == fence[0] and len(closer[0]) >= fence[1] and not closer[1].strip()
    )


def _scannable_lines(text: str) -> list[tuple[int, str, bool]]:
    """``(line_number, line, overlong)`` outside fenced code blocks. An over-long
    line is truncated (its heading still yields an anchor) and flagged so link
    scanning skips it.
    """
    out: list[tuple[int, str, bool]] = []
    fence: tuple[str, int] | None = None  # (fence char, opener length)
    for number, line in enumerate(text.split("\n"), start=1):
        if fence is not None:
            if _closes_fence(line, fence):
                fence = None
            continue
        opener = _fence(line)
        if opener and not (opener[0][0] == "`" and "`" in opener[1]):
            fence = (opener[0][0], len(opener[0]))
            continue
        out.append((number, line[:_MAX_LINE_CHARS], len(line) > _MAX_LINE_CHARS))
    return out


def _front_matter_end(lines: list[tuple[int, str, bool]]) -> int:
    """Index just past a YAML front-matter block that opens on line 1, else 0."""
    if lines and lines[0][:2] == (1, "---"):
        for i in range(1, len(lines)):
            if lines[i][1].rstrip() in ("---", "..."):
                return i + 1
    return 0


def _anchors(lines: list[tuple[int, str, bool]]) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    prev = ""
    para = 0  # lines in the paragraph so far: only a single-line one can be underlined
    for _, line, _ in lines[_front_matter_end(lines) :]:
        anchors.update(m.lower() for m in _HTML_ANCHOR.findall(line))
        text = _atx_text(line)
        if text is not None:
            para = 0
        elif para == 1 and _SETEXT_UNDERLINE.match(line):
            text, para = prev.strip(), 0  # the underline row is consumed by its heading
        elif not line.strip():
            para = 0
        else:
            prev, para = line, para + 1
        if text is None:
            continue
        custom = _custom_id(text)
        if custom:  # kramdown/pandoc ``{#id}``; GitHub keeps it in the text, so allow both
            anchors.add(custom[0].lower())
            anchors.add(_slug(text))
            text = text[: custom[1]].rstrip()
        slug = _slug(text)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _custom_id(text: str) -> tuple[str, int] | None:
    """Return ``(id, start)`` of a trailing ``{#id}`` in stripped heading text (linear scan)."""
    start = text.rfind("{#")
    ident = text[start + 2 : -1]
    if (
        start < 0
        or not text.endswith("}")
        or not ident
        or "}" in ident
        or any(c.isspace() for c in ident)
    ):
        return None
    return ident, start


def _slug(text: str) -> str:
    return _NOT_SLUG.sub("", _INLINE_LINK.sub(r"\1", text).lower()).replace(" ", "-")


def _added_lines(diff: Diff) -> dict[str, frozenset[int]]:
    """New-side line numbers each file's patch adds. GitHub rejects the whole
    create-review call (422) for an inline comment off the diff, so findings
    are only emitted on these lines.
    """
    result: dict[str, frozenset[int]] = {}
    for changed in diff.files:
        lines: set[int] = set()
        new_line = 0
        for raw in (changed.patch or "").splitlines():
            if hunk := _HUNK.match(raw):
                new_line = int(hunk.group(1))
            elif raw.startswith("+") and new_line:
                lines.add(new_line)
                new_line += 1
            elif new_line and not raw.startswith(("-", "\\")):
                new_line += 1  # context line
        result[changed.path] = frozenset(lines)
    return result


def _resolve_target(path: str, target_path: str) -> str:
    if not target_path:
        return path
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), target_path))


def _broken_reason(
    match: re.Match[str],
    line: str,
    path: str,
    content: dict[str, bytes],
    removed: set[str],
    anchors: dict[str, set[str]],
) -> str | None:
    target = match.group(1)
    if _SCHEME.match(target) or target.startswith(("/", "//")):
        return None
    target_path, _, fragment = target.partition("#")
    is_image = match.start() > 0 and line[match.start() - 1] == "!"
    resolved = _resolve_target(path, unquote(target_path.partition("?")[0]))
    if resolved == ".." or resolved.startswith("../"):
        return None
    if resolved in removed and resolved not in content:
        return "links to a file this PR removes"
    if _missing_anchor(fragment, is_image, anchors.get(resolved)):
        return "links to an anchor that does not exist in the target file"
    return None


def _missing_anchor(fragment: str, is_image: bool, target_anchors: set[str] | None) -> bool:
    if not fragment or is_image or target_anchors is None:
        return False
    return unquote(fragment).lower() not in ("top", *target_anchors)  # GitHub's page-top


def _check(
    content: dict[str, bytes], removed: set[str], added: dict[str, frozenset[int]]
) -> list[Finding]:
    files = {
        path: _scannable_lines(raw.decode("utf-8", errors="replace"))
        for path, raw in content.items()
        if _is_markdown(path)
    }
    anchors = {path: _anchors(lines) for path, lines in files.items()}
    findings: list[Finding] = []
    for path, lines in files.items():
        for number, line, overlong in lines:
            if overlong or number not in added.get(path, ()):
                continue  # ReDoS bound: never link-scan an over-long line
            # Blanked (same length) so offsets and the `!` image check still line up.
            line = _blank_code_spans(line)
            for match in _LINK.finditer(line):
                reason = _broken_reason(match, line, path, content, removed, anchors)
                if reason:
                    shown = match.group(1)[:_MAX_TARGET_CHARS]
                    findings.append(
                        Finding(
                            file=path,
                            line=number,
                            severity="low",
                            message=f"Broken link `{shown}`: {reason}.",
                            source="markdown_links",
                            rule_id="markdown-broken-link",
                        )
                    )
    return findings


class MarkdownLinksAnalyzer:
    """``review.static_analysis.StaticAnalyzer`` for markdown links."""

    name = "markdown_links"

    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        sizes = [len(raw) for path, raw in content.items() if _is_markdown(path)]
        if sizes and (max(sizes) > MAX_FILE_BYTES or sum(sizes) > MAX_TOTAL_BYTES):
            raise AnalyzerOversize(f"markdown input over {MAX_FILE_BYTES}/{MAX_TOTAL_BYTES} bytes")
        if not sizes:
            return []
        removed = {f.path for f in diff.files if f.status == "removed"}
        return await asyncio.to_thread(_check, content, removed, _added_lines(diff))


_: type[StaticAnalyzer] = MarkdownLinksAnalyzer
