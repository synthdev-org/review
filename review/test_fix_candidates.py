"""Unit tests for review.fix_candidates (US-AF2): the eligibility matrix,
ordering, same-line collapse and overlap resolution."""

from __future__ import annotations

from findings.models import Finding, Severity, Suggestion
from ingestion.diff import DiffFile
from review.fix_candidates import AcceptedFix, Candidate, resolve_overlaps, select_candidates

_PATCH = "@@ -1,2 +1,3 @@\n a\n+b\n c\n"  # changed new-file line: 2


def _finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "file": "app.py",
        "line": 2,
        "severity": "medium",
        "message": "m",
        "source": "ruff",
    }
    defaults.update(overrides)
    return Finding.model_validate(defaults)


def _files(path: str = "app.py", patch: str | None = _PATCH) -> list[DiffFile]:
    return [DiffFile(path=path, status="modified", patch=patch)]


def _select(
    *findings: Finding, files: list[DiffFile] | None = None, verification: bool = True
) -> list[Candidate]:
    return select_candidates(
        list(findings), files if files is not None else _files(), verification_enabled=verification
    )


def test_every_analyzer_source_is_a_candidate() -> None:
    for source in ("ruff", "eslint", "golangci_lint", "phpstan", "semgrep"):
        assert len(_select(_finding(source=source))) == 1, source


def test_never_candidate_sources() -> None:
    for source in ("gitleaks", "trivy", "clone_detection"):
        assert _select(_finding(source=source)) == [], source


def test_llm_needs_verified_status_and_verification_enabled() -> None:
    verified = _finding(source="llm", verification_status="verified")

    assert len(_select(verified)) == 1
    assert _select(verified, verification=False) == []
    assert _select(_finding(source="llm")) == []
    assert _select(_finding(source="llm", verification_status="rejected")) == []


def test_info_severity_is_never_a_candidate() -> None:
    info: Severity = "info"
    assert _select(_finding(severity=info)) == []


def test_line_patch_and_language_requirements() -> None:
    assert _select(_finding(line=None)) == []
    assert _select(_finding(line=1)) == []  # context line, not changed
    assert _select(_finding(), files=_files(patch=None)) == []
    assert _select(_finding(), files=[]) == []
    assert _select(_finding(file="notes.txt"), files=_files("notes.txt")) == []
    assert _select(_finding(file="a.js"), files=_files("a.js"))[0].line == 2


def test_candidates_are_ordered_by_severity_then_source_rank() -> None:
    low = _finding(line=2, file="a.py", severity="low", message="low")
    llm = _finding(
        line=3, file="a.py", severity="high", source="llm", verification_status="verified"
    )
    ruff = _finding(line=4, file="a.py", severity="high", message="ruff")
    crit = _finding(line=5, file="a.py", severity="critical", message="crit")
    patch = "@@ -1,1 +1,5 @@\n a\n+b\n+c\n+d\n+e\n"

    ordered = [c.finding for c in _select(low, llm, ruff, crit, files=_files("a.py", patch))]

    assert ordered == [crit, ruff, llm, low]


def test_same_file_and_line_collapses_to_the_highest_ranked() -> None:
    weak = _finding(severity="low", message="weak")
    strong = _finding(severity="high", message="strong")

    assert [c.finding for c in _select(weak, strong)] == [strong]


def _fix(
    message: str, start: int, end: int, severity: Severity = "medium", file: str = "a.py"
) -> AcceptedFix:
    return AcceptedFix(
        finding=_finding(file=file, message=message, severity=severity),
        suggestion=Suggestion(start_line=start, end_line=end, replacement="x"),
    )


def test_overlapping_fixes_keep_the_higher_ranked() -> None:
    high = _fix("high", 5, 8, "high")
    low = _fix("low", 8, 10, "low")

    kept, dropped = resolve_overlaps([low, high])

    assert kept == [high]
    assert dropped == [low]


def test_adjacent_and_other_file_fixes_do_not_overlap() -> None:
    a = _fix("a", 1, 3)
    b = _fix("b", 4, 6)
    c = _fix("c", 1, 6, file="other.py")

    kept, dropped = resolve_overlaps([a, b, c])

    assert kept == [a, b, c]
    assert dropped == []
