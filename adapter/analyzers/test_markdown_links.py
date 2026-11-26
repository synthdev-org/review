"""RQ8 (markdown half): broken links are found with no LLM call, and clean
documents stay clean. Unit cases call the pure analyzer; the integration cases
go through ``StaticAnalysisStep``."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from adapter.analyzers import markdown_links
from adapter.analyzers.conftest import RunStep, added_file
from adapter.analyzers.markdown_links import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    MarkdownLinksAnalyzer,
)
from ingestion.diff import Diff, DiffFile
from review.static_analysis import AnalyzerOversize

_ANALYZER = MarkdownLinksAnalyzer()


async def _run(
    content: dict[str, str], removed: tuple[str, ...] = ()
) -> list[tuple[str, int | None]]:
    files = [added_file(k, v.encode()) for k, v in content.items()]
    files += [DiffFile(path=p, status="removed") for p in removed]
    found = await _ANALYZER.run(
        Diff(files=tuple(files)), {k: v.encode() for k, v in content.items()}, timeout=5
    )
    assert all(f.source == "markdown_links" for f in found)
    return [(f.file, f.line) for f in found]


@pytest.mark.asyncio
async def test_renamed_heading_breaks_same_file_anchor() -> None:
    doc = "# Doc\n[see](#nightly-backup)\n## Scheduled backup\n"
    assert await _run({"a.md": doc}) == [("a.md", 2)]


@pytest.mark.asyncio
async def test_valid_anchors_duplicates_html_ids_and_other_file_anchor() -> None:
    doc = (
        "# A & B: `c`\n[x](#a--b-c) [y](#dup-1) [z](#custom) [w](b.md#top)\n"
        '## Dup\n## Dup\n<a name="custom"></a>\n'
    )
    assert await _run({"a.md": doc, "b.md": "# Top\n"}) == []


@pytest.mark.asyncio
async def test_setext_headings_and_custom_ids_are_anchors() -> None:
    doc = (
        "Top Title\n=========\n\nSub Section\n---\n\n## Custom {#my-id}\n\n"
        "[a](#top-title) [b](#sub-section) [c](#my-id) [d](#custom)\n"
    )
    assert await _run({"a.md": doc}) == []


@pytest.mark.asyncio
async def test_setext_false_positives_stay_flagged() -> None:
    # a rule after a blank line, and an ATX heading followed by a rule, are not setext headings
    flagged = await _run({"a.md": "para\n\n---\n[a](#para)\n"})
    assert flagged == [("a.md", 4)]
    assert await _run({"a.md": "# Real\n---\n[a](#---) [b](#real)\n"}) == [("a.md", 3)]


@pytest.mark.asyncio
async def test_anchor_missing_in_other_changed_file_is_flagged() -> None:
    assert await _run({"a.md": "[x](docs/b.md#gone)\n", "docs/b.md": "# Here\n"}) == [("a.md", 1)]


@pytest.mark.asyncio
async def test_link_to_removed_file_flagged_but_unknown_path_is_not() -> None:
    doc = "[gone](old.md) [kept](other.md) [dir](docs/)\n"
    assert await _run({"a.md": doc}, removed=("old.md",)) == [("a.md", 1)]


@pytest.mark.asyncio
async def test_removed_then_readded_path_is_not_flagged() -> None:
    assert await _run({"a.md": "[x](b.md)\n", "b.md": "# B\n"}, removed=("b.md",)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["../../etc/passwd", "/etc/passwd", "//evil.test/x", "https://x.test/#nope", "mailto:a@b.c"],
)
async def test_traversal_absolute_and_external_targets_are_never_resolved(target: str) -> None:
    assert await _run({"a.md": f"[x]({target})\n"}, removed=("etc/passwd", "/etc/passwd")) == []


@pytest.mark.asyncio
async def test_fenced_code_and_non_markdown_files_are_ignored() -> None:
    doc = "```\n[x](#nope)\n```\n"
    assert await _run({"a.md": doc, "b.py": "[x](#nope)"}) == []


@pytest.mark.asyncio
async def test_a_prose_line_starting_with_an_inline_code_span_is_not_a_fence() -> None:
    doc = "```x``` is a span\n[bad](#nope)\n"
    assert await _run({"a.md": doc}) == [("a.md", 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc",
    [
        "````\n```\n[bad](#nope)\n````\n",  # a shorter inner fence does not close it
        "~~~\n```\n[bad](#nope)\n~~~\n",  # a different char does not close it
        "   ```py\n[bad](#nope)\n   ```\n",  # up to 3 spaces of indent
    ],
)
async def test_fences_follow_commonmark_open_and_close_rules(doc: str) -> None:
    assert await _run({"a.md": doc}) == []


@pytest.mark.asyncio
async def test_pathological_long_line_is_skipped_quickly() -> None:
    start = time.monotonic()
    assert await _run({"a.md": "[" * 400_000 + "\n"}) == []
    assert time.monotonic() - start < 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [{"a.md": b"x" * (MAX_FILE_BYTES + 1)}, {f"{i}.md": b"x" * MAX_FILE_BYTES for i in range(5)}],
)
async def test_oversize_input_is_refused_before_parsing(content: dict[str, bytes]) -> None:
    assert sum(map(len, content.values())) > MAX_FILE_BYTES
    with pytest.raises(AnalyzerOversize):
        await _ANALYZER.run(Diff(files=()), content, timeout=5)
    assert MAX_TOTAL_BYTES > MAX_FILE_BYTES


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_while_scanning() -> None:
    doc = ("[a](#x) text\n" * 3000).encode()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    task = asyncio.create_task(ticker())
    await _ANALYZER.run(Diff(files=()), {f"{i}.md": doc for i in range(20)}, timeout=5)
    task.cancel()
    assert ticks > 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rq8_broken_anchor_found_through_step_with_no_llm_call(run_step: RunStep) -> None:
    doc = b"# Doc\n[see](#nightly-backup)\n## Scheduled backup\n"
    out = await run_step({"docs/backup.md": doc}, {"markdown_links": _ANALYZER})
    assert [(f.file, f.line, f.source) for f in out.findings] == [
        ("docs/backup.md", 2, "markdown_links")
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rq8_clean_document_stays_clean_through_step(run_step: RunStep) -> None:
    out = await run_step({"a.md": b"# Doc\n[see](#doc)\n"}, {"markdown_links": _ANALYZER})
    assert out.findings == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oversize_outcome_is_logged_and_does_not_fail_the_step(
    run_step: RunStep, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("review.static_analysis").disabled = False
    with caplog.at_level("WARNING", logger="review.static_analysis"):
        out = await run_step({"a.md": b"x" * (MAX_FILE_BYTES + 1)}, {"markdown_links": _ANALYZER})
    assert out.findings == []
    assert [
        r.status
        for r in caplog.records
        if getattr(r, "analyzer_name", "") == "markdown_links"
        and r.levelname == "WARNING"
        and hasattr(r, "status")
    ][-1:] == ["skipped_oversize"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_timeout_outcome_is_logged_as_error(
    run_step: RunStep, caplog: pytest.LogCaptureFixture
) -> None:
    class _Slow:
        async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[None]:
            await asyncio.sleep(1)
            return []

    logging.getLogger("review.static_analysis").disabled = False
    with caplog.at_level("ERROR", logger="review.static_analysis"):
        await run_step({"a.md": b"# A\n"}, {"markdown_links": _Slow()}, timeout=0.05)
    assert any(
        getattr(r, "analyzer_name", "") == "markdown_links" and r.status == "timeout"  # type: ignore[attr-defined]
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_rule_after_multiline_paragraph_is_not_a_setext_heading() -> None:
    doc = "first line\nsecond line\n---\n[a](#second-line)\n"
    assert await _run({"a.md": doc}) == [("a.md", 4)]


@pytest.mark.asyncio
async def test_front_matter_delimiters_are_not_setext_underlines() -> None:
    doc = "---\ntitle: Hello\n---\n# Real\n[a](#title-hello) [b](#real)\n"
    assert await _run({"a.md": doc}) == [("a.md", 5)]


@pytest.mark.asyncio
async def test_query_string_is_stripped_before_matching_removed_paths() -> None:
    doc = "[old](docs/a.md?plain=1)\n"
    assert await _run({"README.md": doc}, removed=("docs/a.md",)) == [("README.md", 1)]


@pytest.mark.asyncio
async def test_link_inside_an_inline_code_span_is_ignored() -> None:
    doc = "# Doc\nUse `[x](#nope)` syntax, and ``[y](gone.md)``; real: [z](#nope)\n"
    assert await _run({"a.md": doc}, removed=("gone.md",)) == [("a.md", 2)]


@pytest.mark.asyncio
async def test_image_is_skipped_for_anchors_but_still_checked_for_removed_files() -> None:
    doc = "# Doc\n![a](#nope) ![b](gone.png)\n"
    assert await _run({"a.md": doc}, removed=("gone.png",)) == [("a.md", 2)]
    assert await _run({"a.md": "# Doc\n![a](#nope)\n"}) == []


@pytest.mark.asyncio
async def test_percent_encoded_path_is_decoded_before_matching_removed_paths() -> None:
    doc = "# Doc\n[x](my%20file.md)\n"
    assert await _run({"a.md": doc}, removed=("my file.md",)) == [("a.md", 2)]


@pytest.mark.asyncio
async def test_link_target_with_balanced_parentheses_is_matched_whole() -> None:
    doc = "[x](docs/Foo_(bar).md)\n"
    assert await _run({"a.md": doc}, removed=("docs/Foo_(bar).md",)) == [("a.md", 1)]


@pytest.mark.asyncio
async def test_overlong_heading_still_yields_anchor_but_overlong_links_stay_skipped() -> None:
    heading = "## Long" + " " * 2000 + "\n"
    # The over-long link line would be broken, but is never scanned.
    broken = "[a](#nope) " + "y" * 2000 + "\n"
    assert await _run({"a.md": "[ok](#long)\n" + heading + broken}) == []
    # A heading after an over-long one is still reachable (anchor set not truncated).
    assert await _run({"a.md": heading + "[ok](#next)\n## Next\n"}) == []


@pytest.mark.asyncio
async def test_a_broken_link_on_a_line_the_pr_did_not_add_is_not_flagged() -> None:
    # Lines 2 and 4 are broken; the patch adds line 4 only (line 2 is context, line 3 is
    # outside any hunk). GitHub 422s the whole review for an inline comment off the diff.
    doc = "# Doc\n[old](#gone)\nplain\n[new](#gone)\n"
    patch = "@@ -1,2 +1,2 @@\n # Doc\n [old](#gone)\n@@ -9,0 +4,1 @@\n+[new](#gone)"
    diff = Diff(files=(DiffFile(path="a.md", status="modified", patch=patch),))

    found = await _ANALYZER.run(diff, {"a.md": doc.encode()}, timeout=5)

    assert [(f.file, f.line) for f in found] == [("a.md", 4)]


@pytest.mark.asyncio
async def test_a_file_without_a_patch_yields_no_findings() -> None:
    diff = Diff(files=(DiffFile(path="a.md", status="modified"),))

    assert await _ANALYZER.run(diff, {"a.md": b"[x](#gone)\n"}, timeout=5) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("a `b` c", "a     c"),
        ("``a`b`` `c`", " " * 11),
        ("`unclosed and ``double`` x", "`unclosed and " + " " * 10 + " x"),
        ("`a` `b", "    `b"),
        ("no ticks", "no ticks"),
    ],
)
def test_blank_code_spans_blanks_only_equal_length_runs_and_keeps_length(
    line: str, expected: str
) -> None:
    out = markdown_links._blank_code_spans(line)
    assert out == expected
    assert len(out) == len(line)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("## Title ##", "Title"),
        ("#   Spaced   ", "Spaced"),
        ("# C#", "C"),
        ("# #", "#"),
        ("####### seven", None),
        ("#nospace", None),
        ("# ", None),
    ],
)
def test_atx_text(line: str, expected: str | None) -> None:
    assert markdown_links._atx_text(line) == expected


@pytest.mark.parametrize(
    "hostile",
    [
        "`" * 3000 + "x" * 3000,
        "``" * 5000 + "a`" * 5000,
        "#" + " " * 20000 + "x" + " " * 20000,
        "# " + "{#a" * 8000,
        " " * 3 + "`" * 20000 + "a",
        "~" * 20000 + " x",
    ],
)
def test_helpers_stay_linear_on_hostile_lines(hostile: str) -> None:
    start = time.monotonic()
    markdown_links._blank_code_spans(hostile)
    markdown_links._atx_text(hostile)
    markdown_links._FENCE.match(hostile)
    markdown_links._CUSTOM_ID.search(hostile)
    assert time.monotonic() - start < 1.0
