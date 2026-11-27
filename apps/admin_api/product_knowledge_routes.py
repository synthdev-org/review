"""apps.admin_api.product_knowledge_routes: the Admin surface of the product-knowledge provider.

The Admin surface of the optional external product-knowledge provider (US-PK9, US-PK10,
US-PK11; Amendments A2 and A4).

* ``GET /api/v1/product-knowledge/credentials`` — ``require_session`` (Viewer
  parity). Credential *names* always; the hosts each is bound to only to an
  Admin; never a token (the parsed map is not even reachable from a response
  model: only ``ref`` and host strings are copied out).
* ``POST /api/v1/product-knowledge/test-connection`` — ``require_admin_role``.
  A2 ordering, so the probe is neither a token-delivery path nor a
  port-scan oracle:

  1. rate limit (per session, per authenticated user and global; 429),
  2. ``disabled`` when the kill switch is off or the credential map is
     unavailable,
  3. the write-time binding check — a ``credential_ref`` that is unknown or
     not bound to the target host is a 422 and **zero bytes are sent**,
  4. the target must match some credential's ``allowed_hosts`` or the
     operator allow-list, else one collapsed ``blocked_by_policy`` with no
     timing,
  5. only then the same adapter path the Worker uses (egress guard →
     negotiation bypassing the cache → one ``search`` of ``connection
     test``, ``top_k=1``).

The response carries only fields validated against the contract and a
review-generated ``message``; provider text never reaches it.

``create_provider_write_validator`` is the single write-time hook
``config_routes.create_config_router`` runs for the
``product_knowledge_provider`` key.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapter.egress.credentials import CredentialMap
from adapter.egress.egress import (
    canonicalize_host,
    endpoint_origin,
    host_matches_allowlist,
)
from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    RequireAdminRoleDependency,
    RequireSessionDependency,
)
from apps.admin_api.probe_rate_limit import ProbeRateLimiter
from iam.auth import is_admin
from review.context_builder import record_product_knowledge_call
from review.product_knowledge import (
    ProductKnowledgeError,
    ProductKnowledgeProvider,
    ProviderOutcome,
    normalize_results,
)
from tenancy.settings import (
    PRODUCT_KNOWLEDGE_PROVIDER_KEY,
    SettingValidationError,
    validate_endpoint_url,
)

__all__ = [
    "ProbeFactory",
    "ProbeStatus",
    "ProductKnowledgeRouterConfig",
    "create_product_knowledge_router",
    "create_provider_write_validator",
]

logger = logging.getLogger(__name__)

CONNECTION_TEST_QUERY: Final = "connection test"

ProbeStatus = Literal[
    "ok",
    "blocked_by_policy",
    "disabled",
    "unreachable",
    "no_contract",
    "malformed",
    "auth_failed",
    "unsupported_version",
    "deprecated_version",
    "timeout",
]

_MESSAGES: Final[dict[str, str]] = {
    "ok": "Connected: the provider speaks a supported contract version and answered a search.",
    "blocked_by_policy": (
        "Blocked by policy: the target is not bound to a credential's allowed_hosts or the "
        "operator endpoint allow-list, or the egress guard refused it. An unauthenticated "
        "provider (no credential) can only be tested when its host matches the operator "
        "endpoint allow-list. No request was sent to an unverified target."
    ),
    "disabled": (
        "Product knowledge is disabled or unavailable on the Admin API "
        "(kill switch off, or the credential map or endpoint allow-list is invalid - see the "
        "startup log)."
    ),
    "unreachable": "The provider could not be reached or returned an error.",
    "no_contract": "The endpoint does not expose the product-knowledge contract tools.",
    "malformed": "The provider answered, but not in the shape the contract requires.",
    "auth_failed": "The provider rejected the credential.",
    "unsupported_version": "The provider shares no contract version with this review build.",
    "deprecated_version": "Connected, but the negotiated contract version is deprecated.",
    "timeout": "The provider did not answer in time.",
}

_OUTCOME_STATUS: Final[dict[ProviderOutcome, ProbeStatus]] = {
    ProviderOutcome.TIMEOUT: "timeout",
    ProviderOutcome.UNREACHABLE: "unreachable",
    ProviderOutcome.AUTH_FAILED: "auth_failed",
    ProviderOutcome.BLOCKED_EGRESS: "blocked_by_policy",
    ProviderOutcome.CREDENTIAL_UNAVAILABLE: "blocked_by_policy",
    ProviderOutcome.UNSUPPORTED_VERSION: "unsupported_version",
    ProviderOutcome.MALFORMED: "malformed",
    ProviderOutcome.OVERSIZED: "malformed",
    ProviderOutcome.PROVIDER_ERROR: "unreachable",
}


class ProbeFactory(Protocol):
    """Builds the probe client for an endpoint (cache-bypassing).

    Wired at ``apps/admin_api/main.py``; this module never imports the adapter's client.
    """

    def __call__(self, endpoint_url: str, credential_ref: str | None) -> ProductKnowledgeProvider:
        """Build the probe client for ``endpoint_url``."""


class CredentialInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    # ``None`` for a Viewer: bound hosts are an Admin-only detail.
    allowed_hosts: list[str] | None


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: str
    credential_ref: Annotated[str | None, Field(pattern=r"^[a-z0-9_-]{1,64}$")] = None

    @field_validator("endpoint_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_endpoint_url(value)


class ProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ProbeStatus
    message: str
    negotiated_version: str | None = None
    provider_supported_versions: list[str] | None = None
    tools_present: list[str] | None = None
    latency_ms: int | None = None


def _host_of(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    return canonicalize_host(hostname) if hostname else None


def create_provider_write_validator(
    credentials: CredentialMap | None,
    endpoint_allowlist: tuple[str, ...] = (),
) -> Callable[[str, dict[str, object]], Awaitable[None]]:
    """The write-time hook for ``product_knowledge_provider`` (A2/US-PK9).

    Receives the scope descriptor and the registry-validated value. Rejects a
    team-scope write, an unknown ``credential_ref`` and a ``credential_ref``
    not bound to the endpoint's host (shared canonicaliser). With the
    credential map unavailable (malformed on this entrypoint) a reference
    cannot be verified, so it is refused rather than trusted.

    A null ``credential_ref`` (unauthenticated provider) is not exempt: its
    endpoint must match the operator allow-list or some credential's
    ``allowed_hosts`` (``unauthenticated_host_not_allowed``), the same rule
    test-connection and the job-time factory apply.
    """

    async def validate(scope: str, value: dict[str, object]) -> None:
        if scope.startswith("team:"):
            raise SettingValidationError(
                key=PRODUCT_KNOWLEDGE_PROVIDER_KEY, detail="team_scope_not_supported"
            )
        ref = value.get("credential_ref")
        endpoint = value.get("endpoint_url")
        if not isinstance(ref, str):
            problem = (credentials or CredentialMap.empty()).binding_problem(
                None, endpoint if isinstance(endpoint, str) else "", endpoint_allowlist
            )
            if problem is not None:
                raise SettingValidationError(key=PRODUCT_KNOWLEDGE_PROVIDER_KEY, detail=problem)
            return
        if credentials is None:
            raise SettingValidationError(
                key=PRODUCT_KNOWLEDGE_PROVIDER_KEY, detail="credential_map_unavailable"
            )
        problem = credentials.binding_problem(ref, endpoint if isinstance(endpoint, str) else "")
        if problem is not None:
            raise SettingValidationError(key=PRODUCT_KNOWLEDGE_PROVIDER_KEY, detail=problem)

    return validate


@dataclass(frozen=True, slots=True)
class ProductKnowledgeRouterConfig:
    """What the test-connection probe runs against.

    ``credentials=None`` means the map was malformed at startup.
    """

    credentials: CredentialMap | None
    enabled: bool
    endpoint_allowlist: tuple[str, ...]
    probe_factory: ProbeFactory


def create_product_knowledge_router(
    *,
    config: ProductKnowledgeRouterConfig,
    audit_log_repository: PostgresAuditLogRepository,
    require_session: RequireSessionDependency,
    require_admin_role: RequireAdminRoleDependency,
    clock: Callable[[], float] = time.monotonic,
    limiter: ProbeRateLimiter | None = None,
) -> APIRouter:
    """Build the product-knowledge router; ``credentials=None`` means a malformed map.

    The routes stay mounted (the UI needs a clear answer) but report disabled.
    """
    credentials = config.credentials
    router = APIRouter(prefix="/api/v1/product-knowledge")
    limiter = limiter or ProbeRateLimiter(clock=clock)

    @router.get("/credentials")
    async def list_credentials(
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> list[CredentialInfo]:
        if credentials is None:
            return []
        show_hosts = is_admin(authenticated.user)
        return [
            CredentialInfo(
                ref=ref,
                allowed_hosts=list(credentials.allowed_hosts(ref)) if show_hosts else None,
            )
            for ref in credentials.refs
        ]

    @router.post("/test-connection")
    async def test_connection(
        payload: ProbeRequest,
        response: Response,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> ProbeResult:
        return await _run_test_connection(
            config, audit_log_repository, limiter, clock, payload, response, authenticated
        )

    return router


async def _audit(
    audit_log_repository: PostgresAuditLogRepository,
    authenticated: AuthenticatedSession,
    *,
    host: str | None,
    ref: str | None,
    status: str,
) -> None:
    await audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="product_knowledge_connection_tested",
        resource_type="product_knowledge_provider",
        resource_id=host,
        metadata={"endpoint_host": host, "credential_ref": ref, "status": status},
    )
    logger.info(
        "product-knowledge connection tested",
        extra={
            "actor": authenticated.user.username,
            "endpoint_host": host,
            "credential_ref": ref,
            "status": status,
        },
    )


def _result(status: ProbeStatus, **fields: object) -> ProbeResult:
    return ProbeResult.model_validate({"status": status, "message": _MESSAGES[status], **fields})


async def _run_test_connection(
    config: ProductKnowledgeRouterConfig,
    audit_log_repository: PostgresAuditLogRepository,
    limiter: ProbeRateLimiter,
    clock: Callable[[], float],
    payload: ProbeRequest,
    response: Response,
    authenticated: AuthenticatedSession,
) -> ProbeResult:
    credentials = config.credentials
    # 1. Rate limit (per session, then global). Not audited: nothing ran.
    # Both limiters are checked before either is charged, so a global
    # rejection never burns a session slot. The global window is per
    # Admin API pod (in-memory), not cluster-wide.
    limiter.charge(authenticated, response)

    host = _host_of(payload.endpoint_url)
    ref = payload.credential_ref

    # 2. Kill switch / unavailable map.
    if not config.enabled or credentials is None:
        await _audit(audit_log_repository, authenticated, host=host, ref=ref, status="disabled")
        return _result("disabled")

    # 3. Write-time binding check FIRST: nothing is sent on mismatch.
    # (An unauthenticated provider has no binding to check; step 4 is
    # its host constraint, answered as ``blocked_by_policy``.)
    problem = credentials.binding_problem(ref, payload.endpoint_url) if ref is not None else None
    if problem is not None:
        await _audit(audit_log_repository, authenticated, host=host, ref=ref, status=problem)
        raise HTTPException(status_code=422, detail=problem)

    # 4. The target must be one the operator provisioned (host, and port
    # when the entry carries one).
    if not _target_is_provisioned(credentials, payload.endpoint_url, config.endpoint_allowlist):
        await _audit(
            audit_log_repository, authenticated, host=host, ref=ref, status="blocked_by_policy"
        )
        return _result("blocked_by_policy")

    # 5. Same adapter path as the Worker.
    status, fields = await _probe(config.probe_factory, payload, clock)
    await _audit(audit_log_repository, authenticated, host=host, ref=ref, status=status)
    return _result(status, **fields)


def _target_is_provisioned(
    credentials: CredentialMap, endpoint_url: str, endpoint_allowlist: tuple[str, ...]
) -> bool:
    origin = endpoint_origin(endpoint_url)
    return origin is not None and (
        credentials.any_binds(*origin)
        or host_matches_allowlist(origin[0], endpoint_allowlist, origin[1])
    )


async def _probe(
    probe_factory: ProbeFactory,
    payload: ProbeRequest,
    clock: Callable[[], float],
) -> tuple[ProbeStatus, dict[str, object]]:
    started = clock()
    version = "none"

    def record(operation: str, outcome: ProviderOutcome, since: float) -> None:
        record_product_knowledge_call(
            operation, outcome, version, int((clock() - since) * 1000), origin="test_connection"
        )

    step = started
    operation = "contract"
    try:
        provider = probe_factory(payload.endpoint_url, payload.credential_ref)
        negotiated = await provider.negotiate()
        version = negotiated.chosen_version or "unsupported"
        record("contract", ProviderOutcome.OK, step)
        if negotiated.chosen_version is None:
            if "product_knowledge_contract" not in negotiated.tools_present:
                return "no_contract", {}
            if not negotiated.supported_versions:
                return "malformed", {}
            return "unsupported_version", {}
        operation, step = "search", clock()
        normalized = normalize_results(await provider.search(CONNECTION_TEST_QUERY))
        record("search", ProviderOutcome.OK, step)
    except ProductKnowledgeError as exc:
        record(operation, exc.outcome, step)
        return _OUTCOME_STATUS.get(exc.outcome, "unreachable"), {}
    except Exception as exc:
        record(operation, ProviderOutcome.PROVIDER_ERROR, step)
        # Only the type is logged: exception text may carry provider bytes.
        logger.warning(
            "product-knowledge test-connection failed unexpectedly",
            extra={"error_type": type(exc).__name__},
        )
        return "unreachable", {}
    if normalized.outcome not in (ProviderOutcome.OK, ProviderOutcome.EMPTY):
        return "malformed", {}
    fields: dict[str, object] = {
        "negotiated_version": negotiated.chosen_version,
        "provider_supported_versions": list(negotiated.supported_versions),
        "tools_present": list(negotiated.tools_present),
        "latency_ms": int((clock() - started) * 1000),
    }
    return ("deprecated_version" if negotiated.deprecated else "ok"), fields
