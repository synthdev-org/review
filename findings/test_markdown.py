"""Unit tests for findings.markdown's escape_markdown — pure text
transformation, no I/O, per docs/rules/testing.md's Unit tier. Per the
plan's Phase 7 Tests section: a dedicated case per neutralized construct
(code-fence break, link, image, mention, raw HTML tag), since this plan's
own Security review flags unescaped versions of exactly these as a
phishing/notification-spam vector, not just a rendering nit.
"""

from __future__ import annotations

from findings.markdown import escape_markdown, suggestion_block


def test_code_fence_backticks_are_escaped() -> None:
    escaped = escape_markdown("```\nrm -rf /\n```")

    assert "```" not in escaped
    assert r"\`\`\`" in escaped


def test_link_syntax_is_neutralized() -> None:
    escaped = escape_markdown("click [here](https://evil.example/steal)")

    assert "[here](https://evil.example/steal)" not in escaped
    # The link-forming brackets/parens are each individually escaped —
    # GitHub's renderer no longer sees a `[text](url)` construct.
    assert r"\[here\]" in escaped
    assert r"\(https://evil" in escaped
    assert escaped.endswith(r"steal\)")


def test_image_syntax_is_neutralized() -> None:
    escaped = escape_markdown("![beacon](https://evil.example/track.png)")

    assert "![beacon](https://evil.example/track.png)" not in escaped
    assert r"\!\[beacon\]" in escaped
    assert r"\(https://evil" in escaped
    assert escaped.endswith(r"png\)")


def test_mention_is_neutralized_without_visibly_altering_rendered_text() -> None:
    escaped = escape_markdown("cc @octocat please review")

    # The mention-recognition pattern (`@` immediately followed by a word
    # character) no longer matches...
    assert "@octocat" not in escaped
    # ...but the visible text is otherwise unchanged (a zero-width space is
    # invisible when rendered) — stripping it recovers the original.
    assert escaped.replace("\u200b", "") == "cc @octocat please review"


def test_mention_starting_with_an_escapable_character_is_still_neutralized() -> None:
    # "@_user"/"@-user": the identifier character right after "@" ("_"/"-")
    # is itself one of _ESCAPE_CHARS_RE's own escaped characters. Mention
    # neutralization must run before backslash-escaping, or the inserted
    # backslash would sit between "@" and the identifier character and
    # defeat the mention-recognition lookahead, leaving the mention live.
    escaped = escape_markdown("cc @_ci-bot please review")

    assert "@_ci-bot" not in escaped
    assert escaped.replace("\u200b", "") == "cc @\\_ci\\-bot please review"


def test_raw_html_img_tag_renders_as_literal_escaped_text() -> None:
    escaped = escape_markdown('<img src="https://evil.example/track.png">')

    assert "<img" not in escaped
    assert "&lt;img" in escaped
    assert "&gt;" in escaped


def test_raw_html_anchor_tag_renders_as_literal_escaped_text() -> None:
    escaped = escape_markdown('<a href="https://evil.example/phish">click me</a>')

    assert "<a href" not in escaped
    assert "</a>" not in escaped
    assert "&lt;a href" in escaped
    assert "&lt;/a&gt;" in escaped


def test_plain_text_with_no_special_characters_is_unchanged() -> None:
    assert escape_markdown("this is an ordinary finding message") == (
        "this is an ordinary finding message"
    )


def test_suggestion_block_wraps_replacement_in_a_suggestion_fence() -> None:
    assert suggestion_block("x = 1") == "```suggestion\nx = 1\n```"


def test_suggestion_block_does_not_double_a_trailing_newline() -> None:
    assert suggestion_block("x = 1\n") == "```suggestion\nx = 1\n```"


def test_suggestion_block_empty_replacement_is_a_deletion() -> None:
    assert suggestion_block("") == "```suggestion\n```"


def test_suggestion_block_fence_outgrows_backticks_in_the_replacement() -> None:
    block = suggestion_block("a\n```\nb")

    assert block == "````suggestion\na\n```\nb\n````"
    longer = suggestion_block("````\n")
    assert longer.startswith("`````suggestion\n") and longer.endswith("\n`````")


def test_suggestion_block_content_is_not_escaped() -> None:
    assert "<b>@user [x](y)</b>" in suggestion_block("<b>@user [x](y)</b>")
