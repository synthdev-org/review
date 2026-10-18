"""Unit tests for review.walkthrough_render — pure model, sanitizer and renderer
(docs/adr/0005-pure-no-io-logic-unit-tested.md; plan-phase-12.md Phase 1).

``redact_all`` awaits a ``TextRedactor`` but only a fake one here, so there is no
I/O of its own. Story-level integration tests live in Phases 2 and 4.
"""

from __future__ import annotations

import json
import re

import pytest

from review.walkthrough_render import (
    FIXED_FOOTER_LINE,
    MARKER,
    OTHER_FILES_GROUP_NAME,
    WalkthroughFile,
    WalkthroughGroup,
    WalkthroughParseError,
    WalkthroughRedactionError,
    default_nonce,
    parse_walkthrough_response,
    prepare_display,
    reconcile_files,
    reconcile_files_with_stats,
    redact_all,
    redact_for_display,
    render_comment,
)

ZWSP = "​"
SHA = "a" * 40


def _g(name: str, *paths: str, description: str = "", note: str = "") -> WalkthroughGroup:
    return WalkthroughGroup(
        name=name,
        description=description,
        files=tuple(WalkthroughFile(path=p, note=note) for p in paths),
    )


def _render(
    groups: list[WalkthroughGroup],
    *,
    summary: str | None = "Does a thing",
    max_chars: int = 6000,
    extra: int = 0,
) -> str:
    return render_comment(
        summary=summary, groups=groups, head_sha=SHA, max_chars=max_chars, extra_not_shown=extra
    ).body


# --- parse --------------------------------------------------------------------


def test_parse_valid_response_accepts_bare_paths_and_notes() -> None:
    content = json.dumps(
        {
            "groups": [
                {
                    "name": "Core",
                    "description": "d",
                    "files": ["a.py", {"path": "b.py", "note": "n"}],
                }
            ]
        }
    )

    [group] = parse_walkthrough_response(content, job_id="j")

    assert [(f.path, f.note) for f in group.files] == [("a.py", ""), ("b.py", "n")]


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"groups": []}),
        json.dumps({"groups": [1, "x", {"name": 3}, {"name": "n", "files": [{"nopath": 1}]}]}),
        json.dumps({"groups": [{"name": "empty", "files": []}]}),
    ],
)
def test_parse_all_garbage_raises_typed_error(content: str) -> None:
    with pytest.raises(WalkthroughParseError):
        parse_walkthrough_response(content, job_id="j")


def test_parse_drops_bad_group_but_keeps_good_one_and_caps_counts() -> None:
    groups = [{"name": f"g{i}", "files": [f"f{i}-{j}" for j in range(250)]} for i in range(40)]
    content = json.dumps({"groups": [{"bad": True}, *groups]})

    parsed = parse_walkthrough_response(content, job_id="j")

    assert len(parsed) == 30
    assert all(len(g.files) == 200 for g in parsed)


# --- reconcile ----------------------------------------------------------------


def test_reconcile_drops_unknown_and_duplicates_and_appends_omitted_in_diff_order() -> None:
    groups = [_g("A", "a.py", "ghost.py", "b.py"), _g("B", "b.py", "a.py"), _g("C", "ghost2.py")]

    out = reconcile_files(groups, ["z.py", "a.py", "b.py", "y.py"])

    assert [g.name for g in out] == ["A", OTHER_FILES_GROUP_NAME]
    assert [f.path for f in out[0].files] == ["a.py", "b.py"]
    assert [f.path for f in out[1].files] == ["z.py", "y.py"]


def test_reconcile_stats_count_unknown_duplicate_and_omitted() -> None:
    groups = [_g("A", "a.py", "ghost.py", "b.py"), _g("B", "b.py", "a.py"), _g("C", "ghost2.py")]

    _, stats = reconcile_files_with_stats(groups, ["z.py", "a.py", "b.py", "y.py"])

    assert (stats.unknown_path, stats.duplicate_path, stats.omitted) == (2, 2, 2)


def test_reconcile_with_everything_covered_adds_no_other_group() -> None:
    assert [g.name for g in reconcile_files([_g("A", "a.py")], ["a.py"])] == ["A"]


def test_reconcile_matches_raw_path_with_bidi_override_and_display_strips_it() -> None:
    raw = "src/a‮txt.py"

    [group] = reconcile_files([_g("A", raw)], [raw])

    assert group.files[0].path == raw
    body = _render([group])
    assert "‮" not in body
    assert "`src/atxt.py`" in body


