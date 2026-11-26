"""Tests for adapter.github.http — the shared retry/backoff + redacted-
logging helper every real adapter.github GitHub API call routes through
per docs/rules/testing.md's "GitHub adapter against a recorded
cassette (respx for httpx)" convention.

Every test here monkeypatches ``adapter.github.http._backoff_delay_seconds``
to a tiny constant so the bare-5xx exponential-backoff cases run fast —
this doesn't touch the retry *logic* under test, only the real wall-clock
delay between attempts, matching how the 429 cases below use a tiny
``Retry-After`` header value for the same reason.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

import adapter.github.http as http_module
from adapter.github.exceptions import GitHubPermanentAPIError, GitHubRetryExhaustedError
from adapter.github.http import request_with_retry

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_URL = "https://api.github.com/repos/acme/widgets/things"


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_module, "_backoff_delay_seconds", lambda attempt: 0.001)


@pytest.fixture(autouse=True)
def _reenable_http_logger() -> None:
    """`adapter/storage`'s Alembic `env.py` calls `logging.config.fileConfig()`
    on every `migrated_engine` fixture setup (default
    `disable_existing_loggers=True`), which disables every already-created
    logger not explicitly listed in `alembic.ini` — including this module's
    own `adapter.github.http` logger, if a Postgres/migration-touching test
    happened to run earlier in the same `pytest -m integration` process
    (they do, per `pyproject.toml`'s `testpaths` ordering). Re-enabling here
    (autouse, every test in this file) keeps every `caplog`-asserting case
    below deterministic regardless of test order, mirroring
    `apps/webhook_api/test_webhook_routes.py`'s own per-test version of this
    same fix.
    """
    logging.getLogger("adapter.github.http").disabled = False


@respx.mock
async def test_rate_limited_response_honored_up_to_cap_and_eventually_succeeds() -> None:
    route = respx.get(_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.001"}),
        httpx.Response(200, json={"ok": True}),
    ]

    async with httpx.AsyncClient() as client:
        response = await request_with_retry(client, "GET", _URL, max_attempts=3)

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_rate_limited_retry_after_exceeding_cap_fails_without_retrying() -> None:
    route = respx.get(_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))

    with pytest.raises(GitHubRetryExhaustedError):
        async with httpx.AsyncClient() as client:
            await request_with_retry(
                client, "GET", _URL, max_attempts=3, retry_after_cap_seconds=10.0
            )

    # Never retried: exactly one call was made.
    assert route.call_count == 1


@respx.mock
async def test_bare_server_error_retried_with_backoff_and_eventually_succeeds() -> None:
    route = respx.get(_URL)
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(502),
        httpx.Response(200, json={"ok": True}),
    ]

    async with httpx.AsyncClient() as client:
        response = await request_with_retry(client, "GET", _URL, max_attempts=3)

    assert response.status_code == 200
    assert route.call_count == 3


@respx.mock
async def test_bare_server_error_exhausts_retry_budget_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.get(_URL).mock(return_value=httpx.Response(500))

    with (
        caplog.at_level("INFO", logger="adapter.github.http"),
        pytest.raises(GitHubRetryExhaustedError),
    ):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "GET", _URL, max_attempts=3)

    # 1 initial attempt + 3 retries = 4 total calls.
    assert route.call_count == 4
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "retry budget exhausted" in warnings[0].getMessage()
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_records) == 3  # one per retry attempt (not the initial try, not exhaustion)


@respx.mock
async def test_permanent_error_fails_on_first_response_no_retry_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.get(_URL).mock(return_value=httpx.Response(404))

    with (
        caplog.at_level("INFO", logger="adapter.github.http"),
        pytest.raises(GitHubPermanentAPIError),
    ):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "GET", _URL, max_attempts=3)

    assert route.call_count == 1
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert not warnings


@pytest.mark.parametrize("status_code", [401, 403, 404])
@respx.mock
async def test_every_permanent_status_code_is_fatal_without_retry(status_code: int) -> None:
    route = respx.get(_URL).mock(return_value=httpx.Response(status_code))

    with pytest.raises(GitHubPermanentAPIError):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "GET", _URL, max_attempts=3)

    assert route.call_count == 1


@respx.mock
async def test_each_retry_attempt_logs_info_with_attempt_number_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.get(_URL)
    route.side_effect = [httpx.Response(500), httpx.Response(200, json={"ok": True})]

    with caplog.at_level("INFO", logger="adapter.github.http"):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "GET", _URL, max_attempts=3)

    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_records) == 1
    assert info_records[0].__dict__["attempt"] == 1
    assert info_records[0].__dict__["reason"] == "server_error"


@respx.mock
async def test_max_attempts_zero_fails_rate_limited_call_immediately_with_no_retry_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.get(_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "0.001"}))

    with (
        caplog.at_level("INFO", logger="adapter.github.http"),
        pytest.raises(GitHubRetryExhaustedError),
    ):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "GET", _URL, max_attempts=0)

    assert route.call_count == 1
    assert not [r for r in caplog.records if r.levelname == "INFO"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


@respx.mock
async def test_max_attempts_zero_fails_server_error_call_immediately_with_no_retry_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.get(_URL).mock(return_value=httpx.Response(500))

    with (
        caplog.at_level("INFO", logger="adapter.github.http"),
        pytest.raises(GitHubRetryExhaustedError),
    ):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "GET", _URL, max_attempts=0)

    assert route.call_count == 1
    assert not [r for r in caplog.records if r.levelname == "INFO"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


@respx.mock
async def test_authorization_header_never_logged_but_still_sent_correctly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Proves both properties together: (a) no captured log record ever contains the raw header
    value, only the fixed redaction (i.e. simply never mentions it at
    all — this helper's own log records never include headers), and (b)
    the stub endpoint itself received the correct, unredacted header and
    the call succeeded — proving redaction (by omission) happens only in
    the logging path, never by mutating the live request.
    """
    secret_token = "ghs_live_super_secret_token_value"

    route = respx.get(_URL)
    route.side_effect = [httpx.Response(500), httpx.Response(200, json={"ok": True})]

    with caplog.at_level("INFO", logger="adapter.github.http"):
        async with httpx.AsyncClient() as client:
            response = await request_with_retry(
                client,
                "GET",
                _URL,
                max_attempts=3,
                headers={"Authorization": f"Bearer {secret_token}"},
            )

    assert response.status_code == 200
    sent_request = route.calls.last.request
    assert sent_request.headers["authorization"] == f"Bearer {secret_token}"

    for record in caplog.records:
        rendered = logging.Formatter().format(record)
        assert secret_token not in rendered
        for value in vars(record).values():
            assert secret_token not in str(value)


# --- `safe_writes_only`: idempotency-scoped retry eligibility ----
# for non-idempotent writes (adapter/github/posting.py's own call site).


@respx.mock
async def test_safe_writes_only_still_retries_rate_limited_response() -> None:
    """429/secondary-rate-limit is safe to retry even for a non-idempotent
    write — GitHub returns it without ever executing the request."""
    route = respx.post(_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.001"}),
        httpx.Response(201, json={"ok": True}),
    ]

    async with httpx.AsyncClient() as client:
        response = await request_with_retry(
            client, "POST", _URL, max_attempts=3, safe_writes_only=True
        )

    assert response.status_code == 201
    assert route.call_count == 2


@respx.mock
async def test_safe_writes_only_never_retries_bare_server_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare 5xx means the write may already have been transmitted and
    processed — never safe to retry for a non-idempotent POST, regardless
    of `max_attempts`."""
    route = respx.post(_URL).mock(return_value=httpx.Response(500))

    with (
        caplog.at_level("INFO", logger="adapter.github.http"),
        pytest.raises(GitHubRetryExhaustedError),
    ):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "POST", _URL, max_attempts=3, safe_writes_only=True)

    # Never retried: exactly one call was made, despite max_attempts=3.
    assert route.call_count == 1
    assert not [r for r in caplog.records if r.levelname == "INFO"]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "not retrying" in errors[0].getMessage()


@respx.mock
async def test_safe_writes_only_retries_pre_send_connect_error() -> None:
    """A connection that never even establishes (nothing transmitted) is
    still safe to retry, even for a non-idempotent write."""
    route = respx.post(_URL)
    route.side_effect = [httpx.ConnectError("connection refused"), httpx.Response(201)]

    async with httpx.AsyncClient() as client:
        response = await request_with_retry(
            client, "POST", _URL, max_attempts=3, safe_writes_only=True
        )

    assert response.status_code == 201
    assert route.call_count == 2


@respx.mock
async def test_safe_writes_only_never_retries_ambiguous_read_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read timeout may have occurred after the request was already
    transmitted and processed server-side — genuinely ambiguous, and never
    safe to retry for a non-idempotent write."""
    route = respx.post(_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    with (
        caplog.at_level("INFO", logger="adapter.github.http"),
        pytest.raises(GitHubRetryExhaustedError),
    ):
        async with httpx.AsyncClient() as client:
            await request_with_retry(client, "POST", _URL, max_attempts=3, safe_writes_only=True)

    assert route.call_count == 1
    assert not [r for r in caplog.records if r.levelname == "INFO"]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "ambiguous connection error" in errors[0].getMessage()
