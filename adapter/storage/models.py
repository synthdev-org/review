"""adapter.storage.models — SQLAlchemy 2.0 declarative models for the
Postgres schema baseline.

Scope:

- Tenancy scaffolding: ``Org``, ``Repo``, ``Team`` — enough for the job
  queue and config tables below to have somewhere to point their foreign
  keys. Full RBAC/scoping is a later roadmap phase.
- ``JobQueueRow`` — the Postgres-backed job queue (per the C4 doc's Job
  Queue container). Claiming uses SQLAlchemy's native
  ``with_for_update(skip_locked=True)`` (see ``adapter/storage/queue.py``),
  never hand-rolled raw SQL.
- ``ModelRoutingConfig`` / ``OrgRepoTeamSetting`` — day-two platform config
  *scaffolding only* (model routing and per-org/repo/team setting
  placeholders). Per the C4 doc's "Configuration & Secrets" split and
  security considerations, GitHub App and LLM *credentials* are bootstrap
  config read once via ``pydantic-settings`` at each entrypoint
  (``apps/webhook_api/settings.py``, ``apps/worker/settings.py``) — they
  are never modeled or written to any table here. Coupling credential
  storage to this schema would be the exact mistake the C4 doc's
  bootstrap-vs-day-two split exists to prevent.

Per ``docs/rules/architecture.md``, this is infrastructure (an adapter) —
domain packages (``tenancy``, etc.) never import SQLAlchemy or these
classes directly; they depend on domain-defined ``typing.Protocol``
interfaces added as real callers appear.
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
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Dimension of the bundled local embedding model
# (`Xenova/all-MiniLM-L6-v2`) —
# duplicated from `adapter/embedding/local_onnx.py`'s own
# `EMBEDDING_DIMENSION` constant rather than imported, since this module
# (infra-adjacent but still storage-only) has no reason to import an
# embedding-adapter module just for one int; both are pinned to the same
# model and would need to change together regardless.
_EMBEDDING_DIMENSION = 384


class Base(DeclarativeBase):
    """Shared declarative base for every table in this schema."""


class JobType(enum.StrEnum):
    """The job types the Worker dispatches, per the C4 doc's Level 2
    diagram (one Worker pool, dispatched by job type).

    ``MEMORY_REACTION_POLL`` needed a real Alembic migration even though no
    ``CHECK`` constraint blocks a new value (``0001_baseline_schema.py``'s
    ``sa.Enum(..., native_enum=False)`` sets no ``create_constraint=True``).
    What is easy to miss, and surfaces as a real
    ``StringDataRightTruncationError``: SQLAlchemy's
    ``Enum(..., native_enum=False)`` also renders a ``VARCHAR`` sized from
    the *longest enum member name* known when the DDL was generated
    (``max(len("INDEX"), len("REVIEW")) == 6``), and stores a column's
    Python member **name** (``"MEMORY_REACTION_POLL"``, 21 characters), not
    its ``.value`` — so the original ``VARCHAR(6)`` column couldn't hold
    this value at all. Widened to ``VARCHAR(32)`` by
    ``0007_job_queue_job_type_widen.py`` (see that migration's own
    docstring for the full story); ``length=32`` below keeps this ORM
    model's own DDL-generation in sync with that real column width, so
    Alembic's own autogenerate diffing never sees the two drift apart.
    """

    INDEX = "index"
    REVIEW = "review"
    MEMORY_REACTION_POLL = "memory_reaction_poll"


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
    # default: existing early rows
    # predate any installation mapping and have no value to backfill; the
    # resolution logic (tenancy/resolution.py via
    # adapter/storage/tenancy.py) only ever reads/writes it for rows it
    # itself provisions or updates, treating NULL as "not yet resolved"
    # rather than an invariant violation.
    installation_id: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    # The default branch's own last-indexed commit SHA — nullable, no
    # default, the same "NULL means not yet resolved" pattern
    # `installation_id` above already uses. Scoped to the default branch
    # only: a PR-scoped job's delta calculation compares against this same
    # repo-wide cursor, never a separate per-PR cursor, and only a
    # `pr_number == 0` job's completed run ever advances it (an ordering rule
    # that closes the cursor-level poisoning path).
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
        Enum(JobType, name="job_type", native_enum=False, validate_strings=True, length=32),
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
    """Anti-replay record of a uniquely-delivered GitHub webhook, so a replayed
    delivery (same ``X-GitHub-Delivery`` header) is a no-op.

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
    accepted gap; a scheduled pruning job is not built yet.
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
    vector (``indexing.embedding.EmbeddingVector``), US3/US4.

    ``pr_number`` (non-null, default ``0`` — ``0`` means default-branch/
    canonical) is folded into the unique key alongside
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
    # Nullable: populated going forward from `indexing.chunk.Chunk.line_range`, `NULL`
    # for a chunk indexed before this column's migration and never
    # re-indexed since. `index_graph_node` deliberately does not get these
    # columns.
    start_line: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer(), nullable=True)
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
    US5. Same ``pr_number`` row-scoping
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


