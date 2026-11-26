"""index_chunk line_range: start_line/end_line

Additive-only, nullable
``start_line``/``end_line`` integer columns on ``index_chunk`` — needed
because citation rendering (spec-context-builder.md's US6 "line
range" requirement) has no other source for it: ``byte_range`` alone can't
be converted to a line number without fetching a caller/similar-chunk's
*entire* file just to count newlines, an extra per-item I/O cost this
column exists specifically to avoid.

``index_graph_node`` deliberately does **not** get these columns — nothing ever reads a line range
off it; its rows come from ``indexing/graph.py``'s ``extract_graph()``, a structurally
separate model with no byte-range/line concept at all.

This is a documented, deliberate override of
docs/specs/spec-context-builder.md's own Rollout & Release Safety section
("No new Alembic migration ... genuinely nothing new to migrate").

No backfill: nullable, populated going forward as repos are re-indexed; a
pre-migration chunk's ``line_range`` is simply ``NULL`` until its file is
re-indexed.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("index_chunk", sa.Column("start_line", sa.Integer(), nullable=True))
    op.add_column("index_chunk", sa.Column("end_line", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("index_chunk", "end_line")
    op.drop_column("index_chunk", "start_line")
