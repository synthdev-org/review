"""Integration tests for apps/admin_api/stats_routes.py — GET /api/v1/stats
(Unit G, US-A17), driven through the real apps/admin_api FastAPI app via
TestClient, against a real Postgres via testcontainers-python (for login,
per docs/rules/testing.md's mandatory DB-integration rule) with Prometheus's
own HTTP API stubbed via respx, per
docs/plans/plan-deliver-admin-gui.md's Phase 6 "Tests" section:

"stubbed Prometheus-shaped HTTP responses asserting all four categories
present and correctly windowed; an unreachable-Prometheus case asserting
the clean-5xx behavior and the ERROR-level log."
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"
_PROMETHEUS_BASE_URL = "http://prometheus-test.internal:9090"
_QUERY_URL = f"{_PROMETHEUS_BASE_URL}/api/v1/query"


def _settings(*, dsn: str, **overrides: object) -> AdminAPISettings:
    defaults: dict[str, object] = {
        "environment": "development",
        "database_url": SecretStr(dsn),
        "admin_bootstrap_username": _BOOTSTRAP_USERNAME,
        "admin_bootstrap_password": SecretStr(_BOOTSTRAP_PASSWORD),
        "admin_csrf_secret": SecretStr("integration-test-csrf-secret"),
        "session_cookie_secure": False,
        "prometheus_base_url": _PROMETHEUS_BASE_URL,
        "prometheus_retry_max_attempts": 1,
        "_env_file": None,
    }
    defaults.update(overrides)
    return AdminAPISettings(**defaults)  # type: ignore[arg-type]


def _dsn(migrated_engine: AsyncEngine) -> str:
    return str(migrated_engine.url.render_as_string(hide_password=False))


def _login(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/login",
        json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
    )


def _prometheus_response(result: list[dict[str, object]]) -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _sample(value: str, labels: dict[str, str] | None = None) -> dict[str, object]:
    return {"metric": labels or {}, "value": [1700000000.0, value]}


def _mock_every_stats_query(
    *,
    queue: list[dict[str, object]],
    llm_tokens: list[dict[str, object]],
    judged: list[dict[str, object]],
    rejected: list[dict[str, object]],
    unmatched: list[dict[str, object]],
    findings: list[dict[str, object]],
) -> None:
    """Routes each of the six distinct PromQL query strings
    ``apps/admin_api/stats_service.py`` issues to its own canned response —
    matched by the ``query`` URL param since respx's plain URL match can't
    distinguish six calls to the same endpoint otherwise.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if "review_queue_enqueued_total" in query:
            return httpx.Response(200, json=_prometheus_response(queue))
        if "review_llm_tokens_total" in query:
            return httpx.Response(200, json=_prometheus_response(llm_tokens))
        if "review_verification_judged_total" in query:
            return httpx.Response(200, json=_prometheus_response(judged))
        if "review_verification_rejected_total" in query:
            return httpx.Response(200, json=_prometheus_response(rejected))
        if "review_verification_unmatched_total" in query:
            return httpx.Response(200, json=_prometheus_response(unmatched))
        if "review_findings_count_total" in query:
            return httpx.Response(200, json=_prometheus_response(findings))
        raise AssertionError(f"unexpected PromQL query: {query!r}")

    respx.get(_QUERY_URL).mock(side_effect=_handler)


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `apps.admin_api.stats_routes`'s module-level
    `_prometheus_unreachable_counter` (created once, at import time) picks
    it up via OTel's proxy-instrument migration mechanism — mirrors
    `adapter/storage/test_queue_telemetry.py`'s identical fixture.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-admin-api-test"}),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _sum_points(reader: InMemoryMetricReader, metric_name: str) -> list[float]:
    """Mirrors `adapter/storage/test_queue_telemetry.py`'s identical
    helper — see that module's own docstring for why `None` means "nothing
    recorded yet", not a setup bug."""
    metrics_data = reader.get_metrics_data()
    points: list[float] = []
    if metrics_data is None:
        return points
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append(data_point.value)
    return points


@pytest.fixture(autouse=True)
def _reenable_http_logger() -> None:
    """Matches adapter/github/test_http.py's own fixture — see that
    module's docstring for why this is needed alongside a migration-
    touching Postgres fixture in the same `pytest -m integration` process.
    """
    logging.getLogger("adapter.telemetry.prometheus_reader").disabled = False
    logging.getLogger("apps.admin_api.stats_routes").disabled = False


@respx.mock
def test_get_stats_returns_all_four_categories_correctly_windowed(
    migrated_engine: AsyncEngine,
) -> None:
    _mock_every_stats_query(
        queue=[_sample("5")],
        llm_tokens=[
            _sample(
                "1200",
                {"stage": "review_generation", "model": "local-model", "token_type": "prompt"},
            )
        ],
        judged=[_sample("9")],
        rejected=[_sample("1")],
        unmatched=[_sample("0")],
        findings=[_sample("3", {"severity": "high", "source": "llm"})],
    )

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        response = client.get("/api/v1/stats")

    assert response.status_code == 200
    body = response.json()

    assert body["queue_depth"] == {"depth": 5, "window": "current"}

    assert body["llm_token_usage"]["window"] == "1h"
    assert body["llm_token_usage"]["entries"] == [
        {
            "stage": "review_generation",
            "model": "local-model",
            "token_type": "prompt",
            "tokens": 1200,
        }
    ]

    assert body["verification_funnel"] == {
        "judged": 9,
        "rejected": 1,
        "unmatched": 0,
        "window": "1h",
    }

    assert body["findings_by_severity"]["window"] == "1h"
    assert body["findings_by_severity"]["entries"] == [
        {"severity": "high", "source": "llm", "count": 3}
    ]


@respx.mock
def test_get_stats_without_a_session_returns_401(migrated_engine: AsyncEngine) -> None:
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.get("/api/v1/stats")

    assert response.status_code == 401


@respx.mock
def test_get_stats_with_unreachable_prometheus_returns_clean_5xx_and_logs_error(
    migrated_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(_QUERY_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        # Re-enabled here, not only by the module's autouse fixture:
        # `migrated_engine`'s own alembic `fileConfig()` call (disabling
        # every not-explicitly-listed logger) runs as part of this same
        # test's fixture setup, *after* the autouse fixture above already
        # ran — mirrors apps/webhook_api/test_webhook_routes.py's own
        # identical per-test fix for this exact ordering gotcha.
        logging.getLogger("apps.admin_api.stats_routes").disabled = False
        with caplog.at_level("ERROR", logger="apps.admin_api.stats_routes"):
            response = client.get("/api/v1/stats")

    assert response.status_code == 502
    assert "prometheus" in response.json()["detail"].lower()
    assert any("prometheus unreachable" in record.message for record in caplog.records)


@respx.mock
def test_get_stats_with_unreachable_prometheus_increments_the_counter(
    migrated_engine: AsyncEngine,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    respx.get(_QUERY_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        response = client.get("/api/v1/stats")

    assert response.status_code == 502
    points = _sum_points(in_memory_metric_reader, "review.stats.prometheus_unreachable_total")
    assert points == [1.0]
