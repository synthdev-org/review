"""US-C6 integration tests (plan Phase 9): shadow mode never acts on the
classifier; agreement per criterion is recorded."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from review.test_finding_judge_support import (
    FakeLLM,
    Resolver,
    classifier_for,
    finding,
    run_step,
    selection,
    step_with,
    wiring,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_shadow_keeps_and_drops_by_the_llm_verdict_only(
    points: Callable[[], dict[str, list[dict[str, Any]]]],
) -> None:
    sent: list[dict[str, Any]] = []
    classifier = classifier_for(lambda s, q: 0.05, sent)  # says "no" to everything
    resolver = Resolver({"org-1": selection(classifier, "shadow")})
    llm = FakeLLM(fail_indexes=frozenset({1}))

    kept = await run_step(
        step_with(llm, wiring(resolver)), [finding(0), finding(1, severity="high"), finding(2)]
    )

    assert [f.message for f in kept] == ["llm finding 0", "llm finding 2"]  # LLM verdicts only
    assert len(sent) == 3 and len(llm.calls) == 1  # the classifier ran for every finding
    agreement: dict[str, int] = {}
    for a in points()["review.classifier.shadow_agreement_total"]:
        agreement[a["outcome"]] = agreement.get(a["outcome"], 0) + int(a["_value"])
    # 3 findings x 4 criteria: the classifier says "no"; the LLM met 8, failed 4.
    assert agreement == {"disagree": 8, "agree": 4}


async def test_a_shadow_classifier_outage_changes_nothing_but_the_error_metric(
    points: Callable[[], dict[str, list[dict[str, Any]]]],
) -> None:
    import httpx2

    from adapter.classifier.test_typesafe import make

    resolver = Resolver(
        {"org-1": selection(make(lambda r: httpx2.Response(503), sleeps=[]), "shadow")}
    )
    llm = FakeLLM()

    kept = await run_step(step_with(llm, wiring(resolver)), [finding(0), finding(1)])

    assert len(kept) == 2
    outcomes = {a["outcome"] for a in points()["review.classifier.shadow_agreement_total"]}
    assert outcomes == {"error"}
