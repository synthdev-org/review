"""Unit tests for VerificationStep's memory-suppression pre-check, head-file
context loading and per-batch judging. Fakes sit at the EmbeddingClient,
DismissedFindingMatcher, DiffFetcher and FindingJudge Protocol boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from findings.models import Finding
from indexing.embedding import EmbeddingVector
from ingestion.diff import Diff, DiffFile
from review.finding_judge import JudgeInput, Judgement
from review.step import StepContext
from review.verification import (
    MemorySuppressionConfig,
    VerificationFileContext,
    VerificationStep,
    _VerificationInput,
)
from review.verification_context import FileContextConfig

pytestmark = pytest.mark.asyncio


def _finding(file: str, message: str = "m", severity: str = "high") -> Finding:
    return Finding(file=file, line=1, severity=severity, message=message, source="llm")


def _diff(*paths: str, status: str = "modified") -> Diff:
    return Diff(
        files=tuple(DiffFile(path=p, status=status, patch="@@ -1 +1 @@\n-a\n+b") for p in paths)
    )


_SCORES = {"nomatch": 0.0, "lowsim": 5.0, "hisim": 50.0}


class _Embedder:
    """One 1-d vector per text: the score of the marker word the text contains."""

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        return tuple(
            EmbeddingVector(values=(next(v for k, v in _SCORES.items() if k in t),), dimension=1)
            for t in texts
        )


class _Matcher:
    """Similarity is ``vector[0] / 100``; ``None`` (no dismissed match) for a 0 vector."""

    async def best_effective_similarity(
        self, *, repo_id: str, embedding: Sequence[float], half_life_days: float, now: Any
    ) -> float | None:
        assert repo_id == "repo-1" and half_life_days == 30.0
        return None if embedding[0] == 0.0 else embedding[0] / 100


@pytest.mark.parametrize(
    ("threshold", "expected_kept"),
    [
        pytest.param(0.0, ["nomatch"], id="no-dismissed-match-is-always-kept"),
        pytest.param(0.6, ["nomatch", "lowsim", "hisim"], id="none-above-threshold"),
        pytest.param(0.1, ["nomatch", "lowsim"], id="only-the-most-similar-suppressed"),
    ],
)
async def test_memory_suppression_drops_only_findings_above_the_threshold(
    threshold: float, expected_kept: list[str]
) -> None:
    findings = [_finding(f"{k}.py", k) for k in _SCORES]
    config = MemorySuppressionConfig(
        memory_matcher=_Matcher(),
        embedding_client=_Embedder(),
        similarity_threshold=threshold,
        decay_half_life_days=30.0,
        enabled=True,
    )

    kept, suppressed = await VerificationStep._suppress_via_memory(
        findings, config=config, repo_id="repo-1"
    )

    assert [f.message for f in kept] == expected_kept
    assert suppressed == 3 - len(expected_kept)


# --- head-file context ---------------------------------------------------------


class _Fetcher:
    def __init__(
        self,
        contents: dict[str, bytes] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.contents = contents or {}
        self.error = error
        self.delay = delay
        self.paths: list[str] = []

    async def fetch_content(self, *, paths: Sequence[str], **_: Any) -> dict[str, bytes]:
        self.paths.extend(paths)
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.contents


class _Judge:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str], str]] = []

    async def judge(self, findings: Sequence[Finding], job: JudgeInput) -> list[Judgement]:
        self.calls.append(
            ([f.file for f in findings], [f.path for f in job.diff.files], job.file_context)
        )
        return [Judgement(passed=f.file != "bad.py") for f in findings]


def _step(file_context: VerificationFileContext | None = None) -> VerificationStep:
    return VerificationStep(
        object(),  # type: ignore[arg-type]
        model="m",
        timeout=1.0,
        max_diff_bytes=10_000,
        file_context=file_context,
    )


def _parsed(diff: Diff) -> _VerificationInput:
    return _VerificationInput(
        diff=diff,
        org_id="org-1",
        repo_id="repo-1",
        installation_id=1,
        repo_full_name="acme/w",
        pr_number=1,
        head_sha="abc",
    )


def _fc(fetcher: _Fetcher, timeout: float = 1.0) -> VerificationFileContext:
    return VerificationFileContext(
        diff_fetcher=fetcher,  # type: ignore[arg-type]
        config=FileContextConfig(),
        timeout_seconds=timeout,
    )


async def test_file_context_is_built_for_live_cited_files_only() -> None:
    fetcher = _Fetcher({"a.py": b"print('hi')\n"})
    config = _fc(fetcher)
    step = _step(config)

    contexts = await step._load_file_contexts(
        _parsed(_diff("a.py")),
        [_finding("a.py"), _finding("gone.py"), _finding(".env")],
        StepContext(job_id="j"),
        config,
    )

    assert fetcher.paths == ["a.py"]  # not-in-diff and secret-shaped paths are never fetched
    assert list(contexts) == ["a.py"] and "print('hi')" in contexts["a.py"]


async def test_file_context_skips_the_fetch_when_nothing_is_eligible() -> None:
    fetcher = _Fetcher()
    config = _fc(fetcher)

    contexts = await _step(config)._load_file_contexts(
        _parsed(_diff("a.py", status="removed")),
        [_finding("a.py")],
        StepContext(job_id="j"),
        config,
    )

    assert contexts == {} and fetcher.paths == []


@pytest.mark.parametrize(
    "fetcher",
    [
        pytest.param(_Fetcher(error=RuntimeError("token exchange failed")), id="error"),
        pytest.param(_Fetcher({"a.py": b"x"}, delay=0.2), id="timeout"),
    ],
)
async def test_file_context_degrades_to_diff_only_on_fetch_problems(
    fetcher: _Fetcher, caplog: pytest.LogCaptureFixture
) -> None:
    config = _fc(fetcher, timeout=0.01)

    contexts = await _step(config)._load_file_contexts(
        _parsed(_diff("a.py")), [_finding("a.py")], StepContext(job_id="j"), config
    )

    assert contexts == {}
    assert any("head file context unavailable" in r.message for r in caplog.records)


# --- per-batch judging ---------------------------------------------------------


async def test_judge_by_batch_judges_each_batch_against_its_own_files_in_order() -> None:
    judge = _Judge()
    findings = [_finding("a.py"), _finding("bad.py"), _finding("other.py"), _finding("a.py", "2")]
    job = JudgeInput(
        diff=_diff("a.py", "bad.py", "other.py", "z.py"),
        context_bundle=None,
        spec=None,
        org_id="org-1",
        repo_id="repo-1",
        job_id="j",
        review_quality_v2=False,
    )

    judgements = await _step()._judge_by_batch(
        judge, findings, job, (("a.py",), ("bad.py",)), {"a.py": "CTX"}
    )

    assert [j.passed for j in judgements] == [True, False, True, True]  # input order kept
    by_files = {tuple(c[0]): c for c in judge.calls}
    assert by_files[("a.py", "a.py")][1] == ["a.py"]  # batch diff narrowed to cited files
    assert "CTX" in by_files[("a.py", "a.py")][2]
    assert by_files[("bad.py",)][1] == ["bad.py"]
    # findings outside every batch form one group judged on the whole diff
    assert by_files[("other.py",)][1] == ["a.py", "bad.py", "other.py", "z.py"]
