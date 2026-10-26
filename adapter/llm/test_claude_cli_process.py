"""Integration tests for the Claude CLI backend against a recorded subprocess (plan Phase 3b,
US-L7): the real adapter spawns a real process (adapter/llm/fake_claude.py) and the tests assert
what that process received, from the recording, not from the adapter's own opinion of it."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from adapter.llm.claude_cli import ClaudeCliClient, ClaudeCliUnavailableError, build_argv
from adapter.llm.fake_claude import FakeClaude, process_is_gone, result_json
from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, Message

pytestmark = pytest.mark.integration

_REQUEST = CompletionRequest(
    model="sonnet",
    messages=[
        Message(role="system", content="SYSTEM-MARKER"),
        Message(role="user", content="DIFF-MARKER"),
    ],
    max_tokens=100,
    temperature=0.0,
)


@pytest.fixture
def fake(tmp_path: Path) -> FakeClaude:
    return FakeClaude(tmp_path / "bin")


def _client(fake: FakeClaude, **extra_env: str) -> ClaudeCliClient:
    environ = {"PATH": f"{fake.directory}:{os.environ['PATH']}", "HOME": str(fake.directory)}
    return ClaudeCliClient(executable="claude", environ={**environ, **extra_env})


def test_the_client_satisfies_the_llm_client_protocol(fake: FakeClaude) -> None:
    assert isinstance(_client(fake), LLMClient)


@pytest.mark.asyncio
async def test_us_l7_a_completion_returns_the_text_and_reports_usage_as_not_available(
    fake: FakeClaude,
) -> None:
    fake.set_reply("the answer")

    response = await _client(fake).complete(_REQUEST, timeout=10)

    assert response.content == "the answer"
    assert response.model == "sonnet"
    assert response.usage is None  # the CLI's own counters are not API token counts


@pytest.mark.asyncio
async def test_us_l7_tools_are_disabled_and_the_prompt_travels_on_stdin_not_argv(
    fake: FakeClaude,
) -> None:
    await _client(fake).complete(_REQUEST, timeout=10)

    (call,) = fake.calls()
    assert call.argv == build_argv("claude", model="sonnet")[1:]  # exactly the hardened invocation
    assert call.argv[call.argv.index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in call.argv
    assert "bypassPermissions" not in call.argv
    assert "DIFF-MARKER" not in " ".join(call.argv)
    assert "SYSTEM-MARKER" not in " ".join(call.argv)
    assert call.stdin == "[system]\nSYSTEM-MARKER\n\n[user]\nDIFF-MARKER"


@pytest.mark.asyncio
async def test_us_l7_it_runs_in_an_empty_temporary_directory_removed_afterwards(
    fake: FakeClaude, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secret.txt").write_text("x")
    previous = Path.cwd()
    os.chdir(repo)
    try:
        await _client(fake).complete(_REQUEST, timeout=10)
    finally:
        os.chdir(previous)

    (call,) = fake.calls()
    assert Path(call.cwd) != repo and not Path(call.cwd).is_relative_to(repo)
    assert Path(call.cwd).name.startswith("review-claude-")
    assert not Path(call.cwd).exists()


@pytest.mark.asyncio
async def test_us_l7_no_api_key_or_token_is_read_or_passed(
    fake: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = {
        "ANTHROPIC_API_KEY": "canary-anthropic",
        "ANTHROPIC_AUTH_TOKEN": "canary-auth",
        "GITHUB_TOKEN": "canary-github",
        "OPENAI_API_KEY": "canary-openai",
        "REVIEW_LOCAL_LLM_API_KEY": "canary-local",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    client = ClaudeCliClient(
        executable="claude",
        environ={**os.environ, "PATH": f"{fake.directory}:{os.environ['PATH']}"},
    )

    await client.complete(_REQUEST, timeout=10)

    (call,) = fake.calls()
    assert not set(secrets) & set(call.env)
    assert not any("canary" in value for value in call.env.values())
    assert "PATH" in call.env  # the allow-list still lets the CLI find its tools


@pytest.mark.asyncio
async def test_us_l7_a_missing_cli_fails_before_any_process_runs(tmp_path: Path) -> None:
    client = ClaudeCliClient(
        executable="claude-does-not-exist", environ={"PATH": str(tmp_path), "HOME": str(tmp_path)}
    )

    with pytest.raises(ClaudeCliUnavailableError) as preflight:
        await client.preflight()
    with pytest.raises(ClaudeCliUnavailableError) as complete:
        await client.complete(_REQUEST, timeout=10)

    assert preflight.value.reason == complete.value.reason == "missing"
    assert "Claude CLI" in str(preflight.value) and "not found" in str(preflight.value)


@pytest.mark.asyncio
async def test_us_l7_a_logged_out_cli_fails_the_preflight_before_content_is_sent(
    fake: FakeClaude,
) -> None:
    fake.set_mode("logged_out")

    with pytest.raises(ClaudeCliUnavailableError) as caught:
        await _client(fake).preflight()

    assert caught.value.reason == "logged_out"
    assert "not logged in" in str(caught.value) and "Claude CLI" in str(caught.value)
    assert fake.auth_calls == 1
    assert fake.calls() == []  # no completion call, so no prompt, was made


@pytest.mark.asyncio
async def test_a_logged_in_cli_passes_the_preflight_without_sending_a_prompt(
    fake: FakeClaude,
) -> None:
    await _client(fake).preflight()

    assert fake.auth_calls == 1
    assert fake.calls() == []


@pytest.mark.asyncio
async def test_a_login_that_lapses_mid_run_is_reported_as_logged_out(fake: FakeClaude) -> None:
    fake.set_mode("lapsed")

    with pytest.raises(ClaudeCliUnavailableError) as caught:
        await _client(fake).complete(_REQUEST, timeout=10)

    assert caught.value.reason == "logged_out"


@pytest.mark.asyncio
async def test_us_l7_a_hung_cli_is_killed_and_reaped_and_reported_naming_the_backend(
    fake: FakeClaude,
) -> None:
    fake.set_mode("hang")
    started = time.monotonic()

    with pytest.raises(LLMUnreachableError) as caught:
        await _client(fake).complete(_REQUEST, timeout=1)

    assert time.monotonic() - started < 10
    assert "Claude CLI" in str(caught.value) and "killed" in str(caught.value)
    assert process_is_gone(fake.pid)  # reaped: not left running and not a zombie


@pytest.mark.asyncio
async def test_a_crashing_cli_is_reported_without_echoing_its_output(fake: FakeClaude) -> None:
    fake.set_mode("crash")

    with pytest.raises(LLMUnreachableError) as caught:
        await _client(fake).complete(_REQUEST, timeout=10)

    assert "status 3" in str(caught.value) and "boom" not in str(caught.value)


@pytest.mark.asyncio
async def test_output_that_is_not_the_json_result_is_malformed(fake: FakeClaude) -> None:
    fake.set_mode("garbage")

    with pytest.raises(LLMMalformedResponseError):
        await _client(fake).complete(_REQUEST, timeout=10)


@pytest.mark.asyncio
async def test_output_beyond_the_size_cap_is_rejected(fake: FakeClaude) -> None:
    fake.set_mode("huge")

    with pytest.raises(LLMMalformedResponseError) as caught:
        await _client(fake).complete(_REQUEST, timeout=20)

    assert "size cap" in str(caught.value)


@pytest.mark.asyncio
async def test_an_error_result_is_a_failure_not_model_output(fake: FakeClaude) -> None:
    (fake.directory / "reply").write_text(result_json("overloaded", is_error=True))

    with pytest.raises(LLMUnreachableError):
        await _client(fake).complete(_REQUEST, timeout=10)
