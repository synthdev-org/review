"""apps.admin_api.config_routes — ``GET /api/v1/config``,
``PUT /api/v1/config/{scope}/{key}`` (Unit E, US-A12, US-A13), per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 4 section.

``create_config_router(...)`` follows ``apps/admin_api/memory_routes.py``'s
exact factory-router shape (constructor injection, no module-level state),
per ``docs/rules/architecture.md``'s constructor-injection rule.

The read route (US-A12) depends on ``require_session`` alone (Viewer
parity, per US-A5's second clause — reading configuration is not a
mutating action, the same posture this plan already takes for the Memory
queue's list route); the write route (US-A13) depends on
``require_admin_role``. Every write is logged to ``admin_audit_log``, per
this phase's own Observability & rollback section — there is no kill
switch, since nothing yet reads ``OrgRepoTeamSetting`` for runtime
pipeline behavior (this phase only builds the admin round-trip).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    RequireAdminRoleDependency,
    RequireSessionDependency,
)
from tenancy.settings import (
    ConfigTree,
    ScopeDescriptorError,
    SettingRepository,
    SettingRow,
    SettingStoreError,
    SettingValidationError,
    parse_scope_descriptor,
)

logger = logging.getLogger(__name__)


class _SetConfigRequest(BaseModel):
    """``PUT /api/v1/config/{scope}/{key}``'s request body — US-A13's own
    "an Admin sets its value" wording: just the raw JSON object value,
    validated against ``tenancy.settings.SETTING_KEY_REGISTRY`` server-side
    (never trusted or coerced here).
    """

    value: dict[str, object]


def create_config_router(
    *,
    repository: SettingRepository,
    audit_log_repository: PostgresAuditLogRepository,
    require_session: RequireSessionDependency,
    require_admin_role: RequireAdminRoleDependency,
) -> APIRouter:
    """Build the always-registered org/repo/team configuration router.

    A factory closing over its dependencies, matching
    ``apps/admin_api/memory_routes.py``'s ``create_memory_router`` shape —
    only called by ``apps/admin_api/main.py``.
    """
    router = APIRouter(prefix="/api/v1/config")

    @router.get("")
    async def get_config(
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> ConfigTree:
        """US-A12: the full org → repo → team hierarchy, each scope's own
        literal settings, and platform-wide settings every scope falls
        back to. Gated by ``require_session`` alone (Viewer parity) —
        reading configuration is not a mutating action.
        """
        _ = authenticated
        return await repository.get_config_tree()

    @router.put("/{scope}/{key}")
    async def set_config(
        scope: str,
        key: str,
        payload: _SetConfigRequest,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> SettingRow:
        """US-A13: writes/updates ``key``'s value at ``scope`` (one of
        ``"platform"``, ``"org:<org_id>"``, ``"repo:<org_id>:<repo_id>"``,
        ``"team:<org_id>:<team_id>"`` — see
        ``tenancy.settings.parse_scope_descriptor``). A malformed ``scope``
        is a 400; an unknown ``key`` or a value that fails that key's own
        schema is a 422, never silently coerced.
        """
        try:
            org_id, repo_id, team_id = parse_scope_descriptor(scope)
        except ScopeDescriptorError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        try:
            row = await repository.upsert(
                org_id=org_id, repo_id=repo_id, team_id=team_id, key=key, value=payload.value
            )
        except SettingValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.detail) from exc
        except SettingStoreError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        await audit_log_repository.record(
            actor_user_id=authenticated.user.id,
            action="config_setting_updated",
            resource_type="org_repo_team_setting",
            resource_id=f"{scope}:{key}",
            metadata={"scope": scope, "key": key},
        )
        logger.info(
            "config setting updated",
            extra={"scope": scope, "key": key, "actor": authenticated.user.username},
        )
        return row

    return router
