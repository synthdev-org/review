"""clone fingerprints: index_clone_file, index_clone_fingerprint,
repo.clone_fingerprint_version

Phase 10 (clone detection, ``docs/plans/plan-phase-10.md`` Phase 2): additive
only on top of 0010 — two new tables and one ``NOT NULL DEFAULT 0`` column on
``repo``; no existing column or table is altered, so code that predates this
migration simply ignores the new objects.

``index_clone_file`` holds each indexed file's normalized per-token hashes and
cumulative line map (verification data, so a fingerprint hit can be checked to
token equality without refetching code). ``index_clone_fingerprint`` holds the
sampled winnowing fingerprints, with a ``(repo_id, pr_number, hash)`` index for
the review step's batched ``hash = ANY(...)`` lookup. Both are scoped by
``(repo_id, pr_number, file_path)`` like ``index_chunk``, and both cascade from
``repo``. ``repo.clone_fingerprint_version`` records the fingerprint schema
version a repo was last fully backfilled at (0 = never).

Depends on 0010 (the latest revision at the time this migration was drafted —
verified via ``adapter/storage/migrations/versions/`` before writing this file).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo",
        sa.Column("clone_fingerprint_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "index_clone_file",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.String(length=4096), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("token_hashes", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("line_first_token", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repo_id", "pr_number", "file_path"),
    )
    op.create_table(
        "index_clone_fingerprint",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.String(length=4096), nullable=False),
        sa.Column("token_offset", sa.Integer(), nullable=False),
        sa.Column("hash", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repo_id", "pr_number", "file_path", "token_offset"),
    )
    op.create_index(
        "ix_index_clone_fingerprint_repo_pr_hash",
        "index_clone_fingerprint",
        ["repo_id", "pr_number", "hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_index_clone_fingerprint_repo_pr_hash", table_name="index_clone_fingerprint")
    op.drop_table("index_clone_fingerprint")
    op.drop_table("index_clone_file")
    op.drop_column("repo", "clone_fingerprint_version")
