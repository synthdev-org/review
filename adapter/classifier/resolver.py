"""
adapter.classifier.resolver: the per-job reader of the ``classifier_provider`` setting.

Implements ``review.finding_judge.ClassifierSelectionResolver`` (pattern of
``adapter/product_knowledge/provider_resolver.py``). Built once at the Worker composition
root and called once per job, so a mode change takes effect on the next job. Absent,
disabled, invalid or non-org rows mean "no classifier" (``None``). A reader error or
timeout propagates: ``review.finding_judge.select_judge`` treats it as not configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from pydantic import ValidationError

from adapter.classifier.typesafe import BreakerRegistry, TypeSafeClassifier
from adapter.egress.credentials import CredentialMap
from adapter.egress.transport import EgressPolicy
from adapter.product_knowledge.provider_resolver import SettingRowReader
from review.finding_judge import ClassifierSelection
from tenancy.settings import CLASSIFIER_PROVIDER_KEY, ClassifierProviderSetting, resolve_setting

__all__ = ["DEFAULT_RESOLUTION_TIMEOUT_SECONDS", "SettingClassifierResolver"]

logger = logging.getLogger(__name__)

DEFAULT_RESOLUTION_TIMEOUT_SECONDS: Final = 2.0


class SettingClassifierResolver:
    def __init__(
        self,
        reader: SettingRowReader,
        credentials: CredentialMap,
        policy: EgressPolicy,
        *,
        timeout_seconds: float = DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the setting reader, the operator credential map and the egress policy."""
        self._reader = reader
        self._credentials = credentials
        self._policy = policy
        self._timeout = timeout_seconds
        self._breakers = BreakerRegistry()  # worker-wide, one breaker per endpoint+credential

    async def resolve(self, org_id: str) -> ClassifierSelection | None:
        rows = await asyncio.wait_for(
            self._reader.list_rows_for_key(
                key=CLASSIFIER_PROVIDER_KEY, org_id=org_id, repo_id=None
            ),
            timeout=self._timeout,
        )
        row = resolve_setting(CLASSIFIER_PROVIDER_KEY, rows, org_id=org_id)
        if row is None or row.org_id != org_id:  # org scope only: ignore any other scope
            return None
        try:
            setting = ClassifierProviderSetting.model_validate(row.value)
        except ValidationError:
            logger.warning("classifier setting is invalid; ignoring it", extra={"org_id": org_id})
            return None
        if not setting.enabled:
            return None
        credential = self._credentials.get(setting.credential_ref)
        if (
            credential is None
            or self._credentials.binding_problem(setting.credential_ref, setting.endpoint_url)
            is not None
        ):
            # The ref was removed or rebound since the write: the judge falls back per finding.
            return ClassifierSelection(mode=setting.mode, model=setting.model, classifier=None)
        return ClassifierSelection(
            mode=setting.mode,
            model=setting.model,
            classifier=TypeSafeClassifier(
                setting.endpoint_url,
                credential=credential,
                policy=self._policy,
                breaker=self._breakers.for_key(setting.endpoint_url, setting.credential_ref),
            ),
        )
