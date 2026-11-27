"""apps.admin_api.config_routes — ``GET /api/v1/config``,
``PUT /api/v1/config/{scope}/{key}`` (US-A12, US-A13).

``create_config_router(...)`` follows ``apps/admin_api/memory_routes.py``'s
exact factory-router shape (constructor injection, no module-level state),
per ``docs/rules/architecture.md``'s constructor-injection rule.

The read route (US-A12) depends on ``require_session`` alone (Viewer
parity, per US-A5's second clause — reading configuration is not a
mutating action, the same posture the Memory
queue's list route takes); the write route (US-A13) depends on
``require_admin_role``. Every write is logged to ``admin_audit_log``. There is
no kill switch, since nothing yet reads ``OrgRepoTeamSetting`` for runtime
pipeline behavior (this only builds the admin round-trip).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import metrics
from pydantic import BaseModel

from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.classifier_routes import classifier_audit_view
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    RequireAdminRoleDependency,
    RequireSessionDependency,
)
from tenancy.settings import (
    CLASSIFIER_PROVIDER_KEY,
    PLATFORM_OR_ORG_SCOPE_ONLY_KEYS,
    PRODUCT_KNOWLEDGE_PROVIDER_KEY,
    SETTING_KEY_REGISTRY,
    ConfigTree,
    ScopeDescriptorError,
    SettingRepository,
    SettingRow,
    SettingStoreError,
    SettingValidationError,
    parse_scope_descriptor,
    validate_setting_value,
)

logger = logging.getLogger(__name__)

meter = metrics.get_meter("review.config")

_config_write_total = meter.create_counter(
    "review.config.write_total",
    description="Admin config-setting writes, by registry key and outcome.",
)


def _record_write(key: str, outcome: str) -> None:
    # Only registry keys are labelled; an unknown key (a 422 typo) must not
    # become a label value.
    label = key if key in SETTING_KEY_REGISTRY else "unknown"
    _config_write_total.add(1, {"key": label, "outcome": outcome})


ProviderWriteValidator = Callable[[str, dict[str, object]], Awaitable[None]]
"""``(scope descriptor, registry-validated value) -> None``, raising
``SettingValidationError`` — a write-time hook, registered per setting key
(``classifier_provider`` is the second key)."""


def _provider_audit_view(value: dict[str, object] | None) -> dict[str, object] | None:
    """Return a reconstructable-but-secret-free summary of a provider setting.

    For the audit trail (Amendment A4): endpoint *host*, credential
    *reference*, enabled and allow_public_repos. Never a token (none is stored here) and
    never the full URL path.
    """
    if value is None:
        return None
    endpoint = value.get("endpoint_url")
    host: str | None = None
    if isinstance(endpoint, str):
        try:
            host = (urlsplit(endpoint).hostname or "").lower() or None
        except ValueError:
            host = None
    return {
        "endpoint_host": host,
        "credential_ref": value.get("credential_ref"),
        "enabled": value.get("enabled"),
        "allow_public_repos": value.get("allow_public_repos"),
    }


# Keys whose writes are audited with a secret-free old/new summary and run their
# registered write validator on the registry-validated value.
_AUDIT_VIEWS: dict[str, Callable[[dict[str, object] | None], dict[str, object] | None]] = {
    PRODUCT_KNOWLEDGE_PROVIDER_KEY: _provider_audit_view,
    CLASSIFIER_PROVIDER_KEY: classifier_audit_view,
}


class _SetConfigRequest(BaseModel):
    """``PUT /api/v1/config/{scope}/{key}``'s request body — US-A13's own
    "an Admin sets its value" wording: just the raw JSON object value,
    validated against ``tenancy.settings.SETTING_KEY_REGISTRY`` server-side
    (never trusted or coerced here).
    """

    value: dict[str, object]


async def _add_audit_values(
    repository: SettingRepository,
    write_validators: Mapping[str, ProviderWriteValidator] | None,
    metadata: dict[str, object],
    scope: str,
    key: str,
    value: dict[str, object],
) -> None:
    audit_view = _AUDIT_VIEWS.get(key)
    if audit_view is None:
        return
    org_id, repo_id, team_id = parse_scope_descriptor(scope)
    # Registry-validate first so the hook sees the canonical value;
    # the old value is read for the A4 audit trail.
    validated = validate_setting_value(key, value)
    validator = (write_validators or {}).get(key)
    if validator is not None:
        await validator(scope, validated)
    previous = [
        r
        for r in await repository.list_rows_for_key(key=key, org_id=org_id, repo_id=repo_id)
        if (r.org_id, r.repo_id, r.team_id) == (org_id, repo_id, team_id)
    ]
    metadata["old"] = audit_view(previous[0].value if previous else None)
    metadata["new"] = audit_view(validated)


async def _write_setting(
    repository: SettingRepository,
    audit_log_repository: PostgresAuditLogRepository,
    write_validators: Mapping[str, ProviderWriteValidator] | None,
    authenticated: AuthenticatedSession,
    scope: str,
    key: str,
    value: dict[str, object],
) -> SettingRow:
    try:
        org_id, repo_id, team_id = parse_scope_descriptor(scope)
    except ScopeDescriptorError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    if key in PLATFORM_OR_ORG_SCOPE_ONLY_KEYS and (repo_id is not None or team_id is not None):
        _record_write(key, "validation_rejected")
        raise HTTPException(status_code=422, detail="platform_or_org_scope_only")

    metadata: dict[str, object] = {"scope": scope, "key": key}
    try:
        await _add_audit_values(repository, write_validators, metadata, scope, key, value)
        row = await repository.upsert(
            org_id=org_id, repo_id=repo_id, team_id=team_id, key=key, value=value
        )
    except SettingValidationError as exc:
        _record_write(key, "validation_rejected")
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except SettingStoreError as exc:
        _record_write(key, "store_error")
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    _record_write(key, "ok")

    await audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="config_setting_updated",
        resource_type="org_repo_team_setting",
        resource_id=f"{scope}:{key}",
        metadata=metadata,
    )
    logger.info(
        "config setting updated",
        extra={"scope": scope, "key": key, "actor": authenticated.user.username},
    )
    return row


# Build the always-registered org/repo/team configuration router.
#
# A factory closing over its dependencies, matching
# ``apps/admin_api/memory_routes.py``'s ``create_memory_router`` shape —
# only called by ``apps/admin_api/main.py``.
def create_config_router(
    *,
    repository: SettingRepository,
    audit_log_repository: PostgresAuditLogRepository,
    require_session: RequireSessionDependency,
    require_admin_role: RequireAdminRoleDependency,
    write_validators: Mapping[str, ProviderWriteValidator] | None = None,
) -> APIRouter:
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
        return await _write_setting(
            repository,
            audit_log_repository,
            write_validators,
            authenticated,
            scope,
            key,
            payload.value,
        )

    return router
