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
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*)$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
_CUSTOM_ID = re.compile(r"\{#([^}\s]+)\}$")  # matched against already-stripped heading text
_HTML_ANCHOR = re.compile(r"""<a\s[^>]*?(?:name|id)=["']([^"']+)["']""", re.IGNORECASE)
_INLINE_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_NOT_SLUG = re.compile(r"[^\w\- ]", re.UNICODE)
_BACKTICK_RUN = re.compile(r"`+")
_FENCE = re.compile(r"^ {0,3}(`{3,}(?!`)|~{3,}(?!~))(.*)$")  # CommonMark fence line
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _atx_text(line: str) -> str | None:
    """Heading text of an ATX heading line (closing ``#`` run removed), else None."""
    match = _HEADING.match(line)
    if match is None or not match.group(1):
        return None
    rest = match.group(1).rstrip()
    return rest.rstrip("#").rstrip() or rest[:1]


def _blank_code_spans(line: str) -> str:
    """Blank (same length) every code span: a backtick run closed by a run of equal length.

    Linear: one pass records, per run, the next run of the same length."""
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


def _scannable_lines(text: str) -> list[tuple[int, str, bool]]:
    """``(line_number, line, overlong)`` outside fenced code blocks. An over-long
    line is truncated (its heading still yields an anchor) and flagged so link
    scanning skips it."""
    out: list[tuple[int, str, bool]] = []
    fence: tuple[str, int] | None = None  # (fence char, opener length)
    for number, line in enumerate(text.split("\n"), start=1):
        if fence is not None:
            closer = _FENCE.match(line)
            if (
                closer
                and closer.group(1)[0] == fence[0]
                and len(closer.group(1)) >= fence[1]
                and not closer.group(2).strip()
            ):
                fence = None
            continue
        opener = _FENCE.match(line)
        if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
            fence = (opener.group(1)[0], len(opener.group(1)))
            continue
        if fence is None:
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
        custom = _CUSTOM_ID.search(text)
        if custom:  # kramdown/pandoc ``{#id}``; GitHub keeps it in the text, so allow both
            anchors.add(custom.group(1).lower())
            anchors.add(_slug(text))
            text = text[: custom.start()].rstrip()
        slug = _slug(text)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _slug(text: str) -> str:
    return _NOT_SLUG.sub("", _INLINE_LINK.sub(r"\1", text).lower()).replace(" ", "-")


def _added_lines(diff: Diff) -> dict[str, frozenset[int]]:
    """New-side line numbers each file's patch adds. GitHub rejects the whole
    create-review call (422) for an inline comment off the diff, so findings
    are only emitted on these lines."""
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
                target = match.group(1)
                if _SCHEME.match(target) or target.startswith(("/", "//")):
                    continue
                target_path, _, fragment = target.partition("#")
                target_path = unquote(target_path.partition("?")[0])
                is_image = match.start() > 0 and line[match.start() - 1] == "!"
                resolved = (
                    path
                    if not target_path
                    else posixpath.normpath(posixpath.join(posixpath.dirname(path), target_path))
                )
                if resolved == ".." or resolved.startswith("../"):
                    continue
                reason = None
                if resolved in removed and resolved not in content:
                    reason = "links to a file this PR removes"
                elif (
                    fragment
                    and not is_image
                    and resolved in anchors
                    and unquote(fragment).lower()
                    not in ("top", *anchors[resolved])  # GitHub's page-top
                ):
                    reason = "links to an anchor that does not exist in the target file"
                if reason:
                    shown = target[:_MAX_TARGET_CHARS]
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
