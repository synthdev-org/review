"""memory_entry.signal_variant: widen the redelivery-idempotency key

Fix on top of 0005's own ``uq_memory_entry_signal`` — a code-review pass
on the first implementation found that a thread's ``resolved`` event and a later
``unresolved`` event on the *same* thread shared an identical
(``posted_finding_comment_id``, ``raw_signal_type``, ``github_comment_id``)
key (``raw_signal_type`` is always ``"thread_resolution"`` and
``github_comment_id`` is always the thread's own last comment id,
regardless of which action fired), so the second event's ``ON CONFLICT DO
NOTHING`` silently no-opped — losing real state-change data, not just
protecting against GitHub's own redelivery of the identical event.

``signal_variant`` is a plain ``NOT NULL`` column (default ``""``), not a
nullable one or a JSONB expression, precisely so this migration cannot
reintroduce the same class of bug via Postgres's own "NULLs are never
equal" unique-index semantics: every existing signal type keeps colliding
on redelivery exactly as before (empty string is equal to empty string),
while ``PostgresMemoryRepository.record_signal`` now sets it to
``"resolved"``/``"unresolved"`` for a ``thread_resolution`` signal only,
so the two actions on one thread now produce two distinct, intentionally
kept rows.

See adapter/storage/models.py's ``MemoryEntryRow`` docstring for the full
design; ``adapter/storage/memory_repository.py``'s ``record_signal`` for
where ``signal_variant`` is populated.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_entry",
        sa.Column("signal_variant", sa.String(length=16), server_default="", nullable=False),
    )
    op.drop_constraint("uq_memory_entry_signal", "memory_entry", type_="unique")
    op.create_unique_constraint(
        "uq_memory_entry_signal",
        "memory_entry",
        ["posted_finding_comment_id", "raw_signal_type", "github_comment_id", "signal_variant"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_memory_entry_signal", "memory_entry", type_="unique")
    # Collapse any row that only this migration's widened constraint ever
    # allowed to coexist (e.g. a thread's `resolved` and `unresolved` rows,
    # which share an identical (posted_finding_comment_id, raw_signal_type,
    # github_comment_id) triple and differ only by the `signal_variant`
    # column this downgrade is about to drop) down to one, keeping the
    # earliest, before re-adding the narrower 3-column constraint being
    # reverted to — otherwise `ADD CONSTRAINT` below fails outright on any
    # such data. Downgrading past this migration losing the second toggle
    # event's own row is this downgrade's very definition, not a new
    # data-loss bug it introduces.
    op.execute(
        """
        DELETE FROM memory_entry
        WHERE id NOT IN (
            SELECT DISTINCT ON (posted_finding_comment_id, raw_signal_type, github_comment_id) id
            FROM memory_entry
            ORDER BY posted_finding_comment_id, raw_signal_type, github_comment_id, created_at
        )
        """
    )
    op.create_unique_constraint(
        "uq_memory_entry_signal",
        "memory_entry",
        ["posted_finding_comment_id", "raw_signal_type", "github_comment_id"],
    )
    op.drop_column("memory_entry", "signal_variant")
