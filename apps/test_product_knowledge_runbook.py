"""US-PK15's runbook half (plan Phase 9): the operator runbook carries the one
egress-disclosure literal the Admin UI renders, and has a ``##`` entry for
every provider ``outcome`` an operator can see - an assertion, not a
link-check, so a copy edit on either side cannot silently drift.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from review.product_knowledge import ProviderOutcome

_ROOT = Path(__file__).resolve().parents[1]
_COPY = _ROOT / "apps/admin_api/frontend/src/screens/productKnowledgeCopy.ts"
_RUNBOOK = _ROOT / "docs/reference/operations.md"
_DASHBOARD = _ROOT / "charts/review/dashboards/review.json"
_METRIC = "review_product_knowledge_call_total"

# Outcomes that need no runbook entry of their own: the success/benign states
# are covered by the "Reading the signal" entry, and the kill-switch states by
# "Turning it off".
_COVERED_BY_OVERVIEW = {
    ProviderOutcome.OK,
    ProviderOutcome.EMPTY,
    ProviderOutcome.PARTIAL,
    ProviderOutcome.NO_QUERY,
    ProviderOutcome.NOT_CONFIGURED,
    ProviderOutcome.DISABLED,
}


def _disclosure_literal() -> str:
    match = re.search(r'PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE\s*=\s*"([^"]+)"', _COPY.read_text())
    assert match is not None, "disclosure constant not found in productKnowledgeCopy.ts"
    return match.group(1)


def test_runbook_contains_the_shared_egress_disclosure_literal() -> None:
    assert _disclosure_literal() in _RUNBOOK.read_text()


def test_runbook_has_an_entry_for_every_operator_visible_outcome() -> None:
    headings = [line for line in _RUNBOOK.read_text().splitlines() if line.startswith("## ")]
    missing = [
        outcome.value
        for outcome in ProviderOutcome
        if outcome not in _COVERED_BY_OVERVIEW
        and not any(f"`{outcome.value}`" in heading for heading in headings)
    ]
    assert not missing, f"operations.md has no `##` entry for outcomes: {missing}"


def test_runbook_metric_name_matches_the_shipped_dashboard() -> None:
    assert _METRIC in _RUNBOOK.read_text()
    assert _METRIC in _DASHBOARD.read_text()


def test_runbook_carries_the_orphaned_row_cleanup_statement() -> None:
    assert "DELETE FROM org_repo_team_setting" in _RUNBOOK.read_text()


def test_runbook_has_rotation_and_accepted_limitations_headings() -> None:
    headings = [
        line.lstrip("#").strip().lower()
        for line in _RUNBOOK.read_text().splitlines()
        if line.startswith("#")
    ]
    assert any(h.startswith("rotating a credential") for h in headings)
    assert any(h.startswith("accepted limitations") for h in headings)


_ROOT_READ_RE = re.compile(r'(?:_ROOT|ROOT|REPO_ROOT|parents\[\d\])((?:\s*/\s*"[^"]+")+)')
_SELF = Path(__file__).resolve()


def _repo_root_reads(test_file: Path) -> list[str]:
    """Repo-root relative paths a test module builds from ``_ROOT / "a" / "b"``."""
    return [
        "/".join(re.findall(r'"([^"]+)"', match.group(1)))
        for match in _ROOT_READ_RE.finditer(test_file.read_text())
    ]


def _is_copied(path: str, config: dict[str, list[str]]) -> bool:
    also_copy = [entry.rstrip("/") for entry in config["also_copy"]]
    return (
        path == "pyproject.toml"
        or any(path == e or path.startswith(f"{e}/") for e in also_copy)
        or path.startswith(tuple(config["source_paths"]))
        or not (_ROOT / path).exists()
    )


def test_every_repo_root_file_read_by_a_unit_test_is_in_mutmut_also_copy() -> None:
    # mutmut only copies source_paths + tests + also_copy into its sandbox; a
    # unit test reading any other repo-root file fails against every mutant.
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["mutmut"]
    test_files = [
        f
        for package in config["source_paths"]
        for f in (_ROOT / package).rglob("test_*.py")
        if f.resolve() != _SELF and "node_modules" not in f.parts
    ]
    uncovered = {
        f"{test_file.relative_to(_ROOT)}: {path}"
        for test_file in test_files
        for path in _repo_root_reads(test_file)
        if not _is_copied(path, config)
    }
    assert not uncovered, f"add to [tool.mutmut].also_copy: {sorted(uncovered)}"


def test_runbook_promql_block_uses_the_job_operation_worker_origin_and_config_outcomes() -> None:
    blocks = re.findall(r"```promql\n(.*?)```", _RUNBOOK.read_text(), flags=re.DOTALL)
    block = next(b for b in blocks if _METRIC in b)

    assert 'operation="job"' in block
    assert 'origin="worker"' in block
    outcome_re = re.search(r'outcome=~"([^"]+)"', block)
    assert outcome_re is not None
    outcomes = set(outcome_re.group(1).split("|"))
    assert outcomes <= {o.value for o in ProviderOutcome}
    assert {"credential_unavailable", "blocked_egress", "auth_failed"} <= outcomes


def test_runbook_cleanup_sql_is_scoped_to_the_provider_key() -> None:
    assert (
        "DELETE FROM org_repo_team_setting WHERE key = 'product_knowledge_provider'"
        in _RUNBOOK.read_text()
    )
