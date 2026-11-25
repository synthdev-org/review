"""tenancy.show_nits: the runtime reader of the ``findings_show_nits`` org setting
(plan Phase 3, A2), consumed by ``review.output`` through its own narrow
``ShowNitsResolver`` Protocol. Same shape as
``adapter/product_knowledge/provider_resolver.py``: bounded by a timeout and
reusing ``resolve_setting``.

Fails **closed**: a missing row, an invalid row or a read failure all mean
"do not show nits" (the default), never a failed review.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final, Protocol

from pydantic import ValidationError

from tenancy.settings import (
    FINDINGS_SHOW_NITS_KEY,
    FindingsShowNitsSetting,
    SettingRow,
    resolve_setting,
)

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "FailClosedShowNitsResolver"]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final = 2.0


class SettingRowReader(Protocol):
    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        """Return the setting rows for ``key`` visible at the given scope."""


class FailClosedShowNitsResolver:
    """Implements ``review.output.ShowNitsResolver``. Org scope: platform and
    org rows only (``repo_id`` is never passed)."""

    def __init__(
        self, reader: SettingRowReader, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._reader = reader
        self._timeout = timeout_seconds

    async def show_nits(self, org_id: str) -> bool:
        try:
            rows = await asyncio.wait_for(
                self._reader.list_rows_for_key(
                    key=FINDINGS_SHOW_NITS_KEY, org_id=org_id, repo_id=None
                ),
                timeout=self._timeout,
            )
            row = resolve_setting(FINDINGS_SHOW_NITS_KEY, rows, org_id=org_id)
            if row is None:
                return False
            return FindingsShowNitsSetting.model_validate(row.value).enabled
        except ValidationError:
            logger.warning("findings_show_nits setting is invalid; nits stay hidden")
        except Exception as exc:
            # Deliberately broad (documented fail-closed): a settings-store blip must
            # not fail a review, and the safe default is the quieter output.
            logger.warning(
                "findings_show_nits lookup failed; nits stay hidden",
                extra={"org_id": org_id, "error": type(exc).__name__},
            )
        return False
