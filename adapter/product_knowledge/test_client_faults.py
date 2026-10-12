"""
Integration tests for adapter.product_knowledge.client against a fake MCP provider.

The provider is a real fake MCP provider process-equivalent (real uvicorn socket server on loopback,
both protocol eras, FAULT injection) — plan Phase 4; US-PK5 (transport classes), US-PK6 (hostile
metadata / error bodies).

Shared helpers live in ``testing/client_harness.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from adapter.product_knowledge.client import (
    ClientTimeouts,
    EndpointGate,
    McpProductKnowledgeClient,
    NegotiationCache,
)
from adapter.product_knowledge.testing.client_harness import (
    FAST,
    HOST,
    LISTED,
    PRIVATE_ADDRESS,
    TOKEN,
    credential,
    make_client,
    outcome_of,
)
from adapter.product_knowledge.testing.fake_provider import (
    ERROR_BODY_CANARY,
    HOSTILE_TEXT,
    Era,
    FakeProvider,
    Fault,
)
from adapter.product_knowledge.testing.loopback import LoopbackTransport, fixed_resolver
from review.product_knowledge import (
    ProductKnowledgeError,
    ProviderOutcome,
)

pytestmark = pytest.mark.integration


# --- US-PK5: every transport failure class maps to its outcome -----------------

_FAULT_TO_OUTCOME = [
    ("hang", ProviderOutcome.TIMEOUT),
    ("slow_drip", ProviderOutcome.TIMEOUT),
    ("status401", ProviderOutcome.AUTH_FAILED),
    ("status403", ProviderOutcome.AUTH_FAILED),
    ("status429", ProviderOutcome.PROVIDER_ERROR),
    ("status500", ProviderOutcome.PROVIDER_ERROR),
    ("redirect", ProviderOutcome.BLOCKED_EGRESS),
    ("malformed", ProviderOutcome.MALFORMED),
    ("deep_json", ProviderOutcome.MALFORMED),
    ("huge", ProviderOutcome.OVERSIZED),
    ("chunked_no_length", ProviderOutcome.OVERSIZED),
    ("gzip_bomb", ProviderOutcome.MALFORMED),
    ("error_result", ProviderOutcome.PROVIDER_ERROR),
    ("input_required", ProviderOutcome.PROVIDER_ERROR),
    ("error_body_with_token", ProviderOutcome.PROVIDER_ERROR),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
@pytest.mark.parametrize(("mode", "expected"), _FAULT_TO_OUTCOME)
async def test_each_failure_class_yields_its_outcome_and_nothing_escapes(
    era: Era, mode: str, expected: ProviderOutcome
) -> None:
    async with FakeProvider(
        era=era, token=TOKEN, fault=Fault(mode, tool="product_knowledge_search")
    ) as provider:
        client, _ = make_client(provider, credential=credential())
        assert (await client.negotiate()).chosen_version == "1"  # contract unaffected
        started = time.monotonic()
        assert await outcome_of(client.search("billing")) is expected
        assert time.monotonic() - started < 4  # bounded by wall-clock, not provider


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_oversized_connect_response_is_oversized(era: Era) -> None:
    # server/discover (modern) and initialize (legacy) answered over the cap.
    async with FakeProvider(era=era, fault=Fault("huge_connect")) as provider:
        client, _ = make_client(provider)
        assert await outcome_of(client.negotiate()) is ProviderOutcome.OVERSIZED
        assert not any(r.tool for r in provider.requests)  # never reached a tool call


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_oversized_tools_list_is_oversized_at_negotiation_and_at_call_time(
    era: Era,
) -> None:
    async with FakeProvider(era=era, fault=Fault("huge_tools_list")) as provider:
        client, _ = make_client(provider)
        assert await outcome_of(client.negotiate()) is ProviderOutcome.OVERSIZED
    async with FakeProvider(era=era) as provider:
        client, _ = make_client(provider)
        assert (await client.negotiate()).chosen_version == "1"  # cached
        provider.fault = Fault("huge_tools_list")
        assert await outcome_of(client.search("q")) is ProviderOutcome.OVERSIZED
        assert await outcome_of(client.fetch("DOC-1")) is ProviderOutcome.OVERSIZED


@pytest.mark.asyncio
async def test_tool_missing_from_tools_list_at_call_time_is_unsupported_version() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider)
        assert (await client.negotiate()).chosen_version == "1"  # cached
        provider.omit_tools = frozenset({"product_knowledge_search", "product_knowledge_fetch"})
        assert await outcome_of(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION
        assert await outcome_of(client.fetch("DOC-1")) is ProviderOutcome.UNSUPPORTED_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_call_timeout_alone_bounds_a_hung_call(era: Era) -> None:
    # The session deadline (connect + call + grace) is ~31 s, so only
    # ``call_seconds`` can end this attempt within a couple of seconds.
    timeouts = ClientTimeouts(connect_seconds=1.0, call_seconds=0.5, teardown_grace_seconds=30.0)
    async with FakeProvider(
        era=era, fault=Fault("hang", tool="product_knowledge_search")
    ) as provider:
        client, _ = make_client(provider, timeouts=timeouts)
        assert (await client.negotiate()).chosen_version == "1"
        started = time.monotonic()
        assert await outcome_of(client.search("q")) is ProviderOutcome.TIMEOUT
        assert time.monotonic() - started < 3


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["product_knowledge_contract", "product_knowledge_fetch"])
async def test_input_required_is_a_provider_error_at_negotiation_and_fetch(tool: str) -> None:
    async with FakeProvider(era=Era.MODERN, fault=Fault("input_required", tool=tool)) as provider:
        client, _ = make_client(provider)
        if tool == "product_knowledge_contract":
            assert await outcome_of(client.negotiate()) is ProviderOutcome.PROVIDER_ERROR
        else:
            assert (await client.negotiate()).chosen_version == "1"
            assert await outcome_of(client.fetch("DOC-1")) is ProviderOutcome.PROVIDER_ERROR


@pytest.mark.asyncio
async def test_negotiation_change_is_logged_on_first_result_and_on_change_not_on_repeat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="adapter.product_knowledge.client")
    cache = NegotiationCache()

    def logged() -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.getMessage() == "product-knowledge contract negotiation result"
        ]

    async with FakeProvider(era=Era.MODERN, supported_versions=["1"]) as provider:
        client, _ = make_client(
            provider, cache=cache, bypass_cache=True, supported_majors=frozenset({"1", "2"})
        )
        await client.negotiate()
        assert [r.contract_version for r in logged()] == ["1"]  # type: ignore[attr-defined]
        await client.negotiate()  # repeat: same answer, silent
        assert len(logged()) == 1
        provider.supported_versions = ["1", "2"]  # version change
        await client.negotiate()
        assert [r.contract_version for r in logged()] == ["1", "2"]  # type: ignore[attr-defined]
        await client.negotiate()
        assert len(logged()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_connect_phase_hang_is_bounded_by_the_connect_timeout(era: Era) -> None:
    # call_seconds is huge, so only ClientTimeouts.connect_seconds (the timeout
    # around SDK startup: server/discover / initialize) can end this attempt.
    async with FakeProvider(era=era, fault=Fault("hang_connect")) as provider:
        client, _ = make_client(
            provider, timeouts=ClientTimeouts(connect_seconds=0.5, call_seconds=60.0)
        )
        started = time.monotonic()
        assert await outcome_of(client.negotiate()) is ProviderOutcome.TIMEOUT
        assert time.monotonic() - started < 3
        assert not any(r.tool for r in provider.requests)  # never got to a tool call


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hang_delete", "drip_delete"])
async def test_teardown_delete_hang_or_drip_is_bounded_by_the_session_deadline(
    mode: str,
) -> None:
    # Legacy era closes with DELETE + Mcp-Session-Id. The call itself succeeds;
    # only the whole-session wall-clock deadline (connect + call + grace) can
    # end a DELETE that hangs or drips a byte per 0.2 s (defeats read timeouts).
    timeouts = ClientTimeouts(connect_seconds=1.0, call_seconds=1.5, teardown_grace_seconds=0.5)
    async with FakeProvider(era=Era.LEGACY, fault=Fault(mode)) as provider:
        client, _ = make_client(provider, timeouts=timeouts)
        started = time.monotonic()
        with pytest.raises(ProductKnowledgeError) as excinfo:
            await client.search("billing")
        elapsed = time.monotonic() - started
        assert excinfo.value.outcome is ProviderOutcome.TIMEOUT
        assert elapsed < timeouts.session_seconds + 1.5
        assert any(r.http_method == "DELETE" for r in provider.requests)  # path exercised


@pytest.mark.asyncio
async def test_connection_refused_is_unreachable() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider)
        port = provider.port
    # provider is stopped now; the same port refuses connections
    refused = McpProductKnowledgeClient(
        f"http://{HOST}:{port}/mcp",
        timeouts=FAST,
        policy=LISTED,
        resolver=fixed_resolver(PRIVATE_ADDRESS),
        inner_transport=LoopbackTransport(port),
    )
    assert await outcome_of(refused.negotiate()) is ProviderOutcome.UNREACHABLE
    assert client is not None


@pytest.mark.asyncio
async def test_tls_failure_is_unreachable() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client = McpProductKnowledgeClient(
            f"https://{HOST}:{provider.port}/mcp",  # provider speaks plain http
            timeouts=FAST,
            policy=LISTED,
            resolver=fixed_resolver(PRIVATE_ADDRESS),
            inner_transport=LoopbackTransport(provider.port),
        )
        assert await outcome_of(client.negotiate()) is ProviderOutcome.UNREACHABLE


@pytest.mark.asyncio
async def test_unresolvable_host_is_unreachable() -> None:
    async def nxdomain(host: str, port: int) -> list[str]:
        raise OSError("Name or service not known")

    client = McpProductKnowledgeClient(
        f"http://{HOST}:9/mcp",
        timeouts=FAST,
        policy=LISTED,
        resolver=nxdomain,
        inner_transport=LoopbackTransport(9),
    )
    assert await outcome_of(client.negotiate()) is ProviderOutcome.UNREACHABLE


@pytest.mark.asyncio
async def test_redirect_is_never_followed_even_with_a_credential() -> None:
    async with FakeProvider(
        era=Era.MODERN, token=TOKEN, fault=Fault("redirect", tool="product_knowledge_search")
    ) as provider:
        client, loopback = make_client(provider, credential=credential())
        await client.negotiate()
        before = len(provider.requests)
        assert await outcome_of(client.search("q")) is ProviderOutcome.BLOCKED_EGRESS
        assert [r.tool for r in provider.requests[before:]].count("product_knowledge_search") == 1
    assert set(loopback.pinned_hosts) == {PRIVATE_ADDRESS}  # the Location host was never dialled


@pytest.mark.asyncio
async def test_rate_limited_answer_starts_a_cooldown_with_no_further_calls() -> None:
    gate = EndpointGate(clock=time.monotonic)
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("status429", tool="product_knowledge_search")
    ) as provider:
        client, _ = make_client(provider, gate=gate)
        await client.negotiate()
        assert await outcome_of(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        before = len(provider.requests)
        assert await outcome_of(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        assert len(provider.requests) == before  # refused locally, nothing sent


@pytest.mark.asyncio
async def test_provider_rate_limited_error_result_also_cools_down() -> None:
    gate = EndpointGate()
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider, gate=gate)
        await client.negotiate()
        provider.fault = Fault("rate_limited", tool="product_knowledge_search")
        assert await outcome_of(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        before = len(provider.requests)
        assert await outcome_of(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        assert len(provider.requests) == before


@pytest.mark.asyncio
async def test_per_endpoint_semaphore_bounds_in_flight_calls() -> None:
    gate = EndpointGate(max_in_flight=2)
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("slow", tool="product_knowledge_search"), slow_seconds=0.3
    ) as provider:
        client, _ = make_client(
            provider,
            gate=gate,
            cache=NegotiationCache(),
            timeouts=ClientTimeouts(connect_seconds=2, call_seconds=5),
        )
        await client.negotiate()
        await asyncio.gather(*(client.search("q") for _ in range(6)))
    assert provider.peak_in_flight == 2


# --- US-PK6: provider-controlled text never reaches outcome, logs or exceptions --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["hostile_metadata", "error_result", "error_body_with_token", "malformed"]
)
async def test_provider_text_and_token_never_reach_logs_or_exceptions(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with FakeProvider(era=Era.MODERN, token=TOKEN, fault=Fault(mode)) as provider:
        client, _ = make_client(provider, credential=credential())
        try:
            await client.negotiate()
            await client.search("billing")
        except ProductKnowledgeError as err:
            assert str(err) == err.outcome.value
            blob = f"{err!r} {err!s} {err.__cause__!r} {err.__context__!r}"
        else:
            blob = ""
    for secret in (TOKEN, HOSTILE_TEXT, ERROR_BODY_CANARY, "not json"):
        assert secret not in blob
        assert secret not in caplog.text
        assert all(secret not in json.dumps(r.__dict__, default=str) for r in caplog.records)
