"""Unit tests for the CI glue in ``apps/local_cli/ci.py``; the stories are in ``test_ci.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from apps.local_cli.ci import CiConfigurationError, gate_count, load_ci_context
from apps.local_cli.settings import LocalSettings
from findings.models import Finding, Severity


def _settings(tmp_path: Path, event: object | None = None, **overrides: object) -> LocalSettings:
    path = tmp_path / "event.json"
    if event is not None:
        path.write_text(json.dumps(event))
    values: dict[str, object] = {
        "github_actions": True,
        "github_event_name": "pull_request",
        "github_token": SecretStr("tok"),
        "github_repository": "acme/widgets",
        "github_event_path": path,
    }
    return LocalSettings(**{**values, **overrides})  # type: ignore[arg-type]


_PR_EVENT = {"pull_request": {"number": 7, "head": {"sha": "a" * 40}}}


def test_outside_actions_there_is_no_ci_context(tmp_path: Path) -> None:
    assert load_ci_context(_settings(tmp_path, github_actions=False)) is None


def test_a_pull_request_event_gives_the_repository_and_number(tmp_path: Path) -> None:
    ci = load_ci_context(_settings(tmp_path, _PR_EVENT))

    assert ci is not None
    assert (ci.repo_full_name, ci.pr_number, ci.head_sha) == ("acme/widgets", 7, "a" * 40)
    assert ci.token.get_secret_value() == "tok"


@pytest.mark.parametrize(
    ("event", "overrides", "mentions"),
    [
        (_PR_EVENT, {"github_token": None}, "GITHUB_TOKEN"),
        (_PR_EVENT, {"github_token": SecretStr("")}, "GITHUB_TOKEN"),
        (_PR_EVENT, {"github_repository": "not-a-repo"}, "GITHUB_REPOSITORY"),
        (_PR_EVENT, {"github_repository": "a/b/../c"}, "GITHUB_REPOSITORY"),
        (_PR_EVENT, {"github_event_path": None}, "GITHUB_EVENT_PATH"),
        ({"push": {}}, {}, "pull_request"),
        ({"pull_request": {"number": 0}}, {}, "event file"),
        ({"pull_request": {"number": "seven"}}, {}, "event file"),
        (None, {}, "event file"),  # the file does not exist
    ],
)
def test_an_actions_run_that_cannot_post_says_what_is_wrong(
    tmp_path: Path, event: object, overrides: dict[str, object], mentions: str
) -> None:
    with pytest.raises(CiConfigurationError, match=mentions):
        load_ci_context(_settings(tmp_path, event, **overrides))


def test_a_malformed_event_file_is_a_configuration_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path, _PR_EVENT)
    (tmp_path / "event.json").write_text("{not json")
    with pytest.raises(CiConfigurationError, match="event file"):
        load_ci_context(settings)


def _finding(severity: Severity) -> Finding:
    return Finding(file="a.py", line=1, severity=severity, message="m", source="llm")


def test_gate_count_counts_findings_at_or_above_the_threshold() -> None:
    severities: list[Severity] = ["critical", "high", "medium", "info"]
    findings = [_finding(s) for s in severities]
    assert gate_count(None, findings) == 0
    assert gate_count("critical", findings) == 1
    assert gate_count("high", findings) == 2
    assert gate_count("info", findings) == 4
    assert gate_count("high", []) == 0


@pytest.mark.parametrize("name", ["pull_request_target", "push", "workflow_run", None])
def test_only_the_pull_request_event_is_accepted_even_with_a_pull_request_payload(
    tmp_path: Path, name: str | None
) -> None:
    # pull_request_target payloads carry a ``pull_request`` object too and run with secrets.
    with pytest.raises(CiConfigurationError, match="never pull_request_target"):
        load_ci_context(_settings(tmp_path, _PR_EVENT, github_event_name=name))
