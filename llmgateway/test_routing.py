"""Unit tests for llmgateway.routing: FallbackModelRoutingResolver's three-tier
fallback, plus a fake-repository-raises case asserting the resolver falls
open to the static default rather than propagating the error.

Per docs/rules/testing.md's unit tier: exercised against a fake
implementation of the consumer-defined `ModelRoutingConfigReader` Protocol,
not a real adapter — no `sqlalchemy`/`asyncpg` import anywhere in this file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from llmgateway.routing import (
    FallbackModelRoutingResolver,
    ModelRoutingConfigReader,
    ModelRoutingConfigRow,
)

_STAGE = "review_generation"
_STATIC_DEFAULT = "local-model"


class _FakeReader:
    """A fake `ModelRoutingConfigReader` returning canned rows keyed by
    `(org_id, stage)`, or raising a configured error, or hanging past a
    configured delay (to force the resolver's own timeout branch)."""

    def __init__(
        self,
        *,
        rows: dict[tuple[str | None, str], ModelRoutingConfigRow] | None = None,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._rows = rows or {}
        self._error = error
        self._delay_seconds = delay_seconds
        self.calls: list[tuple[str | None, str]] = []

    async def get(self, *, org_id: str | None, stage: str) -> ModelRoutingConfigRow | None:
        self.calls.append((org_id, stage))
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
        return self._rows.get((org_id, stage))


def _assert_is_reader(reader: object) -> None:
    """mypy already enforces Protocol conformance at type-check time; this
    runtime assertion just documents that `_FakeReader` is meant to satisfy
    `ModelRoutingConfigReader`, not merely resemble it."""
    assert isinstance(reader, ModelRoutingConfigReader)


# =============================================================================
# Three-tier fallback: org -> platform -> static default.
# =============================================================================


@pytest.mark.asyncio
async def test_org_specific_row_wins_when_present() -> None:
    reader = _FakeReader(
        rows={
            ("org-1", _STAGE): ModelRoutingConfigRow(
                org_id="org-1", stage=_STAGE, model_name="org-model"
            ),
            (None, _STAGE): ModelRoutingConfigRow(
                org_id=None, stage=_STAGE, model_name="platform-model"
            ),
        }
    )
    _assert_is_reader(reader)
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    resolution = await resolver.resolve_with_source(_STAGE, "org-1")

    assert resolution.model_name == "org-model"
    assert resolution.source == "org"


@pytest.mark.asyncio
async def test_platform_row_wins_when_no_org_specific_row() -> None:
    reader = _FakeReader(
        rows={
            (None, _STAGE): ModelRoutingConfigRow(
                org_id=None, stage=_STAGE, model_name="platform-model"
            )
        }
    )
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    resolution = await resolver.resolve_with_source(_STAGE, "org-1")

    assert resolution.model_name == "platform-model"
    assert resolution.source == "platform"


@pytest.mark.asyncio
async def test_static_default_wins_when_no_rows_at_all() -> None:
    reader = _FakeReader()
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    resolution = await resolver.resolve_with_source(_STAGE, "org-1")

    assert resolution.model_name == _STATIC_DEFAULT
    assert resolution.source == "static_default"


@pytest.mark.asyncio
async def test_platform_row_used_directly_when_org_id_is_none() -> None:
    """A platform-scoped call (``org_id=None``) never even attempts the org
    tier — proven by the reader's own call log staying single-entry."""
    reader = _FakeReader(
        rows={
            (None, _STAGE): ModelRoutingConfigRow(
                org_id=None, stage=_STAGE, model_name="platform-model"
            )
        }
    )
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    resolution = await resolver.resolve_with_source(_STAGE, None)

    assert resolution.model_name == "platform-model"
    assert resolution.source == "platform"
    assert reader.calls == [(None, _STAGE)]


@pytest.mark.asyncio
async def test_resolve_returns_bare_model_name() -> None:
    """``resolve`` (the plain entry point every Worker step calls) returns
    just the model name (``resolve(stage, org_id) -> str``)."""
    reader = _FakeReader()
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    model_name = await resolver.resolve(_STAGE, "org-1")

    assert model_name == _STATIC_DEFAULT


# =============================================================================
# Fail-open: the resolver must never let a reader failure or a timeout
# propagate to the calling job dispatch.
# =============================================================================


@pytest.mark.asyncio
async def test_repository_raising_falls_open_to_static_default() -> None:
    """A fake repository that raises: the resolver falls open to the static
    default rather than propagating the error."""
    reader = _FakeReader(error=RuntimeError("connection refused"))
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    resolution = await resolver.resolve_with_source(_STAGE, "org-1")

    assert resolution.model_name == _STATIC_DEFAULT
    assert resolution.source == "fallback_on_error"


@pytest.mark.asyncio
async def test_repository_raising_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    reader = _FakeReader(error=RuntimeError("connection refused"))
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    with caplog.at_level(logging.WARNING, logger="llmgateway.routing"):
        await resolver.resolve_with_source(_STAGE, "org-1")

    warnings = [r for r in caplog.records if "falling open" in r.message]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_lookup_exceeding_timeout_falls_open_to_static_default() -> None:
    reader = _FakeReader(delay_seconds=0.2)
    resolver = FallbackModelRoutingResolver(
        reader, static_default_model=_STATIC_DEFAULT, timeout_seconds=0.01
    )

    resolution = await resolver.resolve_with_source(_STAGE, "org-1")

    assert resolution.model_name == _STATIC_DEFAULT
    assert resolution.source == "fallback_on_error"


@pytest.mark.asyncio
async def test_resolve_never_raises_even_when_reader_does() -> None:
    """A job dispatch must never hang or fail because the routing lookup
    failed — ``resolve`` (not just ``resolve_with_source``) must also never
    propagate."""
    reader = _FakeReader(error=RuntimeError("boom"))
    resolver = FallbackModelRoutingResolver(reader, static_default_model=_STATIC_DEFAULT)

    model_name = await resolver.resolve(_STAGE, "org-1")

    assert model_name == _STATIC_DEFAULT


# =============================================================================
# Observability: review.llm.model_routing_source_total{source}.
# =============================================================================


@pytest.fixture
def in_memory_metric_reader(reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `llmgateway.routing`'s module-level `meter`/counter
    (created once, at import time) pick it up via OTel's proxy-instrument
    migration mechanism — mirrors
    `adapter/storage/test_queue_telemetry.py`'s identical fixture exactly.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


@pytest.mark.asyncio
async def test_resolve_with_source_increments_metric_with_source_label(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    fake_reader = _FakeReader()
    resolver = FallbackModelRoutingResolver(fake_reader, static_default_model=_STATIC_DEFAULT)

    await resolver.resolve_with_source(_STAGE, "org-1")

    metrics_data = in_memory_metric_reader.get_metrics_data()
    assert metrics_data is not None
    found = False
    for resource_metric in metrics_data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name != "review.llm.model_routing_source_total":
                    continue
                assert isinstance(metric.data, Sum)
                for point in metric.data.data_points:
                    if point.attributes == {"source": "static_default"}:
                        found = True
    assert found, "expected one review.llm.model_routing_source_total{source=static_default}"
