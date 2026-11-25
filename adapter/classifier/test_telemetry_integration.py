"""US-C8: metrics from a real adapter call over a recorded response carry closed-set
labels and no content."""

from __future__ import annotations

import httpx2
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from adapter.classifier import test_typesafe as t
from adapter.classifier.typesafe import RateLimitedError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _points(reader: InMemoryMetricReader) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = [dict(p.attributes) for p in metric.data.data_points]
    return out


async def test_call_metrics_have_closed_labels_and_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    import llmgateway.classifier_telemetry as tel

    meter = provider.get_meter("test")
    monkeypatch.setattr(tel, "_calls", meter.create_counter("review.classifier.calls_total"))
    monkeypatch.setattr(
        tel, "_duration", meter.create_histogram("review.classifier.call_duration_ms")
    )
    monkeypatch.setattr(tel, "_tokens", meter.create_counter("review.classifier.tokens_total"))

    await t.make(lambda r: httpx2.Response(200, json=t.noul_success())).classify(
        t.request_of(*t.NOUL_IDS), timeout=5
    )
    with pytest.raises(RateLimitedError):
        await t.make(lambda r: httpx2.Response(429)).classify(
            t.request_of(*t.NOUL_IDS), timeout=0.5
        )

    points = _points(reader)
    assert set(points) == {
        "review.classifier.calls_total",
        "review.classifier.call_duration_ms",
        "review.classifier.tokens_total",
    }
    allowed = {"backend", "outcome", "token_type"}
    for attrs in (a for pts in points.values() for a in pts):
        assert set(attrs) <= allowed
    outcomes = {a["outcome"] for a in points["review.classifier.calls_total"]}
    assert outcomes == {"ok", "rate_limited"}
    flat = str(points)
    assert t.TOKEN not in flat and "DIFF" not in flat and "api.typesafe" not in flat
