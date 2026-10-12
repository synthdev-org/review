"""Integration tests for adapter.product_knowledge.client against a real fake
MCP provider process-equivalent (real uvicorn socket server on loopback, both
protocol eras, FAULT injection) — plan Phase 4; US-PK5 (transport classes),
US-PK16 (live responses validated against the committed schemas), US-PK18
(versions), US-PK19 (both eras).

Shared helpers live in ``testing/client_harness.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx2
import jsonschema
import pytest
from pydantic import SecretStr

from adapter.product_knowledge.client import (
    McpProductKnowledgeClient,
    NegotiationCache,
)
from adapter.product_knowledge.credentials import CredentialMap
from adapter.product_knowledge.factory import ProviderClientFactory
from adapter.product_knowledge.testing.client_harness import (
    FAST,
    HOST,
    LISTED,
    PRIVATE_ADDRESS,
    TOKEN,
    credential,
    make_client,
    outcome_of,
    schema,
)
from adapter.product_knowledge.testing.fake_provider import (
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


# --- Assumption 3: the two-era test, first -----------------------------------


def _assert_era_evidence(era: Era, provider: FakeProvider) -> None:
    """What the *provider* saw."""
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


def _assert_only_the_guarded_client_spoke(
    provider: FakeProvider, loopback: LoopbackTransport, created: list[httpx2.AsyncClient]
) -> None:
    # One client per operation, every connection the guard's pinned address,
    # every path the endpoint's.
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
        client, loopback = make_client(provider, credential=credential())
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
    jsonschema.validate(search_raw, schema("search.output"))
    jsonschema.validate(fetch_raw, schema("fetch.output"))
    jsonschema.validate(
        {"supported_versions": negotiated.supported_versions}, schema("contract.output")
    )

    _assert_era_evidence(era, provider)

    # US-PK19: the negotiated era is reported in debug logs, one per session.
    era_records = [
        r
        for r in caplog.records
        if r.name == "adapter.product_knowledge.client"
        and r.getMessage() == "product-knowledge protocol era"
    ]
    assert [getattr(r, "protocol_version", None) for r in era_records] == [era.value] * 3

    _assert_only_the_guarded_client_spoke(provider, loopback, created)


@pytest.mark.asyncio
async def test_401_triggers_no_oauth_or_protected_resource_discovery() -> None:
    async with FakeProvider(era=Era.MODERN, token=TOKEN) as provider:
        client, loopback = make_client(provider)  # no credential: every request is a 401
        assert await outcome_of(client.negotiate()) is ProviderOutcome.AUTH_FAILED
    assert {r.path for r in provider.requests} == {"/mcp"}
    assert set(loopback.pinned_hosts) <= {PRIVATE_ADDRESS}


# --- Credentials ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_is_not_sent_when_the_host_is_not_bound() -> None:
    async with FakeProvider(era=Era.MODERN, token=TOKEN) as provider:
        client, _ = make_client(provider, credential=credential(allowed=("other.example",)))
        outcome = await outcome_of(client.negotiate())
    assert outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE
    assert provider.requests == []


def test_credential_repr_never_contains_the_token() -> None:
    assert TOKEN not in repr(credential())


# --- Negotiation: US-PK18 -------------------------------------------------------


@pytest.mark.asyncio
async def test_highest_common_major_is_chosen_and_config_never_pins_it() -> None:
    async with FakeProvider(era=Era.MODERN, supported_versions=["1", "2"]) as provider:
        client, _ = make_client(provider)
        result = await client.negotiate()
        assert (result.chosen_version, result.supported_versions) == ("1", ["1", "2"])
        both, _ = make_client(provider, supported_majors=frozenset({"1", "2"}))
        assert (await both.negotiate()).chosen_version == "2"


@pytest.mark.asyncio
async def test_no_overlap_is_no_provider_not_a_failure_to_negotiate() -> None:
    async with FakeProvider(era=Era.MODERN, supported_versions=["2"]) as provider:
        client, _ = make_client(provider)
        result = await client.negotiate()
        assert result.chosen_version is None
        assert result.supported_versions == ["2"]
        assert await outcome_of(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION


@pytest.mark.asyncio
async def test_deprecated_but_supported_major_still_works_and_is_reported() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider, deprecated_majors=frozenset({"1"}))
        result = await client.negotiate()
        assert result.chosen_version == "1"
        assert result.deprecated is True
        assert (await client.search("q")) is not None


@pytest.mark.asyncio
async def test_missing_or_malformed_contract_tool_is_unsupported_version() -> None:
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("bad_structure", tool="product_knowledge_contract")
    ) as provider:
        client, _ = make_client(provider)
        assert (await client.negotiate()).chosen_version is None


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_contract_tool_missing_from_tools_list_at_negotiation_is_unsupported_version(
    era: Era,
) -> None:
    async with FakeProvider(era=era, omit_tools=frozenset({"product_knowledge_contract"})) as p:
        client, _ = make_client(p)
        assert (await client.negotiate()).chosen_version is None
        assert await outcome_of(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION


@pytest.mark.asyncio
async def test_concurrent_cold_start_negotiates_once() -> None:
    cache = NegotiationCache()
    async with FakeProvider(era=Era.MODERN) as provider:
        clients = [make_client(provider, cache=cache)[0] for _ in range(6)]
        results = await asyncio.gather(*(c.negotiate() for c in clients))
        assert {r.chosen_version for r in results} == {"1"}
        contract_calls = [r for r in provider.requests if r.tool == "product_knowledge_contract"]
        assert len(contract_calls) == 1


@pytest.mark.asyncio
async def test_negotiation_cache_is_invalidated_by_a_provider_unsupported_version() -> None:
    cache = NegotiationCache()
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider, cache=cache)
        assert (await client.negotiate()).chosen_version == "1"
        provider.supported_versions = ["2"]  # provider rolled over under us
        assert await outcome_of(client.search("q")) is ProviderOutcome.UNSUPPORTED_VERSION
        assert (await client.negotiate()).chosen_version is None  # re-negotiated, not cached


@pytest.mark.asyncio
async def test_bypass_cache_probe_negotiates_exactly_once_across_negotiate_and_search() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider, bypass_cache=True, cache=NegotiationCache())
        await client.negotiate()  # the route's own negotiation
        assert (await client.search("q")) is not None
        contract = [r for r in provider.requests if r.tool == "product_knowledge_contract"]
        assert len(contract) == 1


@pytest.mark.asyncio
async def test_cache_key_excludes_the_token_and_cache_expires() -> None:
    now = [0.0]
    cache = NegotiationCache(ttl_seconds=10, clock=lambda: now[0])
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(provider, cache=cache)
        await client.negotiate()
        await client.negotiate()
        assert len([r for r in provider.requests if r.tool == "product_knowledge_contract"]) == 1
        now[0] = 11.0
        await client.negotiate()
        assert len([r for r in provider.requests if r.tool == "product_knowledge_contract"]) == 2


@pytest.mark.asyncio
async def test_only_arguments_the_tool_declares_are_sent() -> None:
    async with FakeProvider(era=Era.MODERN, undeclared=frozenset({"top_k"})) as provider:
        client, _ = make_client(provider)
        await client.search("billing")
        search = next(r for r in provider.requests if r.tool == "product_knowledge_search")
    assert search.arguments is not None
    assert set(search.arguments) == {"contract_version", "query"}


# --- Tolerant consumer sees partial / schema-violating payloads ----------------


@pytest.mark.asyncio
async def test_partial_and_schema_violating_payloads_are_classified_by_normalization() -> None:
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("partial", tool="product_knowledge_search")
    ) as provider:
        client, _ = make_client(provider)
        normalized = normalize_results(await client.search("q"))
        assert normalized.outcome == "partial"
        assert len(normalized.items) == 3
    async with FakeProvider(
        era=Era.MODERN, fault=Fault("bad_structure", tool="product_knowledge_search")
    ) as provider:
        client, _ = make_client(provider)
        assert normalize_results(await client.search("q")).outcome == "malformed"


@pytest.mark.asyncio
async def test_egress_policy_is_applied_by_the_client() -> None:
    async with FakeProvider(era=Era.MODERN) as provider:
        client, _ = make_client(
            provider,
            policy=EgressPolicy(allow_private_networks=False, endpoint_allowlist=(HOST,)),
        )
        assert await outcome_of(client.negotiate()) is ProviderOutcome.BLOCKED_EGRESS
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

    assert await outcome_of(client.negotiate()) is ProviderOutcome.BLOCKED_EGRESS
    assert await outcome_of(client.search("q")) is ProviderOutcome.BLOCKED_EGRESS


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
        assert await outcome_of(client.search("q")) is ProviderOutcome.BLOCKED_EGRESS
        assert provider.requests == []
