"""review.walkthrough_render — the pure model, sanitizer and renderer of the PR
walkthrough comment (docs/specs/spec-pr-walkthrough.md Unit A; plan-phase-12.md
Phase 1).

Nothing here does I/O except ``redact_all``'s single ``await`` on the injected
``TextRedactor``. The walkthrough text is wholly untrusted (LLM output steered
by PR content), so this module is the trust boundary: the step parses
(``parse_walkthrough_response``), repairs (``reconcile_files``), redacts
(``redact_for_display`` / ``redact_all``) and only then renders
(``render_comment``), which neutralizes every prose field via
``findings.markdown.neutralize_prose`` and shows paths in a code span.

``WalkthroughPublisher`` lives here with its data model (ADR-0006): the
renderer's output is what it publishes.

Not a step module: it defines no ``Step`` (see ``review/test_step_boundaries.py``'s
framework-module list).
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Sequence
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from findings.markdown import neutralize_prose, strip_display_text
from findings.redact import TextRedactor
from review.diff_prompt import cap_sequence_length
from review.llm_response import parse_llm_json_envelope
from review.step import JobIdentity

logger = logging.getLogger(__name__)

# First body line of every walkthrough comment; with the App author and the
# fixed footer line it identifies the platform's own comment (US-W4).
MARKER: Final = "<!-- review-walkthrough:v1 -->"
TITLE: Final = "## Walkthrough"
FIXED_FOOTER_LINE: Final = (
    "_This walkthrough is an AI-generated summary of the change, not a review verdict._"
)
OTHER_FILES_GROUP_NAME: Final = "Other files"

# Mirrors ``review.intent_summary.MAX_INTENT_SUMMARY_CHARS`` (a step module, so
# not importable here); the summary is already bounded upstream.
SUMMARY_MAX_CHARS: Final = 2000
NAME_MAX_CHARS: Final = 80
DESCRIPTION_MAX_CHARS: Final = 240
NOTE_MAX_CHARS: Final = 160
PATH_MAX_CHARS: Final = 300
# Bounds one field only so the single redaction blob stays small (Assumption 7(d)).
REDACT_FIELD_MAX_CHARS: Final = 2000
MAX_GROUPS: Final = 30
MAX_FILES_PER_GROUP: Final = 200
# GitHub rejects bodies over 65,536 characters; stay well under, and measure in
# UTF-8 bytes as well as characters so multi-byte input cannot reach it either.
HARD_MAX_CHARS: Final = 60_000
_HARD_MAX_BYTES: Final = 65_000

_GITLEAKS_ALLOW_RE: Final = re.compile(r"gitleaks:allow", re.IGNORECASE)
_SENTINEL_PREFIX: Final = "WT-BOUNDARY-"


class WalkthroughParseError(Exception):
    """The LLM response held no usable group (outcome ``parse_failure``)."""


class WalkthroughRedactionError(Exception):
    """The redaction blob could not be round-tripped (outcome ``internal_error``)."""


class WalkthroughFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    note: str = ""


class WalkthroughGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    files: tuple[WalkthroughFile, ...] = ()

    @field_validator("files", mode="before")
    @classmethod
    def _accept_bare_paths(cls, value: object) -> object:
        # A bare path string is a file with no note; any other shape is validated as-is.
        if isinstance(value, list):
            return [{"path": v} if isinstance(v, str) else v for v in value]
        return value


class RenderedComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    body: str
    files_shown: int
    files_not_shown: int


class WalkthroughPublisher(Protocol):
    """Creates or edits the PR's single walkthrough comment. Takes the job
    identity (repo, PR, head sha) and ``job_id`` for adapter logs; returns
    what it did."""

    async def upsert(
        self, *, job_id: str, identity: JobIdentity, body: str
    ) -> Literal["created", "edited", "deduped"]: ...


def default_nonce() -> str:
    return secrets.token_hex(16)


# --- parse / reconcile --------------------------------------------------------


def parse_walkthrough_response(content: str, *, job_id: str) -> list[WalkthroughGroup]:
    """Parse ``{"groups": [...]}``; cap group and per-group file counts; raise
    ``WalkthroughParseError`` when no group with a file survives (so an
    all-garbage response is a ``parse_failure``, never an empty comment)."""
    items = parse_llm_json_envelope(
        content,
        envelope_key="groups",
        item_model=WalkthroughGroup,
        make_parse_error=WalkthroughParseError,
        logger=logger,
        job_id=job_id,
        step_name="walkthrough",
        item_label="group",
        item_index_field="group_index",
        escalate_if_all_dropped=True,
    )
    groups = [
        g.model_copy(update={"files": g.files[:MAX_FILES_PER_GROUP]}) for g in items if g.files
    ][:MAX_GROUPS]
    if not groups:
        raise WalkthroughParseError("response held no group with files")
    return groups


class ReconcileStats(BaseModel):
    """What ``reconcile_files`` changed: model-quality signal for the step's metrics."""

    model_config = ConfigDict(frozen=True)

    unknown_path: int = 0
    duplicate_path: int = 0
    omitted: int = 0