# --- render: structure --------------------------------------------------------


def test_render_has_marker_first_summary_groups_and_fixed_footer() -> None:
    body = _render([_g("Core", "a.py", description="The core", note="adds x")])

    assert body.startswith(MARKER + "\n")
    assert "### Summary\nDoes a thing\n" in body
    assert "### Core\nThe core\n- `a.py` — adds x" in body
    assert FIXED_FOOTER_LINE in body
    assert f"Describes commit `{SHA}`." in body
    assert body.count(MARKER) == 1
    assert "more files not shown" not in body


def test_render_omits_summary_section_when_no_text() -> None:
    assert "Summary" not in _render([_g("Core", "a.py")], summary=None)
    assert "Summary" not in _render([_g("Core", "a.py")], summary=" \n ")


def test_render_head_sha_is_sanitized() -> None:
    body = render_comment(
        summary=None, groups=[_g("A", "a")], head_sha="ab`\n<x>12", max_chars=6000
    ).body

    assert "Describes commit `abx12`." in body


# --- render: hostile fixtures -------------------------------------------------

HOSTILE = [
    "![beacon](https://evil.example/p.png)",
    "[x](http://evil)",
    "[click](javascript:alert(1))",
    "see https://evil.example/x and www.evil.example",
    "ping @org/team and @user and me@evil.example",
    "fixes #1, owner/repo#1, GH-1, deadbeefcafe1234",
    "<img src=x onerror=y> <a href=z>",
    "```\nnested\n````\n```",
    "line one\n\n### Fake heading\n<!-- review-walkthrough:v1 -->\n---\n_fake footer_",
    "~~~",
    "~~~\nx",
    "bidi ‮⁦ and zero​width",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_is_inert_in_every_prose_field(text: str) -> None:
    group = WalkthroughGroup(
        name=text, description=text, files=(WalkthroughFile(path="p.py", note=text),)
    )

    body = _render([group], summary=text)
    prose = body.split("\n---\n")[0]
    inert = prose.replace(MARKER, "", 1)

    assert body.count(MARKER) == 1
    assert body.count(FIXED_FOOTER_LINE) == 1
    assert body.count("\n---\n") == 1
    assert "\u202e" not in body and "\u2066" not in body
    assert not re.search(r"(?<!\\)!\[|(?<!\\)\]\(|<(?!!--)[a-z!/]", inert)
    assert "<!--" not in inert
    assert "://" not in inert and "www." not in inert
    assert not re.search(r"(?<!\\)@\w", inert)
    assert not re.search(r"#\d|GH-\d|[0-9a-f]{7}", inert)
    # Headings are only ours: title, Summary and the one group heading.
    headings = [line for line in inert.splitlines() if line.startswith("#")]
    assert headings[:2] == ["## Walkthrough", "### Summary"] and len(headings) == 3
    assert all(not line.startswith(("---", "```", "~~~")) for line in inert.splitlines())


def test_sha_like_hex_and_references_broken_in_notes_but_path_byte_identical() -> None:
    path = "migrations/0001_abcdef1234.sql"
    body = _render([_g("DB", path, note=f"see {path}")])

    assert f"- `{path}` — see " in body
    note = body.split(" — see ", 1)[1].splitlines()[0]
    assert "abcdef1234" not in note
    assert ZWSP in note


@pytest.mark.parametrize(
    ("path", "span"),
    [
        ("a`b.py", "``a`b.py``"),
        ("a``b.py", "```a``b.py```"),
        ("`lead.py", "`` `lead.py ``"),
        ("trail.py`", "`` trail.py` ``"),
        ("plain.py", "`plain.py`"),
    ],
)
def test_path_code_span_delimiter_beats_longest_backtick_run(path: str, span: str) -> None:
    assert f"- {span}\n" in _render([_g("A", path)])


def test_path_with_newline_and_markup_stays_on_one_line_in_a_code_span() -> None:
    body = _render([_g("A", "x\n## evil\n[a](http://b).py")])

    assert "- `x ## evil [a](http://b).py`\n" in body


# --- render: size limits ------------------------------------------------------


def _many(n_groups: int, per_group: int) -> list[WalkthroughGroup]:
    return [_g(f"G{i}", *(f"d{i}/f{j}.py" for j in range(per_group))) for i in range(n_groups)]


def test_cut_at_group_boundary_counts_every_omitted_file() -> None:
    groups = _many(20, 5)

    body = _render(groups, max_chars=1200)

    shown_groups = re.findall(r"^### (G\d+)$", body, re.MULTILINE)
    assert 0 < len(shown_groups) < 20
    assert shown_groups == [f"G{i}" for i in range(len(shown_groups))]
    assert body.count("- `") == 5 * len(shown_groups)
    assert f"\n{100 - 5 * len(shown_groups)} more files not shown\n" in body
    assert len(body) <= 1200
    assert FIXED_FOOTER_LINE in body


@pytest.mark.parametrize("max_chars", [400, 600, 900, 1500, 3000])
def test_every_truncation_boundary_respects_budget(max_chars: int) -> None:
    groups = _many(8, 7)
    result = render_comment(summary="s" * 100, groups=groups, head_sha=SHA, max_chars=max_chars)

    assert len(result.body) <= max_chars or result.files_shown == 0
    assert result.files_shown + result.files_not_shown == 56
    assert result.body.count("- `") == result.files_shown


def test_everything_fits_exactly_at_the_limit_and_one_char_less_cuts() -> None:
    groups = _many(2, 2)
    full = _render(groups)

    assert "not shown" not in _render(groups, max_chars=len(full))
    assert "not shown" in _render(groups, max_chars=len(full) - 1)


def test_oversize_first_group_is_truncated_with_summary_and_header_kept() -> None:
    groups = [_g("Big", *(f"f{i}.py" for i in range(100))), _g("Later", "z.py")]

    result = render_comment(summary="Why", groups=groups, head_sha=SHA, max_chars=700)

    assert "### Summary\nWhy" in result.body
    assert "### Big" in result.body
    assert "### Later" not in result.body
    assert 0 < result.files_shown < 100
    assert result.files_not_shown == 101 - result.files_shown
    assert len(result.body) <= 700


def test_first_group_header_survives_even_when_nothing_fits() -> None:
    result = render_comment(summary="Why", groups=[_g("Big", "a.py")], head_sha=SHA, max_chars=10)

    assert "### Big" in result.body
    assert result.files_shown == 0
    assert "1 more files not shown" in result.body


def test_extra_files_never_sent_are_counted_in_the_same_line() -> None:
    body = _render([_g("A", "a.py")], extra=41)

    assert "\n41 more files not shown\n" in body
    assert "\n42 more" not in body


def test_ceiling_holds_for_multibyte_and_zero_width_inflated_input() -> None:
    nasty = "&" * 160 + "é中" * 40 + "#1" * 50
    groups = [
        WalkthroughGroup(
            name=nasty,
            description=nasty * 3,
            files=tuple(WalkthroughFile(path="中" * 300 + str(i), note=nasty) for i in range(200)),
        )
        for _ in range(30)
    ]

    result = render_comment(summary=nasty * 30, groups=groups, head_sha=SHA, max_chars=1_000_000)

    assert len(result.body) <= 60_000
    assert len(result.body.encode()) < 65_536
    assert FIXED_FOOTER_LINE in result.body


# --- redact_all / prepare_display ---------------------------------------------


class _Redactor:
    """Fake scanner: replaces whole lines containing a secret, like Gitleaks."""

    def __init__(self, secret: str = "SECRETKEY123", drop: str | None = None) -> None:
        self.secret, self.drop = secret, drop
        self.calls: list[str] = []

    async def redact(self, text: str) -> str:
        self.calls.append(text)
        out = []
        for line in text.split("\n"):
            if self.drop and line.startswith(self.drop):
                continue
            out.append("[REDACTED]" if self.secret in line else line)
        return "\n".join(out)


def _nonce() -> str:
    return "n0nce"


@pytest.mark.asyncio
async def test_redact_all_is_one_call_and_preserves_count_order_and_empties() -> None:
    redactor = _Redactor()
    strings = ["a", "", "has SECRETKEY123 inside", "", "z"]

    out = await redact_all(strings, redactor, _nonce)

    assert out == ["a", "", "[REDACTED]", "", "z"]
    assert len(redactor.calls) == 1
    assert redactor.calls[0].count("WT-BOUNDARY-n0nce") == len(strings) + 1


@pytest.mark.asyncio
async def test_redact_all_empty_input_makes_no_call() -> None:
    redactor = _Redactor()

    assert await redact_all([], redactor, _nonce) == []
    assert redactor.calls == []


@pytest.mark.parametrize("position", [0, 1, 2, 3])
@pytest.mark.asyncio
async def test_nonce_planted_in_any_field_kind_fails_closed(position: int) -> None:
    # summary, group name, note, path (the order redact_for_display joins them)
    fields = ["summary", "name", "note", "a.py"]
    fields[position] = "x n0nce y"

    with pytest.raises(WalkthroughRedactionError):
        await redact_all(fields, _Redactor(), _nonce)


@pytest.mark.asyncio
async def test_nonce_split_by_a_stripped_character_is_still_detected() -> None:
    with pytest.raises(WalkthroughRedactionError):
        await redact_all(["n0​nce"], _Redactor(), _nonce)


@pytest.mark.asyncio
async def test_dropped_sentinel_is_a_count_mismatch() -> None:
    with pytest.raises(WalkthroughRedactionError):
        await redact_all(["a", "b"], _Redactor(drop="WT-BOUNDARY-n0nce"), _nonce)


@pytest.mark.asyncio
async def test_duplicated_sentinel_is_a_count_mismatch() -> None:
    class Dup:
        async def redact(self, text: str) -> str:
            return text + "\nWT-BOUNDARY-n0nce\nextra"

    with pytest.raises(WalkthroughRedactionError):
        await redact_all(["a"], Dup(), _nonce)


@pytest.mark.asyncio
async def test_multiline_field_is_collapsed_before_the_join() -> None:
    redactor = _Redactor()

    out = await redact_all(["line1\nWT-BOUNDARY-x\nline3\n"], redactor, _nonce)

    assert out == ["line1 WT-BOUNDARY-x line3"]
    assert redactor.calls[0].count("\n") == 2


@pytest.mark.asyncio
async def test_path_with_newline_plus_secret_is_redacted() -> None:
    out = await redact_all(["dir/\nSECRETKEY123.py"], _Redactor(), _nonce)

    assert out == ["[REDACTED]"]


@pytest.mark.asyncio
async def test_nested_gitleaks_allow_marker_is_fully_removed() -> None:
    redactor = _Redactor()

    out = await redact_all(["tok gitleaks:gitleaks:allowallow GITLEAKS:ALLOW x"], redactor, _nonce)

    assert "gitleaks:allow" not in redactor.calls[0].lower()
    assert out == ["tok x"]


@pytest.mark.asyncio
async def test_over_cap_field_truncated_and_secret_straddling_visible_cap_redacted() -> None:
    redactor = _Redactor()
    note = "x" * 150 + "SECRETKEY123" + "y" * 50  # straddles NOTE_MAX_CHARS (160)
    huge = "q" * 5000

    redacted = await redact_all([note, huge], redactor, _nonce)

    assert len(redacted[1]) == 2000
    assert len(redactor.calls[0]) < 4300
    body = _render(
        [WalkthroughGroup(name="G", files=(WalkthroughFile(path="p.py", note=redacted[0]),))]
    )
    assert "SECRETKEY" not in body and "SECRET" not in body
    assert r"\[REDACTED\]" in body


@pytest.mark.asyncio
async def test_redact_for_display_round_trips_every_display_string() -> None:
    groups = [
        WalkthroughGroup(
            name="N SECRETKEY123", description="d", files=(WalkthroughFile(path="a.py", note="n"),)
        ),
        WalkthroughGroup(
            name="M", files=(WalkthroughFile(path="b.py"), WalkthroughFile(path="c.py"))
        ),
    ]
    redactor = _Redactor()

    summary, out = await redact_for_display("sum", groups, redactor, _nonce)

    assert summary == "sum"
    assert [g.name for g in out] == ["[REDACTED]", "M"]
    assert [f.path for g in out for f in g.files] == ["a.py", "b.py", "c.py"]
    assert out[0].files[0].note == "n"
    assert len(redactor.calls) == 1


@pytest.mark.asyncio
async def test_redact_for_display_empty_summary_becomes_none_and_empty_path_is_named() -> None:
    summary, out = await redact_for_display(None, [_g("G", "‮")], _Redactor(), _nonce)

    assert summary is None
    assert out[0].files[0].path == "(unnamed)"


def test_prepare_display_strips_collapses_and_caps() -> None:
    assert prepare_display(" a‮\n b ") == "a b"
    assert prepare_display("x" * 10, max_chars=4) == "xxxx"


def test_default_nonce_is_32_hex_chars_and_unique() -> None:
    nonce = default_nonce()
    assert re.fullmatch(r"[0-9a-f]{32}", nonce)
    assert nonce != default_nonce()
