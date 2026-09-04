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


# ---------------------------------------------------------------------------
# Phase 4: shared retry/backoff helper (adapter.github.http)
# ---------------------------------------------------------------------------


class GitHubPermanentAPIError(GitHubAdapterError):
    """Raised by ``adapter.github.http.request_with_retry`` when GitHub
    responds with a permanent error (401/403/404) — never retried, since a
    later attempt is never expected to succeed."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(f"GitHub API call failed permanently (HTTP {status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class GitHubRetryExhaustedError(GitHubAdapterError):
    """Raised by ``adapter.github.http.request_with_retry`` when a call's
    retry budget (``github_retry_max_attempts``) is exhausted on a bare 5xx,
    or a 429/secondary-rate-limit response's ``Retry-After`` exceeds the
    10-second cap this helper enforces before giving up rather than
    waiting.

    ``status_code`` is ``None`` for a connection-level failure (timeout,
    refused connection) that never received an HTTP response at all.
    """

    def __init__(self, *, status_code: int | None, detail: str) -> None:
        super().__init__(f"GitHub API call retry budget exhausted (HTTP {status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Phase 4: GitHub App installation-token exchange (adapter.github.auth)
# ---------------------------------------------------------------------------


class GitHubPrivateKeyInvalidError(GitHubAdapterError):
    """Raised by ``adapter.github.auth.validate_private_key`` (the Worker's
    fail-fast startup check) when the configured
    ``github_app_private_key`` cannot be used to sign a JWT — a malformed,
    truncated, or otherwise unusable PEM value."""

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"GitHub App private key is not usable for RS256 signing: {detail}")
        self.detail = detail


class GitHubTokenExchangeError(GitHubAdapterError):
    """Base class for failures exchanging a signed App JWT for a real
    installation access token (``adapter.github.auth.GitHubInstallationTokenProvider``).

    Distinct from ``GitHubCodeExchangeError`` above: that one covers the
    one-time, operator-driven manifest-flow code exchange (Phase 0); this
    one covers the ordinary, per-job, per-repo server-to-server App-auth
    flow this phase adds.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"GitHub App installation token exchange failed: {detail}")
        self.detail = detail


class GitHubTokenExchangeUnreachableError(GitHubTokenExchangeError):
    """Raised when GitHub's installation-access-token endpoint could not be
    reached at all, or exhausted its retry budget, or returned a permanent
    error — see ``adapter/github/http.py``'s own exception types, which
    this wraps."""


class GitHubTokenExchangeMalformedResponseError(GitHubTokenExchangeError):
    """Raised when GitHub's installation-access-token endpoint responded,
    but the body did not contain the fields (``token``, ``expires_at``)
    this adapter expects."""


class GitHubTokenExchangeMalformedRequestError(GitHubTokenExchangeError):
    """Raised when the caller-provided ``repo_full_name`` this module needs
    to build the token-exchange request body is not in the expected
    ``"owner/repo"`` form — ``adapter/github/schemas.py`` types it as a bare
    ``str`` with no format constraint, so this must be a typed failure, not
    a silent fallback to sending the whole malformed string as the repo
    name."""


# ---------------------------------------------------------------------------
# Phase 4: diff fetch (adapter.github.diff)
# ---------------------------------------------------------------------------


class GitHubDiffFetchError(GitHubAdapterError):
    """Base class for every fatal failure fetching a PR's diff (changed
    files, real git mode/blob metadata, or per-file content) via
    ``adapter.github.diff.GitHubDiffFetcher``.

    Per the plan's Phase 4 section, every subclass here is treated the same
    way by the pipeline: a typed exception that fails the job (Phase 3's
    ``FAILED`` transition), logged at ``error`` — never a silently
    truncated or garbled result, since several of these guard against a
    PR-crafted bypass of this platform's own static-analysis/LLM coverage.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"GitHub diff fetch failed: {detail}")
        self.detail = detail


class GitHubDiffFetchUnreachableError(GitHubDiffFetchError):
    """Raised when a diff-fetch call (Files/Trees/Blobs endpoint) could not
    be reached at all, exhausted its retry budget, or returned a permanent
    error — see ``adapter/github/http.py``'s own exception types, which
    this wraps."""


class GitHubDiffFetchMalformedResponseError(GitHubDiffFetchError):
    """Raised when a diff-fetch call responded, but the body did not
    contain the fields this adapter expects."""


class GitHubFileCountMismatchError(GitHubDiffFetchError):
    """Raised when the number of files actually returned by the paginated
    Files-endpoint disagrees with the PR object's own ``changed_files``
    count.

    Per the plan's Phase 4 section: the Files-endpoint caps at 3,000 files
    per PR regardless of pagination; a mismatch means the response was
    silently truncated — never accepted, since an attacker could otherwise
    pad a PR past the cap to push files past review entirely.
    """


class GitHubTreeEntryMissingError(GitHubDiffFetchError):
    """Raised when a changed-file path from the Files-endpoint response has
    no matching entry in the Git Trees API response to cross-reference for
    its real git ``mode``.

    Per the plan's Phase 4 section: fatal only when a *needed* entry is
    actually missing (whether due to the Trees API's own 100,000-entry/7MB
    truncation or any other reason) — never merely because the response's
    own ``truncated: true`` flag is set while every changed-file path was
    still resolved. Silently defaulting a missing entry to "regular file"
    would let a truncated response's missing symlink/submodule entries
    bypass the mode-based materialization guard this error exists to
    protect.
    """


class GitHubBlobTooLargeError(GitHubDiffFetchError):
    """Raised when a Git Blobs API fetch for a surviving file's content
    returns an error because the blob is at or beyond that API's
    documented ~100MB size ceiling.

    Per the plan's Phase 4 section: treated the same as any other GitHub
    API failure — a typed exception that fails the job — never a silently
    skipped/empty file, which would let an oversized file hide a secret or
    vulnerable pattern from this platform's own static-analysis/LLM
    coverage.
    """


# ---------------------------------------------------------------------------
# Phase 7: findings posting (adapter.github.posting)
# ---------------------------------------------------------------------------


class GitHubPostingError(GitHubAdapterError):
    """Base class for every failure posting review output (a batched PR
    review, a check-run) via ``adapter.github.posting.GitHubFindingsPublisher``.

    Per the plan's Phase 7 section, every subclass here is treated the same
    way by the pipeline: a typed exception that fails the job (Phase 3's
    ``FAILED`` transition; posting is the findings-merge-and-output macro
    step's own I/O, so a failure here is fatal per US-F6) — never a
    silently swallowed or partial post.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"GitHub posting failed: {detail}")
        self.detail = detail


class GitHubPostingUnreachableError(GitHubPostingError):
    """Raised when a posting call (create PR review, create check-run)
    could not be completed — unreachable, exhausted its (narrower,
    idempotency-scoped) retry budget, or returned a permanent error — see
    ``adapter/github/http.py``'s own exception types, which this wraps."""


class GitHubPostingMalformedRequestError(GitHubPostingError):
    """Raised when the caller-provided ``repo_full_name`` this module needs
    to build a posting URL is not in the expected ``"owner/repo"`` form —
    ``adapter/github/schemas.py`` types it as a bare ``str`` with no format
    constraint, so this must be a typed failure, not an unhandled
    ``ValueError``, even (especially) when raised from the terminal-step
    failure-signal call this platform relies on to guarantee GitHub-visible
    output for every job."""
