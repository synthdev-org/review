"""Integration tests for adapter.product_knowledge.client against a real fake
MCP provider process-equivalent (real uvicorn socket server on loopback, both
protocol eras, FAULT injection) — plan Phase 4; US-PK5 (transport classes),
US-PK6 (hostile metadata / error bodies), US-PK16 (live responses validated
against the committed schemas), US-PK18 (versions), US-PK19 (both eras).

The guard is the production code: the endpoint host resolves to a *private*
address (``10.9.8.7``) and ``LoopbackTransport`` performs only the final
connect to the fake's loopback socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest
from pydantic import SecretStr

from adapter.product_knowledge.client import (
    ClientTimeouts,
    EndpointGate,
    McpProductKnowledgeClient,
    NegotiationCache,
)
from adapter.product_knowledge.credentials import CredentialMap
from adapter.product_knowledge.factory import ProviderClientFactory
from adapter.product_knowledge.testing.fake_provider import (
    ERROR_BODY_CANARY,
    HOSTILE_TEXT,
    Era,
    FakeProvider,
    Fault,
)
from adapter.product_knowledge.testing.loopback import LoopbackTransport, fixed_resolver
from adapter.product_knowledge.transport import EgressPolicy, ProviderCredential
from review.product_knowledge import (
    ProductKnowledgeError,
    ProviderOutcome,
    normalize_results,
)

pytestmark = pytest.mark.integration

SCHEMAS = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "product-knowledge-contract" / "v1"
)
PRIVATE_ADDRESS = "10.9.8.7"
HOST = "wiki.test"
TOKEN = "tok-SECRET-0123456789"
FAST = ClientTimeouts(connect_seconds=1.0, call_seconds=1.5)
# An unauthenticated provider must be operator-listed (M1); tests list HOST.
LISTED = EgressPolicy(endpoint_allowlist=(HOST,))


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / f"{name}.json").read_text())
    return loaded


def _client(
    provider: FakeProvider,
    *,
    credential: ProviderCredential | None = None,
    timeouts: ClientTimeouts = FAST,
    **kwargs: Any,
) -> tuple[McpProductKnowledgeClient, LoopbackTransport]:
    loopback = LoopbackTransport(provider.port)
    kwargs.setdefault("policy", LISTED)
    client = McpProductKnowledgeClient(
        f"http://{HOST}:{provider.port}/mcp",
        credential=credential,
        timeouts=timeouts,
        resolver=fixed_resolver(PRIVATE_ADDRESS),
        inner_transport=loopback,
        **kwargs,
    )
    return client, loopback


def _credential(allowed: tuple[str, ...] = (HOST,)) -> ProviderCredential:
    return ProviderCredential(ref="wiki", token=SecretStr(TOKEN), allowed_hosts=allowed)


async def _outcome(awaitable: Any) -> ProviderOutcome:
    with pytest.raises(ProductKnowledgeError) as excinfo:
        await awaitable
    return excinfo.value.outcome


# --- Assumption 3: the two-era test, first -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_both_eras_complete_search_and_fetch_through_the_injected_client_only(
    era: Era, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="adapter.product_knowledge.client")
    created: list[httpx2.AsyncClient] = []
    original_init = httpx2.AsyncClient.__init__

    def counting_init(self: httpx2.AsyncClient, *args: Any, **kwargs: Any) -> None:
        created.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx2.AsyncClient, "__init__", counting_init)

    async with FakeProvider(era=era, token=TOKEN) as provider:
        client, loopback = _client(provider, credential=_credential())
        negotiated = await client.negotiate()
        search_raw = await client.search("billing retry")
        fetch_raw = await client.fetch("DOC-1")

    assert negotiated.chosen_version == "1"
    assert negotiated.supported_versions == ["1"]
    assert set(negotiated.tools_present) == {
        "product_knowledge_contract",
        "product_knowledge_search",
        "product_knowledge_fetch",
        "product_knowledge_related",
    }
    assert [i["id"] for i in search_raw["results"]] == ["DOC-1", "DOC-2", "DOC-3"]  # type: ignore[index]
    assert fetch_raw["title"] == "Product doc 1"  # type: ignore[index]

    # US-PK16 integration link: live responses validate against the committed schemas.
    jsonschema.validate(search_raw, _schema("search.output"))
    jsonschema.validate(fetch_raw, _schema("fetch.output"))
    jsonschema.validate(
        {"supported_versions": negotiated.supported_versions}, _schema("contract.output")
    )

    # Era evidence: what the *provider* saw.
    methods = [r.rpc_method for r in provider.requests]
    if era is Era.MODERN:
        assert "server/discover" in methods
        assert "initialize" not in methods
        assert {r.protocol_version for r in provider.requests if r.rpc_method == "tools/call"} == {
            "2026-07-28"
        }
    else:
        assert "initialize" in methods  # auto fell back after the 400 probe
        assert any(r.session_id for r in provider.requests)
    assert methods.count("tools/call") == 3  # contract, search, fetch

    # US-PK19: the negotiated era is reported in debug logs, one per session.
    eras = [
        getattr(r, "protocol_version", None)
        for r in caplog.records
        if r.name == "adapter.product_knowledge.client"
        and r.getMessage() == "product-knowledge protocol era"
    ]
    assert eras == [era.value] * 3

    # Nothing but the injected, guarded client spoke: one client per operation,
    # every connection the guard's pinned address, every path the endpoint's.
    assert len(created) == 3
    assert set(loopback.pinned_hosts) == {PRIVATE_ADDRESS}
    assert {r.path for r in provider.requests} == {"/mcp"}
    assert {r.host_header for r in provider.requests} == {f"{HOST}:{provider.port}"}
    # The token went on every request, attached by the transport.
    assert {r.authorization for r in provider.requests} == {f"Bearer {TOKEN}"}
    # The SDK's own default is "gzip, deflate"; the guard must override it so
    # the byte cap sits on the wire (M3).
    assert {r.accept_encoding for r in provider.requests} == {"identity"}


@pytest.mark.asyncio
async def test_401_triggers_no_oauth_or_protected_resource_discovery() -> None:
    async with FakeProvider(era=Era.MODERN, token=TOKEN) as provider:
        client, loopback = _client(provider)  # no credential: every request is a 401
        assert await _outcome(client.negotiate()) is ProviderOutcome.AUTH_FAILED
    assert {r.path for r in provider.requests} == {"/mcp"}
    assert set(loopback.pinned_hosts) <= {PRIVATE_ADDRESS}


# --- Credentials ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_is_not_sent_when_the_host_is_not_bound() -> None:
    async with FakeProvider(era=Era.MODERN, token=TOKEN) as provider:
        client, _ = _client(provider, credential=_credential(allowed=("other.example",)))
        outcome = await _outcome(client.negotiate())
    assert outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE
    assert provider.requests == []


def test_credential_repr_never_contains_the_token() -> None:
    assert TOKEN not in repr(_credential())


# --- Negotiation: US-PK18 -------------------------------------------------------


@pytest.mark.asyncio
async def test_highest_common_major_is_chosen_and_config_never_pins_it() -> None:
    async with FakeProvider(era=Era.MODERN, supported_versions=["1", "2"]) as provider:
        client, _ = _client(provider)
        result = await client.negotiate()
        assert (result.chosen_version, result.supported_versions) == ("1", ["1", "2"])
        both, _ = _client(provider, supported_majors=frozenset({"1", "2"}))
        assert (await both.negotiate()).chosen_version == "2"


@pytest.mark.asyncio
async def test_no_overlap_is_no_provider_not_a_failure_to_negotiate() -> None:
    async with FakeProvider(era=Era.MODERN, supported_versions=["2"]) as provider:
        client, _ = _client(provider)
        result = await client.negotiate()
        assert result.chosen_version is None
        assert result.supported_versions == ["2"]
        assert await _outcome(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION


@pytest.mark.asyncio
async def test_deprecated_but_supported_major_still_works_and_is_reported() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(provider, deprecated_majors=frozenset({"1"}))
        result = await client.negotiate()
        assert result.chosen_version == "1"
        assert result.deprecated is True
        assert (await client.search("q")) is not None


@pytest.mark.asyncio
async def test_missing_or_malformed_contract_tool_is_unsupported_version() -> None:
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("bad_structure", tool="product_knowledge_contract")
    ) as provider:
        client, _ = _client(provider)
        assert (await client.negotiate()).chosen_version is None


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_contract_tool_missing_from_tools_list_at_negotiation_is_unsupported_version(
    era: Era,
) -> None:
    async with FakeProvider(era=era, omit_tools=frozenset({"product_knowledge_contract"})) as p:
        client, _ = _client(p)
        assert (await client.negotiate()).chosen_version is None
        assert await _outcome(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION


@pytest.mark.asyncio
async def test_concurrent_cold_start_negotiates_once() -> None:
    cache = NegotiationCache()
    async with FakeProvider(era=Era.MODERN) as provider:
        clients = [_client(provider, cache=cache)[0] for _ in range(6)]
        results = await asyncio.gather(*(c.negotiate() for c in clients))
        assert {r.chosen_version for r in results} == {"1"}
        contract_calls = [r for r in provider.requests if r.tool == "product_knowledge_contract"]
        assert len(contract_calls) == 1


@pytest.mark.asyncio
async def test_negotiation_cache_is_invalidated_by_a_provider_unsupported_version() -> None:
    cache = NegotiationCache()
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(provider, cache=cache)
        assert (await client.negotiate()).chosen_version == "1"
        provider.supported_versions = ["2"]  # provider rolled over under us
        assert await _outcome(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION
        assert (await client.negotiate()).chosen_version is None  # re-negotiated, not cached


@pytest.mark.asyncio
async def test_bypass_cache_probe_negotiates_exactly_once_across_negotiate_and_search() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(provider, bypass_cache=True, cache=NegotiationCache())
        await client.negotiate()  # the route's own negotiation
        assert (await client.search("q")) is not None
        contract = [r for r in provider.requests if r.tool == "product_knowledge_contract"]
        assert len(contract) == 1


@pytest.mark.asyncio
async def test_cache_key_excludes_the_token_and_cache_expires() -> None:
    now = [0.0]
    cache = NegotiationCache(ttl_seconds=10, clock=lambda: now[0])
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(provider, cache=cache)
        await client.negotiate()
        await client.negotiate()
        assert len([r for r in provider.requests if r.tool == "product_knowledge_contract"]) == 1
        now[0] = 11.0
        await client.negotiate()
        assert len([r for r in provider.requests if r.tool == "product_knowledge_contract"]) == 2


@pytest.mark.asyncio
async def test_only_arguments_the_tool_declares_are_sent() -> None:
    async with FakeProvider(era=Era.MODERN, undeclared=frozenset({"top_k"})) as provider:
        client, _ = _client(provider)
        await client.search("billing")
        search = next(r for r in provider.requests if r.tool == "product_knowledge_search")
    assert search.arguments is not None
    assert set(search.arguments) == {"contract_version", "query"}


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
        client, _ = _client(provider, credential=_credential())
        assert (await client.negotiate()).chosen_version == "1"  # contract unaffected
        started = time.monotonic()
        assert await _outcome(client.search("billing")) is expected
        assert time.monotonic() - started < 4  # bounded by wall-clock, not provider


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_oversized_connect_response_is_oversized(era: Era) -> None:
    # server/discover (modern) and initialize (legacy) answered over the cap.
    async with FakeProvider(era=era, fault=Fault("huge_connect")) as provider:
        client, _ = _client(provider)
        assert await _outcome(client.negotiate()) is ProviderOutcome.OVERSIZED
        assert not any(r.tool for r in provider.requests)  # never reached a tool call


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_oversized_tools_list_is_oversized_at_negotiation_and_at_call_time(
    era: Era,
) -> None:
    async with FakeProvider(era=era, fault=Fault("huge_tools_list")) as provider:
        client, _ = _client(provider)
        assert await _outcome(client.negotiate()) is ProviderOutcome.OVERSIZED
    async with FakeProvider(era=era) as provider:
        client, _ = _client(provider)
        assert (await client.negotiate()).chosen_version == "1"  # cached
        provider.fault = Fault("huge_tools_list")
        assert await _outcome(client.search("q")) is ProviderOutcome.OVERSIZED
        assert await _outcome(client.fetch("DOC-1")) is ProviderOutcome.OVERSIZED


@pytest.mark.asyncio
async def test_tool_missing_from_tools_list_at_call_time_is_unsupported_version() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(provider)
        assert (await client.negotiate()).chosen_version == "1"  # cached
        provider.omit_tools = frozenset({"product_knowledge_search", "product_knowledge_fetch"})
        assert await _outcome(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION
        assert await _outcome(client.fetch("DOC-1")) is ProviderOutcome.UNSUPPORTED_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_call_timeout_alone_bounds_a_hung_call(era: Era) -> None:
    # The session deadline (connect + call + grace) is ~31 s, so only
    # ``call_seconds`` can end this attempt within a couple of seconds.
    timeouts = ClientTimeouts(connect_seconds=1.0, call_seconds=0.5, teardown_grace_seconds=30.0)
    async with FakeProvider(
        era=era, fault=Fault("hang", tool="product_knowledge_search")
    ) as provider:
        client, _ = _client(provider, timeouts=timeouts)
        assert (await client.negotiate()).chosen_version == "1"
        started = time.monotonic()
        assert await _outcome(client.search("q")) is ProviderOutcome.TIMEOUT
        assert time.monotonic() - started < 3


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["product_knowledge_contract", "product_knowledge_fetch"])
async def test_input_required_is_a_provider_error_at_negotiation_and_fetch(tool: str) -> None:
    async with FakeProvider(era=Era.MODERN, fault=Fault("input_required", tool=tool)) as provider:
        client, _ = _client(provider)
        if tool == "product_knowledge_contract":
            assert await _outcome(client.negotiate()) is ProviderOutcome.PROVIDER_ERROR
        else:
            assert (await client.negotiate()).chosen_version == "1"
            assert await _outcome(client.fetch("DOC-1")) is ProviderOutcome.PROVIDER_ERROR


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
        client, _ = _client(
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
        client, _ = _client(
            provider, timeouts=ClientTimeouts(connect_seconds=0.5, call_seconds=60.0)
        )
        started = time.monotonic()
        assert await _outcome(client.negotiate()) is ProviderOutcome.TIMEOUT
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
        client, _ = _client(provider, timeouts=timeouts)
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
        client, _ = _client(provider)
        port = provider.port
    # provider is stopped now; the same port refuses connections
    refused = McpProductKnowledgeClient(
        f"http://{HOST}:{port}/mcp",
        timeouts=FAST,
        policy=LISTED,
        resolver=fixed_resolver(PRIVATE_ADDRESS),
        inner_transport=LoopbackTransport(port),
    )
    assert await _outcome(refused.negotiate()) is ProviderOutcome.UNREACHABLE
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
        assert await _outcome(client.negotiate()) is ProviderOutcome.UNREACHABLE


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
    assert await _outcome(client.negotiate()) is ProviderOutcome.UNREACHABLE


@pytest.mark.asyncio
async def test_redirect_is_never_followed_even_with_a_credential() -> None:
    async with FakeProvider(
        era=Era.MODERN, token=TOKEN, fault=Fault("redirect", tool="product_knowledge_search")
    ) as provider:
        client, loopback = _client(provider, credential=_credential())
        await client.negotiate()
        before = len(provider.requests)
        assert await _outcome(client.search("q")) is ProviderOutcome.BLOCKED_EGRESS
        assert [r.tool for r in provider.requests[before:]].count("product_knowledge_search") == 1
    assert set(loopback.pinned_hosts) == {PRIVATE_ADDRESS}  # the Location host was never dialled


@pytest.mark.asyncio
async def test_rate_limited_answer_starts_a_cooldown_with_no_further_calls() -> None:
    gate = EndpointGate(clock=time.monotonic)
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("status429", tool="product_knowledge_search")
    ) as provider:
        client, _ = _client(provider, gate=gate)
        await client.negotiate()
        assert await _outcome(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        before = len(provider.requests)
        assert await _outcome(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        assert len(provider.requests) == before  # refused locally, nothing sent


@pytest.mark.asyncio
async def test_provider_rate_limited_error_result_also_cools_down() -> None:
    gate = EndpointGate()
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(provider, gate=gate)
        await client.negotiate()
        provider.fault = Fault("rate_limited", tool="product_knowledge_search")
        assert await _outcome(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        before = len(provider.requests)
        assert await _outcome(client.search("q")) is ProviderOutcome.PROVIDER_ERROR
        assert len(provider.requests) == before


@pytest.mark.asyncio
async def test_per_endpoint_semaphore_bounds_in_flight_calls() -> None:
    gate = EndpointGate(max_in_flight=2)
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("slow", tool="product_knowledge_search"), slow_seconds=0.3
    ) as provider:
        client, _ = _client(
            provider,
            gate=gate,
            cache=NegotiationCache(),
            timeouts=ClientTimeouts(connect_seconds=2, call_seconds=5),
        )
        await client.negotiate()
        await asyncio.gather(*(client.search("q") for _ in range(6)))
    assert provider.peak_in_flight == 2


# --- Tolerant consumer sees partial / schema-violating payloads ----------------


@pytest.mark.asyncio
async def test_partial_and_schema_violating_payloads_are_classified_by_normalization() -> None:
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("partial", tool="product_knowledge_search")
    ) as provider:
        client, _ = _client(provider)
        normalized = normalize_results(await client.search("q"))
        assert normalized.outcome == "partial"
        assert len(normalized.items) == 3
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("bad_structure", tool="product_knowledge_search")
    ) as provider:
        client, _ = _client(provider)
        assert normalize_results(await client.search("q")).outcome == "malformed"


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
        client, _ = _client(provider, credential=_credential())
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


@pytest.mark.asyncio
async def test_egress_policy_is_applied_by_the_client() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = _client(
            provider,
            policy=EgressPolicy(allow_private_networks=False, endpoint_allowlist=(HOST,)),
        )
        assert await _outcome(client.negotiate()) is ProviderOutcome.BLOCKED_EGRESS
        assert provider.requests == []


# --- construction is total: malformed endpoint URLs fail closed at call time --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://wiki:abc/mcp",
        "http://wiki:99999/mcp",
        "http://[::1/mcp",
        "https://u:p@wiki.test/mcp",
    ],
)
async def test_malformed_endpoint_url_is_blocked_egress_at_call_time_not_a_constructor_error(
    url: str,
) -> None:
    client = McpProductKnowledgeClient(url)

    assert await _outcome(client.negotiate()) is ProviderOutcome.BLOCKED_EGRESS
    assert await _outcome(client.search("q")) is ProviderOutcome.BLOCKED_EGRESS


# --- factory wires credential hosts into the egress policy --------------------


@pytest.mark.asyncio
async def test_factory_credential_hosts_authorise_unauthenticated_call_no_allowlist() -> None:
    credentials = CredentialMap(
        {"wiki": ProviderCredential(ref="wiki", token=SecretStr(TOKEN), allowed_hosts=(HOST,))}
    )
    async with FakeProvider() as provider:
        factory = ProviderClientFactory(
            credentials=credentials,
            policy=EgressPolicy(),
            timeouts=FAST,
            resolver=fixed_resolver(PRIVATE_ADDRESS),
            inner_transport=LoopbackTransport(provider.port),
        )
        client = factory(f"http://{HOST}:{provider.port}/mcp", None)
        assert await client.search("q") is not None


@pytest.mark.asyncio
async def test_factory_fails_closed_without_a_request_when_host_is_not_bound() -> None:
    credentials = CredentialMap(
        {
            "wiki": ProviderCredential(
                ref="wiki", token=SecretStr(TOKEN), allowed_hosts=("other.test",)
            )
        }
    )
    async with FakeProvider() as provider:
        factory = ProviderClientFactory(
            credentials=credentials,
            policy=EgressPolicy(),
            timeouts=FAST,
            resolver=fixed_resolver(PRIVATE_ADDRESS),
            inner_transport=LoopbackTransport(provider.port),
        )
        with pytest.raises(ProductKnowledgeError) as excinfo:
            factory(f"http://{HOST}:{provider.port}/mcp", None)
        assert excinfo.value.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE
        assert provider.requests == []


@pytest.mark.asyncio
async def test_endpoint_with_userinfo_is_never_usable_even_when_listed_and_reachable() -> None:
    async with FakeProvider() as provider:
        client = McpProductKnowledgeClient(
            f"http://u:p@{HOST}:{provider.port}/mcp",
            policy=LISTED,
            timeouts=FAST,
            resolver=fixed_resolver(PRIVATE_ADDRESS),
            inner_transport=LoopbackTransport(provider.port),
        )
        assert await _outcome(client.search("q")) is ProviderOutcome.BLOCKED_EGRESS
        assert provider.requests == []
