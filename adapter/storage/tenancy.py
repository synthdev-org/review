"""adapter.storage.tenancy — the Postgres-backed implementation of
``tenancy.resolution.TenancyStore``.

Per the plan's Phase 1 "Composition-root wiring for storage" section:
mirrors ``adapter/storage/queue.py``'s ``JobQueueRepository`` own placement
(one adapter module per storage-backed repository). Wired in only at the
composition root (``apps/webhook_api/main.py``), matching the
Protocol-boundary pattern used everywhere else in this plan.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import Org, Repo, WebhookDeliveryRow
from adapter.storage.unit_of_work import run_in_session
from tenancy.resolution import OrgRepoIds, split_owner_repo

_T = TypeVar("_T")


class PostgresTenancyStore:
    """Implements ``tenancy.resolution.TenancyStore`` against real Postgres
    tables (``org``, ``repo``, ``webhook_delivery``).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run(
        self, unit_of_work: object | None, fn: Callable[[AsyncSession], Awaitable[_T]]
    ) -> _T:
        """Runs ``fn`` against ``unit_of_work`` if given (an already-open,
        caller-owned session), or opens and commits a fresh session/
        transaction of its own otherwise. Shared by every public method
        below so the "join the caller's unit of work, or open one" choice
        — and the ``unit_of_work``-is-always-an-``AsyncSession`` cast it
        requires — is made in exactly one place.
        """
        # unit_of_work is always an AsyncSession when set; the domain
        # Protocol types it as object only to stay free of this import.
        session = cast("AsyncSession | None", unit_of_work)
        return await run_in_session(self._session_factory, session, fn)

    async def get_or_create_org_repo(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        unit_of_work: object | None = None,
    ) -> OrgRepoIds:
        return await self._run(
            unit_of_work,
            lambda session: self._get_or_create_org_repo(
                session, installation_id, repo_full_name
            ),
        )

    async def _get_or_create_org_repo(
        self, session: AsyncSession, installation_id: int, repo_full_name: str
    ) -> OrgRepoIds:
        """Auto-provision Org/Repo on first sight, or resolve the existing
        rows for an already-known installation without re-provisioning.

        Org is keyed by the ``owner`` portion of ``repo_full_name`` (GitHub's
        own ``"owner/repo"`` shape) — a repeat webhook for the same
        installation/repo therefore resolves the same Org/Repo rows via that
        same owner slug and full name, rather than needing ``installation_id``
        itself to be the lookup key (only ``Repo`` carries that column; see
        ``adapter/storage/models.py``'s ``Repo.installation_id`` docstring).
        ``installation_id`` is still recorded/kept in sync on the resolved
        ``Repo`` row.
        """
        # Reuses the same owner/repo split adapter/github/diff.py,
        # posting.py, and auth.py already rely on, so a malformed
        # repo_full_name (no "/") raises ValueError here too instead of
        # silently provisioning a bogus Org/Repo row keyed on the whole
        # string as both owner and name.
        owner, repo_name = split_owner_repo(repo_full_name)

        # SELECT-then-INSERT alone races under concurrent webhook deliveries
        # for the same never-before-seen org/repo (e.g. a GitHub redelivery
        # arriving twice): both sessions could see no existing row and both
        # attempt to INSERT, and whichever flushes second would violate
        # Org.slug's/Repo's unique constraints (adapter/storage/models.py)
        # with an unhandled IntegrityError. Postgres's native
        # ``INSERT ... ON CONFLICT DO NOTHING`` (already used by
        # ``_record_delivery_if_new`` below, for the same reason) makes the
        # provisioning step itself race-safe: on conflict, the insert
        # returns no row, and a fast follow-up SELECT resolves the row the
        # other, concurrently-committed session already created.
        org_id = (
            await session.execute(
                pg_insert(Org)
                .values(name=owner, slug=owner)
                .on_conflict_do_nothing(index_elements=["slug"])
                .returning(Org.id)
            )
        ).scalar_one_or_none()
        if org_id is None:
            org_id = (await session.execute(select(Org.id).where(Org.slug == owner))).scalar_one()

        repo_id = (
            await session.execute(
                pg_insert(Repo)
                .values(
                    org_id=org_id,
                    name=repo_name,
                    full_name=repo_full_name,
                    installation_id=installation_id,
                )
                .on_conflict_do_nothing(index_elements=["org_id", "full_name"])
                .returning(Repo.id)
            )
        ).scalar_one_or_none()
        if repo_id is None:
            repo_id = (
                await session.execute(
                    select(Repo.id).where(Repo.org_id == org_id, Repo.full_name == repo_full_name)
                )
            ).scalar_one()
            await session.execute(
                update(Repo)
                .where(
                    Repo.id == repo_id,
                    Repo.installation_id.is_distinct_from(installation_id),
                )
                .values(installation_id=installation_id)
            )

        return OrgRepoIds(org_id=org_id, repo_id=repo_id)

    async def record_delivery_if_new(
        self, delivery_id: str, *, unit_of_work: object | None = None
    ) -> bool:
        return await self._run(
            unit_of_work, lambda session: self._record_delivery_if_new(session, delivery_id)
        )

    async def _record_delivery_if_new(self, session: AsyncSession, delivery_id: str) -> bool:
        """Insert ``delivery_id`` via Postgres's native
        ``INSERT ... ON CONFLICT DO NOTHING``, returning ``True`` if a row
        came back (a genuinely new delivery) and ``False`` if the conflict
        produced none (an already-seen redelivery) — never raising, so a
        redelivery can never poison an outer shared transaction (see the
        plan's Phase 1 "Transaction boundary" section).
        """
        statement = (
            pg_insert(WebhookDeliveryRow)
            .values(id=delivery_id)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(WebhookDeliveryRow.id)
        )
        result = await session.execute(statement)
        return result.first() is not None
