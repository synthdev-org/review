"""Unit tests for tenancy.show_nits: the fail-closed ``findings_show_nits`` reader."""

from __future__ import annotations

import asyncio

import pytest

from tenancy.settings import SettingRow
from tenancy.show_nits import FailClosedShowNitsResolver

_ORG = "org-1"


def _row(org_id: str | None, value: dict[str, object]) -> SettingRow:
    return SettingRow(
        org_id=org_id, repo_id=None, team_id=None, key="findings_show_nits", value=value
    )


class _Reader:
    def __init__(self, rows: tuple[SettingRow, ...] = (), error: Exception | None = None) -> None:
        self.rows = rows
        self.error = error
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        self.calls.append((key, org_id, repo_id))
        if self.error:
            raise self.error
        return self.rows


@pytest.mark.asyncio
async def test_absent_setting_hides_nits() -> None:
    assert await FailClosedShowNitsResolver(_Reader()).show_nits(_ORG) is False


@pytest.mark.asyncio
async def test_org_row_enables_and_reads_at_org_scope_only() -> None:
    reader = _Reader((_row(_ORG, {"enabled": True}),))

    assert await FailClosedShowNitsResolver(reader).show_nits(_ORG) is True
    assert reader.calls == [("findings_show_nits", _ORG, None)]


@pytest.mark.asyncio
async def test_org_row_overrides_platform_row() -> None:
    reader = _Reader((_row(None, {"enabled": True}), _row(_ORG, {"enabled": False})))

    assert await FailClosedShowNitsResolver(reader).show_nits(_ORG) is False


@pytest.mark.asyncio
async def test_invalid_row_fails_closed() -> None:
    reader = _Reader((_row(_ORG, {"enabled": "yes please"}),))

    assert await FailClosedShowNitsResolver(reader).show_nits(_ORG) is False


@pytest.mark.asyncio
async def test_store_error_and_timeout_fail_closed() -> None:
    assert (
        await FailClosedShowNitsResolver(_Reader(error=RuntimeError("db"))).show_nits(_ORG) is False
    )

    class _Slow(_Reader):
        async def list_rows_for_key(
            self, *, key: str, org_id: str | None, repo_id: str | None
        ) -> tuple[SettingRow, ...]:
            await asyncio.sleep(1)
            return ()

    assert await FailClosedShowNitsResolver(_Slow(), timeout_seconds=0.01).show_nits(_ORG) is False
