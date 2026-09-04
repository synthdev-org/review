"""installation_id and webhook_delivery

Phase 1: adds `repo.installation_id` (nullable, no default — existing
Phase-0-era rows predate any installation mapping, see
adapter/storage/models.py's `Repo.installation_id` docstring) and the
`webhook_delivery` table (anti-replay dedupe on GitHub's
`X-GitHub-Delivery` header, see `adapter/storage/models.py`'s
`WebhookDeliveryRow` docstring). The first migration since Phase 0's
baseline (0001) — proves the "every phase adds a migration, never edits
the baseline" discipline that plan established actually holds.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repo", sa.Column("installation_id", sa.BigInteger(), nullable=True))
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("webhook_delivery")
    op.drop_column("repo", "installation_id")
