"""
Unit tests for ``ProductKnowledgeSettingsMixin``.

The mixin is the shared bootstrap settings both entrypoints mix in (plan
Phase 6, Assumption 14).
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from adapter.egress.credentials import CredentialMapError
from adapter.product_knowledge.settings import ProductKnowledgeSettingsMixin

_TOKEN = "tok-canary-should-never-leak"


class _Settings(ProductKnowledgeSettingsMixin):
    pass


def _credentials(**entries: object) -> str:
    return json.dumps(entries)


def test_defaults_are_inert() -> None:
    settings = _Settings(_env_file=None)

    assert settings.product_knowledge_enabled is True
    assert settings.product_knowledge_credentials is None
    assert settings.product_knowledge_call_timeout_seconds == 5
    assert settings.product_knowledge_allow_private_networks is True
    assert settings.endpoint_allowlist() == ()
    assert not settings.credential_map()


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_credentials_are_treated_as_unset(blank: str) -> None:
    settings = _Settings(product_knowledge_credentials=SecretStr(blank))

    assert settings.product_knowledge_credentials is None
    assert not settings.credential_map()


@pytest.mark.parametrize("value", [0, -1])
def test_call_timeout_must_be_at_least_one_second(value: int) -> None:
    with pytest.raises(ValidationError, match="must be >= 1"):
        _Settings(product_knowledge_call_timeout_seconds=value)


def test_endpoint_allowlist_is_parsed_and_validated() -> None:
    settings = _Settings(product_knowledge_endpoint_allowlist="wiki.example.com, *.corp.example")

    assert settings.endpoint_allowlist() == ("wiki.example.com", "*.corp.example")
    with pytest.raises(ValidationError):
        _Settings(product_knowledge_endpoint_allowlist="https://not-a-host/path")


def test_malformed_credential_map_is_reported_without_the_value() -> None:
    settings = _Settings(product_knowledge_credentials=SecretStr('{"a": {"token": "' + _TOKEN))

    with pytest.raises(CredentialMapError) as raised:
        settings.credential_map()
    assert _TOKEN not in str(raised.value)


def test_valid_credential_map_round_trips_and_repr_hides_the_token() -> None:
    raw = _credentials(wiki={"token": _TOKEN, "allowed_hosts": ["wiki.example.com"]})
    settings = _Settings(product_knowledge_credentials=SecretStr(raw))

    assert settings.credential_map().refs == ("wiki",)
    assert _TOKEN not in repr(settings)
    assert _TOKEN not in str(settings)


def test_validation_errors_never_echo_the_credential_map() -> None:
    raw = _credentials(wiki={"token": _TOKEN, "allowed_hosts": ["wiki.example.com"]})

    with pytest.raises(ValidationError) as raised:
        _Settings(
            product_knowledge_credentials=SecretStr(raw),
            product_knowledge_call_timeout_seconds=0,
        )
    assert _TOKEN not in str(raised.value)
