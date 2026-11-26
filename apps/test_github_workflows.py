"""Structural sanity checks for the CI/E2E GitHub Actions workflow files.

Neither .github/workflows/ci.yml nor e2e.yml has any other automated test
guarding its own correctness — a hand-edit that silently dropped a gating
job, a trigger, or a step's `run:` command would only be caught by actually
observing CI misbehave. These tests parse the real YAML and assert the
structure docs/rules/testing.md's CI-gating rule requires, so a regression
here fails locally instead of on the next push.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())  # type: ignore[no-any-return]


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML's default (YAML 1.1) loader reads the unquoted `on:` key as the
    # boolean True, not the string "on" — GitHub Actions workflow YAML hits
    # this every time, so both spellings must be checked.
    return workflow.get("on") or workflow.get(True) or {}


def _job_step_runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def test_ci_workflow_triggers_on_every_push_and_pull_request() -> None:
    triggers = _triggers(_load("ci.yml"))

    assert "push" in triggers, "ci.yml must run on every push (unit tests + lint gate)"
    assert "pull_request" in triggers, "ci.yml must run on every pull_request (integration gate)"


def test_ci_lint_and_typecheck_job_runs_all_three_gates() -> None:
    job = _load("ci.yml")["jobs"]["lint-and-typecheck"]
    runs = _job_step_runs(job)

    assert any("ruff check" in run for run in runs), "missing ruff check step"
    assert any("ruff format --check" in run for run in runs), "missing ruff format --check step"
    assert any("mypy --strict" in run for run in runs), "missing mypy --strict step"


def test_ci_unit_test_job_excludes_integration_tests() -> None:
    job = _load("ci.yml")["jobs"]["test"]
    runs = _job_step_runs(job)

    pytest_runs = [run for run in runs if "pytest" in run]
    assert pytest_runs, "missing a pytest step in the unit-test job"
    # No `-m integration` here on purpose (see ci.yml's own comment) —
    # pyproject.toml's addopts already excludes integration-marked tests
    # from a bare `pytest` invocation.
    assert not any("-m integration" in run for run in pytest_runs), (
        "unit-test job should run plain pytest (integration exclusion comes from "
        "pyproject.toml's addopts), not pass -m integration itself"
    )


def test_ci_integration_test_job_runs_on_pull_requests_and_main() -> None:
    job = _load("ci.yml")["jobs"]["integration-test"]
    runs = _job_step_runs(job)

    assert job.get("if") == (
        "github.event_name == 'pull_request' || github.ref == 'refs/heads/main'"
    ), "integration-test job must run on pull requests and pushes to main, not every push"
    assert any("pytest -m integration" in run for run in runs), (
        "integration-test job must actually run the integration-marked tests"
    )


def test_ci_helm_lint_job_runs_the_render_test_script() -> None:
    job = _load("ci.yml")["jobs"]["helm-lint"]
    runs = _job_step_runs(job)

    assert any("charts/review/test-render.sh" in run for run in runs)


def test_e2e_workflow_triggers_on_merge_to_main() -> None:
    triggers = _triggers(_load("e2e.yml"))

    push_trigger = triggers.get("push") or {}
    assert "main" in push_trigger.get("branches", []), (
        "e2e.yml must trigger on merge to main, per testing.md's E2E CI-gating rule"
    )


def test_e2e_workflow_runs_the_compose_e2e_suite() -> None:
    job = _load("e2e.yml")["jobs"]["e2e"]
    runs = _job_step_runs(job)

    assert any("pytest tests/e2e" in run for run in runs)


def test_live_product_knowledge_conformance_step_is_nightly_only_and_non_gating() -> None:
    steps = [
        step
        for step in _load("e2e.yml")["jobs"]["e2e"]["steps"]
        if "adapter.product_knowledge.conformance" in step.get("run", "")
    ]

    assert len(steps) == 1, "e2e.yml must carry exactly one live conformance step"
    step = steps[0]
    condition = step.get("if", "")
    # e2e.yml also triggers on push to main: the event guard keeps the live
    # run off merges, PK_LIVE_URL opts a repo in, and a red run never gates.
    if "github.event_name == 'schedule'" not in condition:
        pytest.fail("live step must run on schedule")
    if "github.event_name == 'workflow_dispatch'" not in condition:
        pytest.fail("live step must run on workflow_dispatch")
    assert "PK_LIVE_URL" in condition
    if step.get("continue-on-error") is not True:
        pytest.fail("live step must be non-gating (continue-on-error)")
    assert "PK_LIVE_URL" not in (WORKFLOWS_DIR / "ci.yml").read_text()
    if "adapter.product_knowledge.conformance" in (WORKFLOWS_DIR / "ci.yml").read_text():
        pytest.fail("ci.yml must not run the live conformance suite")


def test_ci_sonar_and_codacy_use_the_merged_coverage_job() -> None:
    jobs = _load("ci.yml")["jobs"]

    assert set(jobs["coverage"]["needs"]) == {"test", "integration-test"}
    assert jobs["sonarcloud"]["needs"] == "coverage"
    assert jobs["codacy-coverage"]["needs"] == "coverage"
    assert any("coverage combine" in run for run in _job_step_runs(jobs["coverage"]))
