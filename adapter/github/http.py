"""adapter.github.http — the one shared retry/backoff + redacted-logging
helper every ``adapter.github`` GitHub API call routes through, per the
plan's Phase 4 section ("GitHub-side transient failures, addressed rather
than left uniformly fatal — one shared implementation, not three
independent copies").

Built once here and reused by ``adapter/github/auth.py`` (Phase 4) and
``adapter/github/diff.py`` (Phase 4) — Phase 7's ``posting.py`` reuses it
too, with its own narrower idempotency-scoped retry-eligibility check
layered on top at that call site, not a second copy of this module.
``adapter/github/manifest.py``'s pre-existing ``exchange_temporary_code``
(Phase 0) does not route through this helper — a real, if low-stakes, reuse
gap this plan leaves as-is rather than migrating a one-time,
operator-driven code path outside this phase's own scope (see the plan's
Phase 4 section).

**``safe_writes_only``, added by Phase 7 — additive, defaults to ``False``
so ``auth.py``'s and ``diff.py``'s existing calls (both idempotent: a
token-exchange ``POST`` risks minting an extra token, never duplicating
customer-visible content; every ``diff.py`` call is a ``GET``) are
completely unaffected.** ``adapter/github/posting.py``'s writes (create PR
review, create check-run) are *not* documented as idempotent by GitHub's
API — retrying one after an ambiguous failure risks a duplicate PR review
or check-run landing on the customer's repo. Per the plan's Phase 4
section, those calls are only safe to retry on responses GitHub is known to
return *before* executing the request at all:

- a 429/secondary-rate-limit response (unaffected by ``safe_writes_only`` —
  still retried up to ``retry_after_cap_seconds``, same as any other call);
- ``httpx.ConnectError``/``httpx.ConnectTimeout`` specifically — a failure
  to even establish the connection, so nothing was ever transmitted.

``safe_writes_only=True`` narrows both of the other two paths accordingly:
a bare 5xx/other 4xx response, or any other ``httpx.TimeoutException``/
``httpx.TransportError`` subtype (a read/write timeout or error, which
*may* have occurred after the request was already sent — genuinely
ambiguous, not safe to assume never-executed) fails immediately, on the
first occurrence, as ``GitHubRetryExhaustedError`` — never retried,
regardless of ``max_attempts``. Logged at ``error`` (not the ordinary
retry-exhaustion ``warning``) the moment this happens, since it's a
first-occurrence non-retry decision, not a genuine exhausted-after-trying
one.

Retry policy, per the plan's Phase 4 section:

- A 429/secondary-rate-limit response honors ``Retry-After`` up to a
  10-second cap (``retry_after_cap_seconds``) — waited for at most once per
  call, never more, regardless of ``max_attempts``. A ``Retry-After``
  exceeding the cap fails immediately, without waiting the full duration.
- A bare 5xx retries with a short exponential backoff (roughly 1s/2s/4s),
  bounded by ``max_attempts`` (default 3, per ``WorkerSettings.github_retry_max_attempts``).
- A connection-level failure (timeout, connection refused) before any
  response is received is retried the same way a bare 5xx is.
- A permanent error (401/403/404) is never retried — surfaced as fatal on
  the first response.
- ``max_attempts=0`` disables both retry paths wholesale: a would-be-retryable
  429 or bare 5xx each fail immediately on the first response, with no wait
  and no ``info``-level attempt log.

Logging, per the plan's Phase 4 Observability section: every retry
attempt logs at ``info`` with the call's target, attempt number, and reason
(``rate_limited``/``server_error``/``connection_error``) — ordinary,
expected-to-recover progress. Retry-budget exhaustion (a bare 5xx that
never recovers, or a 429 whose ``Retry-After`` exceeds the cap) logs at
``warning`` — a distinct, less common event from an individual attempt
being retried. A permanent error logs at ``error`` — a confirmed,
non-retryable problem. None of these log calls ever include
``request.headers`` (or any other part of the live ``httpx.Request``/
``Response`` object) — each builds its own log record from a fixed set of
redacted fields (target URL, attempt number, status/reason) instead, so an
``Authorization`` header carried by a caller's ``headers=`` kwarg can never
reach a log line even by accident. The live request object passed to
``httpx``'s transport is never touched or mutated — this module only reads
``response.status_code``/``response.headers.get("retry-after")``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NoReturn

import httpx
from pydantic import SecretStr

from adapter.github.exceptions import GitHubPermanentAPIError, GitHubRetryExhaustedError
from tenancy.resolution import split_owner_repo as split_owner_repo

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 1.0
_PERMANENT_ERROR_STATUS_CODES = frozenset({401, 403, 404})
_RATE_LIMIT_STATUS_CODE = 429

# The shared concurrency bound every ``adapter.github`` module's own
# per-item GitHub API fan-out uses, per
# ``docs/plans/plan-deliver-memory.md``'s Phase 4 section (Unit D):
# previously duplicated as an independent ``10`` in both
# ``adapter/github/diff.py``'s ``_DEFAULT_BLOB_FETCH_CONCURRENCY`` and
# ``findings/redact.py``'s ``_MAX_CONCURRENT_REDACTIONS`` — extracted here so
# ``diff.py`` and this phase's own ``adapter/github/reactions.py`` import one
# shared constant instead of perpetuating the duplication with a third
# independent ``10``. **Not** imported by ``findings/redact.py``, despite
# that module sharing the same value: ``findings`` is a domain package
# (``docs/rules/architecture.md``'s "domain never imports entrypoint or
# adapter code" rule), so it keeps its own local ``_MAX_CONCURRENT_REDACTIONS``
# constant — a deliberate, documented deviation from the plan's literal "all
# three call sites import it" text, since the alternative would cross a
# package boundary the project's own mandatory architecture rules forbid.
DEFAULT_FAN_OUT_CONCURRENCY = 10


def auth_headers(token: SecretStr) -> dict[str, str]:
    """The ``Authorization``/``Accept`` headers every ``adapter.github``
    GitHub API call needs — shared here rather than duplicated in
    ``diff.py`` and ``posting.py``, both of which already import this
    module for ``request_with_retry``.
    """
    return {
        "Authorization": f"Bearer {token.get_secret_value()}",
        "Accept": "application/vnd.github+json",
    }


# `split_owner_repo` used to be defined here; it now lives at
# `tenancy.resolution.split_owner_repo` (a domain module) since
# `adapter/storage/tenancy.py` — a different adapter package — needs the
# same parsing and importing it from here was an adapter-to-adapter
# dependency architecture.md's Package structure section rules out. The
# re-export above keeps this module's own existing callers (`diff.py`,
# `posting.py`, `auth.py`) unchanged.


@asynccontextmanager
async def client_context(
    client: httpx.AsyncClient | None, *, timeout: float
) -> AsyncIterator[httpx.AsyncClient]:
    """Yields ``client`` unchanged if given (a shared, long-lived client
    injected by the composition root — never closed here, since this call
    doesn't own its lifecycle), otherwise opens and tears down a throwaway
    ``httpx.AsyncClient`` scoped to this one call, matching every
    ``adapter.github`` call site's prior behavior when no client is
    injected.

    Centralizes the "shared client if we have one, else a fresh one"
    decision ``adapter/github/posting.py``, ``diff.py``, and ``auth.py``
    each need, so a shared client actually gets keep-alive/TLS-session
    reuse across calls within a job (and across the Worker's whole
    lifetime) instead of paying a fresh TCP+TLS handshake per GitHub API
    call.
    """
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=timeout) as fresh_client:
            yield fresh_client


def _backoff_delay_seconds(attempt: int) -> float:
    """1s/2s/4s/... for attempt=1/2/3/... — a short exponential backoff,
    per the plan's Phase 4 section.

    ``float(...)`` wraps the result explicitly: typeshed types
    ``int.__pow__``'s return as ``Any`` (a negative exponent returns
    ``float`` at runtime, a non-negative one returns ``int``, which isn't
    statically resolvable), so an unwrapped expression here would return
    ``Any`` from a function declared ``-> float`` under ``mypy --strict``.
    """
    return float(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def _parse_retry_after_seconds(response: httpx.Response) -> float | None:
    """GitHub's ``Retry-After`` header is documented as a number of
    seconds. Any other shape (missing, non-numeric, negative) is treated
    as "not safely retryable" — the caller's own value is None, matching a
    ``Retry-After`` that exceeds the cap: fail immediately, don't guess
    at a wait duration this helper can't actually parse. A negative value
    is rejected here rather than clamped to zero and retried instantly —
    ``asyncio.sleep`` with a negative delay returns near-immediately,
    which would hammer a service that just rate-limited this client.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


