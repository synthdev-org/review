"""
One canonicaliser (egress.py docstring, plan Phase 6/7) serves every caller.

The credential map and the Admin routes must normalise hosts through ``egress.canonicalize_host``
so write-time, job-time and connect-time cannot disagree about what
``WIKI.Example.com.`` or ``2130706433`` means. Pure: no network.
"""

from __future__ import annotations

import json

import pytest

from adapter.egress import credentials, egress, transport
from apps.admin_api import product_knowledge_routes as routes


def test_modules_use_the_egress_canonicaliser_itself_not_a_copy() -> None:
    for module in (routes, transport):
        assert vars(module)["canonicalize_host"] is egress.canonicalize_host, module
    # credentials normalises only through egress's entry/origin parsers, which
    # themselves call canonicalize_host (it holds no host-normalising code).
    assert vars(credentials)["parse_allow_entry"] is egress.parse_allow_entry
    assert vars(credentials)["endpoint_origin"] is egress.endpoint_origin


def test_credential_map_binds_the_canonical_form_of_a_host() -> None:
    raw = json.dumps({"wiki": {"token": "t", "allowed_hosts": ["WIKI.Example.com.", "2130706433"]}})
    parsed = credentials.parse_credential_map(raw)

    assert parsed.allowed_hosts("wiki") == ("wiki.example.com", "127.0.0.1")
    # job-time/write-time binding check judges the same canonical form
    assert parsed.binding_problem("wiki", "https://wiki.EXAMPLE.com:8443/mcp") is None
    assert parsed.binding_problem("wiki", "http://127.1/mcp") is None  # inet_aton spelling
    assert parsed.binding_problem("wiki", "https://evil.example.com/mcp") == (
        "credential_host_not_bound"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://WIKI.Example.com./mcp", "wiki.example.com"),
        ("http://2130706433/mcp", "127.0.0.1"),
        ("https://wiki.example.com:8443/x", "wiki.example.com"),
        ("http://[::1]:8080/mcp", "::1"),
        ("not a url", None),
    ],
)
def test_admin_routes_derive_the_endpoint_host_through_the_shared_canonicaliser(
    url: str, expected: str | None
) -> None:
    assert routes._host_of(url) == expected


@pytest.mark.parametrize("entry", ["WIKI.Example.com.", "2130706433"])
def test_allow_list_entries_are_accepted_by_the_shared_canonicaliser(entry: str) -> None:
    assert credentials.parse_endpoint_allowlist(entry) == (entry.lower(),)
    with pytest.raises(credentials.CredentialMapError):
        credentials.parse_endpoint_allowlist("host/path")
