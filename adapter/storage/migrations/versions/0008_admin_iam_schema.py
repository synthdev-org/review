"""admin/iam schema: admin_user, admin_session, admin_audit_log, login_attempt

Additive-only — four brand-new tables backing the first human-identity auth
surface in this codebase (``apps/admin_api``'s local login/session). The
admin API is a brand-new, independently deployable image, so not running it
has zero effect on webhook_api/worker.

- ``admin_user`` — one row per human operator; ``username``
  carries its own ``UniqueConstraint``, ordinary integrity protection
  alongside the bootstrap routine's own advisory-lock race closure
  (``apps/admin_api/bootstrap.py``).
- ``admin_session`` — opaque, hash-lookup session credential, never a JWT; ``token_hash`` is unique.
- ``admin_audit_log`` — the single, generic mutating-admin-action audit log
  every later phase's routes write to.
- ``login_attempt`` — the Postgres-backed windowed login-rate-limit counter,
  unique on ``(username, window_start)``.

See adapter/storage/models.py for the ORM definitions this was hand-written
against, following 0005_memory_schema.py's exact shape (explicit
``sa.Column``/``ForeignKeyConstraint``/``UniqueConstraint`` calls, no ORM
model imports).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("ADMIN", "VIEWER", name="admin_role", native_enum=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
    )

    op.create_table(
        "admin_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["admin_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_admin_session_token_hash"),
    )

    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # No ondelete=CASCADE — see adapter/storage/models.py's
        # AdminAuditLogRow docstring for why a historical audit row must
        # keep resolving to its actor even after that actor is deactivated.
        sa.ForeignKeyConstraint(["actor_user_id"], ["admin_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "login_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", "window_start", name="uq_login_attempt_username_window"),
    )


def downgrade() -> None:
    op.drop_table("login_attempt")
    op.drop_table("admin_audit_log")
    op.drop_table("admin_session")
    op.drop_table("admin_user")
