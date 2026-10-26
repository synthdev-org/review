"""Integration tests for the additive ``stdin``, ``merge_stderr`` and ``env`` options of
``run_analyzer_subprocess`` (plan Phase 3b, for the Claude CLI backend). The defaults must keep
every existing caller's behaviour; the existing tests in test_subprocess_runner.py are unchanged."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adapter.analyzers.exceptions import AnalyzerInvocationError, AnalyzerTimeoutError
from adapter.analyzers.subprocess_runner import run_analyzer_subprocess

pytestmark = pytest.mark.integration


async def _sh(script: str, tmp_path: Path, **kwargs: object) -> bytes:
    return await run_analyzer_subprocess(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        timeout=kwargs.pop("timeout", 10.0),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_stdin_bytes_reach_the_child_then_eof(tmp_path: Path) -> None:
    assert await _sh("cat", tmp_path, stdin=b"hello") == b"hello"


@pytest.mark.asyncio
async def test_stdin_larger_than_the_pipe_buffer_does_not_deadlock(tmp_path: Path) -> None:
    payload = b"x" * 2_000_000

    assert await _sh("wc -c", tmp_path, stdin=payload) == b"2000000\n"


@pytest.mark.asyncio
async def test_a_child_that_exits_without_reading_stdin_is_not_a_broken_pipe_error(
    tmp_path: Path,
) -> None:
    assert await _sh("echo done", tmp_path, stdin=b"y" * 2_000_000) == b"done\n"


@pytest.mark.asyncio
async def test_stdin_is_covered_by_the_timeout_and_the_child_is_killed(tmp_path: Path) -> None:
    with pytest.raises(AnalyzerTimeoutError):
        await _sh("exec sleep 30", tmp_path, stdin=b"x", timeout=0.5)


@pytest.mark.asyncio
async def test_by_default_stderr_is_merged_into_the_returned_bytes(tmp_path: Path) -> None:
    assert await _sh("echo out; echo err >&2", tmp_path) == b"out\nerr\n"


@pytest.mark.asyncio
async def test_without_merging_stderr_stdout_stays_clean(tmp_path: Path) -> None:
    out = await _sh("echo out; echo err >&2", tmp_path, merge_stderr=False)

    assert out == b"out\n"


@pytest.mark.asyncio
async def test_unmerged_stderr_is_kept_for_a_failure_and_a_chatty_one_cannot_block(
    tmp_path: Path,
) -> None:
    script = "head -c 300000 /dev/zero | tr '\\0' e >&2; echo out; exit 4"

    with pytest.raises(AnalyzerInvocationError) as caught:
        await _sh(script, tmp_path, merge_stderr=False)

    assert caught.value.returncode == 4
    assert caught.value.output.startswith(b"out\n" + b"e" * 100)
    assert len(caught.value.output) <= 4 + 65536  # stderr is capped


@pytest.mark.asyncio
async def test_env_replaces_the_inherited_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_INHERITED_VARIABLE", "present")

    out = await _sh(
        'echo "${SOME_INHERITED_VARIABLE:-absent} ${ONLY_HERE}"',
        tmp_path,
        env={"ONLY_HERE": "yes", "PATH": "/usr/bin:/bin"},
    )

    assert out == b"absent yes\n"


@pytest.mark.asyncio
async def test_an_outer_cancellation_still_kills_the_child_with_stdin_in_use(
    tmp_path: Path,
) -> None:
    task = asyncio.create_task(_sh("exec sleep 30", tmp_path, stdin=b"x", timeout=60))
    await asyncio.sleep(0.3)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
