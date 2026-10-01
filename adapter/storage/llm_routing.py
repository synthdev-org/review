"""adapter.storage.llm_routing — ``PostgresModelRoutingRepository``, the
Postgres-backed implementation of
``llmgateway.routing.ModelRoutingConfigReader``, plus the upsert method
``apps/admin_api/model_routing_routes.py``'s write route (US-A15) needs, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 5 section (Unit F).

Mirrors ``adapter/storage/tenancy.py``'s ``PostgresSettingRepository``
placement and shape (one adapter module per storage-backed repository,
wired in only at each entrypoint's own composition root —
``apps/worker/main.py``/``apps/admin_api/main.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import ModelRoutingConfig
from adapter.storage.unit_of_work import run_in_session
from llmgateway.routing import ModelRoutingConfigRow, ModelRoutingStoreError

_T = TypeVar("_T")


def _to_row(row: ModelRoutingConfig) -> ModelRoutingConfigRow:
    return ModelRoutingConfigRow(
        org_id=str(row.org_id) if row.org_id is not None else None,
        stage=row.stage,
        model_name=row.model_name,
    )


class PostgresModelRoutingRepository:
    """Implements ``llmgateway.routing.ModelRoutingConfigReader`` against
    the ``model_routing_config`` table, plus ``upsert`` for US-A15's write
    path. A concrete class — this repository itself has only one real
    implementation, the same "no Protocol without a second implementation"
    reasoning ``adapter/storage/queue.py``'s ``JobQueueRepository`` already
    establishes; the *reader* boundary its ``get`` method satisfies is the
    Protocol that exists, defined at ``llmgateway.routing`` (see that
    module's own docstring for why).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run(self, *, operation: str, fn: Callable[[AsyncSession], Awaitable[_T]]) -> _T:
        """Shared open-a-session-then-translate-the-failure wrapper,
        mirroring ``adapter/storage/tenancy.py``'s ``PostgresSettingRepository``
        own ``_run`` shape. ``llmgateway.routing.FallbackModelRoutingResolver``
        catches any exception ``get`` raises (including this one) and falls
        open to the static default — see that module's own "Fail-open, by
        design" docstring section — so translating to a typed
        ``ModelRoutingStoreError`` here still matters for a caller that
        isn't the resolver (``apps/admin_api/model_routing_routes.py``'s
        write route, US-A15), even though the resolver itself treats it the
        same as any other failure.
        """
        try:
            return await run_in_session(self._session_factory, None, fn)
        except SQLAlchemyError as exc:
            raise ModelRoutingStoreError(operation=operation, detail=str(exc)) from exc

    async def get(self, *, org_id: str | None, stage: str) -> ModelRoutingConfigRow | None:
        """``llmgateway.routing.ModelRoutingConfigReader.get`` — the exact
        row for ``(org_id, stage)``, or ``None`` if no row exists at that
        scope (the caller's own fallback-chain job, not this method's).
        """
        try:
            org_uuid = uuid.UUID(org_id) if org_id is not None else None
        except ValueError as exc:
            raise ModelRoutingStoreError(operation="get", detail=str(exc)) from exc

        async def _read(session: AsyncSession) -> ModelRoutingConfigRow | None:
            statement = select(ModelRoutingConfig).where(ModelRoutingConfig.stage == stage)
            statement = (
                statement.where(ModelRoutingConfig.org_id == org_uuid)
                if org_uuid is not None
                else statement.where(ModelRoutingConfig.org_id.is_(None))
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            return _to_row(row) if row is not None else None

        return await self._run(operation="get", fn=_read)

    async def upsert(
        self, *, org_id: str | None, stage: str, model_name: str
    ) -> ModelRoutingConfigRow:
        """US-A15: writes/updates the ``ModelRoutingConfig`` row at exactly
        ``(org_id, stage)`` — ``org_id=None`` is the platform-wide row.

        Postgres (and the SQL standard) treats ``NULL`` as distinct from
        ``NULL`` in a unique constraint, so ``uq_model_routing_org_stage``
        alone never actually deduplicates a platform-wide write (``org_id``
        ``NULL`` on every row): a native ``INSERT ... ON CONFLICT`` would
        never detect the existing platform-wide row and would insert a
        duplicate on every repeat write instead of updating it. Mirrors
        ``adapter/storage/tenancy.py``'s ``PostgresSettingRepository.upsert``
        own fix for the identical gap on ``uq_setting_scope_key`` — select
        via ``isnot_distinct_from``/``is_(None)`` (NULL-safe), lock the row
        with ``with_for_update()`` for the rest of this transaction so a
        concurrent write to the exact same ``(org_id, stage)`` can't
        interleave between this SELECT and the UPDATE/INSERT below, then
        update in place or insert fresh.
        """
        try:
            org_uuid = uuid.UUID(org_id) if org_id is not None else None
        except ValueError as exc:
            raise ModelRoutingStoreError(operation="upsert", detail=str(exc)) from exc

        async def _write(session: AsyncSession) -> ModelRoutingConfig:
            statement = select(ModelRoutingConfig).where(ModelRoutingConfig.stage == stage)
            statement = (
                statement.where(ModelRoutingConfig.org_id == org_uuid)
                if org_uuid is not None
                else statement.where(ModelRoutingConfig.org_id.is_(None))
            )
            existing = (await session.execute(statement.with_for_update())).scalar_one_or_none()

            if existing is not None:
                existing.model_name = model_name
                await session.flush()
                await session.refresh(existing)
                return existing

            row = ModelRoutingConfig(org_id=org_uuid, stage=stage, model_name=model_name)
            session.add(row)
            await session.flush()
            return row

        row = await self._run(operation="upsert", fn=_write)
        return _to_row(row)
