"""adapter.telemetry.exceptions — typed exceptions for adapter.telemetry,
per docs/rules/architecture.md's "specific exception types per domain
concern" rule: callers catch these, never a raw ``httpx``/transport
exception — mirrors ``adapter/github/exceptions.py``'s ``GitHubAdapterError``
and ``adapter/storage/exceptions.py``'s ``StorageError`` pattern.
"""

from __future__ import annotations


class TelemetryAdapterError(Exception):
    """Base class for every error this adapter raises."""


# ---------------------------------------------------------------------------
# Phase 6 (docs/plans/plan-deliver-admin-gui.md's Phase 6 section, Unit G):
# Prometheus-reading adapter (adapter.telemetry.prometheus_reader)
# ---------------------------------------------------------------------------


class PrometheusQueryError(TelemetryAdapterError):
    """Base class for every failure querying Prometheus's HTTP API
    (``adapter.telemetry.prometheus_reader.PrometheusMetricsReader``)."""

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"Prometheus query failed: {detail}")
        self.detail = detail


class PrometheusUnreachableError(PrometheusQueryError):
    """Raised when a Prometheus ``/api/v1/query`` call could not be reached
    at all, exhausted its retry budget, or returned a permanent error — see
    ``adapter/github/http.py``'s own exception types (reused per
    Assumption 4), which this wraps. Also raised for an unparseable
    (malformed) response body or a well-formed response whose own ``status``
    field reports ``"error"`` — from a caller's perspective, "Prometheus did
    not give us a usable answer" is one failure mode regardless of which of
    these three specific causes produced it, and ``apps/admin_api/stats_routes.py``'s
    "clean 5xx, never a hang" contract treats them identically.
    """
