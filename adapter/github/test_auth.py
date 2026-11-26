"""Tests for adapter.github.auth — GitHub App JWT signing and installation
access-token exchange.

``sign_app_jwt``/``validate_private_key`` are pure (unit-tested directly,
no I/O); ``GitHubInstallationTokenProvider`` does real I/O and is
integration-tested against a recorded cassette (``respx``), per
docs/rules/testing.md's middle tier.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider, sign_app_jwt, validate_private_key
from adapter.github.exceptions import (
    GitHubPrivateKeyInvalidError,
    GitHubTokenExchangeMalformedResponseError,
    GitHubTokenExchangeUnreachableError,
)

_APP_ID = 987654

# ---------------------------------------------------------------------------
# Unit: sign_app_jwt / validate_private_key (pure, no I/O)
# ---------------------------------------------------------------------------


def test_sign_app_jwt_produces_a_verifiable_rs256_token(test_rsa_private_key_pem: str) -> None:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    private_key = load_pem_private_key(test_rsa_private_key_pem.encode(), password=None)
    assert isinstance(private_key, RSAPrivateKey)
    public_key = private_key.public_key()

    token = sign_app_jwt(app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem, now=1_000_000.0)

    decoded = jwt.decode(token, key=public_key, algorithms=["RS256"], options={"verify_exp": False})
    assert decoded["iss"] == str(_APP_ID)
    assert decoded["iat"] < 1_000_000
    assert decoded["exp"] > 1_000_000


def test_validate_private_key_accepts_a_valid_key(test_rsa_private_key_pem: str) -> None:
    validate_private_key(app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem)  # no raise


def test_validate_private_key_rejects_a_malformed_key() -> None:
    with pytest.raises(GitHubPrivateKeyInvalidError):
        validate_private_key(app_id=_APP_ID, private_key_pem="definitely-not-a-key")


# ---------------------------------------------------------------------------
# Integration: GitHubInstallationTokenProvider against a recorded cassette
# ---------------------------------------------------------------------------


def _token_endpoint(installation_id: int) -> str:
    return f"https://api.github.com/app/installations/{installation_id}/access_tokens"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_happy_path(test_rsa_private_key_pem: str) -> None:
    respx.post(_token_endpoint(555)).mock(
        return_value=httpx.Response(
            201,
            json={"token": "ghs_abc123", "expires_at": "2999-01-01T00:00:00Z"},
        )
    )
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    token = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")

    assert token.get_secret_value() == "ghs_abc123"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_request_scopes_to_the_repo_name(test_rsa_private_key_pem: str) -> None:
    route = respx.post(_token_endpoint(555)).mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_abc123", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    await provider.get_token(installation_id=555, repo_full_name="acme/widgets")

    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body == {"repositories": ["widgets"]}


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_unreachable_raises_typed_error(test_rsa_private_key_pem: str) -> None:
    respx.post(_token_endpoint(555)).mock(side_effect=httpx.ConnectError("refused"))
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem, max_retry_attempts=0
    )

    with pytest.raises(GitHubTokenExchangeUnreachableError):
        await provider.get_token(installation_id=555, repo_full_name="acme/widgets")


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_permanent_error_raises_typed_error(test_rsa_private_key_pem: str) -> None:
    respx.post(_token_endpoint(555)).mock(return_value=httpx.Response(401))
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    with pytest.raises(GitHubTokenExchangeUnreachableError):
        await provider.get_token(installation_id=555, repo_full_name="acme/widgets")


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_malformed_response_raises_typed_error(
    test_rsa_private_key_pem: str,
) -> None:
    respx.post(_token_endpoint(555)).mock(return_value=httpx.Response(201, json={"unexpected": 1}))
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    with pytest.raises(GitHubTokenExchangeMalformedResponseError):
        await provider.get_token(installation_id=555, repo_full_name="acme/widgets")


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_cache_not_reused_across_different_repos_same_installation(
    test_rsa_private_key_pem: str,
) -> None:
    route = respx.post(_token_endpoint(555))
    route.side_effect = [
        httpx.Response(
            201, json={"token": "token-for-widgets", "expires_at": "2999-01-01T00:00:00Z"}
        ),
        httpx.Response(
            201, json={"token": "token-for-gadgets", "expires_at": "2999-01-01T00:00:00Z"}
        ),
    ]
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    first = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")
    second = await provider.get_token(installation_id=555, repo_full_name="acme/gadgets")

    assert first.get_secret_value() == "token-for-widgets"
    assert second.get_secret_value() == "token-for-gadgets"
    assert route.call_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_reuses_cached_token_within_expiry(test_rsa_private_key_pem: str) -> None:
    route = respx.post(_token_endpoint(555)).mock(
        return_value=httpx.Response(
            201, json={"token": "cached-token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    first = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")
    second = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")

    assert first.get_secret_value() == second.get_secret_value() == "cached-token"
    assert route.call_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_re_exchanges_once_cached_token_is_stale(
    test_rsa_private_key_pem: str,
) -> None:
    route = respx.post(_token_endpoint(555))
    # Already-expired timestamp, so the second get_token call must not
    # reuse it.
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    route.side_effect = [
        httpx.Response(201, json={"token": "stale-token", "expires_at": past}),
        httpx.Response(201, json={"token": "fresh-token", "expires_at": "2999-01-01T00:00:00Z"}),
    ]
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem
    )

    first = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")
    second = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")

    assert first.get_secret_value() == "stale-token"
    assert second.get_secret_value() == "fresh-token"
    assert route.call_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_get_token_goes_through_the_shared_retry_helper(
    test_rsa_private_key_pem: str,
) -> None:
    """Proves the call site actually routes through
    ``adapter.github.http.request_with_retry`` (a transient 500 followed by
    success still succeeds) rather than re-testing the retry logic itself."""
    route = respx.post(_token_endpoint(555))
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(
            201, json={"token": "ghs_after_retry", "expires_at": "2999-01-01T00:00:00Z"}
        ),
    ]
    provider = GitHubInstallationTokenProvider(
        app_id=_APP_ID, private_key_pem=test_rsa_private_key_pem, max_retry_attempts=3
    )

    token = await provider.get_token(installation_id=555, repo_full_name="acme/widgets")

    assert token.get_secret_value() == "ghs_after_retry"
    assert route.call_count == 2
