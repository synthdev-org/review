"""Integration tests for spec-diff-batching.md (US-B1 .. US-B9): review_generation,
verification and output across the batching path. Fakes sit at the LLMClient,
DiffFetcher, TextRedactor and FindingsPublisher Protocol boundaries; tree-sitter
parsing is real.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from findings.models import Finding
from ingestion.diff import Diff, DiffContentUnavailableError, DiffFile
from llmgateway.models import CompletionRequest, CompletionResponse
from review.output import FindingsOutputStep
from review.review_generation import DiffBatchingConfig, ReviewGenerationStep
from review.step import StepContext
from review.verification import VerificationStep

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAX_BYTES = 600
PATCH = "@@ -1 +1 @@\n-old\n+" + "x" * 200  # one file block is about 240 bytes


def _file(path: str, patch: str = PATCH) -> DiffFile:
    return DiffFile(path=path, status="modified", patch=patch)


def _diff(*paths: str) -> Diff:
    return Diff(files=tuple(_file(p) for p in paths))


@dataclass
class _In:
    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org"
    repo_id: str = "repo"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 7
    head_sha: str = "abc"
    review_batches: tuple[tuple[str, ...], ...] = ()
    unreviewed_files: tuple[str, ...] = ()


class _Redactor:
    async def redact(self, text: str) -> str:
        return text


class _LLM:
    """Answers every call from ``respond(user_text)``; tracks calls in flight."""

    def __init__(self, respond: Callable[[str], str], delay: float = 0.0) -> None:
        self.respond = respond
        self.delay = delay
        self.users: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        user = request.messages[-1].content
        self.users.append(user)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return CompletionResponse(content=self.respond(user), model=request.model, usage=None)
        finally:
            self.in_flight -= 1


class _Fetcher:
    def __init__(
        self, contents: dict[str, bytes] | None = None, error: Exception | None = None
    ) -> None:
        self.contents = contents or {}
        self.error = error
        self.calls = 0

    async def fetch_content(self, **_: Any) -> dict[str, bytes]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.contents


def _finding_json(file: str, message: str = "bug", line: int = 1) -> dict[str, Any]:
    return {"file": file, "line": line, "severity": "high", "message": message}


def _step(
    llm: _LLM,
    fetcher: _Fetcher | None,
    *,
    max_chunks: int = 8,
    concurrency: int = 4,
    link_timeout: float = 5.0,
    link_grouping: bool = True,
) -> ReviewGenerationStep:
    batching = (
        None
        if fetcher is None
        else DiffBatchingConfig(fetcher, max_chunks, concurrency, link_timeout, link_grouping)  # type: ignore[arg-type]
    )
    return ReviewGenerationStep(
        llm,
        redactor=_Redactor(),
        model="m",
        timeout=5.0,
        max_diff_bytes=MAX_BYTES,
        batching=batching,
    )


def _ctx() -> StepContext:
    return StepContext(job_id="job-1")


def _empty(_: str) -> str:
    return '{"findings": []}'


def _diff_text(user: str) -> str:
    return user.split("Diff:\n", 1)[1]


# --- US-B1 -------------------------------------------------------------------


async def test_us_b1_diff_that_fits_is_one_identical_call_and_never_fetches() -> None:
    diff = _diff("a.py", "b.py")  # about 480 bytes
    plain, batched = _LLM(_empty), _LLM(_empty)
    fetcher = _Fetcher()

    await _step(plain, None).run(_In(diff), _ctx())
    out = await _step(batched, fetcher).run(_In(diff), _ctx())

    assert batched.users == plain.users and len(batched.users) == 1
    assert fetcher.calls == 0
    assert out.review_batches == () and out.unreviewed_files == ()


async def test_us_b1_batching_off_cuts_an_oversize_diff() -> None:
    llm = _LLM(_empty)

    out = await _step(llm, None).run(_In(_diff("a.py", "b.py", "c.py", "d.py")), _ctx())

    assert len(llm.users) == 1 and len(_diff_text(llm.users[0]).encode()) <= MAX_BYTES
    assert out.review_batches == ()


# --- US-B2 / B3 / B4 / B5 ----------------------------------------------------


async def test_us_b2_late_defect_is_found_and_cross_batch_duplicates_merge() -> None:
    paths = [f"d{i}/f.py" for i in range(6)]

    def respond(user: str) -> str:
        found = [_finding_json("d0/f.py", "dup")]
        if "d5/f.py" in user:
            found.append(_finding_json("d5/f.py", "late defect"))
        return json.dumps({"findings": found})

    llm = _LLM(respond)

    out = await _step(llm, _Fetcher()).run(_In(_diff(*paths)), _ctx())

    assert len(llm.users) > 1
    assert all(len(_diff_text(u).encode()) <= MAX_BYTES for u in llm.users)
    messages = sorted(f.message for f in out.findings)
    assert messages == ["dup", "late defect"]
    assert out.unreviewed_files == ()


async def test_us_b3_importer_and_definer_share_a_batch() -> None:
    paths = ["a/one.py", "app/service.py", "b/two.py", "app/repo.py", "c/three.py", "d/four.py"]
    contents = {
        "app/service.py": b"from app.repo import save\n\ndef run():\n    save()\n",
        "app/repo.py": b"def save():\n    return 1\n",
    }
    llm = _LLM(_empty)

    out = await _step(llm, _Fetcher(contents)).run(_In(_diff(*paths)), _ctx())

    together = [b for b in out.review_batches if "app/service.py" in b]
    assert len(together) == 1 and "app/repo.py" in together[0]


async def test_link_grouping_off_plans_like_a_diff_with_no_links() -> None:
    paths = ["a/one.py", "app/service.py", "b/two.py", "app/repo.py", "c/three.py", "d/four.py"]
    contents = {
        "app/service.py": b"from app.repo import save\n\ndef run():\n    save()\n",
        "app/repo.py": b"def save():\n    return 1\n",
    }
    off = await _step(_LLM(_empty), _Fetcher(contents), link_grouping=False).run(
        _In(_diff(*paths)), _ctx()
    )
    no_links = await _step(_LLM(_empty), _Fetcher()).run(_In(_diff(*paths)), _ctx())

    assert off.review_batches == no_links.review_batches


async def test_us_b4_test_file_shares_a_batch_with_its_source() -> None:
    paths = ["x/a.py", "pkg/foo.py", "y/b.py", "z/c.py", "pkg/test_foo.py", "w/d.py"]
    llm = _LLM(_empty)

    out = await _step(llm, _Fetcher()).run(_In(_diff(*paths)), _ctx())

    together = [b for b in out.review_batches if "pkg/foo.py" in b]
    assert len(together) == 1 and "pkg/test_foo.py" in together[0]


async def test_us_b5_lone_oversize_file_is_alone_and_truncated() -> None:
    big = _file("big.py", "@@ -1 +1 @@\n+" + "y" * (MAX_BYTES * 2))
    diff = Diff(files=(_file("a.py"), big, _file("b.py")))
    llm = _LLM(_empty)

    out = await _step(llm, _Fetcher()).run(_In(diff), _ctx())

    assert ("big.py",) in out.review_batches
    big_prompts = [u for u in llm.users if "--- big.py" in u]
    assert len(big_prompts) == 1 and "--- a.py" not in big_prompts[0]
    assert len(_diff_text(big_prompts[0]).encode()) <= MAX_BYTES


# --- US-B6 / B7 --------------------------------------------------------------


async def test_us_b6_cap_drops_tail_logs_it_and_the_body_lists_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = [f"d{i}/f.py" for i in range(6)]
    llm = _LLM(_empty)
    caplog.set_level(logging.WARNING)

    out = await _step(llm, _Fetcher(), max_chunks=2).run(_In(_diff(*paths)), _ctx())

    assert len(llm.users) == 2
    assert out.unreviewed_files and set(out.unreviewed_files) <= set(paths)
    assert any(
        r.getMessage().startswith("review-generation: diff cap reached")
        and list(r.dropped_files) == list(out.unreviewed_files)  # type: ignore[attr-defined]
        for r in caplog.records
    )


class _Publisher:
    def __init__(self) -> None:
        self.reviews: list[dict[str, Any]] = []
        self.check_runs: list[int] = []

    async def post_review(self, **kwargs: Any) -> None:
        self.reviews.append(kwargs)

    async def post_check_run(self, *, finding_count: int, **_: Any) -> None:
        self.check_runs.append(finding_count)

    async def post_failure_check_run(self, **_: Any) -> None:
        raise AssertionError("not used")


async def test_us_b6_clean_review_with_unreviewed_files_still_posts() -> None:
    publisher = _Publisher()
    step = FindingsOutputStep(publisher)

    await step.run(_In(_diff("a.py"), unreviewed_files=("late.py",)), _ctx())

    assert publisher.reviews[0]["unreviewed_files"] == ("late.py",)
    assert publisher.reviews[0]["findings"] == []
    assert publisher.check_runs == [0]


async def test_us_b6_nothing_unreviewed_leaves_the_call_unchanged() -> None:
    publisher = _Publisher()
    step = FindingsOutputStep(publisher)
    finding = Finding(file="a.py", line=1, severity="high", message="m", source="llm")

    await step.run(_In(_diff("a.py"), findings=[finding]), _ctx())

    assert "unreviewed_files" not in publisher.reviews[0]


@pytest.mark.parametrize("error_kind", ["fetch", "timeout"])
async def test_us_b7_link_failure_degrades_to_directory_grouping(
    error_kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    paths = [f"d{i}/f.py" for i in range(6)]
    if error_kind == "fetch":
        fetcher = _Fetcher(error=DiffContentUnavailableError(repo_full_name="a/b", detail="x"))
    else:

        class _Slow(_Fetcher):
            async def fetch_content(self, **_: Any) -> dict[str, bytes]:
                await asyncio.sleep(1)
                return {}

        fetcher = _Slow()
    llm = _LLM(_empty)
    caplog.set_level(logging.WARNING)

    out = await _step(llm, fetcher, link_timeout=0.05).run(_In(_diff(*paths)), _ctx())

    assert len(llm.users) == len(out.review_batches) > 1
    assert out.unreviewed_files == ()
    assert any("link building failed" in r.getMessage() for r in caplog.records)


async def test_us_b7_unparseable_and_unsupported_files_do_not_block_links() -> None:
    paths = ["notes/readme.md", "app/service.py", "app/bad.py", "app/repo.py", "q/z.py", "r/y.py"]
    contents = {
        "app/service.py": b"from app.repo import save\n",
        "app/repo.py": b"def save():\n    return 1\n",
        "app/bad.py": b"\xff\xfe\x00 not python (((",
    }
    llm = _LLM(_empty)

    out = await _step(llm, _Fetcher(contents)).run(_In(_diff(*paths)), _ctx())

    together = [b for b in out.review_batches if "app/service.py" in b]
    assert len(together) == 1 and "app/repo.py" in together[0]


# --- US-B9 -------------------------------------------------------------------


async def test_us_b9_concurrency_is_bounded() -> None:
    paths = [f"d{i}/f.py" for i in range(8)]
    llm = _LLM(_empty, delay=0.02)

    await _step(llm, _Fetcher(), concurrency=2).run(_In(_diff(*paths)), _ctx())

    assert len(llm.users) > 2 and llm.max_in_flight == 2


async def test_us_b9_one_failed_batch_keeps_the_rest_and_lists_its_files() -> None:
    paths = [f"d{i}/f.py" for i in range(6)]

    def respond(user: str) -> str:
        if "d0/f.py" in user:
            return "not json"
        return json.dumps({"findings": [_finding_json("d5/f.py")]})

    out = await _step(_LLM(respond), _Fetcher()).run(_In(_diff(*paths)), _ctx())

    assert [f.file for f in out.findings] == ["d5/f.py"]
    assert "d0/f.py" in out.unreviewed_files


async def test_us_b9_all_batches_failing_raises() -> None:
    with pytest.raises(Exception, match="could not parse"):
        await _step(_LLM(lambda _: "not json"), _Fetcher()).run(
            _In(_diff(*[f"d{i}/f.py" for i in range(6)])), _ctx()
        )


# --- US-B8: verification against each file's own diff -------------------------


def _verdicts(_: str) -> str:
    return json.dumps(
        {
            "verdicts": [
                {"index": i, "valid": True, "actionable": True, "specific": True, "valuable": True}
                for i in range(3)
            ]
        }
    )


def _llm_finding(file: str) -> Finding:
    return Finding(file=file, line=1, severity="high", message="m", source="llm")


async def test_us_b8_each_group_is_judged_against_only_its_files_diff() -> None:
    diff = _diff("a.py", "b.py", "c.py")
    llm = _LLM(_verdicts)
    step = VerificationStep(
        llm, model="m", timeout=5.0, max_diff_bytes=MAX_BYTES, batch_concurrency=2
    )
    batches: Sequence[tuple[str, ...]] = (("a.py", "b.py"), ("c.py",))
    findings = [_llm_finding("c.py"), _llm_finding("a.py"), _llm_finding("elsewhere.py")]

    out = await step.run(
        _In(diff, findings=findings, review_batches=tuple(batches), unreviewed_files=("z.py",)),
        _ctx(),
    )

    assert len(llm.users) == 3  # batch 1, batch 2, and the no-batch group
    c_call = next(u for u in llm.users if "--- c.py" in u and "--- a.py" not in u)
    a_call = next(u for u in llm.users if "--- a.py" in u)
    assert "--- c.py" not in a_call and "--- b.py" not in a_call
    assert c_call
    assert len(out.findings) == 3 and out.unreviewed_files == ("z.py",)
    assert [f.file for f in out.findings] == ["c.py", "a.py", "elsewhere.py"]  # order kept


async def test_us_b8_empty_review_batches_is_one_judge_call_over_the_whole_diff() -> None:
    diff = _diff("a.py", "b.py")
    llm = _LLM(_verdicts)
    step = VerificationStep(llm, model="m", timeout=5.0, max_diff_bytes=MAX_BYTES)

    await step.run(_In(diff, findings=[_llm_finding("a.py"), _llm_finding("b.py")]), _ctx())

    assert len(llm.users) == 1 and "--- a.py" in llm.users[0] and "--- b.py" in llm.users[0]


async def test_us_b2_finding_ceiling_scales_with_batch_count() -> None:
    paths = [f"d{i}/f.py" for i in range(6)]

    def respond(user: str) -> str:
        files = [p for p in paths if p in user]
        return json.dumps(
            {"findings": [_finding_json(p, f"bug{n}", n + 1) for p in files for n in range(8)]}
        )

    out = await _step(_LLM(respond), _Fetcher()).run(_In(_diff(*paths)), _ctx())

    assert {f.file for f in out.findings} == set(paths)
