"""Integration tests for adapter.auth.oidc against a fake provider served by
``respx`` (discovery, token and JWKS endpoints) with real RSA signatures —
US-A24's token-validation scenarios."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from adapter.auth.oidc import AuthentikProvider, GenericOIDCProvider
from iam.oidc import OIDCError, new_login_request, pkce_challenge

pytestmark = pytest.mark.integration

_ISSUER = "https://idp.example.com/application/o/review/"
_CLIENT_ID = "review-client"
_REDIRECT = "https://review.example.com/api/v1/auth/oidc/callback"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    return {"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]}


def _claims(nonce: str, **overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "user-1",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "nonce": nonce,
        "preferred_username": "alice",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _mock_idp(
    router: respx.MockRouter, *, id_token: str | None, discovery_issuer: str = _ISSUER
) -> None:
    base = _ISSUER.rstrip("/")
    router.get(f"{base}/.well-known/openid-configuration").respond(
        json={
            "issuer": discovery_issuer,
            "authorization_endpoint": "https://idp.example.com/application/o/authorize/",
            "token_endpoint": "https://idp.example.com/application/o/token/",
            "jwks_uri": f"{base}/jwks/",
        }
    )
    router.get(f"{base}/jwks/").respond(json=_jwks())
    if id_token is None:
        router.post("https://idp.example.com/application/o/token/").respond(400, json={})
    else:
        router.post("https://idp.example.com/application/o/token/").respond(
            json={"id_token": id_token}
        )


def _sign(claims: dict[str, Any], *, key: Any = _KEY, alg: str = "RS256", kid: str = "k1") -> str:
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def _token_form(router: respx.MockRouter) -> httpx.QueryParams:
    post = next(c for c in router.calls if c.request.method == "POST")
    return httpx.QueryParams(post.request.content.decode())


def _provider(**kwargs: Any) -> GenericOIDCProvider:
    return GenericOIDCProvider(
        issuer=_ISSUER, client_id=_CLIENT_ID, client_secret="s3cret", **kwargs
    )


@pytest.mark.asyncio
async def test_authorization_url_carries_state_nonce_and_pkce_challenge() -> None:
    login = new_login_request()
    with respx.mock(assert_all_called=False) as router:
        _mock_idp(router, id_token="unused")
        url = await _provider().authorization_url(login=login, redirect_uri=_REDIRECT)
    query = httpx.URL(url).params
    assert url.startswith("https://idp.example.com/application/o/authorize/?")
    assert query["state"] == login.state
    assert query["nonce"] == login.nonce
    assert query["code_challenge"] == pkce_challenge(login.code_verifier)
    assert query["code_challenge_method"] == "S256"
    assert query["client_id"] == _CLIENT_ID
    assert query["redirect_uri"] == _REDIRECT


@pytest.mark.asyncio
async def test_valid_login_returns_identity_and_sends_verifier_and_secret() -> None:
    login = new_login_request()
    with respx.mock(assert_all_called=False) as router:
        _mock_idp(router, id_token=_sign(_claims(login.nonce)))
        identity = await _provider().complete_login(
            code="c0de", login=login, redirect_uri=_REDIRECT
        )
        form = _token_form(router)
    assert (identity.subject, identity.username) == ("user-1", "alice")
    assert form["code"] == "c0de"
    assert form["code_verifier"] == login.code_verifier
    assert form["client_secret"] == "s3cret"


@pytest.mark.asyncio
async def test_authentik_provider_derives_issuer_from_base_url_and_slug() -> None:
    login = new_login_request()
    provider = AuthentikProvider(
        base_url="https://idp.example.com/",
        application_slug="review",
        client_id=_CLIENT_ID,
        client_secret=None,
    )
    with respx.mock(assert_all_called=False) as router:
        _mock_idp(router, id_token=_sign(_claims(login.nonce)))
        identity = await provider.complete_login(code="c", login=login, redirect_uri=_REDIRECT)
        form = _token_form(router)
    assert identity.username == "alice"
    assert "client_secret" not in form  # public client: PKCE only


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("wrong nonce", lambda n: _sign(_claims("other"))),
        ("wrong audience", lambda n: _sign(_claims(n, aud="someone-else"))),
        ("wrong issuer", lambda n: _sign(_claims(n, iss="https://evil.example.com/"))),
        ("expired", lambda n: _sign(_claims(n, exp=int(time.time()) - 10))),
        ("missing username claim", lambda n: _sign(_claims(n, preferred_username=None))),
        ("signed by another key", lambda n: _sign(_claims(n), key=_OTHER_KEY)),
        ("unknown kid", lambda n: _sign(_claims(n), kid="nope")),
        (
            "hmac alg confusion",
            lambda n: jwt.encode(_claims(n), "x" * 64, algorithm="HS256", headers={"kid": "k1"}),
        ),
        (
            "alg none",
            lambda n: jwt.encode(
                _claims(n),
                None,  # type: ignore[arg-type]
                algorithm="none",
                headers={"kid": "k1"},
            ),
        ),
    ],
)
async def test_invalid_id_token_is_rejected(label: str, build: Any) -> None:
    login = new_login_request()
    with respx.mock(assert_all_called=False) as router:
        _mock_idp(router, id_token=build(login.nonce))
        with pytest.raises(OIDCError):
            await _provider().complete_login(code="c", login=login, redirect_uri=_REDIRECT)


@pytest.mark.asyncio
async def test_token_endpoint_refusal_is_an_oidc_error() -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_idp(router, id_token=None)
        with pytest.raises(OIDCError):
            await _provider().complete_login(
                code="c", login=new_login_request(), redirect_uri=_REDIRECT
            )


@pytest.mark.asyncio
async def test_discovery_issuer_mismatch_is_rejected() -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_idp(router, id_token="x", discovery_issuer="https://evil.example.com/")
        with pytest.raises(OIDCError):
            await _provider().authorization_url(login=new_login_request(), redirect_uri=_REDIRECT)
