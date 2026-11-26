"""findings_show_nits end to end against a real Postgres (testcontainers): two orgs with
different settings, each resolved through the injected reader into the real output step."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import create_session_factory
from adapter.storage.models import Org
from adapter.storage.tenancy import PostgresSettingRepository
from findings.models import Finding
from review.output import FindingsOutputInput, FindingsOutputStep
from review.step import StepContext
from tenancy.show_nits import FailClosedShowNitsResolver

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _Publisher:
    def __init__(self) -> None:
        self.collapsed: list[list[Finding]] = []

    async def post_review(
        self, *, collapsed_findings: list[Finding] | None = None, **_: Any
    ) -> None:
        self.collapsed.append(collapsed_findings or [])

    async def post_check_run(self, **_: Any) -> None:
        return None

    async def post_failure_check_run(self, **_: Any) -> None:
        raise AssertionError("not used")


@pytest_asyncio.fixture
async def orgs(migrated_engine: AsyncEngine) -> tuple[str, str, PostgresSettingRepository]:
    factory = create_session_factory(migrated_engine)
    async with factory() as session, session.begin():
        on = Org(name="on", slug=f"on-{uuid.uuid4().hex}")
        off = Org(name="off", slug=f"off-{uuid.uuid4().hex}")
        session.add_all([on, off])
        await session.flush()
        ids = (str(on.id), str(off.id))
    repo = PostgresSettingRepository(factory)
    await repo.upsert(
        org_id=ids[0], repo_id=None, team_id=None, key="findings_show_nits", value={"enabled": True}
    )
    await repo.upsert(
        org_id=ids[1],
        repo_id=None,
        team_id=None,
        key="findings_show_nits",
        value={"enabled": False},
    )
    return ids[0], ids[1], repo


async def _collapsed_count(repo: PostgresSettingRepository, org_id: str) -> int:
    publisher = _Publisher()
    step = FindingsOutputStep(publisher, show_nits_resolver=FailClosedShowNitsResolver(repo))
    nit = Finding(file="a.py", line=1, severity="low", message="nit", source="llm", tier="nit")
    await step.run(
        FindingsOutputInput(
            findings=[nit],
            org_id=org_id,
            repo_id="r",
            installation_id=1,
            repo_full_name="acme/widgets",
            pr_number=1,
            head_sha="a" * 40,
        ),
        StepContext(job_id="j", review_quality_v2=True),
    )
    return sum(len(c) for c in publisher.collapsed)


async def test_each_org_sees_only_its_own_nit_setting(
    orgs: tuple[str, str, PostgresSettingRepository],
) -> None:
    on_org, off_org, repo = orgs

    assert await _collapsed_count(repo, on_org) == 1
    assert await _collapsed_count(repo, off_org) == 0


async def test_an_org_with_no_row_defaults_to_hidden(
    orgs: tuple[str, str, PostgresSettingRepository], migrated_engine: AsyncEngine
) -> None:
    _, _, repo = orgs
    async with create_session_factory(migrated_engine)() as session, session.begin():
        bare = Org(name="bare", slug=f"bare-{uuid.uuid4().hex}")
        session.add(bare)
        await session.flush()
        bare_id = str(bare.id)

    assert await _collapsed_count(repo, bare_id) == 0
