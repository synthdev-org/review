"""Tests for adapter.github.manifest.

Per docs/rules/testing.md's pyramid: the pure ``generate_setup_state`` /
``verify_setup_state`` functions are unit-tested directly (no I/O); the
real-adapter ``exchange_temporary_code`` function is integration-tested
against a stubbed GitHub endpoint via ``respx``, per testing.md's explicit
"the GitHub adapter against a recorded cassette (respx for httpx)" note.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from adapter.github.exceptions import (
    GitHubCodeExchangeMalformedResponseError,
    GitHubCodeExchangeUnreachableError,
    SetupStateExpiredError,
    SetupStateMismatchError,
    SetupStateMissingError,
)
from adapter.github.manifest import (
    exchange_temporary_code,
    generate_setup_state,
    verify_setup_state,
)

_SECRET = b"unit-test-setup-secret"

# ---------------------------------------------------------------------------
# Unit tests: generate_setup_state / verify_setup_state (pure, no I/O)
# ---------------------------------------------------------------------------


def test_generate_and_verify_setup_state_round_trips() -> None:
    state = generate_setup_state(secret=_SECRET, now=1_000.0)

    # Does not raise: a state checked against its own value, under the same
    # secret, within the TTL, is valid.
    verify_setup_state(
        cookie_value=state, query_state=state, secret=_SECRET, ttl_seconds=600, now=1_100.0
    )


def test_verify_setup_state_missing_cookie_raises() -> None:
    with pytest.raises(SetupStateMissingError):
        verify_setup_state(
            cookie_value=None, query_state="anything", secret=_SECRET, ttl_seconds=600
        )


def test_verify_setup_state_missing_query_state_raises() -> None:
    state = generate_setup_state(secret=_SECRET, now=1_000.0)

    with pytest.raises(SetupStateMissingError):
        verify_setup_state(cookie_value=state, query_state=None, secret=_SECRET, ttl_seconds=600)


def test_verify_setup_state_mismatched_values_raises() -> None:
    cookie_state = generate_setup_state(secret=_SECRET, now=1_000.0)
    other_state = generate_setup_state(secret=_SECRET, now=1_000.0)

    with pytest.raises(SetupStateMismatchError):
        verify_setup_state(
            cookie_value=cookie_state,
            query_state=other_state,
            secret=_SECRET,
            ttl_seconds=600,
            now=1_100.0,
        )


def test_verify_setup_state_tampered_signature_raises() -> None:
    state = generate_setup_state(secret=_SECRET, now=1_000.0)
    nonce, timestamp, _signature = state.split(".")
    bogus_signature = "0" * 64
    tampered = f"{nonce}.{timestamp}.{bogus_signature}"

    with pytest.raises(SetupStateMismatchError):
        verify_setup_state(
            cookie_value=tampered,
            query_state=tampered,
            secret=_SECRET,
            ttl_seconds=600,
            now=1_100.0,
        )


def test_verify_setup_state_wrong_secret_raises() -> None:
    state = generate_setup_state(secret=_SECRET, now=1_000.0)

    with pytest.raises(SetupStateMismatchError):
        verify_setup_state(
            cookie_value=state,
            query_state=state,
            secret=b"a-different-secret-entirely",
            ttl_seconds=600,
            now=1_100.0,
        )


def test_verify_setup_state_malformed_value_raises() -> None:
    with pytest.raises(SetupStateMismatchError):
        verify_setup_state(
            cookie_value="not-shaped-like-a-state-token",
            query_state="not-shaped-like-a-state-token",
            secret=_SECRET,
            ttl_seconds=600,
        )


def test_verify_setup_state_expired_raises() -> None:
    state = generate_setup_state(secret=_SECRET, now=1_000.0)

    with pytest.raises(SetupStateExpiredError):
        verify_setup_state(
            cookie_value=state, query_state=state, secret=_SECRET, ttl_seconds=600, now=1_601.0
        )


def test_verify_setup_state_within_ttl_boundary_does_not_raise() -> None:
    state = generate_setup_state(secret=_SECRET, now=1_000.0)

    verify_setup_state(
        cookie_value=state, query_state=state, secret=_SECRET, ttl_seconds=600, now=1_599.0
    )


# ---------------------------------------------------------------------------
# Integration tests: exchange_temporary_code against a stubbed GitHub API
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exchange_temporary_code_happy_path() -> None:
    respx.post("https://api.github.com/app-manifests/good-code/conversions").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 42,
                "slug": "review-bot",
                "pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": "whsec-value",
                "client_id": "client-id-value",
                "client_secret": "client-secret-value",
            },
        )
    )

    credentials = await exchange_temporary_code("good-code", timeout=5.0)

    assert credentials.app_id == 42
    assert credentials.app_slug == "review-bot"
    assert credentials.private_key.get_secret_value().startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert credentials.webhook_secret.get_secret_value() == "whsec-value"
    assert credentials.client_id == "client-id-value"
    assert credentials.client_secret.get_secret_value() == "client-secret-value"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exchange_temporary_code_timeout_raises_typed_error() -> None:
    respx.post("https://api.github.com/app-manifests/slow-code/conversions").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    with pytest.raises(GitHubCodeExchangeUnreachableError):
        await exchange_temporary_code("slow-code", timeout=1.0)


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exchange_temporary_code_connection_error_raises_typed_error() -> None:
    respx.post("https://api.github.com/app-manifests/unreachable-code/conversions").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(GitHubCodeExchangeUnreachableError):
        await exchange_temporary_code("unreachable-code", timeout=5.0)


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exchange_temporary_code_error_status_raises_typed_error() -> None:
    respx.post("https://api.github.com/app-manifests/bad-code/conversions").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )

    with pytest.raises(GitHubCodeExchangeUnreachableError):
        await exchange_temporary_code("bad-code", timeout=5.0)


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exchange_temporary_code_malformed_response_raises_typed_error() -> None:
    respx.post("https://api.github.com/app-manifests/weird-code/conversions").mock(
        return_value=httpx.Response(201, json={"id": 42})  # missing pem/webhook_secret/etc.
    )

    with pytest.raises(GitHubCodeExchangeMalformedResponseError):
        await exchange_temporary_code("weird-code", timeout=5.0)


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exchange_temporary_code_quotes_a_traversing_code() -> None:
    route = respx.post(url__regex=r"https://api\.github\.com/.*").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(GitHubCodeExchangeUnreachableError):
        await exchange_temporary_code("../../app/installations", timeout=1.0)

    assert route.calls.last.request.url.raw_path == (
        b"/app-manifests/..%2F..%2Fapp%2Finstallations/conversions"
    )
