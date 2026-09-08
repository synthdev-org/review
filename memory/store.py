"""memory.store — the three storage-boundary Protocols Phase 4 (Memory)
needs, per ``docs/plans/plan-deliver-memory.md``'s Phase 1 section (Unit A,
Assumption 1).

Defined here, alongside ``memory/models.py``'s ``MemoryEntry``/
``PostedComment`` — not at any one of their several consumers — per
[ADR-0006](../docs/adr/0006-protocol-defined-at-data-model.md)'s exemption:
``MemorySignalRecorder`` alone has two real consumers spanning two
entrypoints (``adapter/github/posting.py`` in the Worker, Phase 2;
``apps/webhook_api/webhook_routes.py`` in the Webhook API, Phase 3), so
"the first consumer" would be an arbitrary choice among them. All three
Protocols are implemented by one concrete class,
``adapter/storage/memory_repository.py``'s ``PostgresMemoryRepository``, per
Assumption 8 — mirroring ``indexing/repository.py``'s
``IndexRepository``/``IndexCommitTracker`` split, both implemented by one
``IndexingRepository``.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule. Every method raises ``MemoryStoreError`` on a real Postgres-level
failure — never a raw ``sqlalchemy``/``asyncpg`` exception — the same
translation contract ``indexing/repository.py``'s ``IndexingRepositoryError``
and ``review/context_builder.py``'s ``ContextQueryError`` already establish
for their own concrete adapters. ``MemoryAdminRepository.set_status``/
``delete_entry`` additionally raise ``MemoryEntryNotFoundError`` — a
distinct, business-level "no such entry" outcome, never conflated with a
storage-layer failure — for an unknown ``entry_id`` (US-M5's 404 case,
translated by Phase 5's admin API into an HTTP 404).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from memory.models import MemoryEntry, MemoryEntryType, MemoryStatus, PostedComment, RawSignalType


class MemoryStoreError(Exception):
    """Raised by any method on the three Protocols below on a real
    Postgres-level failure (a constraint violation, a dropped connection
    mid-write, etc.) — never a raw ``sqlalchemy``/``asyncpg`` exception, per
    ``docs/rules/architecture.md``'s exception-type rule.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"memory store operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class MemoryEntryNotFoundError(Exception):
    """Raised by ``MemoryAdminRepository.set_status``/``delete_entry`` when
    ``entry_id`` doesn't match any ``memory_entry`` row — US-M5's own 404
    requirement, never surfaced as a generic ``MemoryStoreError``.
    """

    def __init__(self, *, entry_id: str) -> None:
        super().__init__(f"memory entry {entry_id!r} not found")
        self.entry_id = entry_id


class MemorySignalRecorder(Protocol):
    """Records provenance (Phase 2's one consumer) and raw inbound signals
    (Phases 3 and 4's consumers) — a batched-write and a single-write
    method, the two real shapes this plan needs, not a speculative generic
    one. Implemented by ``adapter/storage/memory_repository.py``'s
    ``PostgresMemoryRepository``.
    """

    async def record_comment_provenance(
        self, *, repo_id: str, pr_number: int, commit_sha: str, comments: Sequence[PostedComment]
    ) -> None:
        """Persists one ``posted_finding_comment`` row per entry in
        ``comments`` (a single batched write, not one call per comment) —
        per US-M11, called once ``FindingsOutputStep`` posts a batch of
        inline comments successfully. Raises ``MemoryStoreError`` on a
        Postgres-level failure.
        """
        ...

    async def record_signal(
        self,
        *,
        posted_finding_comment_id: int,
        raw_signal_type: RawSignalType,
        raw_signal_detail: dict[str, object],
        github_comment_id: int | None,
    ) -> None:
        """Persists one ``pending`` ``memory_entry`` row capturing a single
        raw signal against the ``posted_finding_comment`` row it's about —
        per US-M1/US-M2 (Phase 3) and US-M3 (Phase 4). ``github_comment_id``
        is the reply/thread-event's own GitHub comment id, backing the
        redelivery-safe ``(posted_finding_comment_id, raw_signal_type,
        github_comment_id)`` uniqueness constraint Phase 3 relies on —
        ``None`` for a ``reaction`` signal, whose own dedup is
        ``captured_reaction``'s job instead. Raises ``MemoryStoreError`` on
        a Postgres-level failure.
        """
        ...


class DismissedFindingMatcher(Protocol):
    """The read side of US-M6/US-M7/US-M9's similarity-based suppression —
    one method, consulted by ``review.verification.VerificationStep``
    (Phase 6). Implemented by ``adapter/storage/memory_repository.py``'s
    ``PostgresMemoryRepository``.
    """

    async def best_effective_similarity(
        self, *, repo_id: str, embedding: tuple[float, ...], half_life_days: float, now: datetime
    ) -> float | None:
        """The highest decay-weighted (``memory.decay.effective_similarity``)
        cosine similarity between ``embedding`` and any approved,
        repo-scoped ``dismissed_finding`` entry — ``None`` when no such
        entry exists for ``repo_id`` (Phase 6's own "no comparison
        attempted" case). Raises ``MemoryStoreError`` on a Postgres-level
        failure.
        """
        ...


class MemoryAdminRepository(Protocol):
    """The admin API's own storage boundary (Phase 5) — list/approve-or-
    reject/delete, per US-M4/US-M5/US-M8. Implemented by
    ``adapter/storage/memory_repository.py``'s ``PostgresMemoryRepository``.
    """

    async def list_entries(
        self,
        *,
        org_id: str | None,
        repo_id: str | None,
        team_id: str | None,
        status: MemoryStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[MemoryEntry, ...], int]:
        """The page of entries matching every given (optional) filter,
        newest first, plus the total count of entries matching those same
        filters (ignoring ``limit``/``offset``) — per US-M8.
        ``org_id``/``repo_id``/``team_id`` are filter parameters only, never
        an access-control boundary (per ``spec-memory.md``'s own "Admin key
        scope" clarification). Raises ``MemoryStoreError`` on a
        Postgres-level failure.
        """
        ...

    async def set_status(
        self,
        *,
        entry_id: str,
        status: Literal["approved", "rejected"],
        entry_type: MemoryEntryType,
        embedding: tuple[float, ...] | None,
    ) -> MemoryEntry:
        """Approve or reject ``entry_id`` in one method (per US-M4/US-M5's
        "approve and reject are reversible state transitions in either
        direction" design) — sets ``status``, ``entry_type``, and
        ``embedding`` to exactly what's given, and ``approved_at`` to the
        current time only when ``status == "approved"`` (left untouched
        otherwise, since it's only ever read while ``status == "approved"``).
        Raises ``MemoryEntryNotFoundError`` for an unknown ``entry_id``
        (US-M5's 404 case), or ``MemoryStoreError`` on any other
        Postgres-level failure.
        """
        ...

    async def delete_entry(self, *, entry_id: str) -> None:
        """Permanently removes ``entry_id``'s row (US-M5's ``DELETE``
        route). Raises ``MemoryEntryNotFoundError`` for an unknown
        ``entry_id``, or ``MemoryStoreError`` on any other Postgres-level
        failure.
        """
        ...
