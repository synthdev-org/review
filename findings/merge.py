"""findings.merge — pure dedupe/combine logic for review findings, per US6 and
`findings/__init__.py`'s own docstring, which already designates this package for exactly this.

**Dedupe rule.** Two findings collapse to one when they share the same
``file``, the same ``line`` (including both ``None`` — a file-level
finding), and "similar enough" ``message`` text (a `difflib.SequenceMatcher`
ratio, case-insensitive, at or above `_SIMILARITY_THRESHOLD`) — a static-
analysis finding and an LLM finding both flagging the same defect on the
same line rarely word it identically, so an exact string match alone would
under-merge. The **first** occurrence in `findings` order wins (its own
`Finding` object is kept, later duplicates dropped) — since
`review.review_generation.ReviewGenerationOutput.findings` always lists static-analysis
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
ceiling in the review pipeline.

Pure, no I/O — zero infra imports, per architecture.md's domain-package
rule.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

from pydantic import BaseModel, ConfigDict, Field

from findings.models import SEVERITY_RANK, Finding, Tier

# After dedupe, findings beyond this ceiling are dropped entirely — never
# posted. See this module's own docstring for the full reasoning.
POSTED_FINDINGS_CEILING = 100

# A `SequenceMatcher.ratio()` at or above this (case-insensitive) counts two
# findings' messages as "the same defect worded differently" for dedupe
# purposes, not two independently-chosen constants: 0.8 is the same
# similarity bar `difflib`'s own documentation calls "good matches" —
# reused here rather than a bespoke threshold invented for this one case.
_MESSAGE_SIMILARITY_THRESHOLD = 0.8

# v2 within-job root-cause dedupe ignores file and line, so it needs a
# stricter bar than the same-location check above: two LLM
# findings that merely share boilerplate wording must stay separate.
_ROOT_CAUSE_SIMILARITY_THRESHOLD = 0.9

# Lower sorts first; a `None` tier counts as `should_fix`.
TIER_RANK: dict[Tier, int] = {"blocker": 0, "should_fix": 1, "nit": 2}


def effective_tier(finding: Finding) -> Tier:
    return finding.tier or "should_fix"


class MergeResult(BaseModel):
    """`merge_findings`'s own return shape — the deduped, ceiling-applied
    finding list plus how many were dropped by the ceiling (never by
    dedupe: a deduped-away finding simply isn't a duplicate, not a "drop"
    an operator needs visibility into the way a ceiling breach is).
    """

    model_config = ConfigDict(frozen=True)

    findings: list[Finding]
    dropped_by_ceiling_count: int
    # The findings the ceiling dropped, so an operator can see *which kinds*
    # (e.g. low-severity smell findings, which rank last) are being cut.
    dropped_by_ceiling: list[Finding] = Field(default_factory=list)
    # v2 only: one ``(kept "file:line", folded "file:line")`` pair per
    # root-cause merge, so the caller can log and count every merge.
    root_cause_merges: list[tuple[str, str]] = Field(default_factory=list)


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
    kept_by_key: dict[tuple[str, int | None], list[tuple[int, Finding]]] = {}
    for finding in findings:
        key = (finding.file, finding.line)
        bucket = kept_by_key.setdefault(key, [])
        duplicate: tuple[int, int, Finding] | None = None
        for bucket_pos, (deduped_index, existing) in enumerate(bucket):
            if _messages_similar(finding.message, existing.message):
                duplicate = (bucket_pos, deduped_index, existing)
                break
        if duplicate is None:
            deduped.append(finding)
            bucket.append((len(deduped) - 1, finding))
            continue
        duplicate_bucket_pos, duplicate_index, duplicate_of = duplicate
        # The first occurrence still wins the message/severity/source — the
        # PRD's "deterministic first pass as the free trust anchor" rationale
        # (this module's own docstring) — but if the dropped duplicate is the
        # one carrying `context_ref_ids` (US6) and the kept finding
        # isn't, carry that field forward onto the kept finding rather than
        # silently discarding it. Without this, a static-analysis finding
        # deduped against a citation-bearing LLM finding for the same defect
        # would drop the citation entirely — `review.output`'s later
        # `_apply_citations` pass runs *after* `merge_findings`, so a dropped
        # `context_ref_ids` here can never be recovered downstream.
        if duplicate_of.context_ref_ids is None and finding.context_ref_ids is not None:
            merged = duplicate_of.model_copy(update={"context_ref_ids": finding.context_ref_ids})
            deduped[duplicate_index] = merged
            bucket[duplicate_bucket_pos] = (duplicate_index, merged)
    return deduped


# Backticked spans, or identifier-like words (internal `_`/`.` or camelCase).
_CODE_TOKEN_RE = re.compile(r"`[^`\n]+`|\b[A-Za-z0-9]+(?:[._][A-Za-z0-9]+)+\b|\b[a-z]+[A-Z]\w*\b")


def _code_tokens(message: str) -> frozenset[str]:
    return frozenset(t.casefold() for t in _CODE_TOKEN_RE.findall(message))


def _location(finding: Finding) -> str:
    return f"{finding.file}:{finding.line}" if finding.line is not None else finding.file


def _is_root_cause_of(candidate: Finding, existing: Finding) -> bool:
    # LLM-sourced only: a static-analysis rule firing in two files is two
    # real occurrences, not one root cause.
    return (
        candidate.source == "llm"
        and existing.source == "llm"
        and candidate.category == existing.category
        and candidate.rule_id == existing.rule_id
        # Messages differing only in an identifier (`user` vs `users`) are
        # distinct defects, however similar the surrounding prose.
        and _code_tokens(candidate.message) == _code_tokens(existing.message)
        and SequenceMatcher(None, candidate.message.casefold(), existing.message.casefold()).ratio()
        >= _ROOT_CAUSE_SIMILARITY_THRESHOLD
    )


def _winner_rank(finding: Finding) -> tuple[int, int]:
    # Tier first: a blocker report must never lose to a nit-tier duplicate.
    return TIER_RANK[effective_tier(finding)], SEVERITY_RANK[finding.severity]


def _fold_root_cause(finding: Finding, existing: Finding) -> tuple[Finding, tuple[str, str]]:
    # The highest-severity member supplies message/scenario/severity/tier
    # (ties: first occurrence), so text is never posted under a label it
    # did not earn; the displaced member's location is recorded.
    winner, loser = (
        (finding, existing)
        if _winner_rank(finding) < _winner_rank(existing)
        else (existing, finding)
    )
    # Union of both members' other_locations plus the loser's own, ordered,
    # de-duplicated, never including the winner's own location.
    own = _location(winner)
    others = dict.fromkeys(
        loc
        for loc in (
            *(existing.other_locations or ()),
            *(finding.other_locations or ()),
            _location(loser),
        )
        if loc != own
    )
    update: dict[str, object] = {"other_locations": tuple(others)}
    if winner.context_ref_ids is None and loser.context_ref_ids is not None:
        update["context_ref_ids"] = loser.context_ref_ids
    return winner.model_copy(update=update), (_location(winner), _location(loser))


def _dedupe_root_cause(
    findings: Sequence[Finding],
) -> tuple[list[Finding], list[tuple[str, str]]]:
    """v2: fold the same defect reported at different locations into the first
    occurrence's slot, keeping the highest-tier (then highest-severity) member's text, severity and
    tier and recording the other locations in ``other_locations``. Runs after
    the same-location `_dedupe`.
    """
    kept: list[Finding] = []
    merges: list[tuple[str, str]] = []
    for finding in findings:
        index = next((i for i, e in enumerate(kept) if _is_root_cause_of(finding, e)), None)
        if index is None:
            kept.append(finding)
            continue
        kept[index], merge = _fold_root_cause(finding, kept[index])
        merges.append(merge)
    return kept, merges


# US-V8: once the ceiling actually truncates, order survivors by
# severity first, then source-trust (non-"llm" — i.e. static-analysis —
# ahead of "llm" at equal severity; by the time a finding reaches here,
# any "llm"-sourced one has already survived verification, or
# verification was disabled entirely, so `source` alone is a
# well-defined trust signal either way). `sorted()` is stable, so
# original relative order is preserved as the final tiebreak for free.
def _severity_trust_key(finding: Finding) -> tuple[int, int]:
    return SEVERITY_RANK[finding.severity], 0 if finding.source != "llm" else 1


def _tier_key(finding: Finding) -> tuple[int, int, int]:
    tier = TIER_RANK[effective_tier(finding)]
    return (tier, *_severity_trust_key(finding))


def merge_findings(findings: Sequence[Finding], *, tiered: bool = False) -> MergeResult:
    """Dedupe `findings` (see this module's own docstring for the rule),
    then cap the result at `POSTED_FINDINGS_CEILING` — the pure logic
    `review/output.py`'s findings-merge-and-output step wraps with its own
    I/O (posting, dry-run logging) and observability (the `warning` log for
    a non-zero `dropped_by_ceiling_count`).

    ``tiered`` is the v2 behaviour: additionally fold the same
    root cause across locations, and order by tier first so ``nit`` is cut
    first by the ceiling. ``False`` (v1) is exactly the pre-existing behaviour.
    """
    deduped = _dedupe(findings)
    merges: list[tuple[str, str]] = []
    if tiered:
        deduped, merges = _dedupe_root_cause(deduped)
        deduped = sorted(deduped, key=_tier_key)
    if len(deduped) <= POSTED_FINDINGS_CEILING:
        return MergeResult(findings=deduped, dropped_by_ceiling_count=0, root_cause_merges=merges)
    # Under `tiered` the list is already ordered tier-first above.
    ordered = deduped if tiered else sorted(deduped, key=_severity_trust_key)
    return MergeResult(
        findings=ordered[:POSTED_FINDINGS_CEILING],
        dropped_by_ceiling_count=len(deduped) - POSTED_FINDINGS_CEILING,
        dropped_by_ceiling=ordered[POSTED_FINDINGS_CEILING:],
        root_cause_merges=merges,
    )
