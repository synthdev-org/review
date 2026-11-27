"""US-C5 integration tests: every classifier failure mode falls
back per finding to the LLM judge and the job succeeds. Real
`TypeSafeClassifier` over replayed wire responses."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from adapter.classifier.test_typesafe import make, noul_success
from adapter.classifier.typesafe import CircuitBreaker
from review.test_finding_judge_support import (
    FakeLLM,
    MaskingRedactor,
    Resolver,
    finding,
    run_step,
    selection,
    step_with,
    wiring,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def throttled(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(429)


def timing_out(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("slow")


def malformed(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, text="not json")


def wrong_type(request: httpx2.Request) -> httpx2.Response:
    # The live recording answers `tier` (choice) and `reachability` (score) too.
    return httpx2.Response(200, json={**noul_success(), "answers": {"valid": {"type": "score"}}})


def server_error(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(503)


def unauthorized(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(401)


@pytest.mark.parametrize(
    "handler", [throttled, timing_out, malformed, wrong_type, server_error, unauthorized]
)
async def test_each_failure_falls_back_per_finding_and_the_job_succeeds(
    handler: Callable[[httpx2.Request], httpx2.Response],
    points: Callable[[], dict[str, list[dict[str, Any]]]],
) -> None:
    llm = FakeLLM(fail_indexes=frozenset({1}))
    resolver = Resolver({"org-1": selection(make(handler, sleeps=[]), "active")})

    kept = await run_step(step_with(llm, wiring(resolver)), [finding(0), finding(1), finding(2)])

    assert [f.message for f in kept] == ["llm finding 0", "llm finding 2"]  # the LLM decided
    assert len(llm.calls) == 1
    fallbacks = points()["review.verification.llm_fallback_total"]
    assert [(a["reason"], a["_value"]) for a in fallbacks] == [("error", 3)]


async def test_an_open_breaker_makes_every_call_fail_fast_without_touching_the_wire() -> None:
    wire_calls: list[int] = []

    def counting_throttle(request: httpx2.Request) -> httpx2.Response:
        wire_calls.append(1)
        return httpx2.Response(429)

    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: 0.0)
    classifier = make(counting_throttle, breaker=breaker, sleeps=[])
    resolver = Resolver({"org-1": selection(classifier, "active")})
    llm = FakeLLM()

    kept = await run_step(step_with(llm, wiring(resolver, slots=1)), [finding(i) for i in range(5)])

    assert len(kept) == 5  # all handled by the LLM judge
    assert len(wire_calls) == 2  # the breaker opened after two throttles; the rest never left


async def test_credential_ref_that_no_longer_resolves_skips_the_classifier_with_a_warning(
    caplog: pytest.LogCaptureFixture, points: Callable[[], dict[str, list[dict[str, Any]]]]
) -> None:
    llm = FakeLLM()
    resolver = Resolver({"org-1": selection(None, "active")})

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        kept = await run_step(step_with(llm, wiring(resolver)), [finding(0), finding(1)])

    assert len(kept) == 2 and len(llm.calls) == 1
    assert any("credential does not resolve" in r.message for r in caplog.records)
    fallbacks = points()["review.verification.llm_fallback_total"]
    assert [(a["reason"], a["_value"]) for a in fallbacks] == [("credential_unresolved", 2)]


async def test_a_redaction_failure_sends_that_finding_to_the_llm_and_never_to_the_wire() -> None:
    sent: list[int] = []

    def ok(request: httpx2.Request) -> httpx2.Response:
        sent.append(1)
        return httpx2.Response(200, json={**noul_success(), "answers": _all_yes(request)})

    class Flaky(MaskingRedactor):
        async def redact(self, text: str) -> str:
            if "poison" in text:
                raise RuntimeError("scanner crashed")
            return await super().redact(text)

    resolver = Resolver({"org-1": selection(make(ok), "active")})
    llm = FakeLLM()

    kept = await run_step(
        step_with(llm, wiring(resolver, redactor=Flaky())),
        [finding(0, message="poison"), finding(1)],
    )

    assert len(kept) == 2
    assert len(sent) == 1  # only the finding that could be redacted left the deployment
    assert len(llm.calls) == 1


def _all_yes(request: httpx2.Request) -> dict[str, Any]:
    import json

    return {q: {"type": "noul", "noul": 0.95} for q in json.loads(request.content)["questions"]}


async def test_a_resolver_outage_never_fails_the_review() -> None:
    class Down:
        async def resolve(self, org_id: str) -> None:
            raise ConnectionError("postgres down")

    llm = FakeLLM()
    resolver = Resolver({})
    resolver.resolve = Down().resolve  # type: ignore[method-assign]

    kept = await run_step(step_with(llm, wiring(resolver)), [finding(0)])

    assert len(kept) == 1 and len(llm.calls) == 1
