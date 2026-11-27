"""Test fixtures shared by this package's own test modules.

OTel's own ``trace.set_tracer_provider``/``metrics.set_meter_provider``
are "first call wins, every later call is silently ignored" by design (a
real ``Once()`` guard — correct for a real process, which only ever calls
``init_telemetry`` once, at startup) — see
``opentelemetry.trace._TRACER_PROVIDER_SET_ONCE`` /
``opentelemetry.metrics._internal._METER_PROVIDER_SET_ONCE``. Left alone,
only the *first* test in this whole process that calls
``init_telemetry(otlp_endpoint=...)`` with a real endpoint would ever
actually observe a provider getting registered — every later one would
silently no-op against whatever the first test's provider was.

OTel ships no public reset API for this; its own upstream test suite
resets these same private module attributes directly between tests — this
fixture does the same, restoring whatever the pre-test state was on
teardown via ``monkeypatch`` (never leaking a fake/test provider into a
test that didn't ask for one).
"""

from __future__ import annotations

from collections.abc import Iterator

import opentelemetry.metrics._internal as metrics_internal
import opentelemetry.trace as trace_internal
import pytest
from opentelemetry.util._once import Once

from adapter.telemetry import sdk as sdk_module


@pytest.fixture
def reset_otel_global_providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Lets a test call ``init_telemetry`` with a real endpoint and
    genuinely observe the resulting ``TracerProvider``/``MeterProvider``
    get registered globally, regardless of what any other test in the same
    process already registered — see this module's own docstring.

    Also resets ``sdk._TELEMETRY_INITIALIZED_THIS_PROCESS`` alongside
    OTel's own internal state: that flag exists precisely to make a
    *second* real ``init_telemetry`` call in the same process a no-op
    (see its own docstring), which is exactly what this fixture's whole
    purpose is to let a test do deliberately.
    """
    monkeypatch.setattr(trace_internal, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace_internal, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(sdk_module, "_TELEMETRY_INITIALIZED_THIS_PROCESS", False)
    yield
