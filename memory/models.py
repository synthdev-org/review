"""memory.models — the domain models Phase 4 (Memory) reads and writes, per
``docs/plans/plan-deliver-memory.md``'s Phase 1 section (Unit A).

``MemoryEntry`` is the row-level shape of a single captured signal
(``reaction``/``reply``/``thread_resolution``), classified by a human at
approval time (``entry_type``), per
[ADR-0009](../docs/adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md)
decision 1 ("capture is deterministic only, never sentiment/keyword
classification"). ``PostedComment`` is the shape
``adapter/github/posting.py`` (Phase 2) hands back after successfully
posting a batch of inline review comments, so their provenance can be
persisted against the finding each one came from.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule — every field here is a plain pydantic/``typing`` type, never a
SQLAlchemy column or ``uuid.UUID`` (ids are plain ``str``, the same
domain-uses-``str``-ids-adapter-converts-to-``uuid.UUID`` convention
``review/context_builder.py``'s ``GraphQuery``/``SimilarityQuery`` Protocols
already establish).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

MemoryStatus = Literal["pending", "approved", "rejected"]
"""A ``memory_entry`` row's lifecycle state, per US-M4/US-M5: every entry
starts ``pending`` at capture time, and a human moves it to ``approved`` or
``rejected`` via the admin API (Phase 5) — reversibly, in either direction.
"""

MemoryEntryType = Literal["dismissed_finding", "accepted_fix", "correction"] | None
"""What an approved entry means, assigned by a human at approval time, never
inferred from the raw signal itself (ADR-0009 decision 1). ``None`` for
every ``pending`` entry (classification hasn't happened yet) and, per this
plan's own ``set_status`` design, for a ``rejected`` entry (a later
re-approval always supplies its own fresh ``entry_type``, per US-M5's "a
fresh pending entry" framing — reject clears any prior classification
rather than leaving a stale one behind).
"""

RawSignalType = Literal["reaction", "reply", "thread_resolution"]
"""Which kind of raw signal a ``memory_entry`` row captures — a GitHub
reaction on a posted comment (Phase 4/Unit D), a reply to one (Phase 3/Unit
C), or a review-thread resolution/unresolution (Phase 3/Unit C)."""


class MemoryEntry(BaseModel):
    """One row of ``memory_entry`` — a captured signal, optionally
    classified and approved by a human. Frozen: every mutation (capture,
    classification, deletion) goes through a ``MemoryAdminRepository``/
    ``MemorySignalRecorder`` method that returns a fresh instance, never an
    in-place field update, matching every other row-shaped domain model in
    this codebase (``indexing.chunk.Chunk``, ``indexing.embedding.EmbeddingVector``).

    ``approved_by_user_id`` (``docs/plans/plan-deliver-admin-gui.md``'s
    Phase 3 section, Unit D, Assumption 6) is the acting Admin's
    ``iam.models.User.id`` at approval time — set only when ``status ==
    "approved"``, left untouched otherwise, the same conditional-write
    behavior ``approved_at`` already has (it's only ever read while
    ``status == "approved"``). Distinct from the pre-existing, free-text
    ``approved_by`` field above, which no current writer populates.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    repo_id: str
    org_id: str | None
    team_id: str | None
    entry_type: MemoryEntryType
    status: MemoryStatus
    raw_signal_type: RawSignalType
    raw_signal_detail: dict[str, object]
    posted_finding_comment_id: int
    embedding: tuple[float, ...] | None
    approved_at: datetime | None
    approved_by: str | None
    approved_by_user_id: str | None
    created_at: datetime
    updated_at: datetime


class PostedComment(BaseModel):
    """One inline review comment GitHub actually accepted and assigned an
    id to, correlated back to the ``findings.models.Finding`` it came from
    — ``adapter/github/posting.py``'s ``GitHubFindingsPublisher.post_review``
    (Phase 2) builds these from its own follow-up
    ``GET .../reviews/{review_id}/comments`` call (plan Assumption 2) and
    hands a batch of them to ``MemorySignalRecorder.record_comment_provenance``.
    """

    model_config = ConfigDict(frozen=True)

    comment_id: int
    file: str
    line: int | None
    rule_id: str | None
    message: str


class ReactionRecord(BaseModel):
    """One reaction GitHub's own Reactions API reports against a posted
    review comment, per ``docs/plans/plan-deliver-memory.md``'s Phase 4
    section (Unit D, US-M3). ``adapter/github/reactions.py``'s
    ``GitHubReactionFetcher`` builds these from
    ``GET .../pulls/comments/{comment_id}/reactions``;
    ``memory/reaction_poll.py``'s ``run_memory_reaction_poll_job`` uses
    ``reaction_id`` for ``ReactionDedupStore.filter_new``'s own
    ``(posted_finding_comment_id, reaction_id)`` dedup key (Assumption 9) and
    persists ``content`` verbatim as part of a captured entry's
    ``raw_signal_detail`` — an emoji name, never free text, so no redaction
    concern applies (per this plan's own Security review).
    """

    model_config = ConfigDict(frozen=True)

    reaction_id: int
    content: str
