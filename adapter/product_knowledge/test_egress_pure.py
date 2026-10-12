"""Unit tests for adapter.product_knowledge.egress (US-PK12, plan Phase 4 /
Amendment A3) — pure functions only, no sockets."""

from __future__ import annotations

import ipaddress

import pytest

from adapter.product_knowledge.egress import (
    AddressClass,
    canonicalize_host,
    classify_address,
    host_matches_allowlist,
    split_host_port,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.5.5.5",
        "::1",
        "0.0.0.0",
        "::",
        "169.254.169.254",
        "169.254.1.1",
        "fe80::",
        "fe80::1",
        "fd00:ec2::254",  # AWS IMDS IPv6
        "100.100.100.200",  # Alibaba
        "168.63.129.16",  # Azure wireserver
        "192.0.0.192",  # OCI
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:100.100.100.200",
        "64:ff9b::7f00:1",  # NAT64
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::808:808",  # NAT64 well-known prefix embedding a *public* v4
        "64:ff9b:1::7f00:1",  # NAT64 local-use /48
        "64:ff9b:1:ffff::1",
        "::7f00:1",
        "::a00:1",  # IPv4-compatible ::/96 embedding 10.0.0.1
        "::808:808",  # ... and a public v4
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "2001:0:c000:201::1",
        "2002:808:808::",  # 6to4 of a public v4
        "0.0.0.1",  # 0.0.0.0/8 is "this network", never a destination
        "0.255.255.255",
        "::ffff:0.0.0.1",
        "240.0.0.1",  # class E
        "100::1",  # discard-only prefix
        "fec0::1",  # deprecated site-local
        "2002:7f00:1::",  # 6to4
        "::7f00:1",  # IPv4-compatible
        "224.0.0.1",  # multicast
        "ff02::1",
        "255.255.255.255",
        "not-an-ip",
    ],
)
def test_classify_address_always_denies(address: str) -> None:
    assert classify_address(address) is AddressClass.DENIED


def test_deny_set_does_not_depend_on_stdlib_is_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    # The explicit network lists must carry the denial on their own: even if
    # the stdlib's notion of "reserved" were empty, the transition ranges
    # (NAT64, IPv4-compatible, Teredo, 6to4), 0.0.0.0/8 and class E stay denied.
    import ipaddress

    monkeypatch.setattr(ipaddress.IPv4Address, "is_reserved", property(lambda self: False))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_reserved", property(lambda self: False))
    for address in (
        "64:ff9b::7f00:1",
        "64:ff9b:1::7f00:1",
        "::7f00:1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "2002:7f00:1::",
        "0.0.0.1",
        "240.0.0.1",
        "100::1",
    ):
        assert classify_address(address) is AddressClass.DENIED, address


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "172.16.4.4", "192.168.1.10", "fd00::1", "100.64.0.1", "::ffff:10.0.0.1"],
)
def test_classify_address_private_ranges_are_switchable_not_denied(address: str) -> None:
    assert classify_address(address) is AddressClass.PRIVATE


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_classify_address_public(address: str) -> None:
    assert classify_address(address) is AddressClass.PUBLIC


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WIKI.Example.com.", "wiki.example.com"),
        ("wiki.example.com:8443", "wiki.example.com"),
        ("wiki", "wiki"),
        ("[::1]:8080", "::1"),
        ("::1", "::1"),
        ("2130706433", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("017700000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("  host.example  ", "host.example"),
    ],
)
def test_canonicalize_host_normalises(raw: str, expected: str) -> None:
    assert canonicalize_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "host@evil.com",
        "user:pw@host",
        "https://host",
        "host/path",
        "host?x=1",
        "fe80::1%eth0",  # IPv6 zone id
        "host:notaport",
        "[::1",
        "[]:80",
        ":80",
        ".",
        "a..b",
        "-" * 64 + ".example",
    ],
)
def test_canonicalize_host_rejects_invalid(raw: str) -> None:
    assert canonicalize_host(raw) is None


def test_canonicalize_host_does_not_equate_unicode_lookalike_with_ascii() -> None:
    # Cyrillic a (U+0430) in place of Latin "a": must not collapse onto the ascii host.
    assert canonicalize_host("w\u0430ki.example.com") != canonicalize_host("waki.example.com")


def test_split_host_port() -> None:
    assert split_host_port("h:80") == ("h", "80")
    assert split_host_port("h") == ("h", None)
    assert split_host_port("[::1]") == ("::1", None)
    assert split_host_port("[::1]:9") == ("::1", "9")
    assert split_host_port("[::1]x") is None
    assert split_host_port("::1") == ("::1", None)


