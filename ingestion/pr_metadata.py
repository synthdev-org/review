"""ingestion.pr_metadata — pure PR-metadata normalization for the
product-knowledge capture (docs/plans/plan-deliver-product-knowledge.md,
Phase 2, US-PK2 capture half / A1).

No I/O, no adapter imports: ``apps/webhook_api/webhook_routes.py`` feeds it
plain strings from the parsed webhook envelope. The PR **body** is read only
inside ``extract_issue_keys`` and is never returned, persisted or forwarded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

TITLE_MAX_CHARS = 300
HEAD_REF_MAX_CHARS = 255
MAX_ISSUE_KEYS = 10
# Bound the text the key regex scans; GitHub caps a PR body at 65,536 chars.
_SCAN_MAX_CHARS = 70_000

# `PROJ-9` (tracker key) and `#41` (GitHub issue reference). The key pattern
# is deliberately strict (2-10 upper-case alphanumerics, then digits) so that
# ordinary hyphenated words and hex-ish tokens do not match.
_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]{1,9}-[0-9]{1,6})(?![A-Za-z0-9_])")
_HASH_RE = re.compile(r"(?<![A-Za-z0-9_&/#])#([0-9]{1,7})(?![A-Za-z0-9_])")


def normalize_title(title: str | None) -> str | None:
    """Whitespace-normalized title capped at ``TITLE_MAX_CHARS``; ``None`` when
    absent or blank."""
    if title is None:
        return None
    normalized = " ".join(title.split())[:TITLE_MAX_CHARS]
    return normalized or None


def normalize_head_ref(head_ref: str | None) -> str | None:
    """``head_ref`` stripped and capped at ``HEAD_REF_MAX_CHARS``; ``None`` when
    absent or blank."""
    if head_ref is None:
        return None
    return head_ref.strip()[:HEAD_REF_MAX_CHARS] or None


def extract_issue_keys(
    title: str | None, head_ref: str | None, body: str | None
) -> tuple[str, ...]:
    """Issue keys (``PROJ-9``, ``#41``) found in title, then head ref, then
    body — de-duplicated, order-stable, at most ``MAX_ISSUE_KEYS``.

    No prefix filtering here: the effective ``issue_key_prefixes`` setting is
    only known at job time (see ``filter_issue_keys``). ``body`` is only read,
    never returned.
    """
    seen: dict[str, None] = {}
    for text in (title, head_ref, body):
        if not text:
            continue
        text = text[:_SCAN_MAX_CHARS]
        found: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in _KEY_RE.finditer(text)]
        found += [(m.start(), f"#{m.group(1)}") for m in _HASH_RE.finditer(text)]
        for _, key in sorted(found):
            seen.setdefault(key, None)
            if len(seen) >= MAX_ISSUE_KEYS:
                return tuple(seen)
    return tuple(seen)


def filter_issue_keys(keys: Iterable[str], prefixes: Iterable[str]) -> tuple[str, ...]:
    """Keep only keys that start with one of ``prefixes`` (A1(c)); an empty
    ``prefixes`` keeps every key. ``PROJ`` and ``PROJ-`` both match
    ``PROJ-9`` but never ``PROJX-9``."""
    normalized = tuple(p if p.endswith("-") else p + "-" for p in prefixes if p)
    if not normalized:
        return tuple(keys)
    return tuple(k for k in keys if k.startswith(normalized))


def is_fork_pr(
    *,
    head_repo_full_name: str | None,
    head_repo_fork: bool | None,
    base_repo_full_name: str | None,
) -> bool:
    """Whether the PR comes from a fork — **fail closed** (A1(b)).

    ``True`` when the head repo is absent/null (a deleted fork), when GitHub
    flags it ``fork``, when the base repo is unknown, or when the head and base
    full names differ (compared case-insensitively, as GitHub names are).
    """
    if head_repo_full_name is None or base_repo_full_name is None:
        return True
    if head_repo_fork is True:
        return True
    return head_repo_full_name.casefold() != base_repo_full_name.casefold()
