"""Unit tests for ReviewGenerationStep's commit-by-commit review mode
(spec-review-context.md US-X9). Fakes sit at the LLMClient, DiffFetcher,
CommitSource and TextRedactor Protocol boundaries; no real I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from findings.models import Finding
from ingestion.commits import CommitInfo, CommitsUnavailableError
from ingestion.diff import Diff, DiffFile
from llmgateway.models import CompletionRequest, CompletionResponse
from review.review_generation import (
    DiffBatchingConfig,
    ReviewContextConfig,
    ReviewGenerationStep,
)
from review.step import StepContext

pytestmark = pytest.mark.asyncio

MAX_BYTES = 600
_PAD = "+" + "x" * 220
HEAD_PATCH = "@@ -1 +1 @@\n-old\n+" + "x" * 200
SLICE_PATCH = f"@@ -1,1 +1,3 @@\n context\n+    return compute_value(x)\n{_PAD}"
FINDING_LINE_TEXT = b"    return compute_value(x)"


@dataclass
class _In:
    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org"
    repo_id: str = "repo"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 7
    head_sha: str = "c2"


def _diff(*paths: str, patch: str = HEAD_PATCH) -> Diff:
    return Diff(files=tuple(DiffFile(path=p, status="modified", patch=patch) for p in paths))


class _Redactor:
    async def redact(self, text: str) -> str:
        return text


class _LLM:
    """Reports one finding at line 2 for each of the given files it sees in the prompt."""

    def __init__(self, files: tuple[str, ...], fail_on: str | None = None) -> None:
        self.files = files
        self.fail_on = fail_on
        self.users: list[str] = []

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        user = request.messages[-1].content
        self.users.append(user)
        if self.fail_on is not None and self.fail_on in user:
            raise RuntimeError("llm down")
        found = [
            {"file": f, "line": 2, "severity": "high", "message": f"bug in {f}"}
            for f in self.files
            if f in user
        ]
        return CompletionResponse(
            content=json.dumps({"findings": found}), model=request.model, usage=None
        )


class _Fetcher:
    def __init__(self, contents: dict[str, bytes], error: Exception | None = None) -> None:
        self.contents = contents
        self.error = error

    async def fetch_content(self, **_: Any) -> dict[str, bytes]:
        if self.error is not None:
            raise self.error
        return self.contents


class _Commits:
    def __init__(self, commits: tuple[CommitInfo, ...], error: Exception | None = None) -> None:
        self.commits = commits
        self.error = error
        self.range_calls: list[tuple[str, str]] = []

    async def list_commits(self, **_: Any) -> tuple[CommitInfo, ...]:
        if self.error is not None:
            raise self.error
        return self.commits

    async def fetch_range_diff(self, *, base_sha: str, head_sha: str, **_: Any) -> Diff:
        self.range_calls.append((base_sha, head_sha))
        paths = ("a.py", "b.py") if head_sha == "c1" else ("c.py", "d.py")
        return _diff(*paths, patch=SLICE_PATCH)


LINEAR = (
    CommitInfo(sha="c1", parent_shas=("base",)),
    CommitInfo(sha="c2", parent_shas=("c1",)),
)


def _step(llm: _LLM, fetcher: _Fetcher, commits: _Commits) -> ReviewGenerationStep:
    return ReviewGenerationStep(
        llm,
        redactor=_Redactor(),
        model="m",
        timeout=5.0,
        max_diff_bytes=MAX_BYTES,
        batching=DiffBatchingConfig(fetcher, 2, 2, 5.0),  # type: ignore[arg-type]
        context=ReviewContextConfig(
            shrink=None,
            expand=None,
            pr_map_max_bytes=0,
            risk_ordering=False,
            commits=commits,
            commit_max_commits=10,
            commit_fetch_timeout_seconds=5.0,
        ),
    )


HEAD = _diff("a.py", "b.py", "c.py", "d.py", "e.py", "f.py")  # two chunks fit, e/f are dropped
ALL = ("a.py", "b.py", "c.py", "d.py")


async def test_commit_mode_reanchors_present_findings_and_drops_ones_gone_at_head() -> None:
    llm = _LLM(ALL)
    head_files = {
        "a.py": b"p\nq\nr\ns\n" + FINDING_LINE_TEXT + b"\n",
        "b.py": b"unrelated\n",
        "c.py": b"unrelated\n",
        "d.py": b"unrelated\n",
    }
    out = await _step(llm, _Fetcher(head_files), _Commits(LINEAR)).run(
        _In(HEAD), StepContext(job_id="j")
    )

    by_file = {f.file: f for f in out.findings}
    assert by_file["a.py"].line == 5  # moved from line 2 to where the text now sits
    assert set(by_file) == {"a.py"}  # b/c/d lines are gone at head
    assert out.review_batches == (("a.py", "b.py"), ("c.py", "d.py"))
    assert out.unreviewed_files == ("e.py", "f.py")  # touched by no commit slice


async def test_commit_mode_keeps_findings_file_level_when_head_content_unavailable() -> None:
    llm = _LLM(("a.py",))
    out = await _step(llm, _Fetcher({}, error=RuntimeError("token")), _Commits(LINEAR)).run(
        _In(HEAD), StepContext(job_id="j")
    )

    assert [(f.file, f.line) for f in out.findings] == [("a.py", None)]


async def test_commit_mode_reports_a_failed_slice_as_unreviewed() -> None:
    llm = _LLM(("a.py",), fail_on="c.py")
    out = await _step(
        llm, _Fetcher({"a.py": b"x\n" + FINDING_LINE_TEXT + b"\n"}), _Commits(LINEAR)
    ).run(_In(HEAD), StepContext(job_id="j"))

    assert out.unreviewed_files == ("c.py", "d.py", "e.py", "f.py")
    assert out.review_batches == (("a.py", "b.py"),)
    assert [f.file for f in out.findings] == ["a.py"]


@pytest.mark.parametrize(
    "commits",
    [
        pytest.param((), id="no-commits"),
        pytest.param(
            (CommitInfo(sha="c1", parent_shas=("base",)), CommitInfo(sha="c2", parent_shas=("x",))),
            id="non-linear",
        ),
        pytest.param(
            (
                CommitInfo(sha="c0", parent_shas=("base",)),
                CommitInfo(sha="c1", parent_shas=("c0",)),
            ),
            id="does-not-end-at-head",
        ),
        pytest.param(
            tuple(CommitInfo(sha=f"k{i}", parent_shas=(f"k{i - 1}",)) for i in range(1, 12)),
            id="too-many-commits",
        ),
    ],
)
async def test_commit_mode_falls_back_to_batches_when_not_applicable(
    commits: tuple[CommitInfo, ...],
) -> None:
    source = _Commits(commits)
    llm = _LLM(ALL)
    out = await _step(llm, _Fetcher({}), source).run(_In(HEAD), StepContext(job_id="j"))

    assert source.range_calls == []  # nothing spent on range diffs
    assert out.review_batches and all(len(b) <= 2 for b in out.review_batches)
    assert len(out.unreviewed_files) == 2  # the batch plan's own dropped files


async def test_commit_mode_falls_back_when_commits_are_unavailable() -> None:
    err = CommitsUnavailableError(repo_full_name="acme/widgets", detail="down")
    out = await _step(_LLM(ALL), _Fetcher({}), _Commits((), error=err)).run(
        _In(HEAD), StepContext(job_id="j")
    )

    assert len(out.unreviewed_files) == 2
