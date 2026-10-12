"""Integration tests for the connection-time egress guard
(adapter.product_knowledge.transport) over real sockets — US-PK12, plan
Phase 4 / Amendment A3.

The fake provider is a real server on loopback; the guard is the production
code. The endpoint host resolves (via an injected resolver) to the address
under test, and ``LoopbackTransport`` is the last hop, so "refused before any
bytes" is asserted on what the *fake actually received*.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
import pytest_asyncio
from pydantic import SecretStr

from adapter.product_knowledge.testing.fake_provider import Era, FakeProvider, Fault
from adapter.product_knowledge.testing.loopback import (
    LoopbackTransport,
    fixed_resolver,
    sequence_resolver,
)
from adapter.product_knowledge.transport import (
    EgressBlocked,
    EgressPolicy,
    GuardedTransport,
    ProviderCredential,
    Resolver,
    ResponseTooLarge,
    build_http_client,
)
from review.product_knowledge import ProviderOutcome

pytestmark = pytest.mark.integration

HOST = "wiki.test"
TOKEN = "tok-EGRESS-SECRET"
PUBLIC = "93.184.216.34"
PRIVATE = "10.1.2.3"


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[FakeProvider]:
    async with FakeProvider(era=Era.MODERN) as p:
        yield p


def _guard(
    provider: FakeProvider,
    resolver: Resolver,
    *,
    url: str | None = None,
    policy: EgressPolicy | None = None,
    credential: ProviderCredential | None = None,
    **kwargs: Any,
) -> tuple[GuardedTransport, LoopbackTransport, httpx2.AsyncClient]:
    loopback = LoopbackTransport(provider.port)
    url = url or f"http://{HOST}:{provider.port}/mcp"
    transport = GuardedTransport(
        url,
        # Unauthenticated callers must be operator-listed (M1): default to
        # listing the endpoint's own host so each test still exercises the
        # check it names (address class, scheme, redirects, ...).
        policy=policy or EgressPolicy(endpoint_allowlist=(urlsplit(url).hostname or "",)),
        credential=credential,
        resolver=resolver,
        inner=loopback,
        **kwargs,
    )
    return transport, loopback, build_http_client(transport)


async def _post(client: httpx2.AsyncClient, url: str, **kwargs: Any) -> httpx2.Response:
    return await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, **kwargs)


def _url(provider: FakeProvider, host: str = HOST) -> str:
    return f"http://{host}:{provider.port}/mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "::",
        "169.254.169.254",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "fd00:ec2::254",
        "100.100.100.200",
        "168.63.129.16",
        "192.0.0.192",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "224.0.0.1",
    ],
)
async def test_each_always_denied_class_is_refused_before_any_bytes(
    provider: FakeProvider, address: str
) -> None:
    transport, loopback, client = _guard(provider, fixed_resolver(address))
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS
    assert provider.requests == []
    assert loopback.pinned_hosts == []


@pytest.mark.asyncio
async def test_denied_even_when_private_networks_are_allowed_and_allowlisted(
    provider: FakeProvider,
) -> None:
    policy = EgressPolicy(allow_private_networks=True, endpoint_allowlist=(HOST,))
    transport, _, client = _guard(provider, fixed_resolver("169.254.169.254"), policy=policy)
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", ["127.0.0.1", "2130706433", "0x7f.1", "169.254.169.254"])
async def test_ip_literals_including_non_canonical_forms_never_connect(
    provider: FakeProvider, literal: str
) -> None:
    url = _url(provider, literal)

    async def must_not_resolve(host: str, port: int) -> list[str]:
        raise AssertionError("literals are classified directly")

    transport, loopback, client = _guard(provider, must_not_resolve, url=url)
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, url)
    assert provider.requests == []
    assert loopback.pinned_hosts == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address", ["10.1.2.3", "172.16.4.4", "192.168.1.10", "fd00::5", "100.64.0.9"]
)
async def test_private_ranges_are_allowed_by_default(provider: FakeProvider, address: str) -> None:
    _, loopback, client = _guard(provider, fixed_resolver(address))
    async with client:
        response = await _post(client, _url(provider))
    assert response.status_code in (200, 400, 405, 406)  # the fake answered
    assert len(provider.requests) == 1
    assert loopback.pinned_hosts == [address]


@pytest.mark.asyncio
async def test_private_ranges_are_refused_when_the_switch_is_off(provider: FakeProvider) -> None:
    policy = EgressPolicy(allow_private_networks=False)
    transport, _, client = _guard(provider, fixed_resolver(PRIVATE), policy=policy)
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS


@pytest.mark.asyncio
async def test_a_mixed_dns_answer_is_refused_if_any_address_is_denied(
    provider: FakeProvider,
) -> None:
    _, _, client = _guard(provider, fixed_resolver(PRIVATE, "127.0.0.1"))
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []


@pytest.mark.asyncio
async def test_allowlist_refuses_unlisted_hosts_even_if_public_and_admits_listed(
    provider: FakeProvider,
) -> None:
    deny = EgressPolicy(endpoint_allowlist=("*.corp.example",))
    transport, _, client = _guard(provider, fixed_resolver(PUBLIC), policy=deny)
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS

    allow = EgressPolicy(endpoint_allowlist=(HOST,))
    _, _, client = _guard(provider, fixed_resolver(PRIVATE), policy=allow)
    async with client:
        await _post(client, _url(provider))
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_dns_rebinding_connects_to_the_validated_address_and_revalidates(
    provider: FakeProvider,
) -> None:
    # First lookup validates as private; a later answer flips to loopback.
    resolver = sequence_resolver([PRIVATE], ["127.0.0.1"])
    transport, loopback, client = _guard(provider, resolver)
    async with client:
        await _post(client, _url(provider))
        assert loopback.pinned_hosts == [PRIVATE]  # connected to what was validated
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert len(provider.requests) == 1
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS


@pytest.mark.asyncio
async def test_request_url_is_rewritten_to_the_pinned_address_with_host_and_sni_kept(
    provider: FakeProvider,
) -> None:
    _, loopback, client = _guard(
        provider, fixed_resolver(PRIVATE), url=f"https://{HOST}:{provider.port}/mcp"
    )
    async with client:
        with pytest.raises(httpx2.TransportError):  # plain-http fake: TLS handshake fails
            await _post(client, f"https://{HOST}:{provider.port}/mcp")
    assert loopback.pinned_hosts == [PRIVATE]
    assert loopback.host_headers == [f"{HOST}:{provider.port}"]
    assert loopback.sni_hostnames == [HOST]  # certificate is verified against the hostname


@pytest.mark.asyncio
async def test_other_hosts_and_ports_are_not_reachable_through_the_transport(
    provider: FakeProvider,
) -> None:
    transport, _, client = _guard(provider, fixed_resolver(PRIVATE))
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, f"http://other.test:{provider.port}/mcp")
        with pytest.raises(EgressBlocked):
            await _post(client, f"http://{HOST}:{provider.port + 1}/mcp")
    assert provider.requests == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS


@pytest.mark.asyncio
async def test_unsupported_scheme_endpoint_is_never_usable(provider: FakeProvider) -> None:
    _, _, client = _guard(provider, fixed_resolver(PRIVATE), url="ftp://wiki.test/mcp")
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []


@pytest.mark.asyncio
async def test_3xx_is_not_followed_and_an_authorization_header_never_leaves(
    provider: FakeProvider,
) -> None:
    provider.fault = Fault("redirect")
    provider.token = TOKEN
    cred = ProviderCredential("wiki", SecretStr(TOKEN), (HOST,))
    transport, loopback, client = _guard(provider, fixed_resolver(PRIVATE), credential=cred)
    async with client:
        with pytest.raises(EgressBlocked):
            await client.post(
                _url(provider),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "product_knowledge_search", "arguments": {}},
                },
                follow_redirects=True,  # even a per-request opt-in must not follow
            )
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS
    assert len(provider.requests) == 1
    assert loopback.pinned_hosts == [PRIVATE]  # the Location target was never dialled


@pytest.mark.asyncio
async def test_token_is_never_sent_over_cleartext_http_to_a_public_address(
    provider: FakeProvider,
) -> None:
    cred = ProviderCredential("wiki", SecretStr(TOKEN), (HOST,))
    transport, _, client = _guard(provider, fixed_resolver(PUBLIC), credential=cred)
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS

    # No credential: cleartext to a public address is the operator's call.
    _, _, anon = _guard(provider, fixed_resolver(PUBLIC))
    async with anon:
        await _post(anon, _url(provider))
    assert len(provider.requests) == 1
    assert provider.requests[0].authorization is None


@pytest.mark.asyncio
async def test_token_is_attached_by_the_transport_and_a_caller_header_is_replaced(
    provider: FakeProvider,
) -> None:
    cred = ProviderCredential("wiki", SecretStr(TOKEN), (HOST,))
    _, _, client = _guard(provider, fixed_resolver(PRIVATE), credential=cred)
    async with client:
        await _post(client, _url(provider), headers={"Authorization": "Bearer attacker"})
    assert provider.requests[0].authorization == f"Bearer {TOKEN}"

    _, _, anon = _guard(provider, fixed_resolver(PRIVATE))
    async with anon:
        await _post(anon, _url(provider), headers={"Authorization": "Bearer attacker"})
    assert provider.requests[1].authorization is None  # nothing the caller set survives


@pytest.mark.asyncio
async def test_token_is_refused_for_a_host_it_is_not_bound_to(provider: FakeProvider) -> None:
    cred = ProviderCredential("wiki", SecretStr(TOKEN), ("*.other.example",))
    transport, _, client = _guard(provider, fixed_resolver(PRIVATE), credential=cred)
    async with client:
        with pytest.raises(EgressBlocked):
            await _post(client, _url(provider))
    assert provider.requests == []
    assert transport.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE


@pytest.mark.asyncio
async def test_proxy_environment_is_ignored(
    provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()  # nothing listens: a proxy-honouring client would fail
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{dead_port}")
    _, loopback, client = _guard(provider, fixed_resolver(PRIVATE))
    async with client:
        await _post(client, _url(provider))
    assert len(provider.requests) == 1
    assert loopback.pinned_hosts == [PRIVATE]


@pytest.mark.asyncio
async def test_resolution_failure_is_a_connect_error_not_a_policy_refusal(
    provider: FakeProvider,
) -> None:
    async def nxdomain(host: str, port: int) -> list[str]:
        raise OSError("Name or service not known")

    transport, _, client = _guard(provider, nxdomain)
    async with client:
        with pytest.raises(httpx2.ConnectError):
            await _post(client, _url(provider))
    assert transport.outcome is None


@pytest.mark.asyncio
async def test_response_body_is_capped_even_without_content_length(
    provider: FakeProvider,
) -> None:
    provider.fault = Fault("chunked_no_length")
    transport, _, client = _guard(provider, fixed_resolver(PRIVATE), max_response_bytes=64 * 1024)
    async with client:
        with pytest.raises(ResponseTooLarge):
            await client.post(
                _url(provider),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "x", "arguments": {}},
                },
            )
    assert transport.outcome is ProviderOutcome.OVERSIZED


@pytest.mark.asyncio
async def test_content_encoded_responses_are_refused(
    provider: FakeProvider,
) -> None:
    provider.fault = Fault("gzip_bomb")
    transport, _, client = _guard(provider, fixed_resolver(PRIVATE))
    async with client:
        with pytest.raises(ResponseTooLarge):
            await client.post(
                _url(provider),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "x", "arguments": {}},
                },
            )
    assert transport.outcome is ProviderOutcome.MALFORMED


@pytest.mark.parametrize("url", ["http://wiki:abc/mcp", "http://wiki:99999/mcp", "http://[::1/mcp"])
def test_guarded_transport_construction_is_total_for_malformed_urls(url: str) -> None:
    GuardedTransport(url, policy=EgressPolicy())