def reconcile_files_with_stats(
    groups: Sequence[WalkthroughGroup], reviewable_paths: Sequence[str]
) -> tuple[list[WalkthroughGroup], ReconcileStats]:
    """Drop unknown and duplicate paths (on raw paths), drop groups left empty, and
    append omitted reviewable files as a final "Other files" group in diff order."""
    allowed = set(reviewable_paths)
    seen: set[str] = set()
    out: list[WalkthroughGroup] = []
    unknown = duplicate = 0
    for group in groups:
        kept: list[WalkthroughFile] = []
        for file in group.files:
            if file.path not in allowed:
                unknown += 1
            elif file.path in seen:
                duplicate += 1
            else:
                seen.add(file.path)
                kept.append(file)
        if kept:
            out.append(group.model_copy(update={"files": tuple(kept)}))
    omitted = tuple(
        WalkthroughFile(path=p) for p in dict.fromkeys(reviewable_paths) if p not in seen
    )
    if omitted:
        out.append(WalkthroughGroup(name=OTHER_FILES_GROUP_NAME, files=omitted))
    return out, ReconcileStats(unknown_path=unknown, duplicate_path=duplicate, omitted=len(omitted))


def reconcile_files(
    groups: Sequence[WalkthroughGroup], reviewable_paths: Sequence[str]
) -> list[WalkthroughGroup]:
    return reconcile_files_with_stats(groups, reviewable_paths)[0]


# --- redaction ----------------------------------------------------------------


def _cap(text: str, max_chars: int) -> str:
    capped, _, _ = cap_sequence_length(text, max_chars)
    assert isinstance(capped, str)
    return capped


def prepare_display(text: str, *, max_chars: int = REDACT_FIELD_MAX_CHARS) -> str:
    """Assumption 7(b)-(d): strip control/format characters, collapse whitespace,
    remove every ``gitleaks:allow`` marker (looped: ``gitleaks:gitleaks:allowallow``
    collapses to a marker when removed once), cap."""
    out = strip_display_text(text)
    while _GITLEAKS_ALLOW_RE.search(out):
        out = _GITLEAKS_ALLOW_RE.sub("", out)
    return _cap(strip_display_text(out), max_chars)


async def redact_all(
    strings: Sequence[str],
    redactor: TextRedactor,
    nonce_factory: Callable[[], str] = default_nonce,
) -> list[str]:
    """Redact every string with ONE ``redactor.redact`` call (one scanner
    subprocess, not hundreds) and return the redacted display strings in order.

    Each string is first reduced to its display form (``prepare_display``), then
    all are joined by sentinel lines ``WT-BOUNDARY-<nonce>``. Fails closed with
    ``WalkthroughRedactionError`` if any input contains the nonce or the split
    does not give back exactly the input count: the scanner replaces whole
    lines, so a sentinel line can only vanish or be replaced.
    """
    if not strings:
        return []
    display = [prepare_display(s) for s in strings]
    nonce = nonce_factory()
    if any(nonce in s for s in (*strings, *display)):
        raise WalkthroughRedactionError("input contains the redaction boundary nonce")
    sentinel = f"{_SENTINEL_PREFIX}{nonce}"
    blob = "\n".join(part for s in display for part in (sentinel, s)) + f"\n{sentinel}"
    pieces = (await redactor.redact(blob)).split(sentinel)
    if len(pieces) != len(display) + 2 or pieces[0].strip() or pieces[-1].strip():
        raise WalkthroughRedactionError("redacted text did not split into the input count")
    # Re-derive the display form: the scanner may have introduced whitespace.
    return [strip_display_text(p) for p in pieces[1:-1]]


