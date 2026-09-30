"""apps.admin_api.usage_routes — ``GET /api/v1/usage`` (Unit H, US-A18),
per ``docs/plans/plan-deliver-admin-gui.md``'s Phase 7 section.

``create_usage_router(...)`` follows ``apps/admin_api/stats_routes.py``'s
exact factory-router shape (constructor injection, no module-level state),
per ``docs/rules/architecture.md``'s constructor-injection rule.

Depends on ``require_session`` alone (Viewer parity, per US-A5's second
clause — viewing the usage dashboard is not a mutating action, the same
posture every other read route in this plan already takes). No kill
switch, no write path: this endpoint is itself observability/disclosure
tooling with no side effects, per this phase's own Observability & rollback
section — mirrors ``apps/admin_api/stats_routes.py``'s own "no kill switch"
posture exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from apps.admin_api.dependencies import AuthenticatedSession, RequireSessionDependency
from tenancy.usage import FREE_TIER_DAILY_REVIEW_LIMIT, UsageCounterReader


class UsageOrgView(BaseModel):
    """One connected installation's current-day usage row — US-A18's own
    "each connected org's count for the current 04:00-04:00 window" plus
    the over-threshold marker its second Given/When/Then requires.
    """

    model_config = ConfigDict(frozen=True)

    installation_id: int
    org_id: str | None
    org_name: str | None
    day: str
    count: int
    over_threshold: bool


class UsageView(BaseModel):
    """``GET /api/v1/usage``'s full response shape — every connected
    installation with at least one counted review today, plus the
    free-tier threshold they're being compared against (informational
    only, per ADR-0008's Decision — nothing reads this field to gate
    behavior).
    """

    model_config = ConfigDict(frozen=True)

    threshold: int
    orgs: tuple[UsageOrgView, ...]


def create_usage_router(
    *,
    reader: UsageCounterReader,
    require_session: RequireSessionDependency,
) -> APIRouter:
    """Build the always-registered usage router.

    A factory closing over its dependencies, matching
    ``apps/admin_api/stats_routes.py``'s ``create_stats_router`` shape —
    only called by ``apps/admin_api/main.py``.
    """
    router = APIRouter(prefix="/api/v1/usage")

    @router.get("")
    async def get_usage(
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> UsageView:
        """US-A18: every connected installation's current 04:00-anchored
        day's private-repo review count, each marked ``over_threshold``
        when it exceeds the free-tier guideline (20/day, per ADR-0008) —
        informational only, no functionality gated by it (US-A18's second
        Given/When/Then).
        """
        _ = authenticated
        rows = await reader.list_current_day_counts(now=datetime.now(UTC))
        return UsageView(
            threshold=FREE_TIER_DAILY_REVIEW_LIMIT,
            orgs=tuple(
                UsageOrgView(
                    installation_id=row.installation_id,
                    org_id=row.org_id,
                    org_name=row.org_name,
                    day=row.day.isoformat(),
                    count=row.count,
                    over_threshold=row.over_threshold,
                )
                for row in rows
            ),
        )

    return router
