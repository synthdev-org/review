"""Shared helper: run ``StaticAnalysisStep`` with only the real analyzers
live (the Gitleaks/Semgrep slots are inert fakes), so RQ8 is exercised through
the real step, the real inner registry and the real outcome logging."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.static_analysis import StaticAnalysisOutput, StaticAnalysisStep, StaticAnalyzer
from review.step import StepContext


def added_file(path: str, raw: bytes) -> DiffFile:
    """A newly added file whose patch adds every line (all lines are in the diff)."""
    lines = raw.decode("utf-8", errors="replace").split("\n")
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    return DiffFile(path=path, status="added", patch=patch)


RunStep = Callable[..., Awaitable[StaticAnalysisOutput]]


class _Inert:
    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        return []


class _Passthrough:
    async def redact(self, text: str) -> str:
        return text


@dataclass
class _Fetcher:
    content: dict[str, bytes]

    async def fetch_content(self, **kwargs: Any) -> dict[str, bytes]:
        return dict(self.content)


@pytest.fixture
def run_step() -> RunStep:
    async def _run(
        content: dict[str, bytes],
        extra: Mapping[str, StaticAnalyzer],
        *,
        timeout: float = 30.0,
        removed: tuple[str, ...] = (),
    ) -> StaticAnalysisOutput:
        files = [added_file(p, raw) for p, raw in content.items()]
        files += [DiffFile(path=p, status="removed") for p in removed]
        step = StaticAnalysisStep(
            _Fetcher(content),  # type: ignore[arg-type]
            gitleaks_analyzer=_Inert(),
            semgrep_analyzer=_Inert(),
            extra_analyzers=extra,
            redactor=_Passthrough(),
            enabled={"gitleaks": True, "semgrep": True, **dict.fromkeys(extra, True)},
            timeouts={"gitleaks": 5.0, "semgrep": 5.0, **dict.fromkeys(extra, timeout)},
        )
        upstream = SimpleNamespace(
            diff=Diff(files=tuple(files)),
            org_id="org-1",
            repo_id="repo-1",
            installation_id=1,
            repo_full_name="acme/widgets",
            pr_number=1,
            head_sha="abc",
            base_sha=None,
            pr_title=None,
            pr_head_ref=None,
            pr_issue_keys=(),
            pr_is_fork=None,
            private=False,
        )
        return await step.run(upstream, StepContext(job_id="job-1"))

    return _run
