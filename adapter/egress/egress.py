"""
adapter.egress.egress: the pure half of the egress guard.

Covers ADR-0013 decision 3: host
canonicalisation, resolved-address classification and the operator
allow-list match.

No I/O and no infra imports (ADR-0005's pure-logic exemption — this module is
exercised and mutation-tested through ``test_egress_pure.py``). The
connection-time enforcement that *uses* these functions lives in
``adapter/egress/transport.py``.

**One canonicaliser.** ``canonicalize_host`` is the only host normaliser:
the write-time ``allowed_hosts`` binding check, the job-time check
and the transport all call it, so they cannot disagree about what
``WIKI.Example.com.`` or ``host:8443`` means.

**Addresses are classified after resolution, never by URL string matching.**
``2130706433``, ``0x7f.1`` and friends are legacy ``inet_aton`` literals that
``getaddrinfo`` resolves to ``127.0.0.1``; ``canonicalize_host`` rewrites such
literals to dotted-quad so the allow-list cannot be tricked by them, and
``classify_address`` judges whatever the resolver actually returned.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "AddressClass",
    "canonicalize_host",
    "classify_address",
    "endpoint_origin",
    "host_matches_allowlist",
    "parse_allow_entry",
    "split_host_port",
]


class AddressClass(StrEnum):
    PUBLIC = "public"
    # RFC 1918, ULA, CGNAT and other non-global-but-routable ranges: allowed
    # unless the operator switches private networks off.
    PRIVATE = "private"
    # Never connectable, whatever the operator configures.
    DENIED = "denied"


# Cloud metadata endpoints that sit *inside* otherwise-allowed private ranges.
# 169.254.169.254 is link-local and caught by ``is_link_local``.
_METADATA_ADDRESSES: Final = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "fd00:ec2::254",
        "100.100.100.200",
        "168.63.129.16",
        "192.0.0.192",
    )  # NOSONAR S1313: cloud-metadata SSRF blocklist
)

# IPv6 transition/embedding forms that can smuggle an IPv4 target past a
# naive IPv4 check (NAT64 well-known + local-use, 6to4, Teredo, and the
# deprecated IPv4-compatible ``::a.b.c.d`` which also covers ``::`` / ``::1``).
# Defence in depth: today all of these fall outside the 2000::/3 + fc00::/7
# allow-list below, but the explicit denies keep them denied if it ever widens.
_DENIED_V6_NETWORKS: Final = tuple(
    ipaddress.ip_network(n)
    for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "::/96")
)

# IPv4 ranges denied by explicit network, not by stdlib ``is_reserved`` (whose
# definition has shifted between Python releases): "this network" 0.0.0.0/8
# (``0.x.x.x`` connects to localhost on Linux) and 240.0.0.0/4 (class E plus
# the limited broadcast address).
_DENIED_V4_NETWORKS: Final = tuple(
    ipaddress.ip_network(n) for n in ("0.0.0.0/8", "240.0.0.0/4")
)  # NOSONAR S1313: reserved-range SSRF blocklist

# IPv6 is allow-listed rather than deny-listed: only global unicast 2000::/3
# and ULA fc00::/7 are connectable at all, so every unassigned/reserved block
# (``::/8``, ``100::/8``, site-local, ...) is denied without enumerating it.
_ROUTABLE_V6_NETWORKS: Final = tuple(ipaddress.ip_network(n) for n in ("2000::/3", "fc00::/7"))

# Legacy inet_aton numeric forms: 1-4 dot-separated decimal/hex/octal parts.
_NUMERIC_V4_RE: Final = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}$", re.I)


_HOST_FORBIDDEN_CHARS: Final = "@/\\?# \t\r\n%"


def _split_bracketed(value: str) -> tuple[str, str | None] | None:
    """``[v6]`` / ``[v6]:port``; ``value`` starts with ``[``."""
    host, sep, rest = value[1:].partition("]")
    if not sep or not host:
        return None
    if not rest:
        return host, None
    if rest.startswith(":") and rest[1:].isdigit():
        return host, rest[1:]
    return None


def _split_unbracketed(value: str) -> tuple[str, str | None] | None:
    """``host`` / ``host:port`` / a bare IPv6 literal (never has a port)."""
    if value.count(":") >= 2:
        return value, None
    host, sep, port = value.partition(":")
    if not host:
        return None
    if sep and not port.isdigit():
        return None
    return host, (port if sep else None)


def split_host_port(value: str) -> tuple[str, str | None] | None:
    """
    Split ``host``, ``host:port``, ``[v6]`` or ``[v6]:port``.

    Returns ``None`` for anything that is not one of those (userinfo, a
    scheme, a path, whitespace, an IPv6 zone id, a non-numeric port).
    A bare IPv6 literal (two or more colons, no brackets) has no port.
    """
    if not value or any(c in value for c in _HOST_FORBIDDEN_CHARS):
        return None
    if value.startswith("["):
        return _split_bracketed(value)
    return _split_unbracketed(value)


def _canonical_ipv6(host: str) -> str | None:
    try:
        return str(ipaddress.IPv6Address(host))
    except ValueError:
        return None


def _canonical_numeric_ipv4(host: str) -> str | None:
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


def _canonical_dns_name(host: str) -> str | None:
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(host) > 253 or any(not (0 < len(label) <= 63) for label in host.split(".")):
        return None
    return host


def canonicalize_host(value: str) -> str | None:
    """
    Return the canonical form of a host (port, if any, is dropped).

    Lowercase, trailing dot stripped, IDNA/punycode, IPv6 compressed, legacy
    numeric IPv4 rewritten to dotted-quad. ``None`` means "not a valid host" and
    every caller must treat it as a refusal.
    """
    parts = split_host_port(value.strip())
    if parts is None:
        return None
    host = parts[0].lower().rstrip(".")
    if not host:
        return None
    if ":" in host:
        return _canonical_ipv6(host)
    if _NUMERIC_V4_RE.match(host):
        return _canonical_numeric_ipv4(host)
    return _canonical_dns_name(host)


_IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def _in_any(ip: _IP, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    return any(ip in net for net in networks)


def _parse_address(address: str | _IP) -> _IP | None:
    if not isinstance(address, str):
        return address
    try:
        return ipaddress.ip_address(address)
    except ValueError:
        return None


def _is_denied_v6(ip: ipaddress.IPv6Address) -> bool:
    """Outside the allow-listed global/ULA blocks, or an explicit transition deny."""
    return not _in_any(ip, _ROUTABLE_V6_NETWORKS) or _in_any(ip, _DENIED_V6_NETWORKS)


def _unmap_v6(ip: ipaddress.IPv6Address) -> _IP | None:
    """
    Return the address to keep classifying, or ``None`` when ``ip`` is denied outright.

    That is the embedded IPv4 of a mapped address, or ``ip`` itself.
    """
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return None if _is_denied_v6(ip) else ip


def _is_denied_special(ip: _IP) -> bool:
    """Loopback, link-local, unspecified, multicast and cloud-metadata addresses."""
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip in _METADATA_ADDRESSES
    )


def classify_address(address: str | _IP) -> AddressClass:
    """Classifies one *resolved* address. Unparseable input is ``DENIED``."""
    ip = _parse_address(address)
    if ip is None:
        return AddressClass.DENIED
    if isinstance(ip, ipaddress.IPv6Address):
        unmapped = _unmap_v6(ip)
        if unmapped is None:
            return AddressClass.DENIED
        ip = unmapped
    if isinstance(ip, ipaddress.IPv4Address) and _in_any(ip, _DENIED_V4_NETWORKS):
        return AddressClass.DENIED
    if _is_denied_special(ip):
        return AddressClass.DENIED
    return AddressClass.PUBLIC if ip.is_global else AddressClass.PRIVATE


def parse_allow_entry(entry: str) -> tuple[str, bool, int | None] | None:
    """
    Parse an allow-list / ``allowed_hosts`` entry.

    Accepts ``host`` or ``*.suffix``, optionally with ``:port`` (``[v6]:port``
    for IPv6). Returns ``(canonical host or suffix, is_wildcard, port)``; ``None``
    is an unusable entry (scheme, path, embedded ``*``, non-numeric or
    out-of-range port). A bare ``host`` entry (port ``None``) matches that host
    on any port — weaker than ``host:port`` and kept only for backward
    compatibility.
    """
    item = entry.strip()
    wildcard = item.startswith("*.")
    body = item[2:] if wildcard else item
    if "*" in body or "://" in body:
        return None
    parts = split_host_port(body)
    host = canonicalize_host(body)
    if parts is None or host is None:
        return None
    port: int | None = None
    if parts[1] is not None:
        if len(parts[1]) > 5 or not parts[1].isascii():
            return None
        port = int(parts[1])
        if not 0 < port < 65536:
            return None
    return host, wildcard, port


def host_matches_allowlist(host: str, allowlist: Iterable[str], port: int | None = None) -> bool:
    """
    Return True if ``host`` equals an entry or falls under a ``*.suffix`` entry.

    ``*.example.com`` matches ``a.example.com`` but never ``example.com``,
    ``evilexample.com`` or ``example.com.evil.tld``. An entry carrying a
    ``:port`` additionally requires ``port`` to equal it exactly (``port``
    ``None`` therefore never matches a port-bearing entry); an entry without
    one matches any port. An empty allow-list matches nothing — callers
    decide what "no allow-list configured" means.
    """
    candidate = canonicalize_host(host)
    if candidate is None:
        return False
    for entry in allowlist:
        parsed = parse_allow_entry(entry)
        if parsed is None:
            continue
        name, wildcard, entry_port = parsed
        if entry_port is not None and entry_port != port:
            continue
        if candidate.endswith("." + name) if wildcard else candidate == name:
            return True
    return False


def endpoint_origin(url: str) -> tuple[str, int] | None:
    """
    Return ``(canonical host, effective port)`` of an endpoint URL, or ``None`` if unusable.

    The scheme's default port applies when the URL omits one.
    """
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = canonicalize_host(hostname) if hostname else None
    if host is None or scheme not in ("http", "https"):
        return None
    return host, port if port is not None else (443 if scheme == "https" else 80)
