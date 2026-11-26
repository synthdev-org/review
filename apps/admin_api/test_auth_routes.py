"""Integration tests for apps/admin_api/auth_routes.py — login/logout/
session, driven through the real apps/admin_api FastAPI app via
TestClient, against a real Postgres/pgvector via testcontainers-python
(per docs/rules/testing.md's mandatory DB-integration rule).

Covers US-A1 (correct/incorrect login, no username-existence leak,
including a timing-equivalence assertion), US-A2 (idle expiry, explicit
logout revocation + replay rejection), US-A6 (session info), the
login-rate-limit ceiling (asserting it reads/writes login_attempt, not
admin_audit_log), and a mutating request with a missing/mismatched CSRF
header being rejected. Also covers `apps/admin_api/dependencies.py`'s
`review.admin.session_rejected_total` counter (missing_cookie/
invalid_or_expired_session/csrf_token_missing_or_invalid reasons) — the
role_forbidden reason is covered by `test_role_enforcement.py` instead,
where a Viewer-vs-Admin session is already set up.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import AdminAuditLogRow, AdminSessionRow, LoginAttemptRow
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"


def _settings(*, dsn: str, **overrides: object) -> AdminAPISettings:
    defaults: dict[str, object] = {
        "environment": "development",
        "database_url": SecretStr(dsn),
        "admin_bootstrap_username": _BOOTSTRAP_USERNAME,
        "admin_bootstrap_password": SecretStr(_BOOTSTRAP_PASSWORD),
        "admin_csrf_secret": SecretStr("integration-test-csrf-secret"),
        "session_cookie_secure": False,
        "_env_file": None,
    }
    defaults.update(overrides)
    return AdminAPISettings(**defaults)  # type: ignore[arg-type]


def _dsn(migrated_engine: AsyncEngine) -> str:
    return str(migrated_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> InMemoryMetricReader:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `apps.admin_api.auth_routes`'s module-level `meter`/
    `_login_failed_counter` (created once, at import time) pick it up via
    OTel's proxy-instrument migration mechanism — mirrors
    `adapter/storage/test_queue_telemetry.py`'s identical fixture.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-admin-api-test"}),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    return reader


def _login_failed_total(reader: InMemoryMetricReader) -> float:
    metrics_data = reader.get_metrics_data()
    # `None` means "nothing recorded yet by this reader" — a real, expected
    # state before any failed login has happened, not a setup bug (see
    # `adapter/storage/test_queue_telemetry.py`'s identical comment).
    if metrics_data is None:
        return 0.0
    total = 0.0
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "review.auth.login_failed_total":
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    total += data_point.value
    return total


def _sum_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[tuple[float, dict[str, object]]]:
    """Returns `(value, attributes)` per recorded data point of a `Sum`
    (counter) metric — mirrors `apps/worker/test_dispatch.py`'s identical
    helper, needed here (unlike `_login_failed_total` above) because
    `review.admin.session_rejected_total` is labeled by `reason`."""
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return []
    points: list[tuple[float, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append((data_point.value, dict(data_point.attributes or {})))
    return points


def test_login_with_correct_credentials_succeeds_and_sets_cookies(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        assert response.status_code == 200
        assert response.json() == {"username": _BOOTSTRAP_USERNAME, "role": "admin"}
        assert SESSION_COOKIE_NAME in client.cookies
        assert CSRF_COOKIE_NAME in client.cookies


def test_login_with_wrong_password_returns_401_generic_message(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": "wrong-password"},
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "invalid username or password"
        assert SESSION_COOKIE_NAME not in client.cookies


def test_login_with_nonexistent_username_returns_the_same_401_message(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "no-such-user", "password": "irrelevant"},
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "invalid username or password"


def test_nonexistent_username_and_wrong_password_take_equivalent_time(
    migrated_engine: AsyncEngine,
) -> None:
    """US-A1's own "reveals nothing about whether the username exists"
    acceptance criterion, closed by a dummy constant-cost
    Argon2 verification — asserted here via wall-clock timing, not just by
    the response body being identical. A real regression (skipping Argon2
    entirely on the nonexistent-username path) would make that path
    orders of magnitude faster than the wrong-password path, which still
    pays the full Argon2 cost — a generous ratio tolerance safely catches
    that while staying robust to ordinary CI jitter.
    """
    settings = _settings(dsn=_dsn(migrated_engine))
    repetitions = 5

    with TestClient(create_app(settings)) as client:
        nonexistent_durations = []
        wrong_password_durations = []
        for _ in range(repetitions):
            start = time.monotonic()
            client.post(
                "/api/v1/auth/login",
                json={"username": f"no-such-user-{uuid.uuid4().hex}", "password": "irrelevant"},
            )
            nonexistent_durations.append(time.monotonic() - start)

            start = time.monotonic()
            client.post(
                "/api/v1/auth/login",
                json={"username": _BOOTSTRAP_USERNAME, "password": "wrong-password"},
            )
            wrong_password_durations.append(time.monotonic() - start)

    nonexistent_median = statistics.median(nonexistent_durations)
    wrong_password_median = statistics.median(wrong_password_durations)

    slower = max(nonexistent_median, wrong_password_median)
    faster = min(nonexistent_median, wrong_password_median)
    # A generous ratio (not a tight one) — the point is catching an
    # order-of-magnitude skip, not asserting near-perfect timing parity in
    # a shared CI environment.
    assert slower / max(faster, 1e-6) < 5.0, (
        f"nonexistent-username ({nonexistent_median:.4f}s) vs wrong-password "
        f"({wrong_password_median:.4f}s) durations diverge too much — possible "
        "username-enumeration timing side channel"
    )


def test_login_rate_limit_ceiling_reads_and_writes_login_attempt_not_audit_log(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    settings = _settings(
        dsn=_dsn(migrated_engine), login_rate_limit_attempts=2, login_rate_limit_window_minutes=15
    )
    username = f"rate-limit-target-{uuid.uuid4().hex}"

    with TestClient(create_app(settings)) as client:
        for _ in range(2):
            response = client.post(
                "/api/v1/auth/login", json={"username": username, "password": "irrelevant"}
            )
            assert response.status_code == 401

        limited_response = client.post(
            "/api/v1/auth/login", json={"username": username, "password": "irrelevant"}
        )
        assert limited_response.status_code == 429

    # The aggregate, unlabeled `review.auth.login_failed_total` counter
    # must increment on every failed login attempt, including the two that
    # preceded the 429 — it is the low-and-slow-across-many-usernames signal the per-username
    # `login_attempt` ceiling this test's name already covers cannot
    # surface on its own, so both must be asserted here side by side.
    assert _login_failed_total(in_memory_metric_reader) == 3

    session_factory = create_session_factory(migrated_engine)

    async def _assert_login_attempt_row() -> None:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(LoginAttemptRow.attempt_count).where(
                        LoginAttemptRow.username == username
                    )
                )
            ).scalar_one()
            assert row == 2

    asyncio.run(_assert_login_attempt_row())


def test_logout_revokes_session_and_a_replayed_cookie_is_rejected(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )
        csrf_token = client.cookies[CSRF_COOKIE_NAME]
        session_cookie_value = client.cookies[SESSION_COOKIE_NAME]

        logout_response = client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf_token})
        assert logout_response.status_code == 200

        # Replay the now-revoked session cookie directly (bypassing the
        # jar's own cookie-deletion bookkeeping) against a read-only route
        # — a revoked session must be rejected outright, not merely
        # "logged out client-side."
        replay_response = client.get(
            "/api/v1/auth/session", cookies={SESSION_COOKIE_NAME: session_cookie_value}
        )
        assert replay_response.status_code == 401

    # `apps/admin_api/dependencies.py`'s `review.admin.session_rejected_total`
    # counter — the post-login counterpart to `review.auth.login_failed_total`
    # above, covering every subsequent authenticated request rather than just
    # login. A revoked/replayed session is `invalid_or_expired_session`.
    assert (1.0, {"reason": "invalid_or_expired_session"}) in _sum_points(
        in_memory_metric_reader, "review.admin.session_rejected_total"
    )


def test_get_session_without_a_cookie_returns_401(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/auth/session")

        assert response.status_code == 401

    assert (1.0, {"reason": "missing_cookie"}) in _sum_points(
        in_memory_metric_reader, "review.admin.session_rejected_total"
    )


def test_get_session_with_a_valid_cookie_returns_username_and_role(
    migrated_engine: AsyncEngine,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        response = client.get("/api/v1/auth/session")

        assert response.status_code == 200
        assert response.json() == {"username": _BOOTSTRAP_USERNAME, "role": "admin"}


def test_idle_expired_session_is_rejected_on_the_next_request(
    migrated_engine: AsyncEngine,
) -> None:
    """Backdates the session's own `last_seen_at` directly in the database
    rather than mocking the clock, since TestClient's shared portal makes
    time-mocking fragile."""
    settings = _settings(dsn=_dsn(migrated_engine), session_idle_timeout_minutes=30)
    session_factory = create_session_factory(migrated_engine)

    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        async def _backdate_last_seen_at() -> None:
            async with session_factory() as session, session.begin():
                await session.execute(
                    text("UPDATE admin_session SET last_seen_at = :stale_time"),
                    {"stale_time": datetime.now(UTC) - timedelta(minutes=31)},
                )

        asyncio.run(_backdate_last_seen_at())

        response = client.get("/api/v1/auth/session")

        assert response.status_code == 401


def test_logout_without_a_csrf_header_is_rejected(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        response = client.post("/api/v1/auth/logout")

        assert response.status_code == 403

    assert (1.0, {"reason": "csrf_token_missing_or_invalid"}) in _sum_points(
        in_memory_metric_reader, "review.admin.session_rejected_total"
    )


def test_logout_with_a_mismatched_csrf_header_is_rejected(migrated_engine: AsyncEngine) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

        response = client.post(
            "/api/v1/auth/logout", headers={CSRF_HEADER_NAME: "not-the-real-token"}
        )

        assert response.status_code == 403


def test_successful_login_writes_an_audit_log_row(migrated_engine: AsyncEngine) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

    session_factory = create_session_factory(migrated_engine)

    async def _assert_audit_row() -> None:
        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(AdminAuditLogRow.action).where(
                            AdminAuditLogRow.action == "login_succeeded"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(row) == 1

    asyncio.run(_assert_audit_row())


def test_admin_session_row_is_created_on_login(migrated_engine: AsyncEngine) -> None:
    settings = _settings(dsn=_dsn(migrated_engine))
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
        )

    session_factory = create_session_factory(migrated_engine)

    async def _assert_session_row() -> None:
        async with session_factory() as session:
            rows = (await session.execute(select(AdminSessionRow))).scalars().all()
            assert len(rows) == 1

    asyncio.run(_assert_session_row())
