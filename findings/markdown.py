"""findings.markdown — the GitHub-Flavored-Markdown-escaping helper every
piece of PR-derived or LLM-derived text passes through before this platform
posts it to a GitHub-rendered surface (a PR review comment body, a
check-run summary), per the plan's Phase 7 section.

**Why this exists, and why it's more than code-fence escaping.** A
``Finding``'s ``message`` field (and, on the check-run summary surface, any
text embedding a failed step's own exception message) is untrusted content
per this plan's own Security review — a PR's diff content, and the LLM's
own single, unverified inference pass over it, are both attacker-influenceable.
Posted verbatim from the platform's own bot identity, that text could:

- break out of whatever Markdown structure surrounds it (an unescaped
  code-fence sequence);
- render a live ``[text](url)``/``![img](url)`` link or image — a
  phishing/notification-spam vector, not just a rendering glitch; or
- render a live ``@mention``, spamming a real GitHub user with a
  notification they never asked for; or
- render a live raw-HTML tag (``<img src=...>``/``<a href=...>``), a
  tracking-pixel/beacon vector GFM's link/image escaping alone doesn't
  cover.

``escape_markdown`` neutralizes all four, in one pass: backslash-escaping
every standard Markdown special character (covers code-fence backticks,
link/image ``[``/``]``/``(``/``)``/``!`` syntax, emphasis, headers, lists),
HTML-entity-escaping literal ``<``/``>`` (so an embedded tag renders as inert
text rather than being interpreted by GitHub's renderer), and inserting a
zero-width space immediately after every ``@`` that precedes a word
character (breaks GitHub's mention-recognition pattern — which requires
``@`` immediately followed by an identifier character — without visibly
altering the rendered text).

**Placement — pure text transformation, no I/O, no Protocol.** Lives in
``findings`` (the bounded-context package ``findings/merge.py`` already
establishes as home for findings-adjacent pure logic — see this module's
own placement note in the plan's Phase 7 section) rather than
``adapter/github/posting.py`` itself, so it's Unit-tier testable per
``testing.md`` without a recorded GitHub cassette. Deliberately a plain
function, not a ``typing.Protocol`` implementation: a pure, stateless
utility with no swappable-implementation case in view has nothing a
Protocol would abstract over — introducing one here would be exactly the
kind of interface-without-a-second-implementation ``architecture.md``
itself warns against. This is a different kind of deviation from
``docs/adr/0006-protocol-defined-at-data-model.md`` (no Protocol exists
here at all, by design, not a Protocol placed somewhere other than its
first consumer) and stands on its own reasoning, per that ADR's own scope
note.

``adapter/github/posting.py`` calls this for **both** surfaces it builds —
comment bodies and the check-run's own summary text — never passing raw
PR-derived or LLM-derived text through unescaped on either one.
"""

from __future__ import annotations

import re

# Standard Markdown/GFM special characters, backslash-escaped so none of
# them can form a structural construct (code fence, link, image, emphasis,
# header, list item) out of untrusted text. Backslash itself is first in
# the class so it's escaped before anything else in the same pass — safe
# because `re.sub` never re-scans its own replacement output.
_ESCAPE_CHARS_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|])")

# GitHub recognizes `@name` as a mention only when `@` is immediately
# followed by an identifier character (letter/digit/underscore/hyphen) —
# inserting a zero-width space between them breaks that recognition without
# visibly changing the rendered text.
_MENTION_RE = re.compile(r"@(?=\w)")
_ZERO_WIDTH_SPACE = "\u200b"


def escape_markdown(text: str) -> str:
    """Neutralize ``text`` for safe inclusion in a GitHub-rendered Markdown
    surface (a PR review comment body, a check-run summary) — see this
    module's own docstring for the full threat/coverage reasoning.
    """
    # Mention neutralization runs first, against the *raw* text — running it
    # after backslash-escaping would let a mention whose first character is
    # itself an escaped character (e.g. "@_user", "@-user") slip through:
    # the inserted backslash would sit between "@" and the identifier
    # character, defeating the `@(?=\w)` lookahead below.
    escaped = _MENTION_RE.sub(f"@{_ZERO_WIDTH_SPACE}", text)
    escaped = _ESCAPE_CHARS_RE.sub(r"\\\1", escaped)
    escaped = escaped.replace("<", "&lt;").replace(">", "&gt;")
    return escaped


def suggestion_block(replacement: str) -> str:
    """A GitHub ``suggestion`` fenced block for ``replacement`` (empty means
    delete the range). Deliberately *not* passed through ``escape_markdown``:
    the content must render verbatim as code. Safety instead comes from the
    fence being longer than any backtick run inside the replacement, so the
    content can never close its own block."""
    longest = max((len(run) for run in re.findall(r"`+", replacement)), default=0)
    fence = "`" * max(3, longest + 1)
    if not replacement:
        return f"{fence}suggestion\n{fence}"
    body = replacement if replacement.endswith("\n") else replacement + "\n"
    return f"{fence}suggestion\n{body}{fence}"
