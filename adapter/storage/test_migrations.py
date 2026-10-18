"""Integration test: `alembic upgrade head` / `downgrade base` against a
real Postgres/pgvector instance (testcontainers), per the plan's Phase 2
Tests and Acceptance sections.
"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import inspect, text

from adapter.storage.conftest import alembic_config_for
from adapter.storage.engine import create_storage_engine

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {
    "org",
    "repo",
    "team",
    "job_queue",
    "model_routing_config",
    "org_repo_team_setting",
    "webhook_delivery",
    # Phase 6 (indexing plan, Unit F):
    "index_chunk",
    "index_graph_node",
    "index_graph_edge",
    # Phase 10 (clone detection, Unit B):
    "index_clone_file",
    "index_clone_fingerprint",
    # Phase 1 (Memory delivery plan, Unit A):
    "posted_finding_comment",
    "memory_entry",
    "captured_reaction",
    "alembic_version",
}

# Per-table expected columns, keyed to the SQLAlchemy models in models.py —
# proves the migration actually creates the schema the ORM expects, not just
# a table with the right name.
_EXPECTED_COLUMNS = {
    "org": {"id", "name", "slug", "created_at", "updated_at"},
    "repo": {
        "id",
        "org_id",
        "name",
        "full_name",
        "installation_id",
        "last_indexed_commit_sha",
        "clone_fingerprint_version",
        "created_at",
        "updated_at",
    },
    "team": {"id", "org_id", "name", "created_at", "updated_at"},
    "webhook_delivery": {"id", "created_at"},
    "job_queue": {
        "id",
        "job_type",
        "status",
        "payload",
        "org_id",
        "repo_id",
        "attempts",
        "claimed_by",
        "claimed_at",
        "created_at",
        "updated_at",
    },
    "model_routing_config": {
        "id",
        "org_id",
        "stage",
        "model_name",
        "created_at",
        "updated_at",
    },
    "org_repo_team_setting": {
        "id",
        "org_id",
        "repo_id",
        "team_id",
        "key",
        "value",
        "created_at",
        "updated_at",
    },
    "index_chunk": {
        "id",
        "repo_id",
        "pr_number",
        "file_path",
        "chunk_index",
        "language",
        "symbol_name",
        "byte_range_start",
        "byte_range_end",
        "content",
        "embedding",
        "created_at",
        "updated_at",
    },
    "index_graph_node": {
        "id",
        "repo_id",
        "pr_number",
        "file_path",
        "symbol_name",
        "language",
        "created_at",
        "updated_at",
    },
    "index_graph_edge": {
        "id",
        "source_node_id",
        "target_symbol_name",
        "created_at",
    },
    "posted_finding_comment": {
        "id",
        "repo_id",
        "pr_number",
        "commit_sha",
        "finding_file",
        "finding_line",
        "finding_rule_id",
        "finding_message",
        "carried_suggestion",
        "created_at",
    },
    "memory_entry": {
        "id",
        "org_id",
        "repo_id",
        "team_id",
        "entry_type",
        "status",
        "raw_signal_type",
        "raw_signal_detail",
        "posted_finding_comment_id",
        "github_comment_id",
        "embedding",
        "approved_at",
        "approved_by",
        "created_at",
        "updated_at",
    },
    "captured_reaction": {"posted_finding_comment_id", "reaction_id"},
    "index_clone_file": {
        "repo_id",
        "pr_number",
        "file_path",
        "language",
        "token_hashes",
        "line_first_token",
        "updated_at",
    },
    "index_clone_fingerprint": {"repo_id", "pr_number", "file_path", "token_offset", "hash"},
}


async def _table_names(dsn: str) -> set[str]:
    engine = create_storage_engine(dsn)
    try:
        async with engine.connect() as connection:
            return set(
                await connection.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            )
    finally:
        await engine.dispose()


async def _column_names(dsn: str, table_name: str) -> set[str]:
    engine = create_storage_engine(dsn)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(table_name)
            )
            return {column["name"] for column in columns}
    finally:
        await engine.dispose()


async def _pgvector_extension_present(dsn: str) -> bool:
    engine = create_storage_engine(dsn)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            )
            return result.first() is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_head_creates_expected_schema(pgvector_container) -> None:  # type: ignore[no-untyped-def]
    dsn = pgvector_container.get_connection_url()
    config = alembic_config_for(dsn)

    # See conftest.py's migrated_engine fixture for why this runs in a
    # thread: Alembic's async env.py calls `asyncio.run(...)` internally,
    # which cannot nest inside this test's own running event loop.
    await asyncio.to_thread(command.upgrade, config, "head")
    try:
        tables = await _table_names(dsn)
        assert tables >= _EXPECTED_TABLES
        for table_name, expected_columns in _EXPECTED_COLUMNS.items():
            actual_columns = await _column_names(dsn, table_name)
            assert actual_columns >= expected_columns, (
                f"{table_name}: missing columns {expected_columns - actual_columns}"
            )
        assert await _pgvector_extension_present(dsn)
    finally:
        await asyncio.to_thread(command.downgrade, config, "base")


@pytest.mark.asyncio
async def test_downgrade_base_reverses_the_baseline_migration(pgvector_container) -> None:  # type: ignore[no-untyped-def]
    dsn = pgvector_container.get_connection_url()
    config = alembic_config_for(dsn)

    await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.downgrade, config, "base")

    tables = await _table_names(dsn)
    assert not (_EXPECTED_TABLES - {"alembic_version"}) & tables
    assert not await _pgvector_extension_present(dsn)


async def _execute(dsn: str, statement: str, params: dict[str, object] | None = None) -> None:
    engine = create_storage_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), params or {})
    finally:
        await engine.dispose()


async def _scalar(dsn: str, statement: str) -> object:
    engine = create_storage_engine(dsn)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement))).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0011_up_down_on_a_populated_schema(pgvector_container) -> None:  # type: ignore[no-untyped-def]
    """Plan Phase 10/2: existing repo rows default to version 0 on upgrade, and
    a downgrade succeeds with clone rows present (dropping them)."""
    dsn = pgvector_container.get_connection_url()
    config = alembic_config_for(dsn)

    await asyncio.to_thread(command.upgrade, config, "0010")
    try:
        await _execute(
            dsn,
            "INSERT INTO org (id, name, slug) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'o', 'o')",
        )
        await _execute(
            dsn,
            "INSERT INTO repo (id, org_id, name, full_name) VALUES "
            "('00000000-0000-0000-0000-000000000002', "
            "'00000000-0000-0000-0000-000000000001', 'r', 'o/r')",
        )

        await asyncio.to_thread(command.upgrade, config, "0011")
        assert await _scalar(dsn, "SELECT clone_fingerprint_version FROM repo") == 0

        await _execute(
            dsn,
            "INSERT INTO index_clone_file (repo_id, file_path, language, token_hashes, "
            "line_first_token) VALUES ('00000000-0000-0000-0000-000000000002', 'a.ts', "
            "'typescript', ARRAY[1,2], ARRAY[0])",
        )
        await _execute(
            dsn,
            "INSERT INTO index_clone_fingerprint (repo_id, file_path, token_offset, hash) "
            "VALUES ('00000000-0000-0000-0000-000000000002', 'a.ts', 0, 42)",
        )

        await asyncio.to_thread(command.downgrade, config, "0010")
        tables = await _table_names(dsn)
        assert not {"index_clone_file", "index_clone_fingerprint"} & tables
        assert "clone_fingerprint_version" not in await _column_names(dsn, "repo")
        assert await _scalar(dsn, "SELECT count(*) FROM repo") == 1
    finally:
        await asyncio.to_thread(command.downgrade, config, "base")


@pytest.mark.asyncio
async def test_0012_up_down_on_a_populated_schema(pgvector_container) -> None:  # type: ignore[no-untyped-def]
    """Plan Phase 11/6: an existing provenance row reads `false` after upgrade
    and a downgrade drops only the new column."""
    dsn = pgvector_container.get_connection_url()
    config = alembic_config_for(dsn)

    await asyncio.to_thread(command.upgrade, config, "0011")
    try:
        await _execute(
            dsn,
            "INSERT INTO org (id, name, slug) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'o', 'o')",
        )
        await _execute(
            dsn,
            "INSERT INTO repo (id, org_id, name, full_name) VALUES "
            "('00000000-0000-0000-0000-000000000002', "
            "'00000000-0000-0000-0000-000000000001', 'r', 'o/r')",
        )
        await _execute(
            dsn,
            "INSERT INTO posted_finding_comment (id, repo_id, pr_number, commit_sha, "
            "finding_file, finding_message) VALUES (1, "
            "'00000000-0000-0000-0000-000000000002', 1, 'abc', 'a.py', 'm')",
        )

        await asyncio.to_thread(command.upgrade, config, "0012")
        assert await _scalar(dsn, "SELECT carried_suggestion FROM posted_finding_comment") is False

        await asyncio.to_thread(command.downgrade, config, "0011")
        assert "carried_suggestion" not in await _column_names(dsn, "posted_finding_comment")
        assert await _scalar(dsn, "SELECT count(*) FROM posted_finding_comment") == 1
    finally:
        await asyncio.to_thread(command.downgrade, config, "base")
