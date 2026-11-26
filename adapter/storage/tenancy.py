"""adapter.storage.tenancy — the Postgres-backed implementation of
``tenancy.resolution.TenancyStore`` (``PostgresTenancyStore``),
``tenancy.settings.SettingRepository`` (``PostgresSettingRepository``).

Mirrors ``adapter/storage/queue.py``'s ``JobQueueRepository`` own placement
(one adapter module per storage-backed repository). Wired in only at the
composition root (``apps/webhook_api/main.py``/``apps/admin_api/main.py``/
``apps/worker/main.py``), matching the Protocol-boundary pattern used
everywhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import (
    Org,
    OrgRepoTeamSetting,
    Repo,
    Team,
    WebhookDeliveryRow,
)
from adapter.storage.unit_of_work import run_in_session
from tenancy.resolution import OrgRepoIds, split_owner_repo
from tenancy.settings import (
    ConfigTree,
    OrgConfig,
    ScopeSettings,
    SettingRow,
    SettingStoreError,
    effective_settings,
    format_scope_descriptor,
    validate_setting_value,
)

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
            lambda session: self._get_or_create_org_repo(session, installation_id, repo_full_name),
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
        redelivery can never poison an outer shared transaction.
        """
        statement = (
            pg_insert(WebhookDeliveryRow)
            .values(id=delivery_id)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(WebhookDeliveryRow.id)
        )
        result = await session.execute(statement)
        return result.first() is not None


def _to_setting_row(row: OrgRepoTeamSetting) -> SettingRow:
    return SettingRow(
        org_id=str(row.org_id) if row.org_id is not None else None,
        repo_id=str(row.repo_id) if row.repo_id is not None else None,
        team_id=str(row.team_id) if row.team_id is not None else None,
        key=row.key,
        value=row.value,
    )


def _scope_node(
    *,
    entity_id: uuid.UUID,
    name: str,
    org_id: uuid.UUID,
    repo_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
    settings_by_scope: dict[
        tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None], dict[str, dict[str, object]]
    ],
    all_rows: Sequence[SettingRow],
) -> ScopeSettings:
    """Builds one repo/team node for ``GET /api/v1/config``'s response
    tree — shared by ``PostgresSettingRepository.get_config_tree`` for both
    its repo and team branches, which otherwise differ only in which of
    ``repo_id``/``team_id`` is set.
    """
    own_settings = settings_by_scope.get((org_id, repo_id, team_id), {})
    return ScopeSettings(
        id=str(entity_id),
        name=name,
        scope=format_scope_descriptor(
            org_id=str(org_id),
            repo_id=str(repo_id) if repo_id is not None else None,
            team_id=str(team_id) if team_id is not None else None,
        ),
        settings=own_settings,
        using_platform_defaults=not own_settings,
        effective=effective_settings(
            all_rows,
            org_id=str(org_id),
            repo_id=str(repo_id) if repo_id is not None else None,
            team_id=str(team_id) if team_id is not None else None,
        ),
    )