async def _log_retry_and_backoff(*, url: str, attempt: int, reason: str) -> None:
    """Shared "log the retry, then wait the exponential-backoff delay"
    step, used by both the connection-error and bare-server-error retry
    loops in ``request_with_retry`` below — same ``info``-level log shape
    and ``_backoff_delay_seconds`` wait, differing only in ``reason``. The
    429/rate-limit branch waits GitHub's own ``Retry-After`` value instead
    of the exponential backoff, so it isn't a fit for this helper.
    """
    logger.info(
        "github api call retrying",
        extra={"target": url, "attempt": attempt, "reason": reason},
    )
    await asyncio.sleep(_backoff_delay_seconds(attempt))


def _raise_retry_exhausted(
    *,
    url: str,
    attempt: int,
    reason: str,
    status_code: int | None,
    exc: Exception | None = None,
) -> NoReturn:
    """Shared "retry budget exhausted" log-then-raise, used by both the
    connection-error and bare-server-error retry loops in
    ``request_with_retry`` below — same ``warning``-level log shape and
    ``GitHubRetryExhaustedError`` raise, differing only in whether a
    ``status_code``/original exception is available to include.
    """
    extra: dict[str, object] = {"target": url, "attempt": attempt, "reason": reason}
    if status_code is not None:
        extra["status_code"] = status_code
    logger.warning("github api call retry budget exhausted", extra=extra)
    detail = str(exc) if exc is not None else f"HTTP {status_code}"
    if exc is not None:
        raise GitHubRetryExhaustedError(status_code=status_code, detail=detail) from exc
    raise GitHubRetryExhaustedError(status_code=status_code, detail=detail)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int,
    retry_after_cap_seconds: float = 10.0,
    safe_writes_only: bool = False,
    **kwargs: Any,
) -> httpx.Response:
    """Make one logical GitHub API call, transparently retrying a bare 5xx
    or a within-cap 429 per this module's own retry policy (see the module
    docstring). Returns the eventual successful (status < 400) response, or
    raises ``GitHubPermanentAPIError``/``GitHubRetryExhaustedError``.

    ``safe_writes_only`` (Phase 7, default ``False``): when ``True``,
    narrows retry eligibility to only the responses/failures GitHub is known
    to return *before* executing a request — see the module docstring's own
    ``safe_writes_only`` section for the full policy this implements.

    ``max_attempts`` is the number of *retries* this call is allowed
    (matching ``WorkerSettings.github_retry_max_attempts``'s own naming) —
    at ``max_attempts=3`` (the default), a persistently-failing call is
    attempted up to 4 times total (1 initial + 3 retries) before this
    function gives up. A 429/rate-limit response is retried at most once
    total regardless of ``max_attempts``'s value (as long as it's >= 1) —
    "treated as one bounded retry attempt for the purpose of this counter,"
    per the plan's Phase 4 section — never re-entering the bare-5xx backoff
    loop for the same call.

    ``**kwargs`` (``headers``, ``json``, ``content``, ...) are passed
    through to ``httpx.AsyncClient.request`` unchanged on every attempt —
    this function never inspects or mutates them, so a caller's
    ``Authorization`` header always reaches GitHub correctly even though
    this function's own logging never includes it (see the module
    docstring).
    """
    attempt = 1
    rate_limit_retry_used = False

    while True:
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            pre_send_failure = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
            if safe_writes_only and not pre_send_failure:
                logger.error(
                    "github api call failed with an ambiguous connection error; not "
                    "retrying (non-idempotent write)",
                    extra={
                        "target": url,
                        "attempt": attempt,
                        "reason": "ambiguous_connection_error",
                    },
                )
                raise GitHubRetryExhaustedError(status_code=None, detail=str(exc)) from exc
            if attempt > max_attempts:
                _raise_retry_exhausted(
                    url=url,
                    attempt=attempt,
                    reason="connection_error",
                    status_code=None,
                    exc=exc,
                )
            await _log_retry_and_backoff(url=url, attempt=attempt, reason="connection_error")
            attempt += 1
            continue

        if response.status_code < 400:
            return response

        if response.status_code in _PERMANENT_ERROR_STATUS_CODES:
            logger.error(
                "github api call failed permanently",
                extra={"target": url, "status_code": response.status_code},
            )
            raise GitHubPermanentAPIError(
                status_code=response.status_code, detail=f"HTTP {response.status_code}"
            )

        if response.status_code == _RATE_LIMIT_STATUS_CODE:
            retry_after = _parse_retry_after_seconds(response)
            can_retry = (
                not rate_limit_retry_used
                and max_attempts >= 1
                and retry_after is not None
                and retry_after <= retry_after_cap_seconds
            )
            if not can_retry:
                logger.warning(
                    "github api call rate-limited; not retrying",
                    extra={"target": url, "attempt": attempt, "status_code": 429},
                )
                raise GitHubRetryExhaustedError(status_code=429, detail="rate limited")
            rate_limit_retry_used = True
            logger.info(
                "github api call retrying",
                extra={"target": url, "attempt": attempt, "reason": "rate_limited"},
            )
            assert retry_after is not None  # narrowed by can_retry above
            await asyncio.sleep(retry_after)
            attempt += 1
            continue

        # Every other 4xx/5xx: treated as a bare server error, eligible for
        # the same exponential-backoff retry a 5xx gets — GitHub's own
        # non-429/permanent 4xx responses (rare, but possible) get no
        # special-cased handling of their own in this phase's scope. Never
        # retried at all under `safe_writes_only` (Phase 7) — the request
        # was already transmitted and GitHub already responded, so a retry
        # here risks a duplicate non-idempotent write.
        if safe_writes_only:
            logger.error(
                "github api call failed with a non-retryable server error; not retrying "
                "(non-idempotent write)",
                extra={
                    "target": url,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "reason": "non_idempotent_write_not_retried",
                },
            )
            raise GitHubRetryExhaustedError(
                status_code=response.status_code, detail=f"HTTP {response.status_code}"
            )
        if attempt > max_attempts:
            _raise_retry_exhausted(
                url=url,
                attempt=attempt,
                reason="server_error",
                status_code=response.status_code,
            )
        await _log_retry_and_backoff(url=url, attempt=attempt, reason="server_error")
        attempt += 1