@pytest.mark.parametrize(
    ("host", "allowlist", "expected"),
    [
        ("wiki.example.com", ["wiki.example.com"], True),
        ("WIKI.example.com.", ["wiki.example.com"], True),
        ("wiki.example.com:8443", ["wiki.example.com"], True),
        ("a.example.com", ["*.example.com"], True),
        ("a.b.example.com", ["*.example.com"], True),
        ("example.com", ["*.example.com"], False),  # bare suffix
        ("evilexample.com", ["*.example.com"], False),
        ("example.com.evil.tld", ["*.example.com"], False),
        ("evilexample.com", ["example.com"], False),
        ("host.evil.com", ["host"], False),
        ("host@evil", ["host"], False),
        ("anything", [], False),
        ("127.0.0.1", ["127.0.0.1"], True),
        ("2130706433", ["127.0.0.1"], True),  # normalised, not string-matched
    ],
)
def test_host_matches_allowlist(host: str, allowlist: list[str], expected: bool) -> None:
    assert host_matches_allowlist(host, allowlist) is expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("Wiki.Internal.", ("wiki.internal", False, None)),
        ("wiki.internal:8443", ("wiki.internal", False, 8443)),
        ("*.corp.example:443", ("corp.example", True, 443)),
        ("[::1]:9000", ("::1", False, 9000)),
        ("::1", ("::1", False, None)),
        ("h:0", None),
        ("h:65536", None),
        ("h:123456", None),
        ("h:\u0662\u0663", None),  # non-ASCII digits
        ("https://h", None),
        ("h/p", None),
        ("a*b", None),
        ("*", None),
    ],
)
def test_parse_allow_entry(entry: str, expected: tuple[str, bool, int | None] | None) -> None:
    from adapter.product_knowledge.egress import parse_allow_entry

    assert parse_allow_entry(entry) == expected


