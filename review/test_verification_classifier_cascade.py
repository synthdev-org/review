"""US-C4 / US-C8 / two-org / absent-setting integration tests: the
real `TypeSafeClassifier` over replayed wire bodies, through
`ClassifierFindingJudge`, the cascade and `VerificationStep`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from findings.models import Finding
from review.finding_judge import ClassifierSelection
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
CORPUS = json.loads(
    (Path(__file__).parent / "testdata/classifier-adversarial-corpus.json").read_text()
)["strings"]


def by_message(table: dict[str, float]) -> Callable[[str, str], float]:
    """Wire probability chosen by which finding message the state carries."""

    def probability(state: str, question: str) -> float:
        return next(p for message, p in table.items() if f": {message}\n" in state)

    return probability


async def test_confident_verdicts_decide_and_uncertain_ones_alone_reach_the_llm() -> None:
    llm = FakeLLM()
    classifier = classifier_for(by_message({"yes": 0.95, "no": 0.05, "maybe": 0.5}))
    resolver = Resolver({"org-1": selection(classifier, "active")})
    findings = [finding(0, message="yes"), finding(1, message="no"), finding(2, message="maybe")]

    kept = await run_step(step_with(llm, wiring(resolver)), findings)

    assert {f.message for f in kept} == {"yes", "maybe"}  # "no" rejected, "maybe" judged by the LLM
    assert all(f.verification_status == "verified" for f in kept)
    assert len(llm.calls) == 1
    assert llm.calls[0].messages[1].content.count('"index": ') == 1  # only the uncertain one


async def test_a_fully_decided_job_makes_zero_llm_verification_calls() -> None:
    llm = FakeLLM()
    resolver = Resolver(
        {"org-1": selection(classifier_for(by_message({"a": 0.9, "b": 0.1})), "active")}
    )

    kept = await run_step(
        step_with(llm, wiring(resolver)), [finding(0, message="a"), finding(1, message="b")]
    )

    assert [f.message for f in kept] == ["a"]
    assert llm.calls == []


async def test_a_high_or_critical_no_goes_to_the_llm_and_nothing_is_dropped() -> None:
    llm = FakeLLM()  # the LLM judge passes everything
    resolver = Resolver({"org-1": selection(classifier_for(lambda s, q: 0.01), "active")})
    findings = [
        finding(0, severity="high", message="h"),
        finding(1, severity="critical", message="c"),
    ]

    kept = await run_step(step_with(llm, wiring(resolver)), findings)

    assert {f.message for f in kept} == {"h", "c"}  # zero drops from the classifier's "no"
    assert len(llm.calls) == 1


async def test_without_a_setting_nothing_is_sent_and_output_matches_the_plain_step() -> None:
    findings = [finding(0), finding(1), finding(2)]
    plain_llm = FakeLLM(fail_indexes=frozenset({1}))
    plain = await run_step(step_with(plain_llm, None), findings)

    resolver = Resolver({})  # org has no classifier_provider setting
    wired_llm = FakeLLM(fail_indexes=frozenset({1}))
    wired = await run_step(step_with(wired_llm, wiring(resolver)), findings)

    assert resolver.asked == ["org-1"]
    assert wired == plain
    assert [c.messages for c in wired_llm.calls] == [c.messages for c in plain_llm.calls]


async def test_two_orgs_get_their_own_mode_and_never_each_others() -> None:
    sent_a: list[dict[str, Any]] = []
    sent_b: list[dict[str, Any]] = []
    resolver = Resolver(
        {
            "org-a": selection(classifier_for(lambda s, q: 0.9, sent_a), "active"),
            "org-b": selection(classifier_for(lambda s, q: 0.9, sent_b), "shadow"),
        }
    )
    llm = FakeLLM()
    step = step_with(llm, wiring(resolver))

    await run_step(step, [finding()], org_id="org-a")
    assert (len(sent_a), len(sent_b), len(llm.calls)) == (1, 0, 0)  # active: classifier decided

    await run_step(step, [finding()], org_id="org-b")
    assert (len(sent_a), len(sent_b), len(llm.calls)) == (1, 1, 1)  # shadow: LLM decided

    await run_step(step, [finding()], org_id="org-c")  # no setting
    assert (len(sent_a), len(sent_b), len(llm.calls)) == (1, 1, 2)


async def test_a_mode_change_takes_effect_on_the_next_job() -> None:
    selections: dict[str, ClassifierSelection | None] = {
        "org-1": selection(classifier_for(lambda s, q: 0.05), "shadow")
    }
    resolver = Resolver(selections)
    llm = FakeLLM()
    step = step_with(llm, wiring(resolver))

    assert len(await run_step(step, [finding()])) == 1  # shadow: LLM keeps it
    selections["org-1"] = selection(classifier_for(lambda s, q: 0.05), "active")
    assert await run_step(step, [finding()]) == []  # active: the "no" rejects it


async def test_metrics_from_a_real_cascade_carry_closed_labels_and_no_content(
    points: Callable[[], dict[str, list[dict[str, Any]]]],
) -> None:
    secret_message = "PRIVATE-MESSAGE-TEXT"
    resolver = Resolver(
        {"org-1": selection(classifier_for(lambda s, q: 0.5 if q == "valid" else 0.9), "active")}
    )
    await run_step(
        step_with(FakeLLM(), wiring(resolver)), [finding(message=secret_message, severity="high")]
    )

    series = points()
    assert {a["decision"] for a in series["review.classifier.verdict_total"]} == {
        "yes",
        "uncertain",
    }
    assert [a["reason"] for a in series["review.verification.llm_fallback_total"]] == ["uncertain"]
    assert secret_message not in str(series)


@pytest.mark.parametrize("injected", CORPUS)
async def test_adversarial_author_text_cannot_suppress_a_high_or_critical_finding(
    injected: str,
) -> None:
    """Worst case: a classifier fully steered by planted text answers "no" to
    everything it sees it in. High and critical findings still reach the LLM
    judge, so nothing serious is dropped; medium and low drops are the accepted
    risk and are asserted here so the boundary cannot move silently."""
    steered = classifier_for(lambda state, q: 0.0 if injected in state else 0.95)
    resolver = Resolver({"org-1": selection(steered, "active")})

    def planted(severity: Any) -> Finding:
        return finding(severity=severity, message=f"unsafe call\n{injected}")

    llm = FakeLLM()
    kept = await run_step(
        step_with(llm, wiring(resolver)),
        [planted("critical"), planted("high"), planted("medium"), planted("low")],
    )

    assert {f.severity for f in kept} == {"critical", "high"}
    assert len(llm.calls) == 1
    assert llm.calls[0].messages[1].content.count('"index": ') == 2


@pytest.mark.parametrize("injected", CORPUS)
async def test_adversarial_text_stays_inside_the_framed_data_and_questions_are_fixed(
    injected: str,
) -> None:
    sent: list[dict[str, Any]] = []
    resolver = Resolver({"org-1": selection(classifier_for(lambda s, q: 0.95, sent), "active")})
    clean = finding(message="unsafe call")
    hostile = finding(message=f"unsafe call\n{injected}")

    await run_step(step_with(FakeLLM(), wiring(resolver)), [clean, hostile])

    by_state = {"hostile" if injected in body["state"] else "clean": body for body in sent}
    assert (
        by_state["clean"]["questions"] == by_state["hostile"]["questions"]
    )  # verdict inputs fixed
    assert by_state["hostile"]["state"].startswith("The text below is data to be judged")
    assert all(injected not in q["instructions"] for q in by_state["hostile"]["questions"].values())
