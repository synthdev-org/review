"""
apps.admin_api.classifier_routes: the Admin surface of the classifier provider.

spec-classifier US-C1, US-C2, US-C3.

* ``create_classifier_write_validator`` is the per-key write hook
  ``config_routes.create_config_router`` runs for ``classifier_provider``: org scope
  only, the egress disclosure acknowledged before ``enabled`` saves (enforced here,
  not only in the UI), the host deny-list, then the credential binding.
* ``POST /api/v1/classifier/test-connection`` (``require_admin_role``) sends one fixed
  synthetic ``noul`` question (no customer data) through the same adapter path the
  Worker uses, and answers a closed-set status. The target is validated exactly like a
  write first, so the probe is never a token-delivery path to an unbound host; a
  failure is reported as a fixed message, never provider text or the token.

Spends the one shared operator key, so it shares the product-knowledge route's
``ProbeRateLimiter`` (keyed by session, user, org and pod).
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapter.classifier.typesafe import outcome_of
from adapter.egress.credentials import CredentialMap
from adapter.egress.egress import (
    AddressClass,
    canonicalize_host,
    classify_address,
    endpoint_origin,
)
from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    RequireAdminRoleDependency,
)
from apps.admin_api.probe_rate_limit import ProbeRateLimiter
from llmgateway.classifier import ClassificationRequest, Classifier, NoulQuestion
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from tenancy.settings import (
    CLASSIFIER_PROVIDER_KEY,
    SettingValidationError,
    validate_https_endpoint_url,
)

__all__ = [
    "ClassifierFactory",
    "ClassifierProbeStatus",
    "classifier_audit_view",
    "create_classifier_router",
    "create_classifier_write_validator",
]

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS: Final = 10.0
PROBE_STATE: Final = "The sky is blue."
PROBE_QUESTION: Final = "Does the text state that the sky is blue?"

ClassifierProbeStatus = Literal["ok", "unauthorized", "unreachable", "malformed", "rate_limited"]

_MESSAGES: Final[dict[str, str]] = {
    "ok": "Connected: the classifier answered the test question.",
    "unauthorized": "The provider rejected the credential.",
    "unreachable": "The provider could not be reached, or the egress guard refused the target.",
    "malformed": "The provider answered, but not in the shape the classifier contract requires.",
    "rate_limited": "The provider is rate limiting this key; try again later.",
}

_probes = metrics.get_meter("review.classifier").create_counter(
    "review.classifier.probe_total", description="Classifier test-connection probes by outcome."
)

ClassifierFactory = Callable[[str, str], Classifier]
"""``(endpoint_url, credential_ref) -> Classifier``; wired in ``apps/admin_api/main.py``."""


def _is_denied_host(host: str) -> bool:
    """Literal IPs must be public; names must not be local-only ones."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".localhost") or "." not in host
    return classify_address(host) is not AddressClass.PUBLIC


def target_problem(credentials: CredentialMap | None, endpoint_url: str, ref: str) -> str | None:
    """
    The write-time and probe-time target check; a stable code, or ``None``.

    The host deny-list runs first (loopback, private, link-local and metadata IPs, local
    names), then the credential must exist and be bound to this host and port.
    DNS rebinding and redirects are the egress guard's job at connection time.
    """
    origin = endpoint_origin(endpoint_url)
    if origin is None or _is_denied_host(canonicalize_host(origin[0]) or ""):
        return "endpoint_host_denied"
    if credentials is None:
        return "credential_map_unavailable"
    return credentials.binding_problem(ref, endpoint_url)


