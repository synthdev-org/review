"""
Test seams for driving the real guard against a loopback fake provider.

The guard (rightly) refuses loopback, so tests resolve the endpoint host to a
*private* address and let ``LoopbackTransport`` do the final connect to the
fake's real socket. Every guard decision (classification, pinning, header
rewrite) is therefore the production code path; only the last hop's
destination is substituted.
"""

from __future__ import annotations

import ssl

import httpx2

from adapter.egress.transport import Resolver

__all__ = ["LoopbackTransport", "fixed_resolver", "self_signed_cert", "sequence_resolver"]


class LoopbackTransport(httpx2.AsyncBaseTransport):
    """
    Transport that connects to the fake provider's loopback port, whatever the guard pinned.

    Connects to ``127.0.0.1:<port>`` and records what the guard handed it.
    """

    def __init__(self, port: int, *, verify: ssl.SSLContext | bool = True) -> None:
        """Bind the transport to the fake's ``port`` and TLS ``verify`` trust anchor."""
        self._port = port
        # ``verify`` is only ever a *trust anchor* for a test's self-signed
        # listener (never ``False``): TLS tests prove verification is on.
        self._inner = httpx2.AsyncHTTPTransport(trust_env=False, verify=verify)
        self.pinned_hosts: list[str] = []
        self.host_headers: list[str] = []
        self.sni_hostnames: list[object] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.pinned_hosts.append(request.url.host)
        self.host_headers.append(request.headers.get("host", ""))
        self.sni_hostnames.append(request.extensions.get("sni_hostname"))
        forwarded = httpx2.Request(
            request.method,
            request.url.copy_with(host="127.0.0.1", port=self._port),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await self._inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self._inner.aclose()


def self_signed_cert(dns_name: str) -> tuple[bytes, bytes]:
    """
    Return ``(cert_pem, key_pem)`` for a throwaway self-signed certificate.

    Its only SAN is ``dns_name`` (no IP SAN, so IP-based verification cannot
    pass). It is its own CA so a test can use it as the sole trust anchor.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def fixed_resolver(*addresses: str) -> Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def sequence_resolver(*answers: list[str]) -> Resolver:
    """Answers successive lookups with successive lists (DNS rebinding)."""
    remaining = list(answers)

    async def resolve(host: str, port: int) -> list[str]:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return resolve
