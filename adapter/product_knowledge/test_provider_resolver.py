"""Unit tests for the fail-open provider-setting resolver with fakes (plan
Phase 6, Assumption 6; US-PK8, US-PK13). The real-Postgres keyed read is
covered in ``adapter/storage/test_tenancy.py``."""

from __future__ import annotations

import asyncio
import logging

import pytest

import adapter.product_knowledge.provider_resolver as resolver_module
from adapter.product_knowledge.provider_resolver import FailOpenProviderResolver
from tenancy.settings import SettingRow

ORG = "11111111-1111-1111-1111-111111111111"
REPO = "33333333-3333-3333-3333-333333333333"
KEY = "product_knowledge_provider"

pytestmark = pytest.mark.asyncio


def _value(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "display_name": "Wiki",
        "endpoint_url": "https://wiki.internal/mcp",
        "credential_ref": None,
    }
    base.update(overrides)
    return base


def _row(org: str | None, repo: str | None, **overrides: object) -> SettingRow:
    return SettingRow(org_id=org, repo_id=repo, team_id=None, key=KEY, value=_value(**overrides))


class _FakeReader:
    def __init__(self, rows: list[SettingRow] | None = None, *, error: Exception | None = None):
        self._rows = rows or []
        self._error = error
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        self.calls.append((key, org_id, repo_id))
        if self._error is not None:
            raise self._error
        return tuple(self._rows)


class _HungReader:
    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        await asyncio.sleep(3600)
        return ()


class _CounterSpy:
    def __init__(self) -> None:
        self.added: list[dict[str, str]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.added.append(dict(attributes))


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> _CounterSpy:
    spy = _CounterSpy()
    monkeypatch.setattr(resolver_module, "_counter", spy)
    return spy


async def test_narrowest_scope_whole_object_wins(counter: _CounterSpy) -> None:
    reader = _FakeReader(
        [
            _row(None, None, display_name="Platform"),
            _row(ORG, None, display_name="Org"),
            _row(ORG, REPO, display_name="Repo", credential_ref="wiki-prod"),
        ]
    )

    provider = await FailOpenProviderResolver(reader).resolve(ORG, REPO)

    assert provider is not None
    assert provider.display_name == "Repo"
    assert provider.credential_ref == "wiki-prod"
    assert reader.calls == [(KEY, ORG, REPO)]
    assert counter.added == [{"source": "repo"}]


async def test_org_row_is_inherited_by_a_repo(counter: _CounterSpy) -> None:
    provider = await FailOpenProviderResolver(_FakeReader([_row(ORG, None)])).resolve(ORG, REPO)

    assert provider is not None
    assert counter.added == [{"source": "org"}]


async def test_nothing_configured_is_not_configured(counter: _CounterSpy) -> None:
    provider = await FailOpenProviderResolver(_FakeReader([])).resolve(ORG, REPO)

    assert provider is None
    assert counter.added == [{"source": "not_configured"}]


async def test_repo_enabled_false_opts_out_of_an_inherited_provider(counter: _CounterSpy) -> None:
    reader = _FakeReader([_row(None, None), _row(ORG, REPO, enabled=False)])

    assert await FailOpenProviderResolver(reader).resolve(ORG, REPO) is None
    assert counter.added == [{"source": "repo"}]


async def test_platform_enabled_false_disables_the_provider(counter: _CounterSpy) -> None:
    reader = _FakeReader([_row(None, None, enabled=False)])

    assert await FailOpenProviderResolver(reader).resolve(ORG, REPO) is None
    assert counter.added == [{"source": "platform"}]


async def test_an_invalid_stored_row_is_ignored_and_warned(
    counter: _CounterSpy, caplog: pytest.LogCaptureFixture
) -> None:
    reader = _FakeReader([_row(ORG, None, endpoint_url="ftp://nope")])

    with caplog.at_level(logging.WARNING):
        provider = await FailOpenProviderResolver(reader).resolve(ORG, REPO)

    assert provider is None
    assert counter.added == [{"source": "invalid"}]
    assert "invalid" in caplog.text


async def test_a_storage_error_fails_open_with_counter_and_warning(
    counter: _CounterSpy, caplog: pytest.LogCaptureFixture
) -> None:
    reader = _FakeReader(error=RuntimeError("connection refused: postgres://u:pw@db"))

    with caplog.at_level(logging.WARNING):
        provider = await FailOpenProviderResolver(reader).resolve(ORG, REPO)

    assert provider is None
    assert counter.added == [{"source": "fallback_on_error"}]
    assert "lookup failed" in caplog.text
    # Only the exception *type* is logged: its text may carry a DSN.
    assert "postgres://" not in caplog.text


async def test_a_hung_reader_times_out_instead_of_stalling_the_job(
    counter: _CounterSpy, caplog: pytest.LogCaptureFixture
) -> None:
    resolver = FailOpenProviderResolver(_HungReader(), timeout_seconds=0.05)

    with caplog.at_level(logging.WARNING):
        provider = await asyncio.wait_for(resolver.resolve(ORG, REPO), timeout=2)

    assert provider is None
    assert counter.added == [{"source": "fallback_on_error"}]
    assert "timed out" in caplog.text