class MemoryEntryStatus(enum.StrEnum):
    """A ``memory_entry`` row's lifecycle state — mirrors
    ``memory.models.MemoryStatus``'s literal values exactly (storage-layer
    enum vs. domain-layer ``Literal``, the same split ``JobType``/``JobStatus``
    already establish for ``job_queue``)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PostedFindingCommentRow(Base):
    """One inline review comment the platform actually posted, and which
    finding it was about — per US-M11. ``id`` *is* GitHub's own comment id, not a
    separate surrogate key — the same "the natural key already guarantees
    the invariant" reasoning ``WebhookDeliveryRow`` above already
    establishes, since this id is exactly what an inbound reply/reaction/
    thread-resolution signal correlates back against.

    ``finding_message`` is already-redacted text by the time it reaches
    here (``review/review_generation.py``'s existing ``_redact_all`` pass
    already ran before ``FindingsOutputStep`` ever sees it) — this table introduces no new
    redaction gap.
    """

    __tablename__ = "posted_finding_comment"

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_file: Mapped[str] = mapped_column(String(4096), nullable=False)
    finding_line: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    finding_rule_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    finding_message: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemoryEntryRow(Base):
    """One captured signal (raw, or human-classified once approved), per
    [ADR-0009](../../docs/adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md).

    ``org_id``/``repo_id``/``team_id`` are all nullable FKs — the same
    "narrow the scope" convention ``OrgRepoTeamSetting``/
    ``ModelRoutingConfig`` above already establish, not a new pattern.

    ``UniqueConstraint("posted_finding_comment_id", "raw_signal_type",
    "github_comment_id", "signal_variant")`` is the
    redelivery-idempotency key: a GitHub webhook redelivery of the same
    reply/thread-resolution event (GitHub's own documented at-least-once
    semantics) upserts via ``ON CONFLICT DO NOTHING`` against this key
    rather than creating a duplicate ``pending`` row every retry.
    ``github_comment_id`` is ``NULL`` for a ``reaction`` entry —
    that raw signal type's own dedup lives in ``captured_reaction`` instead.

    ``signal_variant`` (added by ``0006_memory_entry_signal_variant.py``,
    after a review found the original 3-column key
    collapsed a thread's ``resolved`` event and a later ``unresolved``
    event on that same thread into a single row — the original key has
    nothing that differs between them, since both share the same
    ``raw_signal_type="thread_resolution"`` and the same
    ``github_comment_id``) disambiguates same-thread, opposite-action
    events without weakening the redelivery guarantee: it's a plain
    ``NOT NULL`` column (never ``NULL``, so Postgres's own "NULLs are never
    equal" unique-index semantics can't silently defeat it the way a
    nullable/JSONB-expression column would), set to ``"resolved"``/
    ``"unresolved"`` by ``PostgresMemoryRepository.record_signal`` for a
    ``thread_resolution`` signal (derived from ``raw_signal_detail``, not a
    new caller-supplied parameter — no ``MemorySignalRecorder`` Protocol
    change) and left at its default ``""`` for every other signal type, so
    a genuine redelivery of a ``reply``/``reaction`` signal still collides
    on the same key it always has.
    """

    __tablename__ = "memory_entry"
    __table_args__ = (
        UniqueConstraint(
            "posted_finding_comment_id",
            "raw_signal_type",
            "github_comment_id",
            "signal_variant",
            name="uq_memory_entry_signal",
        ),
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
    entry_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[MemoryEntryStatus] = mapped_column(
        Enum(
            MemoryEntryStatus, name="memory_entry_status", native_enum=False, validate_strings=True
        ),
        nullable=False,
        default=MemoryEntryStatus.PENDING,
        server_default=MemoryEntryStatus.PENDING.value,
    )
    raw_signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_signal_detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    posted_finding_comment_id: Mapped[int] = mapped_column(
        ForeignKey("posted_finding_comment.id", ondelete="CASCADE"), nullable=False
    )
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    signal_variant: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_EMBEDDING_DIMENSION), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Nullable FK to `admin_user.id` — the acting Admin's real
    # identity at approval time, distinct from the pre-existing free-text
    # `approved_by` column above, which no current writer populates. No
    # `ondelete=CASCADE`, mirroring `AdminAuditLogRow.actor_user_id`'s own
    # reasoning: `admin_user` rows are never hard-deleted (deactivation is a
    # status flip), so this never actually cascades in practice, but a
    # historical approval should keep resolving to its approver the same
    # way a historical audit-log row does.
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_user.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CapturedReactionRow(Base):
    """Junction table backing US-M3's reaction dedup — a composite primary
    key, not a separate surrogate key (the natural key
    already guarantees the invariant), so a repeat poll of the same
    comment/reaction pair is an ``O(1)``-lookup no-op rather than a
    ``raw_signal_detail->>'reaction_id'`` predicate whose cost grows with
    total captured-signal volume.
    """

    __tablename__ = "captured_reaction"
    __table_args__ = (PrimaryKeyConstraint("posted_finding_comment_id", "reaction_id"),)

    posted_finding_comment_id: Mapped[int] = mapped_column(
        ForeignKey("posted_finding_comment.id", ondelete="CASCADE"), nullable=False
    )
    reaction_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)


class IndexGraphEdgeRow(Base):
    """One candidate call/import edge (``indexing.graph.GraphEdge``), per
    US5/US7.

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


