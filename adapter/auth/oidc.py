"""adapter.auth.oidc — ``iam.oidc.OIDCProvider`` implementations.

``GenericOIDCProvider`` works against any standards-compliant issuer via
OpenID Connect discovery. ``AuthentikProvider`` only derives Authentik's
issuer URL (``<base>/application/o/<slug>/``); the protocol work is shared.
Another vendor with a non-standard quirk gets its own subclass.

Validation: authorization code + PKCE (S256), RS256/ES256 signatures only
(never ``none`` or HMAC), ``iss``/``aud``/``exp``/``nonce`` checked. Every
HTTP call has a timeout; the issuer URLs come from operator config, never
from a request.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from iam.oidc import OIDCError, OIDCIdentity, OIDCLoginRequest, pkce_challenge

logger = logging.getLogger(__name__)

_ALLOWED_ALGORITHMS = ["RS256", "ES256"]
_TIMEOUT_SECONDS = 10.0


class GenericOIDCProvider:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        scopes: str = "openid profile email",
        username_claim: str = "preferred_username",
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._username_claim = username_claim
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[
            Any, dict[str, Any]
        ] = {}  # kid -> JWK; shortcut: refetched only on a kid miss

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        try:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise OIDCError(f"fetching {url} failed") from err
        if not isinstance(body, dict):
            raise OIDCError(f"{url} did not return a JSON object")
        return body

    async def _discover(self, client: httpx.AsyncClient) -> dict[str, Any]:
        if self._discovery is None:
            url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
            doc = await self._get_json(client, url)
            if doc.get("issuer") != self._issuer:
                raise OIDCError("discovery document issuer does not match configured issuer")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(doc.get(key), str):
                    raise OIDCError(f"discovery document missing {key}")
            self._discovery = doc
        return self._discovery

    async def authorization_url(self, *, login: OIDCLoginRequest, redirect_uri: str) -> str:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            doc = await self._discover(client)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": self._scopes,
                "state": login.state,
                "nonce": login.nonce,
                "code_challenge": pkce_challenge(login.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in doc["authorization_endpoint"] else "?"
        return f"{doc['authorization_endpoint']}{separator}{query}"

    async def complete_login(
        self, *, code: str, login: OIDCLoginRequest, redirect_uri: str
    ) -> OIDCIdentity:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            doc = await self._discover(client)
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "code_verifier": login.code_verifier,
            }
            if self._client_secret:
                form["client_secret"] = self._client_secret
            try:
                response = await client.post(doc["token_endpoint"], data=form)
                response.raise_for_status()
                id_token = response.json().get("id_token")
            except (httpx.HTTPError, ValueError, AttributeError) as err:
                raise OIDCError("token exchange failed") from err
            if not isinstance(id_token, str):
                raise OIDCError("token response had no id_token")
            claims = await self._verify(client, doc["jwks_uri"], id_token)

        if not hmac.compare_digest(str(claims.get("nonce", "")), login.nonce):
            raise OIDCError("id_token nonce mismatch")
        username = claims.get(self._username_claim)
        if not isinstance(username, str) or not username:
            raise OIDCError(f"id_token has no usable {self._username_claim!r} claim")
        return OIDCIdentity(subject=str(claims["sub"]), username=username)

    async def _verify(
        self, client: httpx.AsyncClient, jwks_uri: str, id_token: str
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
            kid = header.get("kid")
            if kid not in self._jwks:
                keys = (await self._get_json(client, jwks_uri)).get("keys", [])
                self._jwks = {k.get("kid"): k for k in keys if isinstance(k, dict)}
            if kid not in self._jwks:
                raise OIDCError("id_token signed by an unknown key")
            key = jwt.PyJWK(self._jwks[kid]).key
            return jwt.decode(
                id_token,
                key,
                algorithms=_ALLOWED_ALGORITHMS,
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as err:
            raise OIDCError("id_token validation failed") from err


class AuthentikProvider(GenericOIDCProvider):
    """Authentik: one OIDC provider per application, issuer
    ``<base_url>/application/o/<application_slug>/``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        application_slug: str,
        client_id: str,
        client_secret: str | None,
        scopes: str = "openid profile email",
        username_claim: str = "preferred_username",
    ) -> None:
        super().__init__(
            issuer=f"{base_url.rstrip('/')}/application/o/{application_slug}/",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            username_claim=username_claim,
        )
