"""Unit tests for the classifier metrics recorder (closed labels, no content,
failures suppressed)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401
from llmgateway import classifier_telemetry as t
from llmgateway.classifier import ClassifierUsage


@pytest.fixture
def reader(reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:  # noqa: F811
    r = InMemoryMetricReader()
    provider = MeterProvider(resource=Resource.create({"service.name": "t"}), metric_readers=[r])
    metrics.set_meter_provider(provider)
    yield r
    provider.shutdown()


def _points(reader: InMemoryMetricReader, name: str) -> list[dict[str, object]]:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        dict(p.attributes or {})
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == name
        for p in m.data.data_points
    ]


def test_every_recorder_emits_only_its_closed_labels(reader: InMemoryMetricReader) -> None:
    t.record_call(backend="fake", outcome="ok", duration_ms=12.0)
    t.record_token_usage(ClassifierUsage(input_tokens=5, output_tokens=1), backend="fake")
    t.record_verdict(criterion="valid", decision="yes")
    t.record_shadow_agreement(criterion="valid", outcome="agree")
    t.record_llm_fallback(reason="uncertain")

    assert _points(reader, "review.classifier.calls_total") == [
        {"backend": "fake", "outcome": "ok"}
    ]
    assert _points(reader, "review.classifier.call_duration_ms") == [
        {"backend": "fake", "outcome": "ok"}
    ]
    assert sorted(
        str(p["token_type"]) for p in _points(reader, "review.classifier.tokens_total")
    ) == ["input", "output"]
    assert _points(reader, "review.classifier.verdict_total") == [
        {"criterion": "valid", "decision": "yes"}
    ]
    assert _points(reader, "review.classifier.shadow_agreement_total") == [
        {"criterion": "valid", "outcome": "agree"}
    ]
    assert _points(reader, "review.verification.llm_fallback_total") == [{"reason": "uncertain"}]


def test_a_recording_failure_is_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Boom:
        def add(self, *_: object) -> None:
            raise RuntimeError("exporter down")

    monkeypatch.setattr(t, "_calls", _Boom())

    t.record_call(backend="fake", outcome="ok", duration_ms=1.0)

    assert "classifier call telemetry failed" in caplog.text
