"""Unit tests (stub inner transport, no sockets) for the transport's
unauthenticated-host rule (M1), port-aware binding (M2) and defensive header
parsing (L2)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2
import pytest
from pydantic import SecretStr

from adapter.product_knowledge.transport import (
    EgressBlocked,
    EgressPolicy,
    GuardedTransport,
    ProviderCredential,
    ResponseTooLarge,
)
from review.product_knowledge import ProviderOutcome


class _Body(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.iterated = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        yield b"{}"

    async def aclose(self) -> None:
        self.closed = True


class _Inner(httpx2.AsyncBaseTransport):
    def __init__(self, status: int = 200, headers: list[tuple[bytes, bytes]] | None = None):
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.headers = headers or []
        self.body = _Body()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, headers=self.headers, stream=self.body)


async def _resolver(host: str, port: int) -> list[str]:
    return ["10.0.0.5"]


def _transport(
    url: str,
    inner: _Inner,
    policy: EgressPolicy | None = None,
    credential: ProviderCredential | None = None,
) -> GuardedTransport:
    return GuardedTransport(
        url, policy=policy or EgressPolicy(), credential=credential, resolver=_resolver, inner=inner
    )


async def _send(transport: GuardedTransport, url: str) -> httpx2.Response:
    return await transport.handle_async_request(httpx2.Request("POST", url, content=b"{}"))


def _cred(*hosts: str) -> ProviderCredential:
    return ProviderCredential(ref="wiki", token=SecretStr("tok-SECRET"), allowed_hosts=hosts)


URL = "https://wiki.internal:8443/mcp"


@pytest.mark.asyncio
async def test_unauthenticated_request_with_no_allowlist_or_credential_host_sends_nothing() -> None:
    inner = _Inner()
    transport = _transport(URL, inner)  # default policy: nothing listed

    with pytest.raises(EgressBlocked):
        await _send(transport, URL)

    assert inner.requests == []
    assert transport.outcome is ProviderOutcome.BLOCKED_EGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        EgressPolicy(endpoint_allowlist=("wiki.internal:8443",)),
        EgressPolicy(credential_hosts=("wiki.internal:8443",)),
        EgressPolicy(endpoint_allowlist=("other.internal",), credential_hosts=("*.internal",)),
    ],
)
async def test_unauthenticated_request_is_allowed_for_a_listed_origin(policy: EgressPolicy) -> None:
    inner = _Inner()
    response = await _send(_transport(URL, inner, policy), URL)

    assert response.status_code == 200
    assert len(inner.requests) == 1
    assert "authorization" not in inner.requests[0].headers


@pytest.mark.asyncio
async def test_unauthenticated_allowlist_port_mismatch_sends_nothing() -> None:
    inner = _Inner()
    transport = _transport(URL, inner, EgressPolicy(endpoint_allowlist=("wiki.internal:9443",)))

    with pytest.raises(EgressBlocked):
        await _send(transport, URL)

    assert inner.requests == []


@pytest.mark.asyncio
async def test_credential_with_a_different_port_is_refused_and_no_token_leaves() -> None:
    inner = _Inner()
    transport = _transport(URL, inner, credential=_cred("wiki.internal:9443"))

    with pytest.raises(EgressBlocked):
        await _send(transport, URL)

    assert inner.requests == []
    assert transport.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE


@pytest.mark.asyncio
async def test_credential_with_matching_port_or_no_port_sends_the_token() -> None:
    for entry in ("wiki.internal:8443", "wiki.internal"):
        inner = _Inner()
        await _send(_transport(URL, inner, credential=_cred(entry)), URL)
        assert inner.requests[0].headers["authorization"] == "Bearer tok-SECRET"


@pytest.mark.asyncio
async def test_allowlist_port_is_enforced_for_credentialed_requests() -> None:
    inner = _Inner()
    transport = _transport(
        URL,
        inner,
        EgressPolicy(endpoint_allowlist=("wiki.internal:9443",)),
        credential=_cred("wiki.internal"),
    )
    with pytest.raises(EgressBlocked):
        await _send(transport, URL)
    assert inner.requests == []


_LISTED = EgressPolicy(endpoint_allowlist=("wiki.internal",))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"\xb2", b"9" * 5000, b"\xb2" * 3])
@pytest.mark.parametrize("header", [b"retry-after", b"content-length"])
async def test_hostile_numeric_headers_never_raise_a_non_typed_error(
    header: bytes, value: bytes
) -> None:
    inner = _Inner(status=429, headers=[(header, value)])
    transport = _transport(URL, inner, _LISTED)

    try:
        response = await _send(transport, URL)
    except httpx2.TransportError:
        # an oversized declared length is a typed refusal, and the response
        # was closed before it was raised
        assert inner.body.closed
        assert transport.outcome is ProviderOutcome.OVERSIZED
    else:
        assert response.status_code == 429
    assert transport.retry_after_seconds is None
    assert transport.status == 429


@pytest.mark.asyncio
async def test_a_sane_retry_after_is_still_parsed() -> None:
    inner = _Inner(status=429, headers=[(b"retry-after", b"17")])
    transport = _transport(URL, inner, _LISTED)

    await _send(transport, URL)

    assert transport.retry_after_seconds == 17


@pytest.mark.asyncio
async def test_request_always_carries_accept_encoding_identity() -> None:
    inner = _Inner()
    transport = _transport(URL, inner, _LISTED)
    request = httpx2.Request("POST", URL, content=b"{}", headers={"accept-encoding": "gzip, br"})

    await transport.handle_async_request(request)

    assert inner.requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_declared_content_length_over_the_cap_is_refused_before_any_body_read() -> None:
    # A tiny actual body (2 bytes, under the 10-byte cap): only the declared
    # length can trip, so this fails if the early reject is removed.
    inner = _Inner(headers=[(b"content-length", b"1000")])
    transport = GuardedTransport(
        URL, policy=_LISTED, resolver=_resolver, inner=inner, max_response_bytes=10
    )

    with pytest.raises(ResponseTooLarge):
        await _send(transport, URL)

    assert transport.outcome is ProviderOutcome.OVERSIZED
    assert inner.body.closed
    assert not inner.body.iterated


# --- extracted guard steps (each refusal pinned individually) ---------------


def _guard(
    policy: EgressPolicy | None = None, credential: ProviderCredential | None = None
) -> GuardedTransport:
    return _transport(URL, _Inner(), policy=policy, credential=credential)


def test_check_target_accepts_only_the_bound_origin() -> None:
    guard = _guard()

    assert guard._check_target(httpx2.URL("https://WIKI.internal.:8443/x")) == (
        "https",
        "wiki.internal",
        8443,
    )
    for other in (
        "http://wiki.internal:8443/mcp",  # scheme differs
        "https://other.internal:8443/mcp",  # host differs
        "https://wiki.internal/mcp",  # port differs
        "ftp://wiki.internal:8443/mcp",  # scheme not http(s)
    ):
        with pytest.raises(EgressBlocked):
            guard._check_target(httpx2.URL(other))
        assert guard.outcome is ProviderOutcome.BLOCKED_EGRESS


def test_check_target_refuses_everything_on_an_unusable_endpoint() -> None:
    guard = _transport("https://u:p@wiki.internal:8443/mcp", _Inner())

    with pytest.raises(EgressBlocked):
        guard._check_target(httpx2.URL(URL))


def test_check_allowlist_unauthenticated_needs_allowlist_or_credential_host() -> None:
    _guard(EgressPolicy(endpoint_allowlist=("wiki.internal:8443",)))._check_allowlist(
        "wiki.internal", 8443
    )
    _guard(EgressPolicy(credential_hosts=("wiki.internal:8443",)))._check_allowlist(
        "wiki.internal", 8443
    )
    for policy in (EgressPolicy(), EgressPolicy(endpoint_allowlist=("wiki.internal:1",))):
        with pytest.raises(EgressBlocked):
            _guard(policy)._check_allowlist("wiki.internal", 8443)


def test_check_allowlist_authenticated_is_open_without_allowlist_but_enforced_with_one() -> None:
    cred = _cred("wiki.internal:8443")
    _guard(EgressPolicy(), cred)._check_allowlist("wiki.internal", 8443)
    with pytest.raises(EgressBlocked):
        _guard(EgressPolicy(endpoint_allowlist=("other.internal",)), cred)._check_allowlist(
            "wiki.internal", 8443
        )


def test_check_classes_denied_beats_everything() -> None:
    from adapter.product_knowledge.egress import AddressClass

    guard = _guard(EgressPolicy(allow_private_networks=True))
    with pytest.raises(EgressBlocked, match="always denied"):
        guard._check_classes([AddressClass.PUBLIC, AddressClass.DENIED])


def test_check_classes_private_only_when_switched_off() -> None:
    from adapter.product_knowledge.egress import AddressClass

    _guard(EgressPolicy(allow_private_networks=True))._check_classes([AddressClass.PRIVATE])
    _guard(EgressPolicy(allow_private_networks=False))._check_classes([AddressClass.PUBLIC])
    with pytest.raises(EgressBlocked, match="private networks are disabled"):
        _guard(EgressPolicy(allow_private_networks=False))._check_classes([AddressClass.PRIVATE])


def test_outbound_headers_strip_caller_authorization_and_force_identity() -> None:
    from adapter.product_knowledge.egress import AddressClass

    request = httpx2.Request(
        "POST", URL, headers={"authorization": "Bearer attacker", "accept-encoding": "gzip"}
    )
    headers = _guard()._outbound_headers(
        request, "https", "wiki.internal", 8443, [AddressClass.PRIVATE]
    )

    assert "authorization" not in headers
    assert headers["accept-encoding"] == "identity"


def test_outbound_headers_attach_token_only_when_bound_and_not_cleartext_public() -> None:
    from adapter.product_knowledge.egress import AddressClass

    request = httpx2.Request("POST", URL)
    bound = _guard(credential=_cred("wiki.internal:8443"))
    headers = bound._outbound_headers(
        request, "https", "wiki.internal", 8443, [AddressClass.PUBLIC]
    )
    assert headers["authorization"] == "Bearer tok-SECRET"

    # cleartext http is fine to a private address, refused to a public one
    bound._outbound_headers(request, "http", "wiki.internal", 8443, [AddressClass.PRIVATE])
    with pytest.raises(EgressBlocked, match="cleartext"):
        bound._outbound_headers(request, "http", "wiki.internal", 8443, [AddressClass.PUBLIC])
    assert bound.outcome is ProviderOutcome.BLOCKED_EGRESS

    unbound = _guard(credential=_cred("other.internal"))
    with pytest.raises(EgressBlocked, match="not bound"):
        unbound._outbound_headers(request, "https", "wiki.internal", 8443, [AddressClass.PRIVATE])
    assert unbound.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE
