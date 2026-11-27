"""Tests for adapter.telemetry.prometheus_reader — PrometheusMetricsReader
(US-A17), per docs/rules/testing.md's "GitHub adapter against a
recorded cassette (respx for httpx)" convention, extended here to this
reader's own Prometheus HTTP API calls (which reuse
adapter/github/http.py's shared retry/backoff wrapper) —
mirrors adapter/github/test_http.py's own respx-based shape.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

import adapter.github.http as http_module
from adapter.telemetry.exceptions import PrometheusUnreachableError
from adapter.telemetry.prometheus_reader import PrometheusMetricsReader

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_BASE_URL = "http://prometheus.internal:9090"
_QUERY_URL = f"{_BASE_URL}/api/v1/query"


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches adapter/github/test_http.py's own fixture — the bare-5xx
    exponential-backoff retry path under test doesn't need to actually wait
    real wall-clock seconds."""
    monkeypatch.setattr(http_module, "_backoff_delay_seconds", lambda attempt: 0.001)


@pytest.fixture(autouse=True)
def _reenable_http_logger() -> None:
    """Matches adapter/github/test_http.py's own fixture — see that
    module's docstring for why this is needed when this test file runs
    after a migration-touching test in the same `pytest -m integration`
    process."""
    logging.getLogger("adapter.github.http").disabled = False
    logging.getLogger("adapter.telemetry.prometheus_reader").disabled = False


def _success_body(result: list[dict[str, object]]) -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


@respx.mock
async def test_query_parses_a_single_label_free_sample() -> None:
    respx.get(_QUERY_URL).mock(
        return_value=httpx.Response(
            200, json=_success_body([{"metric": {}, "value": [1700000000.0, "42"]}])
        )
    )
    reader = PrometheusMetricsReader(base_url=_BASE_URL)

    samples = await reader.query("sum(review_queue_enqueued_total)")

    assert len(samples) == 1
    assert samples[0].labels == {}
    assert samples[0].value == 42.0


@respx.mock
async def test_query_parses_multiple_labeled_samples() -> None:
    respx.get(_QUERY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_success_body(
                [
                    {
                        "metric": {"severity": "high", "source": "llm"},
                        "value": [1700000000.0, "3"],
                    },
                    {
                        "metric": {"severity": "low", "source": "static_analysis"},
                        "value": [1700000000.0, "8"],
                    },
                ]
            ),
        )
    )
    reader = PrometheusMetricsReader(base_url=_BASE_URL)

    samples = await reader.query("sum by (severity, source) (review_findings_count_total)")

    assert len(samples) == 2
    by_severity = {sample.labels["severity"]: sample.value for sample in samples}
    assert by_severity == {"high": 3.0, "low": 8.0}


@respx.mock
async def test_query_with_empty_result_set_returns_empty_tuple_not_an_error() -> None:
    respx.get(_QUERY_URL).mock(return_value=httpx.Response(200, json=_success_body([])))
    reader = PrometheusMetricsReader(base_url=_BASE_URL)

    samples = await reader.query("increase(review_verification_judged_total[1h])")

    assert samples == ()


@respx.mock
async def test_query_sends_the_promql_as_a_query_param() -> None:
    route = respx.get(_QUERY_URL).mock(return_value=httpx.Response(200, json=_success_body([])))
    reader = PrometheusMetricsReader(base_url=_BASE_URL)

    await reader.query("sum(review_queue_enqueued_total)")

    assert route.calls.last.request.url.params["query"] == "sum(review_queue_enqueued_total)"


@respx.mock
async def test_unreachable_prometheus_raises_prometheus_unreachable_error() -> None:
    respx.get(_QUERY_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    reader = PrometheusMetricsReader(base_url=_BASE_URL, retry_max_attempts=1)

    with pytest.raises(PrometheusUnreachableError):
        await reader.query("sum(review_queue_enqueued_total)")


@respx.mock
async def test_persistent_server_error_exhausts_retry_and_raises_unreachable_error() -> None:
    respx.get(_QUERY_URL).mock(return_value=httpx.Response(503))
    reader = PrometheusMetricsReader(base_url=_BASE_URL, retry_max_attempts=1)

    with pytest.raises(PrometheusUnreachableError):
        await reader.query("sum(review_queue_enqueued_total)")


@respx.mock
async def test_malformed_response_body_raises_prometheus_unreachable_error() -> None:
    respx.get(_QUERY_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    reader = PrometheusMetricsReader(base_url=_BASE_URL)

    with pytest.raises(PrometheusUnreachableError):
        await reader.query("sum(review_queue_enqueued_total)")


@respx.mock
async def test_prometheus_reported_error_status_raises_prometheus_unreachable_error() -> None:
    respx.get(_QUERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "error",
                "errorType": "bad_data",
                "error": "invalid parameter 'query'",
            },
        )
    )
    reader = PrometheusMetricsReader(base_url=_BASE_URL)

    with pytest.raises(PrometheusUnreachableError):
        await reader.query("not valid promql (")


@respx.mock
async def test_query_never_hangs_past_its_bounded_retry_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistently unreachable Prometheus fails within a bounded number
    of attempts, never retries forever — the "never a hang" contract
    apps/admin_api/stats_routes.py depends on."""
    route = respx.get(_QUERY_URL).mock(return_value=httpx.Response(500))
    reader = PrometheusMetricsReader(base_url=_BASE_URL, retry_max_attempts=2)

    with (
        caplog.at_level("ERROR", logger="adapter.telemetry.prometheus_reader"),
        pytest.raises(PrometheusUnreachableError),
    ):
        await reader.query("sum(review_queue_enqueued_total)")

    # 1 initial attempt + 2 retries = 3 total calls, never unbounded.
    assert route.call_count == 3
    assert any("prometheus query failed" in record.message for record in caplog.records)
