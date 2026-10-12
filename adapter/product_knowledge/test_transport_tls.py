"""TLS and environment-isolation tests for the egress transport (plan Phase 4,
US-PK12; audit B2 and cross-cutting M1).

The https path rewrites the request URL to the pinned *private IP* and carries
the hostname in ``sni_hostname``; certificate verification must therefore still
be against the **hostname**. The listener here is a real TLS socket with a
self-signed certificate valid only for ``wiki.test`` (no IP SAN);
``LoopbackTransport`` is the last hop and trusts that certificate as its sole
anchor. Production defaults are asserted structurally (they cannot reach a
private address from a test).
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
import pytest_asyncio

from adapter.product_knowledge.testing.loopback import (
    LoopbackTransport,
    fixed_resolver,
    self_signed_cert,
)
from adapter.product_knowledge.transport import EgressPolicy, GuardedTransport, build_http_client

HOST = "wiki.test"
PRIVATE = "10.1.2.3"
ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


class _TlsListener:
    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        self._ctx = ssl_context
        self.requests = 0
        self.handshakes = 0
        self.port = 0
        self._server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.handshakes += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self._ctx, ssl_handshake_timeout=5
        )
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture(scope="module")
def cert_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    cert_pem, key_pem = self_signed_cert(HOST)
    directory = tmp_path_factory.mktemp("tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    cert.write_bytes(cert_pem)
    key.write_bytes(key_pem)
    return cert, key


@pytest_asyncio.fixture
async def listener(cert_files: tuple[Path, Path]) -> AsyncIterator[_TlsListener]:
    cert, key = cert_files
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    tls = _TlsListener(server_ctx)
    await tls.start()
    yield tls
    await tls.stop()


def _guarded_client(port: int, host: str, inner: LoopbackTransport) -> httpx2.AsyncClient:
    transport = GuardedTransport(
        f"https://{host}:{port}/mcp",
        policy=EgressPolicy(endpoint_allowlist=(host,)),  # unauthenticated => listed
        resolver=fixed_resolver(PRIVATE),
        inner=inner,
    )
    return build_http_client(transport)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_https_hostname_is_verified_against_the_pinned_private_ip_path(
    listener: _TlsListener, cert_files: tuple[Path, Path]
) -> None:
    trust = ssl.create_default_context(cafile=str(cert_files[0]))
    loopback = LoopbackTransport(listener.port, verify=trust)
    async with _guarded_client(listener.port, HOST, loopback) as client:
        response = await client.get(f"https://{HOST}:{listener.port}/mcp")

    assert response.status_code == 200
    assert loopback.pinned_hosts == [PRIVATE]  # the URL really was rewritten to the IP
    assert loopback.sni_hostnames == [HOST]  # ... and the hostname is what TLS verifies
    assert listener.requests == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_https_certificate_for_another_hostname_is_rejected(
    listener: _TlsListener, cert_files: tuple[Path, Path]
) -> None:
    trust = ssl.create_default_context(cafile=str(cert_files[0]))
    loopback = LoopbackTransport(listener.port, verify=trust)
    async with _guarded_client(listener.port, "other.test", loopback) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.get(f"https://other.test:{listener.port}/mcp")

    assert listener.requests == 0  # handshake failed: no HTTP request was ever read


@pytest.mark.integration
@pytest.mark.asyncio
async def test_https_untrusted_self_signed_certificate_is_rejected_by_default(
    listener: _TlsListener,
) -> None:
    # No trust anchor supplied: the default (verifying) context refuses it.
    loopback = LoopbackTransport(listener.port)
    async with _guarded_client(listener.port, HOST, loopback) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.get(f"https://{HOST}:{listener.port}/mcp")
    assert listener.requests == 0


# --- environment isolation (audit B2) ------------------------------------------


def test_build_http_client_ignores_the_environment_and_has_no_proxy_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ENV_VARS:
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    client = build_http_client(GuardedTransport("https://wiki.test/mcp", policy=EgressPolicy()))

    assert client.trust_env is False
    assert client.follow_redirects is False
    assert client._mounts == {}  # no per-scheme proxy transport displaced the guard


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_production_inner_transport_does_not_honour_proxy_or_ca_environment(
    variable: str, monkeypatch: pytest.MonkeyPatch, cert_files: tuple[Path, Path]
) -> None:
    for var in ENV_VARS:
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    cert = cert_files[0]
    monkeypatch.setenv(variable, str(cert if variable.endswith("FILE") else cert.parent))
    guarded = GuardedTransport("https://wiki.test/mcp", policy=EgressPolicy())
    inner = guarded._inner
    assert isinstance(inner, httpx2.AsyncHTTPTransport)
    pool = inner._pool

    assert type(pool).__name__ == "AsyncConnectionPool"  # not AsyncHTTPProxy / SOCKS
    ctx = pool._ssl_context
    assert ctx is not None
    # trust_env=True would build a plain ssl.SSLContext from the env var; the
    # default is the system trust store, and verification is mandatory.
    assert type(ctx) is not ssl.SSLContext
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
