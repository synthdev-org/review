"""private_repo_review_count

Phase 7 of the Admin UI delivery plan (Unit H, US-A19, Assumption 2): the
free-tier usage counter — "nothing counts reviews per org per day today,"
per that phase's own "Why next" note. One row per
``(installation_id, day)``, atomically incremented via
``INSERT ... ON CONFLICT DO UPDATE count = count + 1``
(``adapter/storage/tenancy.py``'s ``PostgresUsageCounterRepository``),
mirroring ``0008_admin_iam_schema.py``'s ``login_attempt`` table's own
atomic-upsert shape for the same structural problem (bounded-window
occurrence counting) — the precedent that table's own docstring already
points back to this table for.

``day`` is a plain ``Date`` (not ``DateTime``) — already the 04:00-anchored
calendar day computed by ``adapter/storage/tenancy.py``'s pure ``day_for``
helper before it ever reaches this table, per ADR-0008's "reset 04:00, not
midnight" Decision. ``installation_id`` and ``day`` are both non-nullable
(unlike ``OrgRepoTeamSetting``'s/``ModelRoutingConfig``'s scope columns),
so a native ``ON CONFLICT`` target works correctly here with no
``isnot_distinct_from`` NULL-handling needed.

Depends on 0009 (the latest revision at the time this migration was
drafted — verified via ``adapter/storage/migrations/versions/`` before
writing this file).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "private_repo_review_count",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint(
            "installation_id", "day", name="uq_private_repo_review_count_installation_day"
        ),
    )


def downgrade() -> None:
    op.drop_table("private_repo_review_count")
