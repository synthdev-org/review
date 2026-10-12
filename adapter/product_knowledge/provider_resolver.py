"""adapter.product_knowledge.provider_resolver — the runtime reader of the
``product_knowledge_provider`` setting (plan Phase 6, Assumption 6; F6).

Mirrors ``llmgateway/routing.py``'s ``FallbackModelRoutingResolver``: bounded
by ``asyncio.wait_for``, broad-except fail-open, and a closed-set
``{source}`` counter so "Postgres blipped" is distinguishable from "not
configured". Reuses ``tenancy.settings.resolve_setting`` (the one precedence
implementation) and the registry model for validation - no second row shape.

Fail-open means *no provider context for this job*, never a failed review.
The Admin UI preview uses the strict config-tree path, not this one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final, Literal, Protocol

from opentelemetry import metrics
from pydantic import ValidationError

from review.product_knowledge import EffectiveProvider
from tenancy.settings import (
    PRODUCT_KNOWLEDGE_PROVIDER_KEY,
    ProductKnowledgeProviderSetting,
    SettingRow,
    resolve_setting,
)

__all__ = ["DEFAULT_RESOLUTION_TIMEOUT_SECONDS", "FailOpenProviderResolver"]

logger = logging.getLogger(__name__)

_counter = metrics.get_meter("review.product_knowledge").create_counter(
    "review.product_knowledge.config_resolution_total",
    description="Product-knowledge provider setting resolutions, by serving scope.",
)

DEFAULT_RESOLUTION_TIMEOUT_SECONDS: Final = 2.0

ResolutionSource = Literal[
    "platform", "org", "repo", "not_configured", "invalid", "fallback_on_error"
]


class SettingRowReader(Protocol):
    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]: ...


def _scope_of(row: SettingRow) -> ResolutionSource:
    if row.repo_id is not None:
        return "repo"
    return "org" if row.org_id is not None else "platform"


class FailOpenProviderResolver:
    """Implements ``review.product_knowledge.EffectiveProviderResolver``."""

    def __init__(
        self,
        reader: SettingRowReader,
        *,
        timeout_seconds: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ) -> None:
        self._reader = reader
        self._timeout = timeout_seconds

    async def resolve(self, org_id: str, repo_id: str) -> EffectiveProvider | None:
        source: ResolutionSource
        provider: EffectiveProvider | None = None
        try:
            rows = await asyncio.wait_for(
                self._reader.list_rows_for_key(
                    key=PRODUCT_KNOWLEDGE_PROVIDER_KEY, org_id=org_id, repo_id=repo_id
                ),
                timeout=self._timeout,
            )
            row = resolve_setting(
                PRODUCT_KNOWLEDGE_PROVIDER_KEY, rows, org_id=org_id, repo_id=repo_id
            )
            if row is None:
                source = "not_configured"
            else:
                source = _scope_of(row)
                try:
                    setting = ProductKnowledgeProviderSetting.model_validate(row.value)
                except ValidationError:
                    # An orphaned/hand-written row: treated as not configured
                    # (fail closed for the provider, open for the review).
                    source = "invalid"
                    logger.warning(
                        "product-knowledge provider setting is invalid; ignoring it",
                        extra={"org_id": org_id, "repo_id": repo_id, "scope": _scope_of(row)},
                    )
                else:
                    if setting.enabled:  # repo/org `enabled: false` is an opt-out
                        provider = EffectiveProvider(
                            enabled=True,
                            display_name=setting.display_name,
                            endpoint_url=setting.endpoint_url,
                            credential_ref=setting.credential_ref,
                            allow_public_repos=setting.allow_public_repos,
                            issue_key_prefixes=tuple(setting.issue_key_prefixes),
                        )
        except TimeoutError:
            source = "fallback_on_error"
            logger.warning(
                "product-knowledge provider setting lookup timed out; continuing without it",
                extra={"org_id": org_id, "repo_id": repo_id, "timeout_seconds": self._timeout},
            )
        except Exception as exc:
            # Deliberately broad (documented fail-open, as routing.py): a
            # storage failure must never fail or stall the review.
            source = "fallback_on_error"
            logger.warning(
                "product-knowledge provider setting lookup failed; continuing without it",
                extra={"org_id": org_id, "repo_id": repo_id, "error_type": type(exc).__name__},
            )
        _counter.add(1, {"source": source})
        return provider
