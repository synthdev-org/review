"""US-C7: the shared classifier conformance suite against the real adapter (wire faked
at the egress guard's inner transport) and, opt-in, the live endpoint."""

from __future__ import annotations

import json
import os
import time

import httpx2
import pytest
from pydantic import SecretStr

from adapter.classifier.test_typesafe import FIXTURES, make
from adapter.classifier.typesafe import CircuitBreaker, TypeSafeClassifier
from adapter.egress.transport import EgressPolicy, ProviderCredential
from llmgateway.classifier import ClassificationRequest, NoulQuestion
from llmgateway.classifier_conformance import CHECKS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def echo(request: httpx2.Request) -> httpx2.Response:
    questions = json.loads(request.content)["questions"]
    return httpx2.Response(
        200,
        json={
            "model": "jev-1.14.0",
            "answers": {q: {"type": "noul", "noul": 0.5} for q in questions},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


@pytest.mark.parametrize("name", sorted(CHECKS))
async def test_typesafe_adapter_conforms(name: str) -> None:
    await CHECKS[name](make(echo))


_LIVE = pytest.mark.skipif(
    not os.environ.get("TYPESAFE_LIVE_KEY"), reason="live TypeSafe test: set TYPESAFE_LIVE_KEY"
)


def _live() -> TypeSafeClassifier:
    return TypeSafeClassifier(
        "https://api.typesafe.ai",
        credential=ProviderCredential(
            ref="live",
            token=SecretStr(os.environ["TYPESAFE_LIVE_KEY"]),
            allowed_hosts=("api.typesafe.ai",),
        ),
        policy=EgressPolicy(),
        breaker=CircuitBreaker(),
    )


@_LIVE
async def test_live_endpoint_conforms() -> None:
    live = _live()
    for check in CHECKS.values():
        await check(live)


LIVE_LATENCY_BOUND_S = 10.0


@_LIVE
async def test_live_five_question_shape_and_latency() -> None:
    """Nightly shape: five questions answered, one answer per id, inside the latency bound."""
    ids = [f"q{i}" for i in range(5)]
    request = ClassificationRequest(
        model="jev-1.14.0",
        state=FIXTURES["multi_question"]["request"]["state"],
        questions={i: NoulQuestion(instructions=f"{i}?") for i in ids},
    )
    started = time.monotonic()
    response = await _live().classify(request, timeout=30.0)
    elapsed = time.monotonic() - started
    assert set(response.answers) == set(ids)
    assert elapsed < LIVE_LATENCY_BOUND_S
