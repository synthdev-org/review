"""Integration tests for US-L7 (use my Claude subscription) and US-L8 (do not smuggle a personal
login into CI), through the real composition root against the recorded Claude CLI
(adapter/llm/fake_claude.py), real git and the real pipeline.

The backend is an explicit opt-in; whether Anthropic's terms permit it is UNVERIFIED (plan B4).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from adapter.llm.fake_claude import FakeClaude, process_is_gone
from adapter.local.conftest import Repo, feature_branch_repo, requires_git
from apps.local_cli.main import (
    EXIT_OK,
    EXIT_RUNTIME_FAILURE,
    EXIT_USAGE,
    build_parser,
    run_review,
)
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_FINDING = "app.py:2: high: result starts as an int, so a list of floats mixes types"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    cli = FakeClaude(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{cli.directory}:{os.environ['PATH']}")
    cli.set_reply(
        json.dumps(
            {
                "findings": [
                    {
                        "file": "app.py",
                        "line": 2,
                        "severity": "high",
                        "message": "result starts as an int, so a list of floats mixes types",
                    }
                ]
            }
        )
    )
    cli.set_reply("Rewrites total() as an explicit accumulation loop.", stage="summary")
    cli.set_reply(
        json.dumps(
            {
                "verdicts": [
                    {
                        "index": 0,
                        "valid": True,
                        "actionable": True,
                        "specific": True,
                        "valuable": True,
                    }
                ]
            }
        ),
        stage="verify",
    )
    return cli


def _settings(**overrides: object) -> LocalSettings:
    return LocalSettings(
        backend="claude_cli",
        static_analysis_enabled=False,
        **overrides,  # type: ignore[arg-type]
    )


async def _run(
    repo: Repo, settings: LocalSettings, *, allow_in_ci: bool = False
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(
        settings,
        base="main",
        repo_dir=repo.path,
        out=out,
        err=err,
        allow_claude_cli_in_ci=allow_in_ci,
    )
    return status, out.getvalue(), err.getvalue()


def test_the_backend_is_opt_in_and_the_default_is_the_openai_compatible_endpoint() -> None:
    assert LocalSettings().backend == "openai_compatible"
    assert LocalSettings(backend="claude_cli").backend == "claude_cli"


def test_the_override_flag_exists_on_the_run_command() -> None:
    args = build_parser().parse_args(["run", "--base", "main", "--allow-claude-cli-in-ci"])

    assert args.allow_claude_cli_in_ci is True
    assert build_parser().parse_args(["run", "--base", "main"]).allow_claude_cli_in_ci is False


@pytest.mark.asyncio
async def test_us_l7_a_review_completes_on_the_cli_with_no_api_key_read_and_usage_not_available(
    tmp_path: Path, fake: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "canary-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "canary-openai")
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings())

    assert status == EXIT_OK, err
    assert out.splitlines() == [_FINDING, "1 finding(s)."]
    assert "backend: claude_cli" in err
    assert "token usage: not available" in err
    assert "overridden" not in err
    calls = fake.calls()
    assert len(calls) >= 2  # summary, review and verification each went through the CLI
    for call in calls:
        assert "--tools" in call.argv and "--dangerously-skip-permissions" not in call.argv
        assert not any("canary" in value for value in call.env.values())
        assert "ANTHROPIC_API_KEY" not in call.env
        assert Path(call.cwd) != repo.path
    assert any("+    result = 0" in call.stdin for call in calls)  # the diff went on stdin
    assert not any("+    result = 0" in " ".join(call.argv) for call in calls)
    assert "canary" not in out + err


@pytest.mark.asyncio
async def test_us_l7_a_missing_cli_exits_non_zero_saying_so_and_sends_nothing(
    tmp_path: Path, fake: FakeClaude
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(claude_cli_executable="claude-not-installed"))

    assert status == EXIT_RUNTIME_FAILURE
    assert "the model backend is unavailable" in err
    assert "Claude CLI" in err and "not found" in err
    assert out == "" and fake.calls() == [] and fake.auth_calls == 0


@pytest.mark.asyncio
async def test_us_l7_a_logged_out_cli_exits_non_zero_saying_so_before_sending_content(
    tmp_path: Path, fake: FakeClaude
) -> None:
    fake.set_mode("logged_out")
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings())

    assert status == EXIT_RUNTIME_FAILURE
    assert "the model backend is unavailable" in err
    assert "Claude CLI" in err and "not logged in" in err
    assert out == "" and fake.calls() == []


@pytest.mark.asyncio
async def test_us_l7_a_hung_cli_fails_within_the_timeout_naming_the_backend_and_is_reaped(
    tmp_path: Path, fake: FakeClaude
) -> None:
    fake.set_mode("hang")
    repo = feature_branch_repo(tmp_path / "repo")
    started = time.monotonic()

    status, out, err = await _run(repo, _settings(llm_timeout_seconds=1.0))

    assert status == EXIT_RUNTIME_FAILURE
    assert time.monotonic() - started < 15
    assert "Claude CLI" in err and "Traceback" not in err
    assert out == ""
    assert process_is_gone(fake.pid)


@pytest.mark.asyncio
async def test_us_l8_in_ci_the_backend_is_refused_and_no_process_is_started(
    tmp_path: Path, fake: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(ci=True))

    assert status == EXIT_USAGE
    assert "CI was detected" in err and "--allow-claude-cli-in-ci" in err
    assert "best-effort guard, not a security control" in err
    assert out == "" and fake.calls() == [] and fake.auth_calls == 0


@pytest.mark.asyncio
async def test_us_l8_github_actions_alone_is_also_detected_as_ci(
    tmp_path: Path, fake: FakeClaude
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    status, _out, err = await _run(repo, _settings(github_actions=True))

    assert status == EXIT_USAGE and "CI was detected" in err and fake.calls() == []


@pytest.mark.asyncio
async def test_us_l8_the_explicit_override_proceeds_and_the_summary_states_it(
    tmp_path: Path, fake: FakeClaude
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    # CI=true without a pull-request context also refuses the memory file; the override is the
    # thing under test, so the memory file is switched off the documented way.
    status, out, err = await _run(
        repo, _settings(ci=True, memory_file_enabled=False), allow_in_ci=True
    )

    assert status == EXIT_OK, err
    assert out.splitlines() == [_FINDING, "1 finding(s)."]
    assert "CI guard overridden by --allow-claude-cli-in-ci" in err
    assert fake.calls()


@pytest.mark.asyncio
async def test_the_override_flag_does_nothing_outside_ci_and_is_not_reported(
    tmp_path: Path, fake: FakeClaude
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    status, _out, err = await _run(repo, _settings(), allow_in_ci=True)

    assert status == EXIT_OK and "overridden" not in err


@pytest.mark.asyncio
async def test_a_dry_run_on_the_cli_backend_starts_no_process_and_names_the_backend(
    tmp_path: Path, fake: FakeClaude
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    out, err = io.StringIO(), io.StringIO()

    status = await run_review(
        _settings(), base="main", repo_dir=repo.path, out=out, err=err, dry_run=True
    )

    assert status == EXIT_OK
    assert "Model endpoint Claude CLI" in out.getvalue()
    assert fake.calls() == [] and fake.auth_calls == 0
