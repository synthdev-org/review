"""Unit tests for the Claude CLI backend's pure parts: the invocation, the environment allow-list
and output parsing (plan Phase 3b). The process-level behaviour is in test_claude_cli_process.py."""

from __future__ import annotations

import pytest

from adapter.llm.claude_cli import (
    BACKEND_NAME,
    DEFAULT_MODEL_LABEL,
    ClaudeCliUnavailableError,
    build_argv,
    claude_cli_env,
    parse_result,
    render_prompt,
)
from adapter.llm.fake_claude import result_json
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, Message

_FORBIDDEN_FLAGS = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "bypassPermissions",
    "acceptEdits",
    "auto",
}


def test_every_tool_is_disabled_and_no_flag_skips_permissions() -> None:
    argv = build_argv("claude", model=None)

    assert argv[argv.index("--tools") + 1] == ""  # `--tools ""`: the CLI's "disable all tools"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert not _FORBIDDEN_FLAGS & set(argv)
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert "--bare" not in argv  # --bare never reads the subscription login


def test_the_model_flag_is_only_passed_when_a_model_was_named() -> None:
    assert not any(a.startswith("--model") for a in build_argv("claude", model=None))
    assert not any(a.startswith("--model") for a in build_argv("claude", model=DEFAULT_MODEL_LABEL))
    assert build_argv("claude", model="sonnet")[-1] == "--model=sonnet"
    assert build_argv("claude", model="--evil")[-1] == "--model=--evil"


def test_the_prompt_text_is_never_in_argv() -> None:
    request = CompletionRequest(
        model="m",
        messages=[
            Message(role="system", content="SYS-MARK"),
            Message(role="user", content="DIFF-MARK"),
        ],
    )

    assert "DIFF-MARK" not in " ".join(build_argv("claude", model=request.model))
    assert "SYS-MARK" not in " ".join(build_argv("claude", model=request.model))
    assert render_prompt(request) == "[system]\nSYS-MARK\n\n[user]\nDIFF-MARK"


def test_the_environment_is_an_allow_list_without_credentials() -> None:
    environ = {
        "PATH": "/bin",
        "HOME": "/home/dev",
        "HTTPS_PROXY": "http://proxy:3128",
        "GITHUB_TOKEN": "x",
        "GH_TOKEN": "x",
        "ANTHROPIC_API_KEY": "x",
        "OPENAI_API_KEY": "x",
        "REVIEW_LOCAL_LLM_API_KEY": "x",
        "CLAUDE_CODE_OAUTH_TOKEN": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
    }

    assert claude_cli_env(environ) == {
        "PATH": "/bin",
        "HOME": "/home/dev",
        "HTTPS_PROXY": "http://proxy:3128",
    }


def test_the_windows_profile_variables_the_cli_needs_pass_but_no_credentials_do() -> None:
    environ = {
        "SystemRoot": "C:\\Windows",
        "USERPROFILE": "C:\\Users\\dev",
        "APPDATA": "x",
        "GH_TOKEN": "x",
    }

    assert claude_cli_env(environ) == {k: v for k, v in environ.items() if k != "GH_TOKEN"}


def test_a_result_object_yields_its_text() -> None:
    assert parse_result(result_json("hello").encode()) == "hello"


@pytest.mark.parametrize(
    "stdout",
    [b"", b"not json", b"[]", b'{"type": "result"}', b'{"type":"assistant","result":"x"}'],
)
def test_anything_but_a_result_object_is_malformed(stdout: bytes) -> None:
    with pytest.raises(LLMMalformedResponseError) as caught:
        parse_result(stdout)

    assert BACKEND_NAME in str(caught.value)


def test_an_error_result_about_the_login_is_logged_out_not_model_output() -> None:
    with pytest.raises(ClaudeCliUnavailableError) as caught:
        parse_result(result_json("Not logged in - Please run /login", is_error=True).encode())

    assert caught.value.reason == "logged_out"


def test_any_other_error_result_is_unreachable_and_does_not_echo_the_text() -> None:
    with pytest.raises(LLMUnreachableError) as caught:
        parse_result(result_json("overloaded SECRET-TEXT", is_error=True).encode())

    assert "SECRET-TEXT" not in str(caught.value)
    assert BACKEND_NAME in str(caught.value)


def test_an_error_result_without_a_result_field_is_unreachable_not_malformed() -> None:
    with pytest.raises(LLMUnreachableError):
        parse_result(b'{"type": "result", "is_error": true, "subtype": "error_max_turns"}')
