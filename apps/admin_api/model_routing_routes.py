"""apps.admin_api.model_routing_routes — ``GET /api/v1/model-routing``,
``PUT /api/v1/model-routing/{stage}`` (US-A14, US-A15).

``create_model_routing_router(...)`` follows
``apps/admin_api/config_routes.py``'s exact factory-router shape
(constructor injection, no module-level state), per
``docs/rules/architecture.md``'s constructor-injection rule.

The read route (US-A14) depends on ``require_session`` alone (Viewer
parity, per US-A5's second clause — viewing the effective model per stage
is not a mutating action); the write route (US-A15) depends on
``require_admin_role``. Every write is logged to ``admin_audit_log``.

Both routes are computed via the **same shared**
``llmgateway.routing.FallbackModelRoutingResolver`` instance
``apps/worker`` also uses — this module never re-derives the
org -> platform -> static-default fallback chain itself.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from adapter.storage.iam import PostgresAuditLogRepository
from adapter.storage.llm_routing import PostgresModelRoutingRepository
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    RequireAdminRoleDependency,
    RequireSessionDependency,
)
from llmgateway.routing import (
    KNOWN_MODEL_ROUTING_STAGES,
    FallbackModelRoutingResolver,
    ModelRoutingConfigRow,
    ModelRoutingSource,
    ModelRoutingStoreError,
)

logger = logging.getLogger(__name__)


class ModelRoutingStageView(BaseModel):
    """One stage's effective model + which tier served it — US-A14's own
    "effective model per stage, and which of the three sources it came
    from" requirement.
    """

    model_config = ConfigDict(frozen=True)

    stage: str
    model_name: str
    source: ModelRoutingSource


class ModelRoutingView(BaseModel):
    """``GET /api/v1/model-routing``'s full response — every known stage,
    each resolved at the requested scope.
    """

    model_config = ConfigDict(frozen=True)

    org_id: str | None
    stages: tuple[ModelRoutingStageView, ...]


class _SetModelRoutingRequest(BaseModel):
    """``PUT /api/v1/model-routing/{stage}``'s request body — US-A15's own
    "a stage and an optional org scope... an Admin sets a model name"
    wording. ``org_id=None`` writes the platform-wide row.
    """

    org_id: str | None = None
    model_name: str


def create_model_routing_router(
    *,
    resolver: FallbackModelRoutingResolver,
    repository: PostgresModelRoutingRepository,
    audit_log_repository: PostgresAuditLogRepository,
    require_session: RequireSessionDependency,
    require_admin_role: RequireAdminRoleDependency,
) -> APIRouter:
    """Build the always-registered model-routing router.

    A factory closing over its dependencies, matching
    ``apps/admin_api/config_routes.py``'s ``create_config_router`` shape —
    only called by ``apps/admin_api/main.py``. ``repository`` (the same
    ``PostgresModelRoutingRepository`` instance the shared ``resolver``
    reads from) is the write path's own storage adapter, passed separately
    since ``FallbackModelRoutingResolver`` itself exposes no write method
    (US-A15's write is not a fallback-chain read).
    """
    router = APIRouter(prefix="/api/v1/model-routing")

    @router.get("")
    async def get_model_routing(
        org_id: str | None = Query(default=None),
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> ModelRoutingView:
        """US-A14: the effective model per stage (``intent_summary``,
        ``review_generation``, ``verification``) at the requested scope —
        an org-specific row if one exists and ``org_id`` is given, else the
        platform-wide row, else today's static default. Gated by
        ``require_session`` alone (Viewer parity) — reading routing
        configuration is not a mutating action.
        """
        _ = authenticated
        stage_views: list[ModelRoutingStageView] = []
        for stage in KNOWN_MODEL_ROUTING_STAGES:
            resolution = await resolver.resolve_with_source(stage, org_id)
            stage_views.append(
                ModelRoutingStageView(
                    stage=stage, model_name=resolution.model_name, source=resolution.source
                )
            )
        return ModelRoutingView(org_id=org_id, stages=tuple(stage_views))

    @router.put("/{stage}")
    async def set_model_routing(
        stage: str,
        payload: _SetModelRoutingRequest,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> ModelRoutingConfigRow:
        """US-A15: writes/updates the ``ModelRoutingConfig`` row for
        ``stage`` at ``payload.org_id`` (``None`` = platform-wide),
        respecting ``uq_model_routing_org_stage``. ``stage`` must be one of
        ``KNOWN_MODEL_ROUTING_STAGES`` — an unknown stage is a 422, never
        silently written.
        """
        if stage not in KNOWN_MODEL_ROUTING_STAGES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown stage {stage!r}; must be one of {KNOWN_MODEL_ROUTING_STAGES}",
            )

        try:
            row = await repository.upsert(
                org_id=payload.org_id, stage=stage, model_name=payload.model_name
            )
        except ModelRoutingStoreError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        await audit_log_repository.record(
            actor_user_id=authenticated.user.id,
            action="model_routing_updated",
            resource_type="model_routing_config",
            resource_id=f"{payload.org_id or 'platform'}:{stage}",
            metadata={"stage": stage, "org_id": payload.org_id, "model_name": payload.model_name},
        )
        logger.info(
            "model routing updated",
            extra={
                "stage": stage,
                "org_id": payload.org_id,
                "model_name": payload.model_name,
                "actor": authenticated.user.username,
            },
        )
        return row

    return router
