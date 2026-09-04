"""findings.merge — pure dedupe/combine logic for review findings, per the
plan's Phase 7 section (US6) and `findings/__init__.py`'s own docstring,
which already designates this package for exactly this.

**Dedupe rule.** Two findings collapse to one when they share the same
``file``, the same ``line`` (including both ``None`` — a file-level
finding), and "similar enough" ``message`` text (a `difflib.SequenceMatcher`
ratio, case-insensitive, at or above `_SIMILARITY_THRESHOLD`) — a static-
analysis finding and an LLM finding both flagging the same defect on the
same line rarely word it identically, so an exact string match alone would
under-merge. The **first** occurrence in `findings` order wins (its own
`Finding` object is kept, later duplicates dropped) — since
`review.llm_review.LLMReviewOutput.findings` always lists static-analysis
findings before LLM-sourced ones (see that module's own docstring), this
means a static-analysis finding is preferred over an LLM-sourced duplicate
of it wherever both fire on the same defect, per the PRD's own "deterministic
first pass as the free trust anchor" design rationale (US4).

**Posted-findings ceiling.** GitHub's PR-review API has its own practical
limits on comment count/payload size per review; an otherwise-successful
review with an implausibly large merged-finding count (a pathological diff,
or a redaction-cap-adjacent malformed LLM response) could fail the posting
call outright with no findings delivered at all. Findings beyond
`POSTED_FINDINGS_CEILING` (applied *after* dedupe) are dropped — never
posted — so a large-but-real diff still gets a posted review instead of an
outright posting failure; the caller (`review/output.py`) logs the dropped
count at `warning`, the same capped-and-logged posture as every other
ceiling this plan adds.

Pure, no I/O — zero infra imports, per architecture.md's domain-package
rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict

from findings.models import Finding

# After dedupe, findings beyond this ceiling are dropped entirely — never
# posted. See this module's own docstring for the full reasoning.
POSTED_FINDINGS_CEILING = 100

# A `SequenceMatcher.ratio()` at or above this (case-insensitive) counts two
# findings' messages as "the same defect worded differently" for dedupe
# purposes, not two independently-chosen constants: 0.8 is the same
# similarity bar `difflib`'s own documentation calls "good matches" —
# reused here rather than a bespoke threshold invented for this one case.
_MESSAGE_SIMILARITY_THRESHOLD = 0.8


class MergeResult(BaseModel):
    """`merge_findings`'s own return shape — the deduped, ceiling-applied
    finding list plus how many were dropped by the ceiling (never by
    dedupe: a deduped-away finding simply isn't a duplicate, not a "drop"
    an operator needs visibility into the way a ceiling breach is).
    """

    model_config = ConfigDict(frozen=True)

    findings: list[Finding]
    dropped_by_ceiling_count: int


def _messages_similar(a: str, b: str) -> bool:
    ratio = SequenceMatcher(None, a.casefold(), b.casefold()).ratio()
    return ratio >= _MESSAGE_SIMILARITY_THRESHOLD


def _is_duplicate_of(candidate: Finding, existing: Finding) -> bool:
    return (
        candidate.file == existing.file
        and candidate.line == existing.line
        and _messages_similar(candidate.message, existing.message)
    )


def _dedupe(findings: Sequence[Finding]) -> list[Finding]:
    deduped: list[Finding] = []
    # Bucketed by (file, line) first: `_is_duplicate_of` already requires an
    # exact file/line match before it bothers with the expensive
    # `SequenceMatcher` similarity check, so grouping candidates by that same
    # key up front skips the O(n^2) similarity scan entirely for findings on
    # different files/lines, which is the overwhelming majority of pairs on
    # any diff touching more than a couple of files.
    kept_by_key: dict[tuple[str, int | None], list[Finding]] = {}
    for finding in findings:
        key = (finding.file, finding.line)
        bucket = kept_by_key.setdefault(key, [])
        if not any(_messages_similar(finding.message, existing.message) for existing in bucket):
            bucket.append(finding)
            deduped.append(finding)
    return deduped


def merge_findings(findings: Sequence[Finding]) -> MergeResult:
    """Dedupe `findings` (see this module's own docstring for the rule),
    then cap the result at `POSTED_FINDINGS_CEILING` — the pure logic
    `review/output.py`'s findings-merge-and-output step wraps with its own
    I/O (posting, dry-run logging) and observability (the `warning` log for
    a non-zero `dropped_by_ceiling_count`).
    """
    deduped = _dedupe(findings)
    if len(deduped) <= POSTED_FINDINGS_CEILING:
        return MergeResult(findings=deduped, dropped_by_ceiling_count=0)

    kept = deduped[:POSTED_FINDINGS_CEILING]
    dropped_count = len(deduped) - POSTED_FINDINGS_CEILING
    return MergeResult(findings=kept, dropped_by_ceiling_count=dropped_count)
