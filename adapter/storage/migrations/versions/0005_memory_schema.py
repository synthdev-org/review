"""memory schema: posted_finding_comment, memory_entry, captured_reaction

Phase 1 (Memory delivery plan, Unit A): additive-only on top of
0001_baseline_schema.py .. 0004_index_chunk_line_range.py, per that plan's
Observability & rollback section ("new tables and domain code only —
nothing reads or writes them yet ... fully reversible by reverting this
migration and commit with zero interaction with anything already shipped").

- ``posted_finding_comment`` — US-M11's provenance mapping (comment id,
  repo/PR/commit, finding fingerprint). ``id`` is GitHub's own comment id,
  not a separate surrogate key, mirroring ``webhook_delivery``'s own
  "natural key is the invariant" precedent.
- ``memory_entry`` — one captured signal (raw, or human-classified once
  approved), per ADR-0009. ``uq_memory_entry_signal`` is Phase 3's
  redelivery-idempotency key (``posted_finding_comment_id``,
  ``raw_signal_type``, ``github_comment_id``).
- ``captured_reaction`` — Assumption 9's reaction-dedup junction table, a
  composite primary key rather than a JSONB containment query.

See adapter/storage/models.py for the ORM definitions this was hand-written
against (following 0003_indexing_schema.py's exact shape: explicit
``sa.Column``/``ForeignKeyConstraint``/``UniqueConstraint`` calls, ``Vector``
import from ``pgvector.sqlalchemy``, a duplicated ``_EMBEDDING_DIMENSION``
constant per that file's own "duplicated ... rather than importing across a
package boundary for one int" precedent).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dimension of the bundled local embedding model
# (`Xenova/all-MiniLM-L6-v2`) — duplicated from adapter/storage/models.py's
# own `_EMBEDDING_DIMENSION` constant, the same way 0003_indexing_schema.py
# already duplicates it rather than importing across a package boundary for
# one int.
_EMBEDDING_DIMENSION = 384


def upgrade() -> None:
    op.create_table(
        "posted_finding_comment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("finding_file", sa.String(length=4096), nullable=False),
        sa.Column("finding_line", sa.Integer(), nullable=True),
        sa.Column("finding_rule_id", sa.String(length=255), nullable=True),
        sa.Column("finding_message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "memory_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("repo_id", sa.Uuid(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("entry_type", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "APPROVED", "REJECTED", name="memory_entry_status", native_enum=False
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("raw_signal_type", sa.String(length=32), nullable=False),
        sa.Column("raw_signal_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("posted_finding_comment_id", sa.BigInteger(), nullable=False),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("embedding", Vector(_EMBEDDING_DIMENSION), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
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
        sa.ForeignKeyConstraint(["org_id"], ["org.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repo_id"], ["repo.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["team.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["posted_finding_comment_id"], ["posted_finding_comment.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "posted_finding_comment_id",
            "raw_signal_type",
            "github_comment_id",
            name="uq_memory_entry_signal",
        ),
    )

    op.create_table(
        "captured_reaction",
        sa.Column("posted_finding_comment_id", sa.BigInteger(), nullable=False),
        sa.Column("reaction_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["posted_finding_comment_id"], ["posted_finding_comment.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "posted_finding_comment_id", "reaction_id", name="pk_captured_reaction"
        ),
    )


def downgrade() -> None:
    op.drop_table("captured_reaction")
    op.drop_table("memory_entry")
    op.drop_table("posted_finding_comment")
