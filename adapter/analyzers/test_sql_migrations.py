"""RQ8 (SQL half): unsafe migrations are found by the pinned Semgrep SQL
ruleset (no separate Python analyzer was needed) with no LLM call, through the
real ``StaticAnalysisStep`` and the real ``semgrep`` binary."""

from __future__ import annotations

import hashlib

import pytest

from adapter.analyzers.conftest import RunStep
from adapter.analyzers.semgrep import SQL_MIGRATIONS_RULESET_PATH, SemgrepAnalyzer


def test_pinned_sql_ruleset_checksum_matches() -> None:
    actual = hashlib.sha256(SQL_MIGRATIONS_RULESET_PATH.read_bytes()).hexdigest()
    pinned = SQL_MIGRATIONS_RULESET_PATH.with_suffix(".yaml.sha256").read_text().strip()
    assert actual == pinned, "re-pin deliberately if the SQL ruleset change is intentional"


_UNSAFE = b"""CREATE INDEX idx_card ON canvas_connections (card_id);
DROP POLICY p ON t;
DROP TABLE old_t;
ALTER TABLE t DROP COLUMN c;
"""
_CLEAN = b"""CREATE INDEX CONCURRENTLY idx_card ON canvas_connections (card_id);
create unique index concurrently if not exists i2 on t (b);
DROP TABLE IF EXISTS old_t;
ALTER TABLE t DROP COLUMN IF EXISTS c;
"""


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rq8_unsafe_migration_found_through_step_with_no_llm_call(run_step: RunStep) -> None:
    analyzer = SemgrepAnalyzer(SQL_MIGRATIONS_RULESET_PATH)
    out = await run_step({"migrations/001.sql": _UNSAFE}, {"sql_migrations": analyzer})
    got = {(f.rule_id, f.line) for f in out.findings}
    assert got == {
        ("review-sql-index-not-concurrent", 1),
        ("review-sql-drop-policy", 2),
        ("review-sql-drop-missing-if-exists", 3),
        ("review-sql-drop-missing-if-exists", 4),
    }
    assert all(f.source == "semgrep" for f in out.findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rq8_clean_migration_and_non_sql_files_stay_clean(run_step: RunStep) -> None:
    analyzer = SemgrepAnalyzer(SQL_MIGRATIONS_RULESET_PATH)
    content = {"migrations/002.sql": _CLEAN, "notes.md": b"CREATE INDEX x ON t (a);\n"}
    out = await run_step(content, {"sql_migrations": analyzer})
    assert out.findings == []


_COMMENTED_AND_QUOTED = b"""-- CREATE INDEX idx ON t (a);
/* DROP POLICY p ON t;
   DROP TABLE old_t; */
SELECT 'DROP TABLE old_t', 'CREATE INDEX i ON t (a)';
COMMENT ON TABLE t IS 'it''s DROP POLICY'; -- don't DROP VIEW v
DROP POLICY real_one ON t; /* trailing DROP TABLE x */
"""


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rq8_matches_inside_comments_and_string_literals_are_not_flagged(
    run_step: RunStep,
) -> None:
    analyzer = SemgrepAnalyzer(SQL_MIGRATIONS_RULESET_PATH)
    out = await run_step(
        {"migrations/003.sql": _COMMENTED_AND_QUOTED}, {"sql_migrations": analyzer}
    )
    assert {(f.rule_id, f.line) for f in out.findings} == {("review-sql-drop-policy", 6)}
