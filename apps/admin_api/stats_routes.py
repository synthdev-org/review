"""apps.admin_api.stats_routes — ``GET /api/v1/stats`` (US-A17).

``create_stats_router(...)`` follows ``apps/admin_api/model_routing_routes.py``'s
exact factory-router shape (constructor injection, no module-level state),
per ``docs/rules/architecture.md``'s constructor-injection rule.

Depends on ``require_session`` alone (Viewer parity, per US-A5's second
clause — reading the dashboard is not a mutating action, the same posture
every other read route already takes).

A thin handler: all query-building and derived-metric math lives in
``apps/admin_api/stats_service.py`` — this module's own job
is auth, the unreachable-Prometheus -> clean-5xx translation, and the
``review.stats.prometheus_unreachable_total`` observability
signal.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import metrics

from adapter.telemetry.exceptions import PrometheusUnreachableError
from apps.admin_api.dependencies import AuthenticatedSession, RequireSessionDependency
from apps.admin_api.stats_service import AdminStatsView, MetricsQuerier, get_admin_stats

logger = logging.getLogger(__name__)

meter = metrics.get_meter("review.stats")
_prometheus_unreachable_counter = meter.create_counter(
    "review.stats.prometheus_unreachable_total",
    description="Count of GET /api/v1/stats requests that failed because Prometheus could "
    "not be reached/queried successfully — an operator-alertable signal distinguishing "
    "'Prometheus had one slow request' from 'Prometheus has been down for an hour and "
    "nobody's opened the dashboard to notice.'",
)

_UNREACHABLE_DETAIL = (
    "the metrics backend (Prometheus) could not be reached or returned an unusable response"
)


def create_stats_router(
    *,
    querier: MetricsQuerier,
    require_session: RequireSessionDependency,
) -> APIRouter:
    """Build the always-registered admin-stats router.

    A factory closing over its dependencies, matching
    ``apps/admin_api/model_routing_routes.py``'s ``create_model_routing_router``
    shape — only called by ``apps/admin_api/main.py``.
    """
    router = APIRouter(prefix="/api/v1/stats")

    @router.get("")
    async def get_stats(
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> AdminStatsView:
        """US-A17: current queue depth, recent LLM token usage,
        verification funnel counts, and a findings-by-severity breakdown,
        each labeled with the time window it covers. If Prometheus is
        unreachable, returns a clean 5xx with a distinct error body, never
        a hang (Reliability NFR), logs at ``ERROR``, and increments
        ``review.stats.prometheus_unreachable_total``.
        """
        _ = authenticated
        try:
            return await get_admin_stats(querier)
        except PrometheusUnreachableError as exc:
            logger.error(
                "admin stats dashboard read failed: prometheus unreachable",
                extra={"error": str(exc)},
            )
            _prometheus_unreachable_counter.add(1)
            raise HTTPException(status_code=502, detail=_UNREACHABLE_DETAIL) from exc

    return router
