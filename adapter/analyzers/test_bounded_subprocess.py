"""Integration tests for ``run_bounded_subprocess``: the non-raising runner ``run_git`` and
``run_analyzer_subprocess`` both build on."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from adapter.analyzers.exceptions import AnalyzerTimeoutError
from adapter.analyzers.subprocess_runner import run_bounded_subprocess

pytestmark = pytest.mark.integration


async def _sh(script: str, tmp_path: Path, **kwargs: object) -> object:
    return await run_bounded_subprocess(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        timeout=kwargs.pop("timeout", 10.0),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_non_zero_exit_is_returned_not_raised(tmp_path: Path) -> None:
    result = await _sh("echo out; exit 3", tmp_path)

    assert (result.returncode, result.stdout) == (3, b"out\n")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_isolated_discards_stderr_and_gives_the_child_no_stdin(tmp_path: Path) -> None:
    result = await _sh("echo warn >&2; cat; echo done", tmp_path, isolated=True)

    assert result.stdout == b"done\n"  # type: ignore[attr-defined]
    assert result.stderr == b""  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_isolated_timeout_kills_a_grandchild_holding_the_pipe(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(AnalyzerTimeoutError):
        await _sh("sleep 30 & sleep 30", tmp_path, timeout=0.5, isolated=True)

    assert time.monotonic() - started < 10  # without the group kill the reap waits ~30s


@pytest.mark.asyncio
async def test_isolated_timeout_kills_a_grandchild_that_outlives_its_exited_leader(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pid"

    with pytest.raises(AnalyzerTimeoutError):
        await _sh(f"sleep 30 & echo $! > {pid_file}", tmp_path, timeout=0.5, isolated=True)

    pid = int(pid_file.read_text())
    for _ in range(50):  # the kill is a signal: give the kernel a moment to deliver it
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail("the grandchild survived the timeout kill")
