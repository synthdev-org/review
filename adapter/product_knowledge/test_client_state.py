"""
Unit tests for the client's in-process state and classification seams.

No sockets: endpoint gate cool-down/eviction, negotiation cache key, and the
exception-to-outcome mapping. Network behaviour is in ``test_client.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from adapter.egress.transport import (
    EgressOutcome,
    EgressPolicy,
    GuardedTransport,
    ProviderCredential,
)
from adapter.product_knowledge.client import (
    EndpointGate,
    McpProductKnowledgeClient,
    NegotiationCache,
)
from review.product_knowledge import NegotiationResult, ProductKnowledgeError, ProviderOutcome

URL = "https://wiki.example.com:8443/mcp"


@pytest.mark.asyncio
async def test_cooldown_blocks_until_the_clock_reaches_expiry_then_clears() -> None:
    now = [100.0]
    gate = EndpointGate(clock=lambda: now[0])
    gate.start_cooldown("ep", 30.0)

    assert gate.cooling("ep") is True
    with pytest.raises(ProductKnowledgeError) as blocked:
        async with gate.slot("ep"):
            raise AssertionError("slot must not be granted while cooling")
    assert blocked.value.outcome is ProviderOutcome.PROVIDER_ERROR

    now[0] = 129.9
    assert gate.cooling("ep") is True
    now[0] = 130.0  # boundary: expired
    assert gate.cooling("ep") is False
    assert ("ep", None) not in gate._cooldowns  # expired entries are dropped, not kept
    async with gate.slot("ep"):
        pass
    assert gate.cooling("other") is False  # per-endpoint


@pytest.mark.asyncio
async def test_idle_endpoints_are_evicted_oldest_first_and_held_ones_are_kept() -> None:
    gate = EndpointGate(max_in_flight=1, max_endpoints=2)
    async with gate.slot("a"):  # "a" stays held while the table overflows
        async with gate.slot("b"):
            pass
        async with gate.slot("c"):
            pass
        async with gate.slot("d"):
            pass
        assert "a" in gate._semaphores  # held: never evicted
        assert len(gate._semaphores) <= 3  # bounded: idle ones were dropped
        assert "b" not in gate._semaphores  # oldest idle went first
    async with gate.slot("e"):
        pass
    assert len(gate._semaphores) <= 2


@pytest.mark.asyncio
async def test_gate_tracks_in_flight_users_explicitly_and_releases_them() -> None:
    gate = EndpointGate(max_in_flight=2)
    async with gate.slot("a"), gate.slot("a"):
        assert gate._users == {"a": 2}
    assert gate._users == {}


def test_cooldown_is_per_credential_ref_on_the_same_origin() -> None:
    gate = EndpointGate()
    gate.start_cooldown("https://h:443", 30.0, "tenant-a")

    assert gate.cooling("https://h:443", "tenant-a") is True
    assert gate.cooling("https://h:443", "tenant-b") is False
    assert gate.cooling("https://h:443", None) is False


def test_gate_origin_ignores_the_path_and_spells_the_default_port() -> None:
    a = _client("t", url="https://WIKI.Example.com./mcp")
    b = _client("t", url="https://wiki.example.com:443/other/path")

    assert a._origin == b._origin == "https://wiki.example.com:443"
    assert a._cache_key != b._cache_key  # negotiation stays per URL


def test_cooldown_table_is_bounded() -> None:
    now = [0.0]
    gate = EndpointGate(max_endpoints=3, clock=lambda: now[0])
    for i in range(10):
        now[0] += 1
        gate.start_cooldown(f"ep{i}", 1000.0)

    assert len(gate._cooldowns) <= 3
    assert gate.cooling("ep9")  # newest kept; soonest-to-expire dropped first
    now[0] += 5000
    gate.start_cooldown("fresh", 10.0)
    assert len(gate._cooldowns) == 1  # expired entries pruned


@pytest.mark.asyncio
async def test_negotiation_locks_are_dropped_after_failures_and_never_while_awaited() -> None:
    cache = NegotiationCache()
    result = NegotiationResult(chosen_version="1", supported_versions=["1"], tools_present=[])

    async def boom() -> NegotiationResult:
        raise RuntimeError

    for i in range(20):
        with pytest.raises(RuntimeError):
            await cache.get_or_negotiate((f"u{i}", None), boom)
    assert cache._locks == {} and cache._lock_users == {}

    release = asyncio.Event()
    calls = 0

    async def slow() -> NegotiationResult:
        nonlocal calls
        calls += 1
        await release.wait()
        return result

    key = ("held", None)
    first = asyncio.create_task(cache.get_or_negotiate(key, slow))
    second = asyncio.create_task(cache.get_or_negotiate(key, slow))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert key in cache._locks  # held + awaited: present
    release.set()
    assert await first == await second == result
    assert calls == 1  # single-flight survived
    assert cache._locks == {}


def test_last_chosen_is_bounded() -> None:
    cache = NegotiationCache(max_entries=3)
    for i in range(10):
        cache.changed((f"u{i}", None), "1")

    assert len(cache._last_chosen) <= 3


def _client(token: str, ref: str = "wiki", url: str = URL) -> McpProductKnowledgeClient:
    credential = ProviderCredential(
        ref=ref, token=SecretStr(token), allowed_hosts=("wiki.example.com",)
    )
    return McpProductKnowledgeClient(url, credential=credential)


def test_negotiation_cache_key_is_endpoint_plus_credential_ref_never_the_token() -> None:
    first = _client("tok-AAAA-secret")
    second = _client("tok-BBBB-secret")

    assert first._cache_key == second._cache_key  # token is not part of the key
    assert first._cache_key == ("https://wiki.example.com:8443/mcp", "wiki")
    for token in ("tok-AAAA-secret", "tok-BBBB-secret"):
        assert token not in repr(first._cache_key)
    # a different ref is a different key
    assert _client("tok-AAAA-secret", ref="other")._cache_key != first._cache_key
    # host spelling is canonicalised into the key
    assert _client("t", url="https://WIKI.Example.com.:8443/mcp")._cache_key == first._cache_key


@pytest.mark.parametrize(
    "exc",
    [
        RecursionError("maximum recursion depth exceeded"),
        json.JSONDecodeError("bad", "{", 0),
    ],
    ids=["recursion", "json_decode"],
)
def test_parse_failures_each_map_to_malformed_on_their_own(exc: BaseException) -> None:
    client = _client("t")
    transport = GuardedTransport(URL, policy=EgressPolicy())
    assert client._classify(exc, transport, URL) is ProviderOutcome.MALFORMED
    # nested in the SDK's task-group wrapping, as it arrives in production
    wrapped: Any = BaseExceptionGroup("tg", [exc])
    assert client._classify(wrapped, transport, URL) is ProviderOutcome.MALFORMED


def test_unrelated_exception_is_a_provider_error_not_malformed() -> None:
    client = _client("t")
    transport = GuardedTransport(URL, policy=EgressPolicy())
    assert client._classify(ValueError("x"), transport, URL) is ProviderOutcome.PROVIDER_ERROR
    assert client._classify(httpx2.ConnectError("x"), transport, URL) is ProviderOutcome.UNREACHABLE


# --- extracted classification / construction helpers ------------------------


def _bare_transport(status: int | None = None, retry: int | None = None) -> GuardedTransport:
    transport = GuardedTransport(URL, policy=EgressPolicy())
    transport.status = status
    transport.retry_after_seconds = retry
    return transport


def test_classify_precedence_typed_then_timeout_then_transport_then_status() -> None:
    client = _client("t")
    typed = ProductKnowledgeError(ProviderOutcome.UNSUPPORTED_VERSION)
    group: Any = BaseExceptionGroup("tg", [httpx2.ReadTimeout("x"), typed])
    assert client._classify(group, _bare_transport(401), URL) is ProviderOutcome.UNSUPPORTED_VERSION

    timeout_and_transport: Any = BaseExceptionGroup(
        "tg", [httpx2.ConnectError("x"), httpx2.ReadTimeout("x")]
    )
    assert (
        client._classify(timeout_and_transport, _bare_transport(401), URL)
        is ProviderOutcome.TIMEOUT
    )
    assert client._classify(TimeoutError(), _bare_transport(), URL) is ProviderOutcome.TIMEOUT
    assert (
        client._classify(httpx2.ConnectError("x"), _bare_transport(401), URL)
        is ProviderOutcome.UNREACHABLE
    )
    assert client._classify(ValueError(), _bare_transport(401), URL) is ProviderOutcome.AUTH_FAILED
    assert client._classify(ValueError(), _bare_transport(403), URL) is ProviderOutcome.AUTH_FAILED
    # a status wins over a malformed body
    assert (
        client._classify(RecursionError(), _bare_transport(401), URL) is ProviderOutcome.AUTH_FAILED
    )


def test_classify_transport_outcome_wins_over_everything() -> None:
    client = _client("t")
    transport = _bare_transport(401)
    transport.outcome = EgressOutcome.OVERSIZED

    typed = ProductKnowledgeError(ProviderOutcome.UNSUPPORTED_VERSION)
    assert client._classify(typed, transport, URL) is ProviderOutcome.OVERSIZED


def test_status_outcome_429_starts_a_capped_cooldown() -> None:
    gate = EndpointGate()
    credential = ProviderCredential(ref="w", token=SecretStr("t"), allowed_hosts=("h",))
    client = McpProductKnowledgeClient(URL, credential=credential, gate=gate)

    assert client._status_outcome(_bare_transport(500), "ep") is None
    assert not gate.cooling("ep", "w")
    assert (
        client._status_outcome(_bare_transport(429, 99999), "ep") is ProviderOutcome.PROVIDER_ERROR
    )
    assert gate.cooling("ep", "w")
    assert gate._cooldowns[("ep", "w")] - gate._clock() <= 300.0 + 1


def test_unknown_tuning_keyword_is_a_type_error_like_a_named_parameter() -> None:
    with pytest.raises(TypeError, match="bypas_cache"):
        McpProductKnowledgeClient(URL, bypas_cache=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="supported_majors"):
        from adapter.egress.credentials import CredentialMap
        from adapter.product_knowledge.factory import ProviderClientFactory

        ProviderClientFactory(
            credentials=CredentialMap.empty(),
            policy=EgressPolicy(),
            supported_majors=frozenset(),  # type: ignore[call-arg]
        )


def test_tuning_options_are_applied() -> None:
    cache, gate = NegotiationCache(), EndpointGate()
    client = McpProductKnowledgeClient(
        URL,
        cache=cache,
        gate=gate,
        supported_majors=frozenset({"7"}),
        deprecated_majors=frozenset({"6"}),
        rate_limit_cooldown_seconds=1.5,
        bypass_cache=True,
        top_k=2,
    )

    assert client._cache is cache
    assert client._gate is gate
    assert client._supported == frozenset({"7"})
    assert client._deprecated == frozenset({"6"})
    assert client._cooldown == 1.5
    assert client._bypass_cache is True
    assert client._top_k == 2


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://Wiki.example.com:8443/mcp",
            (True, "wiki.example.com", "https://wiki.example.com:8443", "wiki.example.com:8443"),
        ),
        ("http://h.internal/x", (True, "h.internal", "http://h.internal:80", "h.internal:")),
        ("https://h.internal/", (True, "h.internal", "https://h.internal:443", "h.internal:")),
    ],
)
def test_endpoint_identity(url: str, expected: tuple[bool, str, str, str]) -> None:
    from adapter.product_knowledge.client import _endpoint_identity

    usable, host, origin, key = _endpoint_identity(url, "ref")
    assert (usable, host, origin) == expected[:3]
    assert key[1] == "ref"
    assert expected[3] in key[0]


@pytest.mark.parametrize("url", ["http://h:abc/", "http://h:99999/", "http://[::1/"])
def test_endpoint_identity_malformed_url_is_unusable(url: str) -> None:
    from adapter.product_knowledge.client import _endpoint_identity

    assert _endpoint_identity(url, "r") == (
        False,
        "invalid",
        "invalid://invalid",
        ("invalid://invalid", "r"),
    )
