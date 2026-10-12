"""Unit tests for ``adapter/storage/tenancy.py``'s ``day_for`` — the pure
04:00-anchored-day helper, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 7 "Tests" section:
"unit tests for ``day_for``'s 04:00 boundary math."

No I/O, no database — ``day_for`` is a pure function of its one argument,
so its tests need no ``migrated_engine``/``pgvector_container`` fixture and
carry no ``@pytest.mark.integration`` marker.

The second half of this module (``plan-deliver-product-knowledge.md``
Phase 6, Unit F; US-PK8, US-PK13; F6, F11) is the opposite: the first
runtime reader of ``OrgRepoTeamSetting`` — the keyed effective-setting read,
the fail-open resolver over it, and ``get_config_tree``'s ``effective``
field — against a real Postgres via ``testcontainers-python``, each test
individually marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import adapter.product_knowledge.provider_resolver as provider_resolver_module
from adapter.product_knowledge.provider_resolver import FailOpenProviderResolver
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.models import Org, OrgRepoTeamSetting, Repo, Team
from adapter.storage.tenancy import PostgresSettingRepository, day_for
from tenancy.settings import SettingRow, SettingStoreError


def test_day_for_just_before_reset_belongs_to_the_previous_date() -> None:
    """03:59 — one minute before the 04:00 reset — still belongs to the
    *previous* calendar date's window, per ADR-0008's "reset 04:00, not
    midnight" Decision.
    """
    now = datetime(2026, 3, 15, 3, 59, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 14)


def test_day_for_exactly_at_reset_belongs_to_that_date() -> None:
    """04:00:00 exactly is the first instant of the new day's window."""
    now = datetime(2026, 3, 15, 4, 0, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_just_after_reset_belongs_to_that_date() -> None:
    """04:01 — one minute after the reset — belongs to the new day."""
    now = datetime(2026, 3, 15, 4, 1, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_midday_belongs_to_that_calendar_date() -> None:
    """An ordinary midday timestamp (well clear of either boundary) is the
    straightforward, non-edge-case sanity check.
    """
    now = datetime(2026, 3, 15, 12, 30, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_just_before_midnight_is_unaffected_by_the_date_rollover() -> None:
    """23:59 is nowhere near the 04:00 boundary — this guards against an
    implementation that accidentally anchors on midnight instead of 04:00.
    """
    now = datetime(2026, 3, 15, 23, 59, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_crosses_a_month_boundary_correctly() -> None:
    """03:59 on the first of a month still belongs to the previous month's
    last date — exercises the boundary math against ``date`` arithmetic
    that has to roll over a month (and, transitively, verifies no
    off-by-one from naive string/int date manipulation).
    """
    now = datetime(2026, 4, 1, 3, 59, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 31)


# --- Product knowledge provider setting: keyed read + fail-open resolver ----

_KEY = "product_knowledge_provider"


def _value(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "display_name": "Wiki",
        "endpoint_url": "https://wiki.internal/mcp",
        "credential_ref": None,
    }
    base.update(overrides)
    return base


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def hierarchy(
    session_factory: async_sessionmaker[Any],
) -> dict[str, uuid.UUID]:
    """Two orgs (acme: repo widgets + other, team platform; globex: repo
    gadgets) — enough to prove the keyed read never crosses scopes."""
    async with session_factory() as session, session.begin():
        acme = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        globex = Org(name="globex", slug=f"globex-{uuid.uuid4().hex}")
        session.add_all([acme, globex])
        await session.flush()
        widgets = Repo(org_id=acme.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        other = Repo(org_id=acme.id, name="other", full_name=f"acme/other-{uuid.uuid4().hex}")
        gadgets = Repo(org_id=globex.id, name="gadgets", full_name=f"g/gadgets-{uuid.uuid4().hex}")
        team = Team(org_id=acme.id, name="platform")
        session.add_all([widgets, other, gadgets, team])
        await session.flush()
        return {
            "acme": acme.id,
            "globex": globex.id,
            "widgets": widgets.id,
            "other": other.id,
            "gadgets": gadgets.id,
            "team": team.id,
        }


async def _insert_raw(
    session_factory: async_sessionmaker[Any],
    *,
    org_id: uuid.UUID | None,
    repo_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    key: str = _KEY,
    value: dict[str, object] | None = None,
) -> None:
    """Bypasses the registry (like a hand-written/orphaned row)."""
    async with session_factory() as session, session.begin():
        session.add(
            OrgRepoTeamSetting(
                org_id=org_id,
                repo_id=repo_id,
                team_id=team_id,
                key=key,
                value=value if value is not None else _value(),
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keyed_read_returns_only_platform_org_and_repo_rows_of_that_key(
    session_factory: async_sessionmaker[Any], hierarchy: dict[str, uuid.UUID]
) -> None:
    acme, globex, widgets = hierarchy["acme"], hierarchy["globex"], hierarchy["widgets"]
    await _insert_raw(session_factory, org_id=None, value=_value(display_name="Platform"))
    await _insert_raw(session_factory, org_id=acme, value=_value(display_name="Acme"))
    await _insert_raw(session_factory, org_id=acme, repo_id=widgets, value=_value(display_name="W"))
    # Must NOT be returned: sibling repo, other org, a team row, another key.
    await _insert_raw(session_factory, org_id=acme, repo_id=hierarchy["other"])
    await _insert_raw(session_factory, org_id=globex, value=_value(display_name="Globex"))
    await _insert_raw(session_factory, org_id=acme, team_id=hierarchy["team"])
    await _insert_raw(session_factory, org_id=None, key="memory_suppression_enabled", value={})

    rows = await PostgresSettingRepository(session_factory).list_rows_for_key(
        key=_KEY, org_id=str(acme), repo_id=str(widgets)
    )

    assert {r.value["display_name"] for r in rows} == {"Platform", "Acme", "W"}
    assert all(r.team_id is None and r.key == _KEY for r in rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keyed_read_without_a_repo_omits_repo_rows(
    session_factory: async_sessionmaker[Any], hierarchy: dict[str, uuid.UUID]
) -> None:
    acme, widgets = hierarchy["acme"], hierarchy["widgets"]
    await _insert_raw(session_factory, org_id=acme)
    await _insert_raw(session_factory, org_id=acme, repo_id=widgets)

    rows = await PostgresSettingRepository(session_factory).list_rows_for_key(
        key=_KEY, org_id=str(acme), repo_id=None
    )

    assert [r.repo_id for r in rows] == [None]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keyed_read_rejects_a_malformed_id_as_a_store_error(
    session_factory: async_sessionmaker[Any],
) -> None:
    with pytest.raises(SettingStoreError):
        await PostgresSettingRepository(session_factory).list_rows_for_key(
            key=_KEY, org_id="not-a-uuid", repo_id=None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolver_narrowest_scope_wins_and_repo_opt_out_hides_the_inherited_provider(
    session_factory: async_sessionmaker[Any], hierarchy: dict[str, uuid.UUID]
) -> None:
    acme, widgets, other = hierarchy["acme"], hierarchy["widgets"], hierarchy["other"]
    await _insert_raw(session_factory, org_id=None, value=_value(display_name="Platform"))
    await _insert_raw(session_factory, org_id=acme, value=_value(display_name="Acme"))
    await _insert_raw(
        session_factory, org_id=acme, repo_id=widgets, value=_value(display_name="W", enabled=False)
    )
    resolver = FailOpenProviderResolver(PostgresSettingRepository(session_factory))

    inherited = await resolver.resolve(str(acme), str(other))
    opted_out = await resolver.resolve(str(acme), str(widgets))
    platform_only = await resolver.resolve(str(hierarchy["globex"]), str(hierarchy["gadgets"]))

    assert inherited is not None and inherited.display_name == "Acme"
    assert opted_out is None
    assert platform_only is not None and platform_only.display_name == "Platform"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_platform_scope_enabled_false_disables_the_provider_for_every_org_and_repo(
    session_factory: async_sessionmaker[Any], hierarchy: dict[str, uuid.UUID]
) -> None:
    await _insert_raw(session_factory, org_id=None, value=_value(enabled=False))
    resolver = FailOpenProviderResolver(PostgresSettingRepository(session_factory))

    assert await resolver.resolve(str(hierarchy["acme"]), str(hierarchy["widgets"])) is None
    assert await resolver.resolve(str(hierarchy["globex"]), str(hierarchy["gadgets"])) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolver_fails_open_with_counter_and_warning_when_postgres_is_unreachable(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources: list[str] = []

    class _Spy:
        def add(self, amount: int, attributes: dict[str, str]) -> None:
            sources.append(attributes["source"])

    monkeypatch.setattr(provider_resolver_module, "_counter", _Spy())
    # Alembic's `fileConfig` (run by the migrated_engine fixture) disables
    # pre-existing loggers; re-enable this one so caplog can see it.
    provider_resolver_module.logger.disabled = False
    dead = create_storage_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
    try:
        resolver = FailOpenProviderResolver(
            PostgresSettingRepository(create_session_factory(dead)), timeout_seconds=5
        )
        with caplog.at_level(logging.WARNING):
            provider = await resolver.resolve(str(uuid.uuid4()), str(uuid.uuid4()))
    finally:
        await dead.dispose()

    assert provider is None
    assert sources == ["fallback_on_error"]
    assert "lookup failed" in caplog.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_hung_repository_cannot_stall_the_caller_past_the_resolver_timeout(
    session_factory: async_sessionmaker[Any],
) -> None:
    class _HungRepository(PostgresSettingRepository):
        async def list_rows_for_key(
            self, *, key: str, org_id: str | None, repo_id: str | None
        ) -> tuple[SettingRow, ...]:
            await asyncio.sleep(3600)
            return ()

    resolver = FailOpenProviderResolver(_HungRepository(session_factory), timeout_seconds=0.1)
    started = asyncio.get_running_loop().time()

    provider = await resolver.resolve(str(uuid.uuid4()), str(uuid.uuid4()))

    assert provider is None
    assert asyncio.get_running_loop().time() - started < 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_config_tree_tolerates_an_orphaned_key_and_reports_effective_values(
    session_factory: async_sessionmaker[Any], hierarchy: dict[str, uuid.UUID]
) -> None:
    """F11: after a code revert (or a hand-written row) an unregistered key
    must not break the tree read; ``effective`` resolves only registry keys,
    server-side, with the scope each value came from."""
    acme, widgets, other = hierarchy["acme"], hierarchy["widgets"], hierarchy["other"]
    await _insert_raw(session_factory, org_id=None, key="retired_key", value={"x": 1})
    await _insert_raw(session_factory, org_id=acme, value=_value(display_name="Acme"))
    await _insert_raw(
        session_factory, org_id=acme, repo_id=widgets, value=_value(display_name="W", enabled=False)
    )
    # A registered key holding an invalid stored value (hand-written row):
    # the tree read must still succeed.
    await _insert_raw(
        session_factory,
        org_id=hierarchy["globex"],
        value=_value(endpoint_url="ftp://x"),
    )
    # A team-scoped provider row is never effective for anything.
    await _insert_raw(
        session_factory, org_id=acme, team_id=hierarchy["team"], value=_value(display_name="T")
    )

    tree = await PostgresSettingRepository(session_factory).get_config_tree()

    assert tree.platform_settings == {"retired_key": {"x": 1}}
    assert tree.platform_effective == {}
    acme_node = next(o for o in tree.orgs if o.id == str(acme))
    assert acme_node.effective[_KEY].source == f"org:{acme}"
    repos = {r.id: r for r in acme_node.repos}
    assert repos[str(other)].effective[_KEY].value["display_name"] == "Acme"
    assert repos[str(other)].effective[_KEY].source == f"org:{acme}"
    assert repos[str(widgets)].effective[_KEY].value["enabled"] is False
    assert repos[str(widgets)].effective[_KEY].source == f"repo:{acme}:{widgets}"
    team_node = acme_node.teams[0]
    assert team_node.effective[_KEY].value["display_name"] == "Acme"
    assert "retired_key" not in acme_node.effective
    # The invalid stored row is unusable: it must not be presented as the
    # effective value (nor fall through to a parent) - it is unconfigured.
    globex_node = next(o for o in tree.orgs if o.id == str(hierarchy["globex"]))
    assert _KEY not in globex_node.effective
    assert all(_KEY not in r.effective for r in globex_node.repos)
