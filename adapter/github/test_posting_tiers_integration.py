"""RQ4 and RQ5 (docs/specs/spec-review-quality.md): the real FindingsOutputStep driving the
real GitHubFindingsPublisher over a stubbed GitHub API (respx). Asserts the posted bodies.
"""

from __future__ import annotations

import pytest

from adapter.github.conftest import RunOutputStep
from findings.merge import POSTED_FINDINGS_CEILING
from findings.models import Finding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _f(i: int, tier: str | None, message: str | None = None) -> Finding:
    return Finding.model_validate(
        {
            "file": f"f{i}.py",
            "line": i + 1,
            "severity": "medium",
            "message": message or f"problem number {i}",
            "source": "semgrep",
            "rule_id": f"R{i}",
            "tier": tier,
        }
    )


async def test_rq4_nits_are_not_posted_when_the_org_has_not_opted_in(
    run_output_step: RunOutputStep,
) -> None:
    posted = await run_output_step(
        [_f(1, "should_fix", "real bug"), _f(2, "nit", "trivial remark")], show_nits=False
    )

    assert posted.review is not None
    assert [c["path"] for c in posted.review["comments"]] == ["f1.py"]
    assert "trivial" not in str(posted.review)
    assert "<details>" not in posted.review["body"]
    assert posted.check_run is not None
    assert "1 finding\\(s\\) posted" in posted.check_run["output"]["summary"]


async def test_rq4_nits_appear_in_one_collapsed_summary_when_opted_in(
    run_output_step: RunOutputStep,
) -> None:
    posted = await run_output_step(
        [_f(1, "should_fix", "real bug"), _f(2, "nit", "trivial remark")], show_nits=True
    )

    assert posted.review is not None
    assert [c["path"] for c in posted.review["comments"]] == ["f1.py"]
    body = posted.review["body"]
    assert body.count("<details>") == 1
    assert "trivial remark" in body
    assert "trivial" not in str(posted.review["comments"])


async def test_all_nit_pr_with_nits_hidden_posts_no_review_and_a_clean_check_run(
    run_output_step: RunOutputStep,
) -> None:
    posted = await run_output_step([_f(1, "nit"), _f(2, "nit")], show_nits=False)

    assert posted.review is None
    assert posted.check_run is not None
    assert posted.check_run["conclusion"] == "neutral"
    assert "No findings" in posted.check_run["output"]["summary"]


async def test_all_nit_pr_with_nits_shown_posts_no_inline_comments_only_the_summary(
    run_output_step: RunOutputStep,
) -> None:
    posted = await run_output_step([_f(1, "nit")], show_nits=True)

    assert posted.review is not None
    assert posted.review["comments"] == []
    assert "<details>" in posted.review["body"]


async def test_rq5_at_most_the_cap_inline_and_the_rest_summarised(
    run_output_step: RunOutputStep,
) -> None:
    findings = [_f(i, None) for i in range(POSTED_FINDINGS_CEILING + 5)]

    posted = await run_output_step(findings)

    assert posted.review is not None
    assert len(posted.review["comments"]) == POSTED_FINDINGS_CEILING
    assert "5 more finding(s)" in posted.review["body"]
    assert posted.check_run is not None
    assert (
        f"{POSTED_FINDINGS_CEILING + 5} finding\\(s\\) posted"
        in posted.check_run["output"]["summary"]
    )


async def test_rq5_none_tier_is_ordered_as_should_fix_so_it_outranks_nits(
    run_output_step: RunOutputStep,
) -> None:
    findings = [_f(i, "nit") for i in range(POSTED_FINDINGS_CEILING)] + [_f(900, None, "kept bug")]

    posted = await run_output_step(findings, show_nits=True)

    assert posted.review is not None
    assert [c["path"] for c in posted.review["comments"]] == ["f900.py"]


async def test_v1_layout_is_unchanged(run_output_step: RunOutputStep) -> None:
    posted = await run_output_step([_f(1, "nit", "kept under v1")], v2=False)

    assert posted.review is not None
    assert len(posted.review["comments"]) == 1
    assert "<details>" not in str(posted.review)