class PostgresSettingRepository:
    """Implements ``tenancy.settings.SettingRepository`` against the
    ``org_repo_team_setting`` table, plus read-only joins against
    ``org``/``repo``/``team`` for ``get_config_tree``'s hierarchy (US-A12).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run[T](self, *, operation: str, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Shared open-a-session-then-translate-the-failure wrapper, the
        same shape ``adapter/storage/memory_repository.py``'s own ``_run``
        establishes.
        """
        try:
            return await run_in_session(self._session_factory, None, fn)
        except SQLAlchemyError as exc:
            raise SettingStoreError(operation=operation, detail=str(exc)) from exc

    async def get_config_tree(self) -> ConfigTree:
        """US-A12: every org → repo → team, each with its own literal
        ``OrgRepoTeamSetting`` rows (never resolved/inherited — that's
        ``tenancy.settings.resolve_setting``'s job, not this read's) and an
        ``using_platform_defaults`` flag for an empty scope.
        """

        async def _read(session: AsyncSession) -> ConfigTree:
            orgs = (await session.execute(select(Org).order_by(Org.name))).scalars().all()
            repos = (await session.execute(select(Repo).order_by(Repo.name))).scalars().all()
            teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
            setting_rows = (await session.execute(select(OrgRepoTeamSetting))).scalars().all()
            domain_rows = [_to_setting_row(r) for r in setting_rows]

            settings_by_scope: dict[
                tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None],
                dict[str, dict[str, object]],
            ] = {}
            for setting_row in setting_rows:
                scope = (setting_row.org_id, setting_row.repo_id, setting_row.team_id)
                settings_by_scope.setdefault(scope, {})[setting_row.key] = setting_row.value

            repos_by_org: dict[uuid.UUID, list[Repo]] = {}
            for repo in repos:
                repos_by_org.setdefault(repo.org_id, []).append(repo)
            teams_by_org: dict[uuid.UUID, list[Team]] = {}
            for team in teams:
                teams_by_org.setdefault(team.org_id, []).append(team)

            org_configs = []
            for org in orgs:
                org_settings = settings_by_scope.get((org.id, None, None), {})
                org_configs.append(
                    OrgConfig(
                        id=str(org.id),
                        name=org.name,
                        scope=format_scope_descriptor(
                            org_id=str(org.id), repo_id=None, team_id=None
                        ),
                        settings=org_settings,
                        using_platform_defaults=not org_settings,
                        effective=effective_settings(
                            domain_rows, org_id=str(org.id), repo_id=None, team_id=None
                        ),
                        repos=tuple(
                            _scope_node(
                                entity_id=repo.id,
                                name=repo.name,
                                org_id=org.id,
                                repo_id=repo.id,
                                team_id=None,
                                settings_by_scope=settings_by_scope,
                                all_rows=domain_rows,
                            )
                            for repo in repos_by_org.get(org.id, [])
                        ),
                        teams=tuple(
                            _scope_node(
                                entity_id=team.id,
                                name=team.name,
                                org_id=org.id,
                                repo_id=None,
                                team_id=team.id,
                                settings_by_scope=settings_by_scope,
                                all_rows=domain_rows,
                            )
                            for team in teams_by_org.get(org.id, [])
                        ),
                    )
                )

            return ConfigTree(
                platform_settings=settings_by_scope.get((None, None, None), {}),
                platform_effective=effective_settings(
                    domain_rows, org_id=None, repo_id=None, team_id=None
                ),
                orgs=tuple(org_configs),
            )

        return await self._run(operation="get_config_tree", fn=_read)

    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        """
        Read the platform, org and repo rows (never team rows) for one ``key``.

        This is the keyed runtime read behind ``tenancy.settings.resolve_setting``.
        Deliberately not ``get_config_tree``: one indexed-by-key query, no hierarchy joins,
        and no registry validation of stored values (the consumer validates
        what it uses, so an orphaned/odd row can never fail a read here).
        """
        try:
            org_uuid = uuid.UUID(org_id) if org_id is not None else None
            repo_uuid = uuid.UUID(repo_id) if repo_id is not None else None
        except ValueError as exc:
            raise SettingStoreError(operation="list_rows_for_key", detail="malformed id") from exc

        async def _read(session: AsyncSession) -> tuple[SettingRow, ...]:
            scopes = [
                and_(
                    OrgRepoTeamSetting.org_id.is_(None),
                    OrgRepoTeamSetting.repo_id.is_(None),
                    OrgRepoTeamSetting.team_id.is_(None),
                )
            ]
            if org_uuid is not None:
                scopes.append(
                    and_(
                        OrgRepoTeamSetting.org_id == org_uuid,
                        OrgRepoTeamSetting.repo_id.is_(None),
                        OrgRepoTeamSetting.team_id.is_(None),
                    )
                )
                if repo_uuid is not None:
                    scopes.append(
                        and_(
                            OrgRepoTeamSetting.org_id == org_uuid,
                            OrgRepoTeamSetting.repo_id == repo_uuid,
                            OrgRepoTeamSetting.team_id.is_(None),
                        )
                    )
            found = (
                (
                    await session.execute(
                        select(OrgRepoTeamSetting).where(
                            OrgRepoTeamSetting.key == key, or_(*scopes)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return tuple(_to_setting_row(r) for r in found)

        return await self._run(operation="list_rows_for_key", fn=_read)

    async def upsert(
        self,
        *,
        org_id: str | None,
        repo_id: str | None,
        team_id: str | None,
        key: str,
        value: dict[str, object],
    ) -> SettingRow:
        """US-A13: writes/updates ``key``'s value at the exact scope named
        by ``(org_id, repo_id, team_id)`` — ``value`` is validated against
        ``tenancy.settings.SETTING_KEY_REGISTRY`` (raising
        ``tenancy.settings.SettingValidationError``, never silently
        coerced) before this method ever touches Postgres.
        """
        validated_value = validate_setting_value(key, value)

        try:
            org_uuid = uuid.UUID(org_id) if org_id is not None else None
            repo_uuid = uuid.UUID(repo_id) if repo_id is not None else None
            team_uuid = uuid.UUID(team_id) if team_id is not None else None
        except ValueError as exc:
            raise SettingStoreError(operation="upsert", detail=str(exc)) from exc

        async def _write(session: AsyncSession) -> OrgRepoTeamSetting:
            # Postgres (and the SQL standard) treats NULL as distinct from
            # NULL in a unique constraint, so `uq_setting_scope_key`'s own
            # (org_id, repo_id, team_id, key) — all three scope columns
            # nullable — never actually deduplicates a real write: every
            # platform/org/repo/team-scope write has at least one NULL
            # scope column, so a native `INSERT ... ON CONFLICT` would
            # never detect the existing row and would insert a duplicate on
            # every repeat write instead of updating it. `isnot_distinct_from`
            # (already used by `PostgresTenancyStore._get_or_create_org_repo`
            # above for the same NULL-vs-value comparison need) makes this
            # SELECT treat NULL=NULL as a match, mirroring SQL's own
            # `IS NOT DISTINCT FROM` operator. `with_for_update()` holds the
            # row lock for the rest of this transaction so a concurrent
            # write to the exact same scope+key can't interleave between
            # this SELECT and the UPDATE/INSERT below — the same
            # "select, mutate, flush" shape
            # `adapter/storage/memory_repository.py`'s own `set_status`
            # already establishes. This does not close the narrower race
            # where two concurrent writers both see no existing row for a
            # brand-new scope+key and both INSERT a first one — an
            # accepted gap for this low-frequency, human-triggered admin
            # action (never a multi-replica hot path the way
            # `login_attempt` is), not fixed here
            # since closing it fully needs a schema change (a functional
            # unique index, or COALESCE-normalized sentinel columns) this
            # change adds no migration for.
            scope_match = (
                (OrgRepoTeamSetting.key == key)
                & OrgRepoTeamSetting.org_id.isnot_distinct_from(org_uuid)
                & OrgRepoTeamSetting.repo_id.isnot_distinct_from(repo_uuid)
                & OrgRepoTeamSetting.team_id.isnot_distinct_from(team_uuid)
            )
            existing = (
                await session.execute(
                    select(OrgRepoTeamSetting).where(scope_match).with_for_update()
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.value = validated_value
                await session.flush()
                await session.refresh(existing)
                return existing

            row = OrgRepoTeamSetting(
                org_id=org_uuid,
                repo_id=repo_uuid,
                team_id=team_uuid,
                key=key,
                value=validated_value,
            )
            session.add(row)
            await session.flush()
            return row

        row = await self._run(operation="upsert", fn=_write)
        return _to_setting_row(row)