class AdminRole(enum.StrEnum):
    """Storage-layer mirror of ``iam.models.Role`` (a domain ``Literal``) —
    the same domain-``Literal``-vs-storage-``enum.StrEnum`` split
    ``MemoryStatus``/``MemoryEntryStatus`` already establish."""

    ADMIN = "admin"
    VIEWER = "viewer"


class AdminUserRow(Base):
    """One human operator who can sign in to the Admin UI. ``username`` carries its own
    ``UniqueConstraint`` regardless of the bootstrap routine's own
    advisory-lock-based race closure (``apps/admin_api/bootstrap.py``) —
    ordinary integrity protection for ``POST /api/v1/users``. ``is_active``
    exists so the deactivation route needs no later migration; every row is
    created with the default ``True``.
    """

    __tablename__ = "admin_user"
    __table_args__ = (UniqueConstraint("username", name="uq_admin_user_username"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # Always an already-computed Argon2id hash
    # (adapter/auth/argon2_hasher.py) — this table never stores a plaintext
    # password. Argon2id's own encoded-hash format
    # ($argon2id$v=...$m=...,t=...,p=...$salt$hash) comfortably fits within
    # 255 characters for the OWASP-baseline cost parameters this codebase
    # uses.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        Enum(AdminRole, name="admin_role", native_enum=False, validate_strings=True),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AdminSessionRow(Base):
    """One ``admin_user``'s signed-in session. ``token_hash`` — never the raw session
    token itself — is the SHA-256 hex digest of the opaque token delivered
    via the ``HttpOnly`` session cookie; carries its own
    ``UniqueConstraint`` so ``PostgresSessionStore.get_by_token_hash``'s
    lookup is a real unique-index hit, not a table scan.

    ``ondelete="CASCADE"`` on ``user_id`` is defense-in-depth for a case
    this codebase never actually exercises (``admin_user`` rows are
    deactivated, never hard-deleted) — kept for the same "narrow the
    scope"-style integrity-protection reasoning
    ``adapter/storage/models.py``'s other FK columns already use.
    """

    __tablename__ = "admin_session"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_admin_session_token_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # US-A2's sliding-window idle expiry (iam.auth.is_session_expired) is
    # computed against this column — updated on every authenticated request
    # (PostgresSessionStore.touch_last_seen), never only at creation.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AdminAuditLogRow(Base):
    """One entry of the single, generic admin-action audit log — every mutating admin action
    (approve/reject/delete, config write,
    routing write, user management, bulk actions, login failures/successes)
    is recorded here, rather than bespoke attribution columns bolted onto
    every affected table.

    ``actor_user_id`` is nullable — a failed login attempt against a
    nonexistent username (or before any session exists at all) has no
    resolved actor to attribute; a successful login's row does carry the
    real ``id``. No ``ON DELETE CASCADE`` from ``admin_user`` is declared
    (``ondelete`` left unset, i.e. Postgres's own default ``NO ACTION``) —
    ``admin_user`` rows are deactivated, never hard-deleted, so a
    historical audit row always keeps resolving to the acting
    user's id even after that user is later deactivated (carried by
    the FK staying valid rather than by any cascade behavior).

    ``metadata`` is the column name (the audit log's field name); the Python
    attribute is named ``extra`` instead, since ``metadata`` collides with SQLAlchemy's own reserved
    ``DeclarativeBase.metadata`` class attribute.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_user.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LoginAttemptRow(Base):
    """A Postgres-backed windowed counter of failed login attempts per
    username — **not** the
    existing in-process ``InMemoryRateLimiter`` (undercounts across
    ``apps/admin_api``'s multiple stateless replicas) and **not** the
    general ``admin_audit_log`` table above (unbounded, no index/retention
    policy suited to this hot-path access pattern).

    ``(username, window_start)`` is this table's own uniqueness key — one
    row per username per fixed window, atomically incremented via
    ``INSERT ... ON CONFLICT DO UPDATE attempt_count = attempt_count + 1``
    (``adapter/storage/iam.py``'s ``PostgresLoginAttemptRepository``),
    mirroring ``LoginAttemptRow``'s atomic-upsert
    pattern for the same structural problem (bounded-window occurrence
    counting). ``PostgresLoginAttemptRepository`` also purges rows older
    than a few windows on every write, bounding this table's growth against
    a flood of attempts against nonexistent usernames (the Security review
    section's own residual-risk note).
    """

    __tablename__ = "login_attempt"
    __table_args__ = (
        UniqueConstraint("username", "window_start", name="uq_login_attempt_username_window"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
