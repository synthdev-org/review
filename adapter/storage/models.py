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

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Dimension of the bundled local embedding model
# (`Xenova/all-MiniLM-L6-v2`, per the plan's Assumption 2/Phase 4 section) —
# duplicated from `adapter/embedding/local_onnx.py`'s own
# `EMBEDDING_DIMENSION` constant rather than imported, since this module
# (infra-adjacent but still storage-only) has no reason to import an
# embedding-adapter module just for one int; both are pinned to the same
# model and would need to change together regardless.
_EMBEDDING_DIMENSION = 384


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
    # Which GitHub App installation this repo belongs to. Nullable, no
    # default, per the plan's Phase 1 section: existing Phase-0-era rows
    # predate any installation mapping and have no value to backfill; Phase
    # 1's own resolution logic (tenancy/resolution.py via
    # adapter/storage/tenancy.py) only ever reads/writes it for rows it
    # itself provisions or updates, treating NULL as "not yet resolved"
    # rather than an invariant violation.
    installation_id: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    # The default branch's own last-indexed commit SHA (Phase 6 of the
    # indexing plan) — nullable, no default, the same "NULL means not yet
    # resolved" pattern `installation_id` above already uses. Scoped to the
    # default branch only (per the plan's Phase 6 section): a PR-scoped
    # job's delta calculation compares against this same repo-wide cursor,
    # never a separate per-PR cursor, and only a `pr_number == 0` job's
    # completed run ever advances it (Phase 7's own ordering rule, closing
    # the cursor-level poisoning path an independent security review found).
    last_indexed_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class WebhookDeliveryRow(Base):
    """Anti-replay record of a uniquely-delivered GitHub webhook, per the
    plan's Phase 1 section (closing the gap Phase 0's own plan flagged
    forward: "Phase 1 ... should dedupe on GitHub's ``X-GitHub-Delivery``
    header before this becomes a real vector").

    ``id`` *is* the delivery id (GitHub's own ``X-GitHub-Delivery`` header
    value) rather than a separate surrogate key — its own uniqueness is
    exactly the anti-replay invariant this table exists to enforce, so a
    second primary-key column would add nothing.
    ``adapter/storage/tenancy.py``'s ``record_delivery_if_new`` inserts via
    Postgres's native ``INSERT ... ON CONFLICT DO NOTHING`` against this
    primary key rather than a raw ``IntegrityError`` catch, per that
    module's own docstring.

    No retention/pruning policy yet — every uniquely-delivered webhook
    inserts a permanent row here, including ones later rejected by the
    per-repo rate limiter. Unbounded Postgres growth over time is a named,
    accepted gap for this phase; a scheduled pruning job is Phase 8
    operational hardening, not built here.
    """

    __tablename__ = "webhook_delivery"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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


class IndexChunkRow(Base):
    """One AST-aware chunk (``indexing.chunk.Chunk``) plus its embedding
    vector (``indexing.embedding.EmbeddingVector``), per the plan's Phase 6
    section (US3/US4).

    ``pr_number`` (non-null, default ``0`` — ``0`` means default-branch/
    canonical, per Assumption 9) is folded into the unique key alongside
    ``file_path``/``chunk_index`` so a PR-branch row (``pr_number > 0``, a
    potentially zero-permission-fork-authored push) can never collide with,
    and therefore never overwrite, the canonical ``pr_number = 0`` row for
    the same file path — closing the fork-PR data-poisoning gap an
    independent security review found in the original single-key design.
    """

    __tablename__ = "index_chunk"
    __table_args__ = (
        UniqueConstraint(
            "repo_id",
            "pr_number",
            "file_path",
            "chunk_index",
            name="uq_index_chunk_repo_pr_file_chunk",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer(), nullable=False, default=0, server_default="0")
    file_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    byte_range_start: Mapped[int] = mapped_column(Integer(), nullable=False)
    byte_range_end: Mapped[int] = mapped_column(Integer(), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIMENSION), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class IndexGraphNodeRow(Base):
    """One call/import-graph definition node (``indexing.graph.GraphNode``),
    per the plan's Phase 6 section (US5). Same ``pr_number`` row-scoping
    reasoning as ``IndexChunkRow`` above.
    """

    __tablename__ = "index_graph_node"
    __table_args__ = (
        UniqueConstraint(
            "repo_id",
            "pr_number",
            "file_path",
            "symbol_name",
            name="uq_index_graph_node_repo_pr_file_symbol",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer(), nullable=False, default=0, server_default="0")
    file_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    symbol_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class IndexGraphEdgeRow(Base):
    """One candidate call/import edge (``indexing.graph.GraphEdge``), per
    the plan's Phase 6 section (US5/US7).

    ``target_symbol_name`` is a plain string, deliberately **not** a
    foreign key, per US5/US7's dangling-reference design (a call/import
    target that never matches any node, or a node whose file was since
    deleted, is left in place as a dangling reference resolved as "no
    match" by a future query — never an FK violation).

    Unique on ``(source_node_id, target_symbol_name)`` — an independent
    security review found the original draft specified no uniqueness key
    here at all, so ordinary repeated pushes to an active file could
    accumulate duplicate edge rows without bound. ``pr_number`` doesn't
    need to be repeated here since it's already carried transitively via
    ``source_node_id``'s own ``(repo_id, pr_number, ...)`` scoping on
    ``IndexGraphNodeRow``. ``source_node_id``'s own ``ondelete=CASCADE``
    is what deletes an edge when its *source* node is deleted (US7) — no
    separate explicit delete needed for that case.

    ``target_symbol_name`` also carries its own standalone index, separate
    from the compound uniqueness key above (which leads with
    ``source_node_id`` and so can't serve a "what depends on this symbol"
    lookup) — the blast-radius query ADR-0007/spec-indexing name as this
    table's own stated purpose filters by ``target_symbol_name`` alone.
    """

    __tablename__ = "index_graph_edge"
    __table_args__ = (
        UniqueConstraint(
            "source_node_id", "target_symbol_name", name="uq_index_graph_edge_source_target"
        ),
        Index("ix_index_graph_edge_target_symbol_name", "target_symbol_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("index_graph_node.id", ondelete="CASCADE"), nullable=False
    )
    target_symbol_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
