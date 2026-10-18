"""review.fix_candidates — pure fix-candidate selection and overlap
resolution (docs/specs/spec-auto-fix.md US-AF2). A helper of the
``fix_generation`` step with no ``Step`` of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from findings.models import SEVERITY_RANK, Finding, Source, Suggestion, source_rank
from ingestion.diff import DiffFile
from review.changed_lines import changed_lines
from review.fix_validation import is_supported_path

# Producers whose findings are trusted enough to attempt a fix for. "llm" is
# handled separately (verified only); gitleaks, trivy and clone_detection
# are never candidates.
_ANALYZER_SOURCES: frozenset[Source] = frozenset(
    {"ruff", "eslint", "golangci_lint", "phpstan", "semgrep"}
)


@dataclass(frozen=True)
class Candidate:
    finding: Finding
    line: int
    patch: str


@dataclass(frozen=True)
class AcceptedFix:
    finding: Finding
    suggestion: Suggestion


def _rank(finding: Finding) -> tuple[int, int]:
    return (SEVERITY_RANK[finding.severity], source_rank(finding.source))


def _is_eligible_source(finding: Finding, verification_enabled: bool) -> bool:
    if finding.severity == "info":
        return False
    if finding.source == "llm":
        return verification_enabled and finding.verification_status == "verified"
    return finding.source in _ANALYZER_SOURCES


def select_candidates(
    findings: Sequence[Finding],
    diff_files: Sequence[DiffFile],
    *,
    verification_enabled: bool,
) -> list[Candidate]:
    """Eligible findings, best-ranked first, at most one per ``(file, line)``."""
    patches = {file.path: file.patch for file in diff_files}
    changed: dict[str, frozenset[int]] = {}
    eligible: list[Candidate] = []
    for finding in findings:
        patch = patches.get(finding.file)
        if (
            finding.line is None
            or not patch
            or not _is_eligible_source(finding, verification_enabled)
            or not is_supported_path(finding.file)
        ):
            continue
        if finding.file not in changed:
            changed[finding.file] = changed_lines(patch)
        if finding.line in changed[finding.file]:
            eligible.append(Candidate(finding=finding, line=finding.line, patch=patch))
    eligible.sort(key=lambda candidate: _rank(candidate.finding))
    seen: set[tuple[str, int]] = set()
    selected: list[Candidate] = []
    for candidate in eligible:
        key = (candidate.finding.file, candidate.line)
        if key not in seen:
            seen.add(key)
            selected.append(candidate)
    return selected


def resolve_overlaps(
    accepted: Sequence[AcceptedFix],
) -> tuple[list[AcceptedFix], list[AcceptedFix]]:
    """Keep the higher-ranked of any two same-file fixes whose ranges overlap;
    returns ``(kept, dropped)`` with ``kept`` best-ranked first."""
    kept: list[AcceptedFix] = []
    dropped: list[AcceptedFix] = []
    for fix in sorted(accepted, key=lambda item: _rank(item.finding)):
        clash = any(
            other.finding.file == fix.finding.file
            and fix.suggestion.start_line <= other.suggestion.end_line
            and other.suggestion.start_line <= fix.suggestion.end_line
            for other in kept
        )
        (dropped if clash else kept).append(fix)
    return kept, dropped
