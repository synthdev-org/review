"""memory_entry.approved_by_user_id

Phase 3 (Admin UI delivery plan, Unit D, Assumption 6): adds one nullable
FK column, ``approved_by_user_id`` -> ``admin_user.id``, to the existing
``memory_entry`` table — the acting Admin's real identity at approval time,
distinct from the pre-existing free-text ``approved_by`` column (which no
current writer populates). No ``ondelete=CASCADE``, mirroring
``admin_audit_log.actor_user_id``'s own FK (0008_admin_iam_schema.py):
``admin_user`` rows are never hard-deleted (deactivation is a status flip),
so this never actually cascades in practice, but a historical approval
should keep resolving to its approver.

Depends on 0008 (creates ``admin_user``, the table this FK targets).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_entry",
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_memory_entry_approved_by_user_id_admin_user",
        "memory_entry",
        "admin_user",
        ["approved_by_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_memory_entry_approved_by_user_id_admin_user", "memory_entry", type_="foreignkey"
    )
    op.drop_column("memory_entry", "approved_by_user_id")
