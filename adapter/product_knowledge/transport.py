"""adapter.product_knowledge.transport — the connection-time egress guard
(ADR-0013 decision 3, plan Phase 4 / Amendment A3, US-PK12) as an ``httpx2``
transport, plus the 256 KiB response cap.

The ``mcp`` v2 SDK's HTTP stack is ``httpx2`` (not ``httpx``), and it follows
same-origin redirects on its own regardless of the client's
``follow_redirects`` setting — so *this* transport must itself refuse every
``3xx``. Everything the SDK sends goes through ``GuardedTransport``:

* only ``http``/``https``, only the one bound endpoint origin;
* the operator allow-list and the private-network switch, judged on the
  addresses ``getaddrinfo`` actually returned (``egress.classify_address``);
* the connection is made to the *validated* address (the request URL is
  rewritten to the IP, ``Host`` and TLS SNI/verification keep the hostname),
  so a DNS answer that changes after validation cannot redirect it;
* the bearer token is attached here, per request, after the host checks —
  never as a client default header — and never over cleartext ``http`` to a
  public address;
* ``Accept-Encoding: identity`` is forced and any ``Content-Encoding`` is
  refused (the byte cap sits on the wire; a compressed bomb would slip past
  it), and the body is counted as it streams (``MAX_RESPONSE_BYTES``).

A refusal records the closed ``ProviderOutcome`` on the transport instance
(``outcome``) and raises ``EgressBlocked``/``ResponseTooLarge``; the client
reads ``outcome`` rather than parsing whatever the SDK wraps the exception
in. Nothing here logs; no token, header or body text is ever in a message.

``trust_env`` is not a transport knob: ``build_http_client`` creates the
``httpx2.AsyncClient`` with ``trust_env=False`` so ``HTTP(S)_PROXY`` can
never take over resolve-and-connect.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2
from pydantic import SecretStr

from adapter.product_knowledge.egress import (
    AddressClass,
    canonicalize_host,
    classify_address,
    host_matches_allowlist,
)
from review.product_knowledge import MAX_RESPONSE_BYTES, ProviderOutcome

__all__ = [
    "EgressBlocked",
    "EgressPolicy",
    "GuardedTransport",
    "ProviderCredential",
    "Resolver",
    "ResponseTooLarge",
    "build_http_client",
    "system_resolver",
]

Resolver = Callable[[str, int], Awaitable[list[str]]]


class EgressBlocked(httpx2.TransportError):
    """The guard refused the request before any byte was sent (or refused to
    follow a redirect). The message is a fixed string, never request data."""


class ResponseTooLarge(httpx2.TransportError):
    """The response body exceeded ``MAX_RESPONSE_BYTES``."""


@dataclass(frozen=True)
class EgressPolicy:
    allow_private_networks: bool = True
    endpoint_allowlist: tuple[str, ...] = ()
    # Hosts some operator credential is bound to. An *unauthenticated*
    # provider may only reach the allow-list or these (A2 rule); the factory
    # fills this in, the transport only reads it.
    credential_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderCredential:
    """One operator-provisioned credential: the token is only ever sent to a
    host in ``allowed_hosts`` (ADR-0013 decision 2)."""

    ref: str
    token: SecretStr
    allowed_hosts: tuple[str, ...]

    def __repr__(self) -> str:  # never let a token reach a log via repr
        return f"ProviderCredential(ref={self.ref!r})"


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: dict[str, None] = {}
    for info in infos:
        seen.setdefault(str(info[4][0]), None)
    return list(seen)


_MAX_DIGITS = 10


def _is_digits(raw: str) -> bool:
    """ASCII digits only: ``str.isdigit`` is true for ``'\xb2'``, which
    ``int`` rejects (ValueError escaping the transport)."""
    return raw.isascii() and raw.isdigit()


def _is_small_int(raw: str) -> bool:
    # Length-bounded as well: ``int`` of a multi-thousand-digit string is
    # slow and, past 4300 digits, raises.
    return _is_digits(raw) and len(raw) < _MAX_DIGITS


def _effective_port(scheme: str, port: int | None) -> int:
    return port if port is not None else (443 if scheme == "https" else 80)


class _CappedStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, cap: int, on_exceed: Callable[[], None]):
        self._inner = inner
        self._cap = cap
        self._on_exceed = on_exceed

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._cap:
                self._on_exceed()
                await self._inner.aclose()
                raise ResponseTooLarge("response exceeded the size cap")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class GuardedTransport(httpx2.AsyncBaseTransport):
    """One instance per provider operation, bound to one endpoint origin."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        policy: EgressPolicy,
        credential: ProviderCredential | None = None,
        resolver: Resolver = system_resolver,
        inner: httpx2.AsyncBaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        # Total: a malformed URL (bad port, unbalanced IPv6 bracket) makes the
        # transport unusable (every request is refused), never raises here.
        port: int | None
        try:
            parts = urlsplit(endpoint_url)
            port = parts.port
            has_userinfo = parts.username is not None or parts.password is not None
            self._scheme = parts.scheme.lower()
            self._host = canonicalize_host(parts.hostname or "") if parts.hostname else None
        except ValueError:
            port = -1
            has_userinfo = False
            self._scheme = ""
            self._host = None
        self._port = _effective_port(self._scheme, port)
        self._usable = (
            self._scheme in ("http", "https")
            and self._host is not None
            and port != -1
            and not has_userinfo
        )
        self._policy = policy
        self._credential = credential
        self._resolver = resolver
        self._inner = inner or httpx2.AsyncHTTPTransport(trust_env=False, http2=False)
        self._cap = max_response_bytes
        # Filled in as the operation runs; read by the client to classify.
        self.outcome: ProviderOutcome | None = None
        self.status: int | None = None
        self.retry_after_seconds: int | None = None

    def _refuse(self, outcome: ProviderOutcome, message: str) -> EgressBlocked:
        if self.outcome is None:
            self.outcome = outcome
        return EgressBlocked(message)

    def _check_target(self, url: httpx2.URL) -> tuple[str, str, int]:
        """``(scheme, canonical host, port)`` of the request, refusing anything
        that is not the one bound endpoint origin."""
        scheme = url.scheme.lower()
        if scheme not in ("http", "https") or not self._usable:
            raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "scheme or endpoint not permitted")
        host = canonicalize_host(url.host)
        port = _effective_port(scheme, url.port)
        if host is None or host != self._host or port != self._port or scheme != self._scheme:
            raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "request target is not the endpoint")
        return scheme, host, port

    def _check_allowlist(self, host: str, port: int) -> None:
        policy = self._policy
        listed = host_matches_allowlist(host, policy.endpoint_allowlist, port)
        if self._credential is None and not listed:
            # Unauthenticated: never unconstrained, even with no allow-list.
            listed = host_matches_allowlist(host, policy.credential_hosts, port)
            if not listed:
                raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "host not in the allow-list")
        elif policy.endpoint_allowlist and not listed:
            raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "host not in the allow-list")

    def _check_classes(self, classes: list[AddressClass]) -> None:
        if AddressClass.DENIED in classes:
            raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "address class is always denied")
        if AddressClass.PRIVATE in classes and not self._policy.allow_private_networks:
            raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "private networks are disabled")

    def _outbound_headers(
        self,
        request: httpx2.Request,
        scheme: str,
        host: str,
        port: int,
        classes: list[AddressClass],
    ) -> httpx2.Headers:
        """Request headers with any caller-supplied ``Authorization`` dropped
        and the bearer token attached only after the binding checks."""
        headers = httpx2.Headers(request.headers)
        headers.pop("authorization", None)
        headers["accept-encoding"] = "identity"
        credential = self._credential
        if credential is not None:
            if not host_matches_allowlist(host, credential.allowed_hosts, port):
                raise self._refuse(
                    ProviderOutcome.CREDENTIAL_UNAVAILABLE, "credential is not bound to this host"
                )
            if scheme == "http" and AddressClass.PUBLIC in classes:
                raise self._refuse(
                    ProviderOutcome.BLOCKED_EGRESS, "credential refused over cleartext http"
                )
            headers["authorization"] = f"Bearer {credential.token.get_secret_value()}"
        return headers

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        scheme, host, port = self._check_target(request.url)
        self._check_allowlist(host, port)
        pinned, classes = await self._resolve(host, port)
        self._check_classes(classes)
        headers = self._outbound_headers(request, scheme, host, port, classes)
        extensions = dict(request.extensions)
        if scheme == "https":
            extensions["sni_hostname"] = host
        forwarded = httpx2.Request(
            request.method,
            request.url.copy_with(host=pinned),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        response = await self._inner.handle_async_request(forwarded)
        return await self._check_response(response)

    async def _resolve(self, host: str, port: int) -> tuple[str, list[AddressClass]]:
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = await self._resolver(host, port)
            except OSError as exc:
                raise httpx2.ConnectError("name resolution failed") from exc
        if not addresses:
            raise httpx2.ConnectError("name resolution returned no address")
        return addresses[0], [classify_address(a) for a in addresses]

    async def _reject_redirect(self, response: httpx2.Response) -> None:
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise self._refuse(ProviderOutcome.BLOCKED_EGRESS, "redirects are never followed")

    def _record_error_status(self, response: httpx2.Response) -> None:
        if response.status_code >= 400:
            # The *last* error status: a legacy provider legitimately answers
            # the modern probe with 400 before the real failure.
            self.status = response.status_code
            raw = response.headers.get("retry-after", "")
            if _is_small_int(raw):
                self.retry_after_seconds = int(raw)

    async def _reject_encoded(self, response: httpx2.Response) -> None:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            await response.aclose()
            if self.outcome is None:
                self.outcome = ProviderOutcome.MALFORMED
            raise ResponseTooLarge("encoded response bodies are refused")

    async def _reject_oversized_length(self, response: httpx2.Response) -> None:
        length = response.headers.get("content-length", "")
        if _is_digits(length) and (len(length) >= _MAX_DIGITS or int(length) > self._cap):
            await response.aclose()
            if self.outcome is None:
                self.outcome = ProviderOutcome.OVERSIZED
            raise ResponseTooLarge("response exceeded the size cap")

    async def _check_response(self, response: httpx2.Response) -> httpx2.Response:
        await self._reject_redirect(response)
        self._record_error_status(response)
        await self._reject_encoded(response)
        await self._reject_oversized_length(response)
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            await response.aclose()
            raise self._refuse(ProviderOutcome.MALFORMED, "response stream is not async")

        def _exceeded() -> None:
            if self.outcome is None:
                self.outcome = ProviderOutcome.OVERSIZED

        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_CappedStream(stream, self._cap, _exceeded),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_http_client(transport: GuardedTransport) -> httpx2.AsyncClient:
    """The only way the adapter builds an HTTP client: guarded transport,
    redirects off, environment (proxy vars, netrc, SSL_CERT_*) ignored, TLS
    verification on (the inner transport's default)."""
    return httpx2.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