def create_classifier_write_validator(
    credentials: CredentialMap | None,
) -> Callable[[str, dict[str, object]], Awaitable[None]]:
    """The ``classifier_provider`` write hook (scope, registry-validated value)."""

    async def validate(scope: str, value: dict[str, object]) -> None:
        def refuse(detail: str) -> SettingValidationError:
            return SettingValidationError(key=CLASSIFIER_PROVIDER_KEY, detail=detail)

        if not scope.startswith("org:"):
            raise refuse("org_scope_only")
        if value.get("enabled") is True and value.get("disclosure_acknowledged") is not True:
            raise refuse("disclosure_not_acknowledged")
        if value.get("enabled") is not True:
            return  # disabling must always succeed, even after the credential is unbound
        problem = target_problem(
            credentials, str(value.get("endpoint_url")), str(value.get("credential_ref"))
        )
        if problem is not None:
            raise refuse(problem)

    return validate


def _host_of(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    return canonicalize_host(hostname) if hostname else None


def classifier_audit_view(value: dict[str, object] | None) -> dict[str, object] | None:
    """Audit summary: endpoint host, credential reference, enabled and mode only."""
    if value is None:
        return None
    endpoint = value.get("endpoint_url")
    return {
        "endpoint_host": _host_of(endpoint) if isinstance(endpoint, str) else None,
        "credential_ref": value.get("credential_ref"),
        "enabled": value.get("enabled"),
        "mode": value.get("mode"),
    }


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: Annotated[str, Field(min_length=1, max_length=64)]
    endpoint_url: str
    credential_ref: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]

    @field_validator("endpoint_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_https_endpoint_url(value)


class ProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ClassifierProbeStatus
    message: str
    latency_ms: int | None = None
    model: str | None = None


def create_classifier_router(
    *,
    credentials: CredentialMap | None,
    classifier_factory: ClassifierFactory,
    audit_log_repository: PostgresAuditLogRepository,
    require_admin_role: RequireAdminRoleDependency,
    limiter: ProbeRateLimiter,
    org_exists: Callable[[str], Awaitable[bool]],
    clock: Callable[[], float] = time.monotonic,
) -> APIRouter:
    """Build the classifier router; ``credentials=None`` means the map was malformed at startup.

    ``org_exists`` gates the payload ``org_id``: it keys a limiter window, so only a
    real org may reach it (admins are platform-wide; there is no per-admin org binding).
    """
    router = APIRouter(prefix="/api/v1/classifier")

    @router.post("/test-connection")
    async def test_connection(
        payload: ProbeRequest,
        response: Response,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> ProbeResult:
        if not await org_exists(payload.org_id):  # before any limiter window is touched
            raise HTTPException(status_code=422, detail="unknown_org")
        # pure local checks first: requests that never reach the wire must not burn quota
        problem = target_problem(credentials, payload.endpoint_url, payload.credential_ref)
        if problem is not None:
            raise HTTPException(status_code=422, detail=problem)
        limiter.charge(authenticated, response, org_id=payload.org_id)

        started = clock()
        status: ClassifierProbeStatus = "ok"
        try:
            await classifier_factory(payload.endpoint_url, payload.credential_ref).classify(
                ClassificationRequest(
                    model=payload.model,
                    state=PROBE_STATE,
                    questions={"probe": NoulQuestion(instructions=PROBE_QUESTION)},
                ),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except (LLMUnreachableError, LLMMalformedResponseError) as exc:
            status = outcome_of(exc)
        except Exception as exc:
            # Only the type is logged: exception text may carry provider bytes.
            logger.warning(
                "classifier test-connection failed unexpectedly",
                extra={"error_type": type(exc).__name__},
            )
            status = "unreachable"
        _probes.add(1, {"outcome": status})
        host = _host_of(payload.endpoint_url)
        await audit_log_repository.record(
            actor_user_id=authenticated.user.id,
            action="classifier_connection_tested",
            resource_type="classifier_provider",
            resource_id=host,
            metadata={
                "endpoint_host": host,
                "credential_ref": payload.credential_ref,
                "status": status,
            },
        )
        return ProbeResult(
            status=status,
            message=_MESSAGES[status],
            latency_ms=int((clock() - started) * 1000),
            model=payload.model if status == "ok" else None,
        )

    return router
