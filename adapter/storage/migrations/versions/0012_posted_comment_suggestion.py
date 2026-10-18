"""posted_finding_comment.carried_suggestion

Phase 11 (auto-fix, ``docs/plans/plan-phase-11.md`` Phase 6): additive only on
top of 0011 — one ``NOT NULL DEFAULT false`` column recording whether a posted
review comment carried a suggested-change block, so the Admin Memory list can
mark fix threads. Old Workers never write it; existing rows read ``false``.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-18 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posted_finding_comment",
        sa.Column("carried_suggestion", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("posted_finding_comment", "carried_suggestion")
