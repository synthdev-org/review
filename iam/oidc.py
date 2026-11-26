"""iam.oidc — the OIDC login behaviour (``OIDCProvider``) and the pure,
I/O-free pieces of the authorization-code + PKCE flow, per
``docs/specs/spec-admin-gui.md`` US-A24 and ADR-0010.

Zero infra imports, per ``docs/rules/architecture.md``. The Protocol is
defined here, at the consumer (``apps/admin_api/oidc_routes.py``); the
HTTP/JWT work lives in ``adapter/auth/oidc.py``. Adding a provider means a
new adapter class and a settings value, never a route change.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class OIDCError(Exception):
    """Any failure to complete an OIDC login: unreachable provider, bad
    discovery document, token exchange refused, or an ID token that fails
    validation. The message is for logs only; callers never show it to the
    browser."""


class OIDCIdentity(BaseModel):
    """The validated result of a login: the provider's stable subject id and
    the value of the configured username claim."""

    model_config = ConfigDict(frozen=True)

    subject: str
    username: str


class OIDCLoginRequest(BaseModel):
    """Per-login secrets the callback must see again: ``state`` (CSRF),
    ``nonce`` (ID token replay) and the PKCE ``code_verifier``."""

    model_config = ConfigDict(frozen=True)

    state: str
    nonce: str
    code_verifier: str


class OIDCProvider(Protocol):
    """One OIDC identity provider."""

    async def authorization_url(self, *, login: OIDCLoginRequest, redirect_uri: str) -> str:
        """The provider URL to send the browser to."""
        ...

    async def complete_login(
        self, *, code: str, login: OIDCLoginRequest, redirect_uri: str
    ) -> OIDCIdentity:
        """Exchange ``code`` and return the validated identity.

        Raises ``OIDCError`` on any failure.
        """
        ...


def new_login_request() -> OIDCLoginRequest:
    return OIDCLoginRequest(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(64),
    )


def pkce_challenge(code_verifier: str) -> str:
    """RFC 7636 S256: base64url(sha256(verifier)) with no padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def encode_login_cookie(login: OIDCLoginRequest) -> str:
    # urlsafe tokens never contain "."
    return f"{login.state}.{login.nonce}.{login.code_verifier}"


def decode_login_cookie(value: str | None) -> OIDCLoginRequest | None:
    parts = (value or "").split(".")
    if len(parts) != 3 or not all(parts):
        return None
    return OIDCLoginRequest(state=parts[0], nonce=parts[1], code_verifier=parts[2])
