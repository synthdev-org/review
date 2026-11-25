"""
Unit tests for the credential map, binding check, allow-list parsing and client factory.

Also covers the per-endpoint client factory's job-time re-check (plan
Phase 6; US-PK9, US-PK13). Pure: no network, no database.
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from adapter.egress.credentials import (
    CredentialMap,
    CredentialMapError,
    parse_credential_map,
    parse_endpoint_allowlist,
)
from adapter.egress.transport import EgressPolicy
from adapter.product_knowledge.factory import ProviderClientFactory
from review.product_knowledge import ProductKnowledgeError, ProviderOutcome

CANARY = "CANARY-TOKEN-d41d8cd98f"


def _raw(**entries: object) -> str:
    return json.dumps(entries)


def _valid() -> CredentialMap:
    return parse_credential_map(
        _raw(**{"wiki-prod": {"token": CANARY, "allowed_hosts": ["Wiki.Internal.", "kb.internal"]}})
    )


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t"])
def test_blank_or_unset_map_is_empty_not_an_error(blank: str | None) -> None:
    assert not parse_credential_map(blank)
    assert not parse_credential_map(SecretStr(blank) if blank is not None else None)


def test_valid_map_exposes_refs_and_canonical_hosts_but_never_the_token_in_repr() -> None:
    credentials = _valid()

    assert credentials.refs == ("wiki-prod",)
    assert credentials.allowed_hosts("wiki-prod") == ("wiki.internal", "kb.internal")
    assert CANARY not in repr(credentials)
    assert CANARY not in repr(credentials.get("wiki-prod"))


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not json", "not valid JSON"),
        ("[1, 2]", "non-empty JSON object"),
        ("{}", "non-empty JSON object"),
        ('{"A B": {"token": "x", "allowed_hosts": ["h"]}}', "invalid ref"),
        ('{"ref": "token-as-string"}', "invalid"),
        ('{"ref": {"token": "", "allowed_hosts": ["h"]}}', "empty token"),
        ('{"ref": {"token": "x", "allowed_hosts": []}}', "no allowed_hosts"),
        ('{"ref": {"token": "x"}}', "invalid"),
        ('{"ref": {"token": "x", "allowed_hosts": ["*"]}}', "invalid allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["*.example.com"]}}', "invalid allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["https://h"]}}', "invalid allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["h/path"]}}', "invalid allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["u@h"]}}', "invalid allowed_hosts"),
        ('{"ref": {"token": "x", "allowed_hosts": ["h"], "extra": 1}}', "invalid"),
        ('{"ref": {"token": "x", "allowed_hosts": ["h"]}, "ref": {"token": "y"}}', "duplicate"),
    ],
)
def test_malformed_map_is_rejected_naming_the_problem(raw: str, fragment: str) -> None:
    with pytest.raises(CredentialMapError) as excinfo:
        parse_credential_map(raw)

    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        # Token inside otherwise-malformed structures must never be echoed.
        json.dumps({"ref": {"token": CANARY, "allowed_hosts": []}}),
        json.dumps({"ref": {"token": CANARY, "allowed_hosts": ["*"]}}),
        json.dumps({"ref": {"token": CANARY, "allowed_hosts": "h"}}),
        json.dumps({"ref": {"token": 12345, "allowed_hosts": ["h"], CANARY: 1}}),
        json.dumps([CANARY]),
        '{"ref": {"token": "' + CANARY + '"',  # truncated JSON
    ],
)
def test_errors_never_contain_the_token(raw: str) -> None:
    with pytest.raises(CredentialMapError) as excinfo:
        parse_credential_map(raw)

    assert CANARY not in str(excinfo.value)
    assert CANARY not in repr(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    ("ref", "url", "expected"),
    [
        (None, "https://anything.example/mcp", "unauthenticated_host_not_allowed"),
        (None, "https://kb.internal/mcp", None),  # some credential is bound to it
        ("wiki-prod", "https://wiki.internal/mcp", None),
        ("wiki-prod", "https://WIKI.Internal./mcp", None),
        ("wiki-prod", "https://wiki.internal:8443/mcp", None),
        ("missing", "https://wiki.internal/mcp", "unknown_credential_ref"),
        ("wiki-prod", "https://evil.example/mcp", "credential_host_not_bound"),
        ("wiki-prod", "https://wiki.internal.evil.com/mcp", "credential_host_not_bound"),
        ("wiki-prod", "https://evil.com/wiki.internal", "credential_host_not_bound"),
        ("wiki-prod", "https://wiki.internal@evil.com/mcp", "credential_host_not_bound"),
        ("wiki-prod", "https://xn--wiki-internal-9ib.test/mcp", "credential_host_not_bound"),
        ("wiki-prod", "https://w\u0456ki.internal/mcp", "credential_host_not_bound"),  # Cyrillic i
        ("wiki-prod", "not a url", "credential_host_not_bound"),
    ],
)
def test_binding_problem_uses_the_shared_canonicaliser(
    ref: str | None, url: str, expected: str | None
) -> None:
    assert _valid().binding_problem(ref, url) == expected


def test_any_binds_matches_only_provisioned_hosts() -> None:
    credentials = _valid()

    assert credentials.any_binds("kb.internal")
    assert not credentials.any_binds("other.internal")


@pytest.mark.parametrize("raw", ["", "  ", " , ,"])
def test_blank_allowlist_is_empty(raw: str) -> None:
    assert parse_endpoint_allowlist(raw) == ()


def test_allowlist_accepts_hosts_and_wildcard_suffixes() -> None:
    assert parse_endpoint_allowlist("Wiki.internal, *.corp.example ,wiki.internal") == (
        "wiki.internal",
        "*.corp.example",
    )


@pytest.mark.parametrize("raw", ["*", "https://h", "a*b.example", "h/p"])
def test_allowlist_rejects_unusable_entries(raw: str) -> None:
    with pytest.raises(CredentialMapError):
        parse_endpoint_allowlist(raw)


# --- the job-time re-check in the factory ------------------------------------


def _factory(credentials: CredentialMap) -> ProviderClientFactory:
    return ProviderClientFactory(credentials=credentials, policy=EgressPolicy())


def test_factory_fails_closed_for_an_unknown_ref_at_job_time() -> None:
    with pytest.raises(ProductKnowledgeError) as excinfo:
        _factory(_valid())("https://wiki.internal/mcp", "gone")

    assert excinfo.value.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE


def test_factory_fails_closed_when_a_row_was_written_with_an_unbound_host() -> None:
    with pytest.raises(ProductKnowledgeError) as excinfo:
        _factory(_valid())("https://evil.example/mcp", "wiki-prod")

    assert excinfo.value.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE


def test_factory_builds_a_client_for_a_bound_endpoint_and_for_no_credential() -> None:
    factory = _factory(_valid())

    assert factory("https://wiki.internal/mcp", "wiki-prod") is not None
    # unauthenticated: only a host the operator provisioned (credential host)
    assert factory("https://kb.internal/mcp", None) is not None
    assert (
        ProviderClientFactory(
            credentials=CredentialMap.empty(),
            policy=EgressPolicy(endpoint_allowlist=("*.corp.example",)),
        )("https://wiki.corp.example/mcp", None)
        is not None
    )


@pytest.mark.parametrize("allowlist", [(), ("other.internal",)])
def test_factory_refuses_an_unauthenticated_host_outside_the_allowlist_at_job_time(
    allowlist: tuple[str, ...],
) -> None:
    factory = ProviderClientFactory(
        credentials=CredentialMap.empty(), policy=EgressPolicy(endpoint_allowlist=allowlist)
    )
    with pytest.raises(ProductKnowledgeError) as excinfo:
        factory("https://anywhere.internal/mcp", None)

    assert excinfo.value.outcome is ProviderOutcome.CREDENTIAL_UNAVAILABLE


# --- M2: optional :port on allowed_hosts and allow-list entries ---------------


def _ported() -> CredentialMap:
    return parse_credential_map(
        _raw(
            wiki={"token": CANARY, "allowed_hosts": ["wiki.internal:8443", "[::1]:9000"]},
            plain={"token": CANARY, "allowed_hosts": ["plain.internal"]},
        )
    )


@pytest.mark.parametrize(
    ("ref", "url", "expected"),
    [
        ("wiki", "https://wiki.internal:8443/mcp", None),
        ("wiki", "https://wiki.internal:9443/mcp", "credential_host_not_bound"),
        ("wiki", "https://wiki.internal/mcp", "credential_host_not_bound"),  # 443 != 8443
        ("wiki", "http://[::1]:9000/mcp", None),
        ("wiki", "http://[::1]/mcp", "credential_host_not_bound"),
        ("plain", "https://plain.internal:1234/mcp", None),  # no port => host-only
    ],
)
def test_allowed_hosts_port_must_match_exactly(ref: str, url: str, expected: str | None) -> None:
    assert _ported().binding_problem(ref, url) == expected


def test_scheme_default_port_applies_when_url_omits_one() -> None:
    creds = parse_credential_map(_raw(a={"token": CANARY, "allowed_hosts": ["h.internal:443"]}))
    assert creds.binding_problem("a", "https://h.internal/mcp") is None
    assert creds.binding_problem("a", "http://h.internal/mcp") == "credential_host_not_bound"


@pytest.mark.parametrize("entry", ["h.internal:0", "h.internal:70000", "h.internal:abc", "h:"])
def test_invalid_port_entries_are_rejected(entry: str) -> None:
    with pytest.raises(CredentialMapError):
        parse_credential_map(_raw(a={"token": CANARY, "allowed_hosts": [entry]}))
    with pytest.raises(CredentialMapError):
        parse_endpoint_allowlist(entry)


def test_allowlist_port_entries_are_validated_and_matched() -> None:
    assert parse_endpoint_allowlist("Wiki.internal:8443, *.corp.example:443") == (
        "wiki.internal:8443",
        "*.corp.example:443",
    )
    creds = CredentialMap.empty()
    assert (
        creds.binding_problem(None, "https://wiki.internal:8443/m", ("wiki.internal:8443",)) is None
    )
    assert (
        creds.binding_problem(None, "https://wiki.internal:9443/m", ("wiki.internal:8443",))
        == "unauthenticated_host_not_allowed"
    )
    assert creds.binding_problem(None, "https://a.corp.example/m", ("*.corp.example:443",)) is None


def test_invalid_credential_error_has_no_exception_context_and_no_token_named_key() -> None:
    token_like = "ghp_SECRETKEYNAME1234"
    raw = json.dumps({"wiki": {"token": CANARY, "allowed_hosts": ["h.internal"], token_like: 1}})
    with pytest.raises(CredentialMapError) as excinfo:
        parse_credential_map(raw)

    err: BaseException | None = excinfo.value
    while err is not None:
        assert token_like not in str(err) and token_like not in repr(err)
        assert CANARY not in str(err)
        err = err.__cause__ or err.__context__
    assert excinfo.value.__context__ is None and excinfo.value.__cause__ is None


# --- extracted helpers ------------------------------------------------------


def test_load_entry_error_has_no_exception_context_and_never_echoes_key_names() -> None:
    from adapter.egress.credentials import _load_entry

    with pytest.raises(CredentialMapError) as excinfo:
        _load_entry("ref", {"token": "x", "allowed_hosts": ["h"], CANARY: 1})

    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None
    assert CANARY not in str(excinfo.value)
    assert "unexpected field" in str(excinfo.value)


def test_load_entry_names_only_known_fields_sorted() -> None:
    from adapter.egress.credentials import _load_entry

    with pytest.raises(CredentialMapError) as excinfo:
        _load_entry("ref", {"token": 1, "allowed_hosts": "h"})

    assert "check allowed_hosts, token " in str(excinfo.value)


def test_bound_hosts_checks_token_then_hosts_and_dedupes() -> None:
    from adapter.egress.credentials import _bound_hosts, _Entry

    ok = _Entry(token=SecretStr("t"), allowed_hosts=["A.internal", "a.internal.", "b:8443"])
    assert _bound_hosts("r", ok) == ("a.internal", "b:8443")
    with pytest.raises(CredentialMapError, match="empty token"):
        _bound_hosts("r", _Entry(token=SecretStr(" "), allowed_hosts=[]))
    with pytest.raises(CredentialMapError, match="no allowed_hosts"):
        _bound_hosts("r", _Entry(token=SecretStr("t"), allowed_hosts=[]))
    for bad in ("*.example.com", "https://h", "h/p"):
        with pytest.raises(CredentialMapError, match="invalid allowed_hosts entry"):
            _bound_hosts("r", _Entry(token=SecretStr("t"), allowed_hosts=[bad]))


def test_duplicate_ref_is_rejected_by_the_decoder() -> None:
    from adapter.egress.credentials import _decode_map

    with pytest.raises(CredentialMapError, match="duplicate"):
        _decode_map('{"a": {}, "a": {}}')


def test_parse_credential_numbers_the_invalid_ref_by_position() -> None:
    raw = _raw(
        **{
            "ok": {"token": "t", "allowed_hosts": ["h"]},
            "BAD REF": {"token": "t", "allowed_hosts": ["h"]},
        }
    )
    with pytest.raises(CredentialMapError, match=r"credential #2 has an invalid ref"):
        parse_credential_map(raw)
