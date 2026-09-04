"""adapter.github.auth — GitHub App server-to-server authentication: RS256
JWT signing and exchanging that JWT for a short-lived, repo-scoped
installation access token, per the plan's Phase 4 section.

**Moved here from where an earlier plan draft placed it (Phase 7), and
built in this phase's own PR — see the plan's Phase 4 "Why here" note:**
``adapter/github/diff.py`` (this phase) cannot fetch anything without a
real installation token, and this phase's own Integration tests need a
real (cassette-recorded) token exchange to run. Phase 7's posting client
reuses ``GitHubInstallationTokenProvider`` unchanged.

Two pieces:

- ``sign_app_jwt`` / ``validate_private_key`` — pure, no-I/O RS256 JWT
  signing (via ``pyjwt``/``cryptography``, the App's own documented
  server-to-server auth flow). ``validate_private_key`` is the Worker's
  fail-fast startup check (``apps/worker/main.py``): a single reused call
  into ``pyjwt``'s own signing path, which already performs PEM parsing
  internally, rather than a separate hand-rolled parse step that would
  duplicate validation ``pyjwt`` already does.
- ``GitHubInstallationTokenProvider`` — the one place this adapter does
  real I/O: exchanges a signed JWT for an installation access token
  (``POST /app/installations/{id}/access_tokens``), scoped to just the
  job's target repo via the endpoint's ``repositories`` field (an array of
  repo *names*, not ``repository_ids`` — this plan never captures a
  numeric GitHub repo id anywhere). Caches the result, keyed by
  ``(installation_id, repo_full_name)`` — not ``installation_id`` alone,
  since a token is now repo-scoped, and not a numeric GitHub "repo_id"
  either, since this plan never captures one; ``repo_full_name`` already
  uniquely identifies the target repo and is what the ``repositories``
  field itself is derived from. Constructed once at the composition root
  (``apps/worker/main.py``) and passed down via constructor injection —
  the cache is instance state, never a module-level dict, per
  architecture.md's "no module-level singletons or global mutable state"
  rule. No eviction policy (flagged, not silently unaddressed — see the
  plan's Phase 4 section: low-severity, in-memory, reset on every Worker
  restart, deferred to the same Phase 8 pruning bucket as
  ``webhook_delivery``'s unbounded Postgres growth).

Every real I/O call here routes through ``adapter/github/http.py``'s
shared retry/backoff helper — a token-exchange ``POST`` is safe to retry on
any 429/5xx (unlike Phase 7's ``posting.py`` writes): retrying it after an
ambiguous timeout or bare 5xx risks minting an extra valid token, not
duplicating any customer-visible content, per the plan's Phase 4 section.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from pydantic import SecretStr

from adapter.github.exceptions import (
    GitHubPermanentAPIError,
    GitHubPrivateKeyInvalidError,
    GitHubRetryExhaustedError,
    GitHubTokenExchangeMalformedRequestError,
    GitHubTokenExchangeMalformedResponseError,
    GitHubTokenExchangeUnreachableError,
)
from adapter.github.http import auth_headers, client_context, request_with_retry, split_owner_repo

_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
_JWT_ALGORITHM = "RS256"
# Per GitHub's documented App-auth flow: iat set slightly in the past to
# tolerate clock drift between this process and GitHub's own clock; exp
# capped well under GitHub's own 10-minute maximum JWT lifetime.
_JWT_CLOCK_DRIFT_LEEWAY_SECONDS = 60
_JWT_TTL_SECONDS = 9 * 60
# A cached token is treated as stale slightly before its real GitHub-issued
# expiry — a small safety margin so a call already in flight when a token
# is "almost expired" doesn't race GitHub's own clock.
_TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 30


def sign_app_jwt(*, app_id: int, private_key_pem: str, now: float | None = None) -> str:
    """Sign a short-lived App-identity JWT (``iat``/``exp``/``iss`` claims),
    per GitHub's documented App-auth flow. Pure — no I/O, no network — the
    one function both ``validate_private_key`` (startup check) and
    ``GitHubInstallationTokenProvider`` (real token exchange) call, so PEM
    parsing/signing is validated identically in both places rather than by
    two divergent code paths.
    """
    issued_at = int(now if now is not None else time.time())
    payload = {
        "iat": issued_at - _JWT_CLOCK_DRIFT_LEEWAY_SECONDS,
        "exp": issued_at + _JWT_TTL_SECONDS,
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key_pem, algorithm=_JWT_ALGORITHM)


def validate_private_key(*, app_id: int, private_key_pem: str) -> None:
    """Confirm ``private_key_pem`` is usable for RS256 signing by attempting
    one real ``sign_app_jwt`` call with it.

    Called from the Worker's composition root (``apps/worker/main.py``) at
    startup, alongside the existing pgvector/LLM-reachability checks —
    catches a malformed/truncated key (a Helm value copy-paste or
    newline-escaping error) at boot, not on the first real job's GitHub
    write. Raises ``GitHubPrivateKeyInvalidError``; the caller decides
    ``SystemExit(1)``, matching every other startup check in this codebase
    (``llmgateway.startup_check``, ``adapter.storage.pgvector_check``,
    ``review.config.check_step_configuration_viable``).
    """
    try:
        sign_app_jwt(app_id=app_id, private_key_pem=private_key_pem)
    except Exception as exc:
        # pyjwt/cryptography raise a variety of exception types for a
        # malformed/truncated PEM (ValueError, TypeError, and
        # cryptography's own exception types) — none of them is a type this
        # module can usefully enumerate in advance, so this is the one
        # deliberately broad `except Exception` in this module, immediately
        # re-raised as this adapter's own typed error rather than left bare.
        raise GitHubPrivateKeyInvalidError(detail=str(exc)) from exc


class _CachedToken:
    __slots__ = ("expires_at", "token")

    def __init__(self, *, token: SecretStr, expires_at: datetime) -> None:
        self.token = token
        self.expires_at = expires_at


class GitHubInstallationTokenProvider:
    """Exchanges a signed App JWT for a repo-scoped installation access
    token, caching it per ``(installation_id, repo_full_name)`` until it's
    within ``_TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS`` of its own
    GitHub-issued expiry.
    """

    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: str,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._github_api_base_url = github_api_base_url
        self._timeout = timeout
        self._max_retry_attempts = max_retry_attempts
        self._cache: dict[tuple[int, str], _CachedToken] = {}
        # One lock per (installation_id, repo_full_name) key, created
        # lazily — without it, concurrent callers racing a cache miss for
        # the same key (e.g. diff-fetch and posting overlapping for one
        # repo) each perform a full token exchange, doubling GitHub API
        # usage under load. Instance state, never a module-level dict, per
        # architecture.md's "no module-level singletons or global mutable
        # state" rule — same reasoning as ``self._cache`` above.
        self._exchange_locks: dict[tuple[int, str], asyncio.Lock] = {}
        # Optional shared, long-lived client (constructed once at the
        # composition root — apps/worker/main.py). Defaults to None so
        # existing callers/tests keep the prior per-call `httpx.AsyncClient`
        # behavior unchanged. See adapter/github/http.py's `client_context`.
        self._client = client

    async def get_token(self, *, installation_id: int, repo_full_name: str) -> SecretStr:
        """Return a valid installation access token scoped to
        ``repo_full_name`` under ``installation_id``, reusing a cached one
        if it isn't yet within its own expiry's safety margin.
        """
        key = (installation_id, repo_full_name)
        cached = self._cache.get(key)
        if cached is not None and not self._is_stale(cached):
            return cached.token

        lock = self._exchange_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check under the lock: a concurrent caller that raced us
            # here may have already populated (or refreshed) the cache
            # while we were waiting on it.
            cached = self._cache.get(key)
            if cached is not None and not self._is_stale(cached):
                return cached.token

            token, expires_at = await self._exchange(
                installation_id=installation_id, repo_full_name=repo_full_name
            )
            self._cache[key] = _CachedToken(token=token, expires_at=expires_at)
            return token

    def _is_stale(self, cached: _CachedToken) -> bool:
        margin = _TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS
        now = datetime.now(UTC)
        return (cached.expires_at - now).total_seconds() <= margin

    async def _exchange(
        self, *, installation_id: int, repo_full_name: str
    ) -> tuple[SecretStr, datetime]:
        # RS256 signing is CPU-bound (PEM parsing + RSA signing via
        # cryptography) — off the event loop via `to_thread` so a cache-miss
        # token exchange (every new (installation, repo) pair, and roughly
        # every `_TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS` thereafter) doesn't
        # stall other concurrently in-flight jobs' await points while this
        # lock is held.
        jwt_token = await asyncio.to_thread(
            sign_app_jwt, app_id=self._app_id, private_key_pem=self._private_key_pem
        )
        try:
            _owner, repo_name = split_owner_repo(repo_full_name)
        except ValueError as exc:
            raise GitHubTokenExchangeMalformedRequestError(detail=str(exc)) from exc
        url = f"{self._github_api_base_url}/app/installations/{installation_id}/access_tokens"

        try:
            async with client_context(self._client, timeout=self._timeout) as client:
                response = await request_with_retry(
                    client,
                    "POST",
                    url,
                    max_attempts=self._max_retry_attempts,
                    headers=auth_headers(SecretStr(jwt_token)),
                    json={"repositories": [repo_name]},
                )
        except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
            raise GitHubTokenExchangeUnreachableError(detail=str(exc)) from exc

        try:
            data: dict[str, Any] = response.json()
            token = data["token"]
            expires_at = _parse_github_timestamp(data["expires_at"])
        except (KeyError, ValueError, TypeError) as exc:
            raise GitHubTokenExchangeMalformedResponseError(detail=str(exc)) from exc

        return SecretStr(token), expires_at


def _parse_github_timestamp(value: str) -> datetime:
    """GitHub timestamps are ISO 8601 with a trailing ``Z`` —
    ``datetime.fromisoformat`` (stdlib, Python 3.11+) doesn't accept a bare
    ``Z`` suffix, only an explicit UTC offset."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
