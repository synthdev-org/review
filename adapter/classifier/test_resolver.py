"""Unit tests for the per-job classifier setting resolver."""

from __future__ import annotations

import asyncio

import pytest

from adapter.classifier.resolver import SettingClassifierResolver
from adapter.classifier.typesafe import TypeSafeClassifier
from adapter.egress.credentials import CredentialMap, parse_credential_map
from adapter.egress.transport import EgressPolicy
from tenancy.settings import SettingRow

ORG = "11111111-1111-1111-1111-111111111111"
OTHER_ORG = "22222222-2222-2222-2222-222222222222"
KEY = "classifier_provider"
CREDENTIALS = parse_credential_map(
    '{"typesafe": {"token": "tsk-unit", "allowed_hosts": ["api.typesafe.ai"]}}'
)

pytestmark = pytest.mark.asyncio


def _value(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "backend": "typesafe",
        "endpoint_url": "https://api.typesafe.ai",
        "credential_ref": "typesafe",
        "model": "jev-1.14.0",
        "mode": "shadow",
        "disclosure_acknowledged": True,
    }
    base.update(overrides)
    return base


def _row(org: str | None, **overrides: object) -> SettingRow:
    return SettingRow(org_id=org, repo_id=None, team_id=None, key=KEY, value=_value(**overrides))


class _Reader:
    def __init__(self, rows: list[SettingRow], *, error: Exception | None = None) -> None:
        self._rows = rows
        self._error = error

    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        if self._error is not None:
            raise self._error
        return tuple(self._rows)


class _HungReader:
    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        await asyncio.sleep(3600)
        return ()


def _resolver(
    reader: _Reader | _HungReader,
    credentials: CredentialMap = CREDENTIALS,
    timeout: float = 2.0,
) -> SettingClassifierResolver:
    return SettingClassifierResolver(
        reader, credentials, EgressPolicy(allow_private_networks=False), timeout_seconds=timeout
    )


async def test_setting_absent_yields_no_classifier() -> None:
    assert await _resolver(_Reader([])).resolve(ORG) is None


async def test_enabled_setting_builds_the_typesafe_classifier_with_its_mode_and_model() -> None:
    selection = await _resolver(_Reader([_row(ORG, mode="active")])).resolve(ORG)

    assert selection is not None
    assert (selection.mode, selection.model) == ("active", "jev-1.14.0")
    assert isinstance(selection.classifier, TypeSafeClassifier)


@pytest.mark.parametrize(
    "row",
    [
        _row(ORG, enabled=False),
        _row(ORG, endpoint_url="http://insecure.example"),  # invalid shape
        _row(None),  # platform scope is not honoured
        _row(OTHER_ORG),  # another org's row is never served
    ],
)
async def test_disabled_invalid_or_foreign_rows_yield_no_classifier(row: SettingRow) -> None:
    assert await _resolver(_Reader([row])).resolve(ORG) is None


@pytest.mark.parametrize(
    "credentials",
    [
        CredentialMap.empty(),  # ref removed
        parse_credential_map('{"typesafe": {"token": "t", "allowed_hosts": ["other.example"]}}'),
    ],
)
async def test_credential_that_no_longer_resolves_keeps_the_mode_with_no_classifier(
    credentials: CredentialMap,
) -> None:
    selection = await _resolver(_Reader([_row(ORG)]), credentials).resolve(ORG)

    assert selection is not None
    assert selection.classifier is None
    assert selection.mode == "shadow"


async def test_reader_failure_and_timeout_propagate_for_the_judge_to_fail_open() -> None:
    with pytest.raises(RuntimeError):
        await _resolver(_Reader([], error=RuntimeError("db down"))).resolve(ORG)
    with pytest.raises(TimeoutError):
        await _resolver(_HungReader(), timeout=0.01).resolve(ORG)


async def test_one_orgs_throttling_never_opens_another_credentials_breaker() -> None:
    creds = parse_credential_map(
        '{"typesafe": {"token": "t1", "allowed_hosts": ["api.typesafe.ai"]},'
        ' "other": {"token": "t2", "allowed_hosts": ["api.typesafe.ai"]}}'
    )
    resolver = _resolver(_Reader([_row(ORG), _row(OTHER_ORG, credential_ref="other")]), creds)
    first = (await resolver.resolve(ORG)).classifier  # type: ignore[union-attr]
    again = (await resolver.resolve(ORG)).classifier  # type: ignore[union-attr]
    other = (await resolver.resolve(OTHER_ORG)).classifier  # type: ignore[union-attr]
    assert isinstance(first, TypeSafeClassifier) and isinstance(other, TypeSafeClassifier)
    for _ in range(5):
        first._breaker.record_throttled()
    assert not again._breaker.allow()  # type: ignore[union-attr]  # worker-wide per key
    assert other._breaker.allow()
