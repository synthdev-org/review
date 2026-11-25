"""RQ6: the same defect in two files reaches GitHub as one finding (the real output step
into the real publisher over respx)."""

from __future__ import annotations

import pytest

from adapter.github.conftest import RunOutputStep
from findings.models import Finding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MSG = "Missing authorization check on the delete endpoint allows any user to delete"


def _llm(file: str, line: int, message: str, severity: str, tier: str) -> Finding:
    return Finding.model_validate(
        {
            "file": file,
            "line": line,
            "severity": severity,
            "message": message,
            "source": "llm",
            "tier": tier,
        }
    )


async def test_rq6_one_comment_with_highest_severity_and_the_other_location(
    run_output_step: RunOutputStep,
) -> None:
    posted = await run_output_step(
        [
            _llm("a.py", 3, _MSG, "low", "nit"),
            _llm("b.py", 9, _MSG + " records", "high", "blocker"),
        ],
        show_nits=True,
    )

    assert posted.review is not None
    assert len(posted.review["comments"]) == 1
    comment = posted.review["comments"][0]
    assert comment["path"] == "b.py"
    assert "[high]" in comment["body"]
    assert "Also at: a\\.py:3" in comment["body"]
    assert "<details>" not in posted.review["body"]


async def test_rq6_distinct_defects_sharing_boilerplate_stay_separate(
    run_output_step: RunOutputStep,
) -> None:
    posted = await run_output_step(
        [
            _llm(
                "a.py",
                1,
                "Unchecked None: user.email is lowercased without a null check in signup",
                "medium",
                "should_fix",
            ),
            _llm(
                "b.py",
                2,
                "Unchecked None: order.owner is lowercased without a null check in checkout",
                "medium",
                "should_fix",
            ),
        ]
    )

    assert posted.review is not None
    assert len(posted.review["comments"]) == 2
