"""adapter.github.token_provider — where an ``adapter.github`` client gets its token from.

Kept apart from ``adapter.github.auth`` (GitHub App JWT signing and token exchange) so that a
caller holding a token already, such as the local CLI in GitHub Actions, uses it without importing
the App machinery (``pyjwt``, private keys).
"""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr


class InstallationTokenProvider(Protocol):
    """What every ``adapter.github`` client needs of a token source: one repo-scoped token per
    call. ``GitHubInstallationTokenProvider`` (server mode) and ``StaticTokenProvider`` (local
    mode in CI) both satisfy it."""

    async def get_token(self, *, installation_id: int, repo_full_name: str) -> SecretStr: ...


class StaticTokenProvider:
    """A fixed token, for runs that already hold one (the ``GITHUB_TOKEN`` of a GitHub Actions
    job). It ignores ``installation_id`` and ``repo_full_name``: the token's scope was set by the
    workflow, not by this process. Its repr never shows the token."""

    def __init__(self, token: SecretStr) -> None:
        if not token.get_secret_value():
            raise ValueError("a static token provider needs a non-empty token")
        self._token = token

    async def get_token(self, *, installation_id: int, repo_full_name: str) -> SecretStr:
        return self._token

    def __repr__(self) -> str:
        return "StaticTokenProvider(token=**********)"
