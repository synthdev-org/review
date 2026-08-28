"""Typed exceptions for adapter.github.

Per docs/rules/architecture.md's "specific exception types per domain
concern" rule: callers catch these, never a raw ``httpx``/transport
exception — mirrors ``adapter/storage/exceptions.py``'s ``StorageError``
and ``llmgateway/exceptions.py``'s ``LLMError`` pattern.
"""

from __future__ import annotations


class GitHubAdapterError(Exception):
    """Base class for every error this adapter raises."""


class GitHubCodeExchangeError(GitHubAdapterError):
    """Base class for failures exchanging a manifest-flow temporary code
    for real App credentials (``adapter.github.manifest.exchange_temporary_code``)."""

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"GitHub App manifest code exchange failed: {detail}")
        self.detail = detail


class GitHubCodeExchangeUnreachableError(GitHubCodeExchangeError):
    """Raised when GitHub's code-exchange endpoint could not be reached at
    all — connection failure, timeout, or a non-2xx HTTP status.

    Per the plan's Phase 4 section, this call is explicitly timeout-bound;
    this is the error a caller sees when that timeout (or any other
    connectivity failure) fires.
    """


class GitHubCodeExchangeMalformedResponseError(GitHubCodeExchangeError):
    """Raised when GitHub's code-exchange endpoint responded, but the body
    did not contain the fields (``id``, ``pem``, ``webhook_secret``,
    ``client_id``, ``client_secret``) this adapter expects."""


class SetupStateError(GitHubAdapterError):
    """Base class for manifest-flow CSRF ``state`` validation failures
    (``adapter.github.manifest.verify_setup_state``).

    Deliberately three distinct subclasses rather than a single error with a
    reason string, per the plan's Phase 4 Tests section: "a missing state
    parameter, a state that doesn't match the signed cookie, and an expired
    state are each rejected" — each needs to be independently assertable and
    independently loggable.
    """


class SetupStateMissingError(SetupStateError):
    """Raised when the cookie value or the callback's ``state`` query
    parameter is absent."""


class SetupStateMismatchError(SetupStateError):
    """Raised when the callback's ``state`` query parameter does not match
    the signed cookie value set at initiation, or the cookie's embedded
    HMAC signature does not verify."""


class SetupStateExpiredError(SetupStateError):
    """Raised when the state value's embedded timestamp is older than the
    configured TTL."""