def test_host_matches_allowlist_port_semantics() -> None:
    from adapter.product_knowledge.egress import endpoint_origin, host_matches_allowlist

    assert host_matches_allowlist("h.internal", ["h.internal:8443"], 8443)
    assert not host_matches_allowlist("h.internal", ["h.internal:8443"], 443)
    assert not host_matches_allowlist("h.internal", ["h.internal:8443"], None)
    assert host_matches_allowlist("h.internal", ["h.internal"], 1234)  # host-only entry
    assert host_matches_allowlist("a.corp.example", ["*.corp.example:443"], 443)
    assert not host_matches_allowlist("a.corp.example", ["*.corp.example:443"], 80)
    assert endpoint_origin("https://H.internal/mcp") == ("h.internal", 443)
    assert endpoint_origin("http://h.internal/mcp") == ("h.internal", 80)
    assert endpoint_origin("http://h.internal:8080/mcp") == ("h.internal", 8080)
    assert endpoint_origin("ftp://h.internal/") is None
    assert endpoint_origin("http://h:abc/") is None


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::808:808", "64:ff9b:1::1", "::1.2.3.4", "2002:808:808::1", "2001:0:4136::1"],
)
def test_explicit_v6_denies_hold_without_the_routable_allow_list(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adapter.product_knowledge import egress

    monkeypatch.setattr(egress, "_ROUTABLE_V6_NETWORKS", (ipaddress.ip_network("::/0"),))

    assert classify_address(address) is AddressClass.DENIED


# --- extracted predicates (each deny network pinned individually) -------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "127.255.255.254", "::1"],
)
def test_is_denied_special_loopback(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_special

    assert _is_denied_special(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["169.254.169.254", "169.254.0.1", "fe80::1"])
def test_is_denied_special_link_local(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_special

    assert _is_denied_special(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["0.0.0.0", "::"])
def test_is_denied_special_unspecified(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_special

    assert _is_denied_special(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["224.0.0.1", "239.255.255.255", "ff02::1", "ff0e::1"])
def test_is_denied_special_multicast(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_special

    assert _is_denied_special(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address", ["fd00:ec2::254", "100.100.100.200", "168.63.129.16", "192.0.0.192"]
)
def test_is_denied_special_cloud_metadata(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_special

    assert _is_denied_special(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "10.0.0.1", "100.100.100.201", "168.63.129.17", "192.0.0.193", "2606:4700::1111"],
)
def test_is_denied_special_is_false_for_ordinary_addresses(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_special

    assert not _is_denied_special(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::808:808",  # NAT64
        "64:ff9b:1::1",  # NAT64 local-use
        "2002:808:808::1",  # 6to4
        "2001:0:4136::1",  # Teredo
        "::1.2.3.4",  # IPv4-compatible
        "::1",
        "::",
        "100::1",  # outside 2000::/3 and fc00::/7
        "fe80::1",
        "fec0::1",
        "ff02::1",
    ],
)
def test_is_denied_v6_denies(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_v6

    assert _is_denied_v6(ipaddress.IPv6Address(address))


@pytest.mark.parametrize(
    "address", ["2606:4700:4700::1111", "2000::1", "3fff::1", "fc00::1", "fd00::1", "fdff::1"]
)
def test_is_denied_v6_allows_global_unicast_and_ula(address: str) -> None:
    from adapter.product_knowledge.egress import _is_denied_v6

    assert not _is_denied_v6(ipaddress.IPv6Address(address))


def test_unmap_v6() -> None:
    from adapter.product_knowledge.egress import _unmap_v6

    assert _unmap_v6(ipaddress.IPv6Address("::ffff:10.0.0.1")) == ipaddress.IPv4Address("10.0.0.1")
    assert _unmap_v6(ipaddress.IPv6Address("::1")) is None
    global_v6 = ipaddress.IPv6Address("2606:4700:4700::1111")
    assert _unmap_v6(global_v6) == global_v6


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "0.0.0.1", "0.255.255.255", "240.0.0.0", "247.1.2.3", "255.255.255.255"],
)
def test_denied_v4_networks_each_deny(address: str) -> None:
    assert classify_address(ipaddress.IPv4Address(address)) is AddressClass.DENIED


@pytest.mark.parametrize("address", ["1.0.0.0", "223.255.255.255"])
def test_denied_v4_networks_do_not_overreach(address: str) -> None:
    assert classify_address(ipaddress.IPv4Address(address)) is AddressClass.PUBLIC


def test_classify_address_accepts_address_objects() -> None:
    assert classify_address(ipaddress.IPv6Address("::1")) is AddressClass.DENIED
    assert classify_address(ipaddress.IPv4Address("8.8.8.8")) is AddressClass.PUBLIC


def test_parse_address() -> None:
    from adapter.product_knowledge.egress import _parse_address

    assert _parse_address("8.8.8.8") == ipaddress.IPv4Address("8.8.8.8")
    assert _parse_address("nope") is None
    obj = ipaddress.IPv6Address("::1")
    assert _parse_address(obj) is obj


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("[::1]", ("::1", None)),
        ("[::1]:80", ("::1", "80")),
        ("[::1]:", None),
        ("[::1]:x", None),
        ("[::1]x", None),
        ("[]", None),
        ("[::1", None),
    ],
)
def test_split_bracketed(value: str, expected: tuple[str, str | None] | None) -> None:
    from adapter.product_knowledge.egress import _split_bracketed

    assert _split_bracketed(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("h", ("h", None)),
        ("h:80", ("h", "80")),
        ("h:", None),
        ("h:x", None),
        (":80", None),
        ("::1", ("::1", None)),
        ("a:b:c", ("a:b:c", None)),
    ],
)
def test_split_unbracketed(value: str, expected: tuple[str, str | None] | None) -> None:
    from adapter.product_knowledge.egress import _split_unbracketed

    assert _split_unbracketed(value) == expected


@pytest.mark.parametrize("char", list("@/\\?# \t\r\n%"))
def test_split_host_port_rejects_each_forbidden_char(char: str) -> None:
    assert split_host_port(f"ho{char}st") is None


def test_split_host_port_rejects_empty() -> None:
    assert split_host_port("") is None


def test_canonical_helpers() -> None:
    from adapter.product_knowledge.egress import (
        _canonical_dns_name,
        _canonical_ipv6,
        _canonical_numeric_ipv4,
    )

    assert _canonical_ipv6("0:0:0:0:0:0:0:1") == "::1"
    assert _canonical_ipv6("zz::1") is None
    assert _canonical_numeric_ipv4("0x7f.1") == "127.0.0.1"
    assert _canonical_numeric_ipv4("1.2.3.4.5") is None
    assert _canonical_dns_name("bücher.example") == "xn--bcher-kva.example"
    assert _canonical_dns_name("a..b") is None
    assert _canonical_dns_name("a" * 64) is None
    assert _canonical_dns_name("a" * 63) == "a" * 63
    assert _canonical_dns_name(".".join(["a" * 63] * 4)) is None  # > 253 chars