async def redact_for_display(
    summary: str | None,
    groups: Sequence[WalkthroughGroup],
    redactor: TextRedactor,
    nonce_factory: Callable[[], str] = default_nonce,
) -> tuple[str | None, list[WalkthroughGroup]]:
    """Redact exactly the strings that will be displayed (summary, group names and
    descriptions, notes, paths) in one ``redact_all`` call and rebuild the model."""
    strings: list[str] = [summary or ""]
    for g in groups:
        strings += [g.name, g.description]
        for f in g.files:
            strings += [f.path, f.note]
    it = iter(await redact_all(strings, redactor, nonce_factory))
    out_summary = next(it)
    out_groups: list[WalkthroughGroup] = []
    for g in groups:
        name, description = next(it), next(it)
        files = tuple(WalkthroughFile(path=next(it) or "(unnamed)", note=next(it)) for _ in g.files)
        out_groups.append(WalkthroughGroup(name=name, description=description, files=files))
    return (out_summary or None), out_groups


# --- render -------------------------------------------------------------------


def _code_span(path: str) -> str:
    """Inline code whose delimiter is longer than the longest backtick run in the
    path (the ``suggestion_block`` technique), padded when it starts or ends with one."""
    longest = max((len(run) for run in re.findall(r"`+", path)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if path.startswith("`") or path.endswith("`") else ""
    return f"{fence}{pad}{path}{pad}{fence}"


def _file_line(file: WalkthroughFile) -> str:
    path = _cap(strip_display_text(file.path), PATH_MAX_CHARS) or "(unnamed)"
    note = neutralize_prose(_cap(strip_display_text(file.note), NOTE_MAX_CHARS))
    return f"- {_code_span(path)}" + (f" — {note}" if note else "")


def _group_header(group: WalkthroughGroup) -> str:
    name = neutralize_prose(_cap(strip_display_text(group.name), NAME_MAX_CHARS)) or "Changes"
    description = neutralize_prose(
        _cap(strip_display_text(group.description), DESCRIPTION_MAX_CHARS)
    )
    return f"### {name}\n" + (f"{description}\n" if description else "")


def _fits(body: str, max_chars: int) -> bool:
    return len(body) <= max_chars and len(body.encode()) <= _HARD_MAX_BYTES


def render_comment(
    *,
    summary: str | None,
    groups: Sequence[WalkthroughGroup],
    head_sha: str,
    max_chars: int,
    extra_not_shown: int = 0,
) -> RenderedComment:
    """Render the fixed-structure comment from redacted display data.

    Cut at a group boundary to fit ``max_chars`` (and the hard ceiling) with a
    final "N more files not shown" line counting every omitted file plus
    ``extra_not_shown`` (files never sent to the model). The summary and the first
    group's header are always kept; if they alone overflow, that group's file list
    is truncated (US-W6). ``extra_not_shown`` is never negative.
    """
    budget = min(max_chars, HARD_MAX_CHARS)
    sha = "".join(c for c in head_sha if c.isalnum())[:40]
    head = f"{MARKER}\n{TITLE}\n\n"
    clean_summary = neutralize_prose(_cap(strip_display_text(summary or ""), SUMMARY_MAX_CHARS))
    if clean_summary:
        head += f"### Summary\n{clean_summary}\n\n"
    footer = f"\n---\n{FIXED_FOOTER_LINE}\nDescribes commit `{sha}`.\n"
    total_files = sum(len(g.files) for g in groups)

    def assemble(blocks: list[str], shown: int) -> str:
        missing = total_files - shown + max(extra_not_shown, 0)
        tail = f"\n{missing} more files not shown\n" if missing else ""
        return head + "".join(blocks) + tail + footer

    blocks: list[str] = []
    shown = 0
    for index, group in enumerate(groups):
        lines = [_file_line(f) for f in group.files]
        block = _group_header(group) + "".join(f"{line}\n" for line in lines) + "\n"
        if _fits(assemble([*blocks, block], shown + len(lines)), budget):
            blocks.append(block)
            shown += len(lines)
            continue
        if index == 0:
            keep = len(lines)
            while keep > 0:
                keep -= 1
                block = _group_header(group) + "".join(f"{line}\n" for line in lines[:keep]) + "\n"
                if _fits(assemble([block], keep), budget):
                    break
            blocks.append(block)
            shown = keep
        break
    body = assemble(blocks, shown)
    return RenderedComment(
        body=body,
        files_shown=shown,
        files_not_shown=total_files - shown + max(extra_not_shown, 0),
    )
