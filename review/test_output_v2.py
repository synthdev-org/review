"""Unit tests for the v2 (tiered, collapsed) layout of review.output.FindingsOutputStep
(plan Phase 3), against a fake FindingsPublisher and a fake ShowNitsResolver. The
real posting adapter is covered by adapter/github/test_posting_*_integration.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from findings.merge import POSTED_FINDINGS_CEILING
from findings.models import Finding
from review.output import FindingsOutputInput, FindingsOutputStep
from review.step import StepContext


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


class _Nits:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def show_nits(self, org_id: str) -> bool:
        self.asked.append(org_id)
        return self.answer


def _f(i: int, tier: str | None, **kw: object) -> Finding:
    return Finding.model_validate(
        {
            "file": f"f{i}.py",
            "line": i + 1,
            "severity": "medium",
            "message": f"distinct problem {i}",
            "source": "semgrep",
            "rule_id": f"R{i}",
            "tier": tier,
            **kw,
        }
    )


def _input(findings: list[Finding], org_id: str = "org-1") -> FindingsOutputInput:
    return FindingsOutputInput(
        findings=findings,
        org_id=org_id,
        repo_id="repo-1",
        installation_id=1,
        repo_full_name="acme/widgets",
        pr_number=1,
        head_sha="a" * 40,
    )


def _ctx(v2: bool) -> StepContext:
    return StepContext(job_id="j", review_quality_v2=v2)


@pytest.mark.asyncio
async def test_v1_call_has_no_collapsed_kwarg_and_ignores_the_resolver() -> None:
    pub, nits = _Publisher(), _Nits(True)
    step = FindingsOutputStep(pub, show_nits_resolver=nits)

    await step.run(_input([_f(1, "nit")]), _ctx(False))

    assert "collapsed_findings" not in pub.reviews[0]
    assert len(pub.reviews[0]["findings"]) == 1
    assert nits.asked == []


@pytest.mark.asyncio
async def test_all_nit_pr_with_nits_hidden_posts_no_review_and_zero_check_run() -> None:
    pub = _Publisher()
    step = FindingsOutputStep(pub, show_nits_resolver=_Nits(False))

    result = await step.run(_input([_f(1, "nit"), _f(2, "nit")]), _ctx(True))

    assert pub.reviews == []
    assert pub.check_runs == [0]
    assert result.comment_count == 0 and result.check_run_conclusion == "neutral"


@pytest.mark.asyncio
async def test_missing_resolver_hides_nits() -> None:
    pub = _Publisher()

    await FindingsOutputStep(pub).run(_input([_f(1, "nit")]), _ctx(True))

    assert pub.reviews == []


@pytest.mark.asyncio
async def test_nits_shown_go_to_collapsed_never_inline() -> None:
    pub, nits = _Publisher(), _Nits(True)
    step = FindingsOutputStep(pub, show_nits_resolver=nits)

    await step.run(_input([_f(1, "nit"), _f(2, "should_fix"), _f(3, None)], "org-9"), _ctx(True))

    review = pub.reviews[0]
    assert {f.file for f in review["findings"]} == {"f2.py", "f3.py"}
    assert [f.file for f in review["collapsed_findings"]] == ["f1.py"]
    assert pub.check_runs == [3]
    assert nits.asked == ["org-9"]


@pytest.mark.asyncio
async def test_all_nit_pr_with_nits_shown_posts_a_review_with_only_the_summary() -> None:
    pub = _Publisher()
    step = FindingsOutputStep(pub, show_nits_resolver=_Nits(True))

    await step.run(_input([_f(1, "nit")]), _ctx(True))

    assert pub.reviews[0]["findings"] == []
    assert len(pub.reviews[0]["collapsed_findings"]) == 1


@pytest.mark.asyncio
async def test_overflow_beyond_the_cap_is_summarised_and_nits_go_first() -> None:
    pub = _Publisher()
    findings = [_f(i, "should_fix") for i in range(POSTED_FINDINGS_CEILING)]
    findings += [_f(900 + i, "blocker") for i in range(3)]
    step = FindingsOutputStep(pub, show_nits_resolver=_Nits(False))

    await step.run(_input(findings), _ctx(True))

    review = pub.reviews[0]
    assert len(review["findings"]) == POSTED_FINDINGS_CEILING
    assert {f.tier for f in review["findings"][:3]} == {"blocker"}
    assert len(review["collapsed_findings"]) == 3


class _Counter:
    def __init__(self) -> None:
        self.added: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attrs: dict[str, str]) -> None:
        self.added.append((amount, attrs))


@pytest.mark.asyncio
async def test_hidden_nits_are_merged_first_so_duplicates_dedupe_and_hidden_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _Counter()
    monkeypatch.setattr("review.output._findings_collapsed_total", counter)
    pub = _Publisher()
    twin = _f(1, "nit")  # exact duplicate of a posted finding: deduped, not "hidden"
    findings = [_f(1, "should_fix"), twin, _f(2, "nit"), _f(2, "nit", message="other wording")]
    step = FindingsOutputStep(pub, show_nits_resolver=_Nits(False))

    await step.run(_input(findings), _ctx(True))

    assert [f.file for f in pub.reviews[0]["findings"]] == ["f1.py"]
    assert pub.reviews[0]["collapsed_findings"] == []
    hidden = sum(n for n, a in counter.added if a == {"reason": "nit_hidden"})
    assert hidden == 2  # f2's two nits differ in message so both stay; the twin deduped away


@pytest.mark.asyncio
async def test_hidden_nits_neither_consume_nor_overflow_the_ceiling_for_non_nits() -> None:
    pub = _Publisher()
    findings = [_f(i, "should_fix") for i in range(POSTED_FINDINGS_CEILING)]
    findings += [_f(500 + i, "nit") for i in range(20)]
    step = FindingsOutputStep(pub, show_nits_resolver=_Nits(False))

    result = await step.run(_input(findings), _ctx(True))

    assert len(pub.reviews[0]["findings"]) == POSTED_FINDINGS_CEILING
    assert pub.reviews[0]["collapsed_findings"] == []  # cut nits are hidden, not "overflow"
    assert result.comment_count == POSTED_FINDINGS_CEILING


@pytest.mark.asyncio
async def test_hidden_nits_log_one_info_line_with_count_and_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    step = FindingsOutputStep(_Publisher(), show_nits_resolver=_Nits(False))

    with caplog.at_level("INFO", logger="review.output"):
        await step.run(_input([_f(1, "should_fix"), _f(2, "nit"), _f(3, "nit")]), _ctx(True))

    records = [r for r in caplog.records if getattr(r, "hidden_count", None) is not None]
    assert len(records) == 1
    assert records[0].levelname == "INFO"
    assert records[0].hidden_count == 2  # type: ignore[attr-defined]
    assert "distinct problem" not in records[0].getMessage()


@pytest.mark.asyncio
async def test_inline_finding_outside_the_diff_is_demoted_to_file_level() -> None:
    from ingestion.diff import Diff, DiffFile

    patch = "@@ -1,2 +1,3 @@\n a\n+b\n c"  # new-side lines 1..3 are commentable
    diff = Diff(files=(DiffFile(path="f.py", status="modified", patch=patch),))
    inside = _f(0, None, file="f.py", line=2)
    outside = _f(1, None, file="f.py", line=40)
    unknown_file = _f(2, None, file="other.py", line=1)
    publisher = _Publisher()
    step = FindingsOutputStep(publisher)
    parsed = _input([inside, outside, unknown_file]).model_copy(update={"diff": diff})
    await step.run(parsed, _ctx(v2=True))
    posted = {f.message: f.line for f in publisher.reviews[0]["findings"]}
    assert posted == {
        inside.message: 2,
        outside.message: None,
        unknown_file.message: None,
    }


@pytest.mark.asyncio
async def test_v1_never_demotes_off_diff_findings() -> None:
    from ingestion.diff import Diff, DiffFile

    patch = "@@ -1,2 +1,3 @@\n a\n+b\n c"
    diff = Diff(files=(DiffFile(path="f.py", status="modified", patch=patch),))
    outside = _f(1, None, file="f.py", line=40)
    publisher = _Publisher()
    parsed = _input([outside]).model_copy(update={"diff": diff})
    await FindingsOutputStep(publisher).run(parsed, _ctx(v2=False))
    assert publisher.reviews[0]["findings"][0].line == 40


@pytest.mark.asyncio
async def test_no_diff_means_no_demotion() -> None:
    publisher = _Publisher()
    await FindingsOutputStep(publisher).run(_input([_f(0, None, line=999)]), _ctx(v2=False))
    assert publisher.reviews[0]["findings"][0].line == 999
