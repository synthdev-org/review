"""job_queue.job_type: widen to fit MEMORY_REACTION_POLL

Phase 4 (Memory delivery plan, Unit D) correction — this plan's own
Assumption 5 said a migration was needed "only if inspection finds a CHECK
constraint this new value would violate," reasoning from
``0001_baseline_schema.py``'s ``sa.Enum("INDEX", "REVIEW", name="job_type",
native_enum=False)`` having no ``create_constraint=True`` (so, correctly, no
``CHECK`` constraint blocks a new value). What that reasoning missed, caught
by a real integration-test failure during this phase's own implementation
rather than left silently wrong: SQLAlchemy's ``Enum(..., native_enum=False)``
also renders a ``VARCHAR`` sized from the *longest enum member name* known at
the point the DDL was generated — ``max(len("INDEX"), len("REVIEW")) == 6``,
so ``0001``'s migration created ``job_type VARCHAR(6)``. SQLAlchemy stores an
``Enum`` column's Python member *name* (``JobType.MEMORY_REACTION_POLL.name``
== ``"MEMORY_REACTION_POLL"``, 21 characters), not its ``.value`` — a real
``StringDataRightTruncationError`` from Postgres, not a hypothetical one.

Widened to ``VARCHAR(32)`` — matching this schema's existing
``String(32)``-sized short-code columns (``memory_entry.entry_type``,
``memory_entry.raw_signal_type``) — comfortable headroom over
``MEMORY_REACTION_POLL``'s own 21 characters for a future job type name too,
without going so wide it stops looking like a bounded, deliberately-sized
column.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NARROW_JOB_TYPE = sa.Enum("INDEX", "REVIEW", name="job_type", native_enum=False, length=6)
_WIDE_JOB_TYPE = sa.Enum(
    "INDEX", "REVIEW", "MEMORY_REACTION_POLL", name="job_type", native_enum=False, length=32
)


def upgrade() -> None:
    op.alter_column(
        "job_queue",
        "job_type",
        existing_type=_NARROW_JOB_TYPE,
        type_=_WIDE_JOB_TYPE,
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing back to VARCHAR(6) fails outright (the same
    # StringDataRightTruncationError this migration exists to fix) against
    # any row this migration's own widened column ever allowed to exist —
    # deleted here first, the same "downgrading past this migration losing
    # this data is this downgrade's very definition, not a new data-loss bug
    # it introduces" reasoning `0006_memory_entry_signal_variant.py`'s own
    # downgrade already establishes for an analogous case.
    op.execute("DELETE FROM job_queue WHERE job_type = 'MEMORY_REACTION_POLL'")
    op.alter_column(
        "job_queue",
        "job_type",
        existing_type=_WIDE_JOB_TYPE,
        type_=_NARROW_JOB_TYPE,
        existing_nullable=False,
    )
