"""adapter.storage.models — SQLAlchemy 2.0 declarative models for the
Postgres schema baseline (Phase 2 of the delivery plan).

Scope, per the plan's Phase 2 section:

- Tenancy scaffolding: ``Org``, ``Repo``, ``Team`` — enough for the job
  queue and config tables below to have somewhere to point their foreign
  keys. Full RBAC/scoping is a later roadmap phase.
- ``JobQueueRow`` — the Postgres-backed job queue (per the C4 doc's Job
  Queue container). Claiming uses SQLAlchemy's native
  ``with_for_update(skip_locked=True)`` (see ``adapter/storage/queue.py``),
  never hand-rolled raw SQL.
- ``ModelRoutingConfig`` / ``OrgRepoTeamSetting`` — day-two platform config
  *scaffolding only* (model routing and per-org/repo/team setting
  placeholders). Per the C4 doc's "Configuration & Secrets" split and this
  plan's Security review, GitHub App and LLM *credentials* are bootstrap
  config read once via ``pydantic-settings`` at each entrypoint
  (``apps/webhook_api/settings.py``, ``apps/worker/settings.py``) — they
  are never modeled or written to any table here. Coupling credential
  storage to this schema would be the exact mistake the C4 doc's
  bootstrap-vs-day-two split exists to prevent.

Per ``docs/rules/architecture.md``, this is infrastructure (an adapter) —
domain packages (``tenancy``, etc.) never import SQLAlchemy or these
classes directly; they depend on domain-defined ``typing.Protocol``
interfaces that a later phase adds as real callers appear.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for every table in this schema."""


class JobType(enum.StrEnum):
    """The two job types the Worker dispatches, per the C4 doc's Level 2
    diagram (one Worker pool, dispatched by job type)."""

    INDEX = "index"
    REVIEW = "review"


class JobStatus(enum.StrEnum):
    """Lifecycle of a `job_queue` row from enqueue to terminal state."""

    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"


class Org(Base):
    """Tenancy root. One row per customer organization."""

    __tablename__ = "org"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    repos: Mapped[list[Repo]] = relationship(back_populates="org", cascade="all, delete-orphan")
    teams: Mapped[list[Team]] = relationship(back_populates="org", cascade="all, delete-orphan")


class Repo(Base):
    """A single git repository under an ``Org``."""

    __tablename__ = "repo"
    __table_args__ = (UniqueConstraint("org_id", "full_name", name="uq_repo_org_full_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(511), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    org: Mapped[Org] = relationship(back_populates="repos")


class Team(Base):
    """A team scope under an ``Org``, used later for review-setting scoping."""

    __tablename__ = "team"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_team_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    org: Mapped[Org] = relationship(back_populates="teams")


class JobQueueRow(Base):
    """Postgres-backed job queue row — claimed via
    ``with_for_update(skip_locked=True)`` (see ``adapter/storage/queue.py``),
    not a separate broker, per the C4 doc's "No broker beyond Postgres" note.
    """

    __tablename__ = "job_queue"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_type: Mapped[JobType] = mapped_column(
        Enum(JobType, name="job_type", native_enum=False, validate_strings=True),
        nullable=False,
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=False, validate_strings=True),
        nullable=False,
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org.id", ondelete="CASCADE"), nullable=True
    )
    repo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ModelRoutingConfig(Base):
    """Day-two platform config *scaffolding*: which model to use for a given
    pipeline stage, optionally scoped to a single org (``org_id`` NULL means
    the platform-wide default).

    Placeholder only — no reader/writer logic exists yet; a later roadmap
    phase adds the admin API and pipeline-stage code that consult this
    table. LLM *credentials* are never stored here (see module docstring).
    """

    __tablename__ = "model_routing_config"
    __table_args__ = (UniqueConstraint("org_id", "stage", name="uq_model_routing_org_stage"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org.id", ondelete="CASCADE"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgRepoTeamSetting(Base):
    """Day-two platform config *scaffolding*: a generic per-org/repo/team
    setting placeholder (rules, Memory toggles, etc. land here in later
    roadmap phases). ``repo_id``/``team_id`` NULL narrows the scope up to
    org-wide or platform-wide (when ``org_id`` is also NULL).

    Placeholder only — no reader/writer logic exists yet.
    """

    __tablename__ = "org_repo_team_setting"
    __table_args__ = (
        UniqueConstraint("org_id", "repo_id", "team_id", "key", name="uq_setting_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("org.id", ondelete="CASCADE"), nullable=True
    )
    repo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("team.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
