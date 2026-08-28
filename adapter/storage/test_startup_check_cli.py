"""Unit tests for adapter.storage.startup_check_cli's exit-code contract.

Per docs/rules/testing.md's unit tier: no real Postgres here (that is
adapter/storage/test_pgvector_presence.py's integration-tier job, per Phase
2's plan section) — this module substitutes a fake at the CLI's own `_check`
seam to prove the exit-code/log-line behavior Docker Compose's `migrate`
service (docker-compose.yml) depends on for its fail-fast contract.
"""

from __future__ import annotations

import pytest

import adapter.storage.startup_check_cli as cli
from adapter.storage.exceptions import PgVectorNotInstalledError


def test_main_exits_non_zero_when_database_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVIEW_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_main_exits_non_zero_when_pgvector_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_DATABASE_URL", "postgresql+asyncpg://user:pass@host/db")

    async def _fake_check(dsn: str) -> None:
        raise PgVectorNotInstalledError(dsn_summary="host/db")

    monkeypatch.setattr(cli, "_check", _fake_check)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1


def test_main_succeeds_when_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_DATABASE_URL", "postgresql+asyncpg://user:pass@host/db")
    calls: list[str] = []

    async def _fake_check(dsn: str) -> None:
        calls.append(dsn)

    monkeypatch.setattr(cli, "_check", _fake_check)

    cli.main()  # must not raise

    assert calls == ["postgresql+asyncpg://user:pass@host/db"]
