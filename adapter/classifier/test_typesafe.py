"""Wire tests for the TypeSafe adapter over `httpx2.MockTransport`.

respx patches `httpx`, but the egress guard is an `httpx2` transport, so the
wire is faked one layer down (the guard's `inner` seam) and the guard still
runs. Fixtures are the committed live recordings in `testdata/`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from adapter.classifier.typesafe import (
    BreakerRegistry,
    CircuitBreaker,
    RateLimitedError,
    TypeSafeClassifier,
    UnauthorizedError,
)
from adapter.egress.transport import EgressPolicy, ProviderCredential
from llmgateway.classifier import ClassificationRequest, NoulQuestion
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError

FIXTURES = json.loads((Path(__file__).parent / "testdata/jev-live-wire-examples.json").read_text())
TOKEN = "tsk-SECRET-0123456789abcdef"
URL = "https://api.typesafe.ai"
pytestmark = pytest.mark.asyncio
NOUL_IDS = ("valid", "actionable", "has_scenario")


def request_of(*ids: str) -> ClassificationRequest:
    return ClassificationRequest(
        model="jev-1.14.0",
        state=FIXTURES["multi_question"]["request"]["state"],
        questions={i: NoulQuestion(instructions=f"{i}?") for i in ids},
    )


def noul_success() -> dict[str, Any]:
    """The live 200 body, narrowed to the noul answers v1 asks for."""
    body: dict[str, Any] = json.loads(json.dumps(FIXTURES["multi_question"]["response"]))
    body["answers"] = {k: body["answers"][k] for k in NOUL_IDS}
    return body


async def resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def make(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    breaker: CircuitBreaker | None = None,
    sleeps: list[float] | None = None,
) -> TypeSafeClassifier:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return TypeSafeClassifier(
        URL,
        credential=ProviderCredential(
            ref="typesafe", token=SecretStr(TOKEN), allowed_hosts=("api.typesafe.ai",)
        ),
        policy=EgressPolicy(),
        breaker=breaker or CircuitBreaker(),
        resolver=resolver,
        inner_transport=httpx2.MockTransport(handler),
        sleep=sleep,
    )


def sequence(*responses: httpx2.Response) -> tuple[Callable[..., httpx2.Response], list[int]]:
    seen: list[int] = []
    it = iter(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(1)
        return next(it)

    return handler, seen


def throttled(status: int = 429, retry_after: str | None = None) -> httpx2.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx2.Response(status, headers=headers)


async def test_success_maps_wire_to_domain_and_sends_the_contract() -> None:
    captured: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json=noul_success())

    result = await make(handler).classify(request_of(*NOUL_IDS), timeout=5)

    assert {k: v.p for k, v in result.answers.items()} == {
        "valid": 0.93,
        "actionable": 0.89,
        "has_scenario": 0.76,
    }
    assert result.usage is not None and result.usage.input_tokens == 489
    sent = captured[0]
    assert sent.url.path == "/v1/systemone"
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(sent.content)["questions"]["valid"] == {
        "type": "noul",
        "instructions": "valid?",
    }


async def test_429_twice_then_success_uses_retry_after() -> None:
    handler, seen = sequence(
        throttled(retry_after="2"), throttled(), httpx2.Response(200, json=noul_success())
    )
    sleeps: list[float] = []

    await make(handler, sleeps=sleeps).classify(request_of(*NOUL_IDS), timeout=30)

    assert len(seen) == 3
    assert sleeps == [2.0, 1.0]  # provider retry-after, then the 0.5*2 backoff


async def test_429_three_times_is_a_closed_failure() -> None:
    handler, seen = sequence(throttled(), throttled(), throttled())

    with pytest.raises(RateLimitedError):
        await make(handler).classify(request_of(*NOUL_IDS), timeout=30)
    assert len(seen) == 3


async def test_retry_after_above_the_cap_is_capped_at_10s() -> None:
    handler, _ = sequence(throttled(retry_after="120"), httpx2.Response(200, json=noul_success()))
    sleeps: list[float] = []

    await make(handler, sleeps=sleeps).classify(request_of(*NOUL_IDS), timeout=300)

    assert sleeps == [10.0]


async def test_retry_after_beyond_the_remaining_timeout_fails_without_waiting() -> None:
    handler, seen = sequence(throttled(retry_after="9"), httpx2.Response(200, json=noul_success()))
    sleeps: list[float] = []

    with pytest.raises(RateLimitedError):
        await make(handler, sleeps=sleeps).classify(request_of(*NOUL_IDS), timeout=5)
    assert sleeps == [] and len(seen) == 1


async def test_529_is_retried_like_429() -> None:
    handler, seen = sequence(throttled(529), httpx2.Response(200, json=noul_success()))

    await make(handler).classify(request_of(*NOUL_IDS), timeout=30)
    assert len(seen) == 2


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_is_not_retried(status: int) -> None:
    handler, seen = sequence(httpx2.Response(status), httpx2.Response(200, json=noul_success()))

    with pytest.raises(LLMUnreachableError):
        await make(handler).classify(request_of(*NOUL_IDS), timeout=30)
    assert len(seen) == 1


async def test_timeout_is_unreachable_and_not_retried() -> None:
    seen: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(1)
        raise httpx2.ReadTimeout("slow")

    with pytest.raises(LLMUnreachableError):
        await make(handler).classify(request_of(*NOUL_IDS), timeout=30)
    assert len(seen) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_401_403_are_unauthorized_and_logged_at_error(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(UnauthorizedError):
        await make(lambda r: httpx2.Response(status)).classify(request_of(*NOUL_IDS), timeout=5)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_422_is_malformed() -> None:
    f = FIXTURES["empty_questions"]
    with pytest.raises(LLMMalformedResponseError):
        await make(lambda r: httpx2.Response(f["http"], json=f["response"])).classify(
            request_of("a"), timeout=5
        )


async def test_invalid_body_is_malformed() -> None:
    with pytest.raises(LLMMalformedResponseError):
        await make(lambda r: httpx2.Response(200, content=b"not json")).classify(
            request_of(*NOUL_IDS), timeout=5
        )


async def test_wrong_answer_type_fails_the_whole_response() -> None:
    # The live recording answers `tier` (choice) and `reachability` (score) too.
    live = FIXTURES["multi_question"]["response"]
    with pytest.raises(LLMMalformedResponseError):
        await make(lambda r: httpx2.Response(200, json=live)).classify(
            request_of(*live["answers"]), timeout=5
        )


async def test_answers_for_other_ids_are_malformed() -> None:
    with pytest.raises(LLMMalformedResponseError):
        await make(lambda r: httpx2.Response(200, json=noul_success())).classify(
            request_of("valid", "other", "has_scenario"), timeout=5
        )


async def test_breaker_opens_then_half_opens_and_closes() -> None:
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return throttled() if len(calls) <= 2 else httpx2.Response(200, json=noul_success())

    c = make(handler, breaker=breaker)
    with pytest.raises(RateLimitedError):  # two 429s open it mid-call
        await c.classify(request_of(*NOUL_IDS), timeout=30)
    assert len(calls) == 2

    with pytest.raises(RateLimitedError, match="circuit breaker"):  # open: no wire call
        await c.classify(request_of(*NOUL_IDS), timeout=30)
    assert len(calls) == 2

    now[0] = 31.0  # cooldown over: one probe goes through and closes it
    await c.classify(request_of(*NOUL_IDS), timeout=30)
    await c.classify(request_of(*NOUL_IDS), timeout=30)
    assert len(calls) == 4


async def test_throttle_then_500_probe_closes_the_breaker() -> None:
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) <= 2:
            return throttled()
        return (
            httpx2.Response(500) if len(calls) == 3 else httpx2.Response(200, json=noul_success())
        )

    c = make(handler, breaker=breaker)
    with pytest.raises(RateLimitedError):
        await c.classify(request_of(*NOUL_IDS), timeout=30)
    now[0] = 31.0
    with pytest.raises(LLMUnreachableError) as probe:  # the probe is a 500, not a throttle
        await c.classify(request_of(*NOUL_IDS), timeout=30)
    assert not isinstance(probe.value, RateLimitedError)
    await c.classify(request_of(*NOUL_IDS), timeout=30)  # closed again: no second cooldown wait
    assert len(calls) == 4


async def test_token_never_appears_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    cases: list[Callable[[httpx2.Request], httpx2.Response]] = [
        lambda r: httpx2.Response(401, text=TOKEN),
        lambda r: httpx2.Response(500, text=TOKEN),
        lambda r: httpx2.Response(200, text=f'{{"model": "{TOKEN}"}}'),
        lambda r: httpx2.Response(422, text=TOKEN),
        lambda r: throttled(),
    ]
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for h in cases:
            with pytest.raises((LLMUnreachableError, LLMMalformedResponseError)) as info:
                await make(h).classify(request_of(*NOUL_IDS), timeout=5)
            texts += [str(info.value), repr(info.value), repr(vars(info.value))]
    texts += [r.getMessage() + str(r.exc_info) for r in caplog.records]
    assert not [t for t in texts if TOKEN in t]


async def test_breaker_registry_isolates_by_origin_and_credential_ref() -> None:
    reg = BreakerRegistry()
    a = reg.for_key("https://api.typesafe.ai/v1/x", "ref-a")
    assert reg.for_key("HTTPS://API.typesafe.ai/other", "ref-a") is a  # same origin+ref
    assert reg.for_key("https://api.typesafe.ai.:443/x", "ref-a") is a  # trailing dot, default port
    assert reg.for_key("https://api.typesafe.ai:8443", "ref-a") is not a
    assert reg.for_key("https://api.typesafe.ai", "ref-b") is not a
    assert reg.for_key("https://other.example", "ref-a") is not a
    for _ in range(5):
        a.record_throttled()
    assert not a.allow()
    assert reg.for_key("https://api.typesafe.ai", "ref-b").allow()
    assert BreakerRegistry().for_key("https://api.typesafe.ai", "ref-a").allow()  # no global
