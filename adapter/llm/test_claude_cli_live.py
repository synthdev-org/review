"""Live-mode checks for the Claude CLI backend (plan Phase 3b), so the recorded fixture in
adapter/llm/fake_claude.py cannot drift from the real CLI unnoticed. Manual or nightly only,
never the default CI gate (docs/rules/testing.md).

- ``test_every_flag_we_pass_is_in_the_installed_clis_help`` reads ``claude --help`` only. It runs
  whenever a ``claude`` executable is on PATH (it makes no model call and uses no login) and is
  skipped, with the reason, otherwise.
- The completion test makes ONE real, tiny model call with your own login (so it counts against
  your subscription). It is skipped unless ``REVIEW_LIVE_CLAUDE_CLI=1``. Whether Anthropic's terms
  permit this use is UNVERIFIED (plan B4): run it only if you are satisfied they do.
  To run (host with the CLI installed and logged in):
      REVIEW_LIVE_CLAUDE_CLI=1 pytest -m integration adapter/llm/test_claude_cli_live.py
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from adapter.llm.claude_cli import ClaudeCliClient, build_argv
from llmgateway.models import CompletionRequest, Message

pytestmark = pytest.mark.integration

_needs_cli = pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not installed")
_live = pytest.mark.skipif(
    os.environ.get("REVIEW_LIVE_CLAUDE_CLI") != "1",
    reason="live mode: set REVIEW_LIVE_CLAUDE_CLI=1 (one real call, your own login; B4 unverified)",
)


@_needs_cli
def test_every_flag_we_pass_is_in_the_installed_clis_help() -> None:
    help_text = subprocess.run(
        ["claude", "--help"], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    flags = {arg for arg in build_argv("claude", model=None) if arg.startswith("-")}

    assert flags
    assert {flag for flag in flags if flag not in help_text} == set()


@_needs_cli
@_live
@pytest.mark.asyncio
async def test_a_real_completion_parses_and_reports_no_usage() -> None:
    client = ClaudeCliClient(executable="claude", environ=os.environ)
    await client.preflight()

    response = await client.complete(
        CompletionRequest(
            model="claude-cli-default",
            messages=[Message(role="user", content="Reply with the single word: pong")],
        ),
        timeout=120,
    )

    assert "pong" in response.content.lower()
    assert response.usage is None
