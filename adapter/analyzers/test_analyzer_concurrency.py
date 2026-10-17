"""Integration test for US-A8: the analyzer concurrency bound, proven with
real short-lived subprocesses (the real ``run_analyzer_subprocess``), not
timers around fakes — at most N analyzer processes alive at once, and time
spent queued for a slot does not consume an analyzer's own timeout.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from adapter.analyzers.subprocess_runner import run_analyzer_subprocess
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.static_analysis import AnalyzerRegistration, StaticAnalysisStep
from review.step import StepContext

# Appends "+" on start and "-" on exit to a shared log, then sleeps.
_SCRIPT = (
    "import sys,time\n"
    "log=sys.argv[1]\n"
    "open(log,'a').write('+')\n"
    "time.sleep(float(sys.argv[2]))\n"
    "open(log,'a').write('-')\n"
)


@dataclass
class _PrefilterOutput:
    diff: Diff
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"
    base_sha: str | None = None
    pr_title: str | None = None
    pr_head_ref: str | None = None
    pr_issue_keys: tuple[str, ...] = ()
    pr_is_fork: bool | None = None
    private: bool = False


class _FetchesContent:
    def __init__(self, content: dict[str, bytes]) -> None:
        self._content = content

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        raise NotImplementedError("StaticAnalysisStep never calls DiffFetcher.fetch()")

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        return dict(self._content)


class _PassthroughRedactor:
    async def redact(self, text: str) -> str:
        return text


class _SubprocessAnalyzer:
    def __init__(self, log: Path, seconds: float) -> None:
        self._log = log
        self._seconds = seconds

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        await run_analyzer_subprocess(
            [sys.executable, "-c", _SCRIPT, str(self._log), str(self._seconds)],
            cwd=self._log.parent,
            timeout=timeout,
        )
        return []


def _max_alive(log: Path) -> int:
    alive = peak = 0
    for mark in log.read_text():
        alive += 1 if mark == "+" else -1
        peak = max(peak, alive)
    return peak


def _step(
    log: Path, *, analyzers: int, seconds: float, timeout: float, slots: int
) -> tuple[StaticAnalysisStep, Diff, StepContext]:
    names = [f"a{i}" for i in range(analyzers)]
    diff = Diff(files=(DiffFile(path="x.py", status="added", mode="100644"),))
    step = StaticAnalysisStep(
        _FetchesContent({"x.py": b"pass"}),
        registrations=[AnalyzerRegistration(n, _SubprocessAnalyzer(log, seconds)) for n in names],
        redactor=_PassthroughRedactor(),
        enabled={},
        timeouts={"static_analysis_analyzer": timeout},
        analyzer_slots=asyncio.Semaphore(slots),
    )
    return step, diff, StepContext(job_id="job-1")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_a8_at_most_n_analyzer_subprocesses_are_alive_at_once(tmp_path: Path) -> None:
    log = tmp_path / "alive.log"
    step, diff, context = _step(log, analyzers=4, seconds=0.4, timeout=10.0, slots=2)

    await step.run(_PrefilterOutput(diff=diff), context)

    assert log.read_text().count("+") == 4  # all four ran
    assert _max_alive(log) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_a8_queue_time_does_not_consume_the_analyzer_timeout(tmp_path: Path) -> None:
    """Three 1s analyzers behind one slot with a 2.5s per-analyzer timeout:
    the last waits ~2s in the queue (so it would time out if waiting counted)
    but still completes."""
    log = tmp_path / "alive.log"
    step, diff, context = _step(log, analyzers=3, seconds=1.0, timeout=2.5, slots=1)
    started = time.monotonic()

    await step.run(_PrefilterOutput(diff=diff), context)

    assert time.monotonic() - started > 2.5  # total exceeded the per-analyzer timeout
    assert log.read_text() == "+-+-+-"  # every analyzer ran to completion, serially
