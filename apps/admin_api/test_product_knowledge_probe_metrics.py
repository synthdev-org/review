"""Unit test: the Admin API test-connection probe records the shared
``review.product_knowledge.*`` instruments with ``origin=test_connection``
(US-PK14; the closed ``origin`` set is {worker, test_connection})."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from apps.admin_api.product_knowledge_routes import ProbeRequest, _probe
from review.product_knowledge import NegotiationResult


class _OkProvider:
    async def negotiate(self) -> NegotiationResult:
        return NegotiationResult(
            chosen_version="1",
            supported_versions=["1"],
            tools_present=["product_knowledge_contract", "product_knowledge_search"],
        )

    async def search(self, query: str) -> object:
        return {"results": []}

    async def fetch(self, item_id: str) -> object:
        raise AssertionError("not used")


@pytest.fixture
def reader(reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


@pytest.mark.asyncio
async def test_probe_records_call_metrics_with_test_connection_origin(
    reader: InMemoryMetricReader,
) -> None:
    status, _ = await _probe(
        lambda url, ref: _OkProvider(),
        ProbeRequest(endpoint_url="https://wiki.example.com/mcp", credential_ref=None),
        lambda: 0.0,
    )

    assert status == "ok"
    data = reader.get_metrics_data()
    assert data is not None
    points = [
        dict(p.attributes or {})
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.product_knowledge.call_total"
        for p in m.data.data_points
    ]
    assert {(p["operation"], p["origin"]) for p in points} == {
        ("contract", "test_connection"),
        ("search", "test_connection"),
    }
