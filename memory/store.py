"""memory.store — the storage-boundary Protocols Phase 4 (Memory) needs, per
``docs/plans/plan-deliver-memory.md``'s Phase 1 section (Unit A, Assumption
1), Phase 4 section (Unit D), and Phase 5 section (Unit E).

Defined here, alongside ``memory/models.py``'s ``MemoryEntry``/
``PostedComment`` — not at any one of their several consumers — per
[ADR-0006](../docs/adr/0006-protocol-defined-at-data-model.md)'s exemption:
``MemorySignalRecorder`` alone has two real consumers spanning two
entrypoints (``adapter/github/posting.py`` in the Worker, Phase 2;
``apps/webhook_api/webhook_routes.py`` in the Webhook API, Phase 3), so
"the first consumer" would be an arbitrary choice among them. All six
Protocols below (the original three from Phase 1, Phase 4's
``ReactionFetcher``/``ReactionDedupStore``, and Phase 5's
``PostedCommentReader``) are implemented by one concrete class,
``adapter/storage/memory_repository.py``'s ``PostgresMemoryRepository``
(``ReactionFetcher`` being the one exception — implemented by
``adapter/github/reactions.py``'s ``GitHubReactionFetcher`` instead, since
it's a GitHub API read, not a storage concern), per Assumption 8 —
mirroring ``indexing/repository.py``'s ``IndexRepository``/
``IndexCommitTracker`` split, both implemented by one ``IndexingRepository``.
Every Protocol here is kept at 1-3 methods, per
``docs/rules/architecture.md``'s Protocol-size rule — ``PostedCommentReader``
was split out from ``MemoryAdminRepository`` for exactly this reason (see
that Protocol's own docstring below), rather than merged into it as a
fourth method.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule. Every storage-backed method raises ``MemoryStoreError`` on a real
Postgres-level failure — never a raw ``sqlalchemy``/``asyncpg`` exception —
the same translation contract ``indexing/repository.py``'s
``IndexingRepositoryError`` and ``review/context_builder.py``'s
``ContextQueryError`` already establish for their own concrete adapters.
``MemoryAdminRepository.set_status``/``delete_entry`` additionally raise
``MemoryEntryNotFoundError`` — a distinct, business-level "no such entry"
outcome, never conflated with a storage-layer failure — for an unknown
``entry_id`` (US-M5's 404 case, translated by Phase 5's admin API into an
HTTP 404).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from memory.models import (
    MemoryEntry,
    MemoryEntryType,
    MemoryStatus,
    PostedComment,
    RawSignalType,
    ReactionRecord,
)


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
    ) -> bool:
        """Persists one ``pending`` ``memory_entry`` row capturing a single
        raw signal against the ``posted_finding_comment`` row it's about —
        per US-M1/US-M2 (Phase 3) and US-M3 (Phase 4). ``github_comment_id``
        is the reply/thread-event's own GitHub comment id, backing the
        redelivery-safe ``(posted_finding_comment_id, raw_signal_type,
        github_comment_id, signal_variant)`` uniqueness constraint Phase 3
        relies on — ``None`` for a ``reaction`` signal, whose own dedup is
        ``captured_reaction``'s job instead. ``signal_variant`` is not a
        parameter of this method — the implementation derives it internally
        from ``raw_signal_detail`` (``"resolved"``/``"unresolved"`` for a
        ``thread_resolution`` signal, ``""`` otherwise) purely to keep a
        thread's ``resolved`` event and a later ``unresolved`` event on that
        same thread from colliding on this key and silently discarding the
        second one (both otherwise share an identical ``raw_signal_type``
        and ``github_comment_id``) — see
        ``adapter/storage/models.py``'s ``MemoryEntryRow`` docstring.

        Returns ``True`` when ``posted_finding_comment_id`` correlates to a
        real ``posted_finding_comment`` row and the signal was recorded —
        either a fresh insert, or a no-op ``ON CONFLICT DO NOTHING`` against
        that same uniqueness constraint (a GitHub redelivery of the same
        reply/thread-resolution event, per Phase 3's own redelivery-safety
        design). Returns ``False`` when ``posted_finding_comment_id`` does
        not correlate to any known row (the webhook comment this signal is
        about isn't one the platform posted, or isn't a reply to one) — the
        caller (``apps/webhook_api/webhook_routes.py``) treats this as an
        accepted-but-uncaptured outcome, never an error. Raises
        ``MemoryStoreError`` on a genuine Postgres-level failure.
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

    Kept at exactly three methods, per ``docs/rules/architecture.md``'s
    "Protocols are 1-3 methods" rule. The approve route's own need to read
    a linked ``posted_finding_comment`` row before calling ``set_status``
    (to build a ``dismissed_finding`` entry's embedding input) is served by
    the separate, single-method ``PostedCommentReader`` Protocol below —
    mirroring how ``ReactionFetcher``/``ReactionDedupStore`` stay separate,
    narrow Protocols rather than growing one Protocol past three methods —
    not folded in here as a fourth method.
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


class PostedCommentReader(Protocol):
    """One method, kept separate from ``MemoryAdminRepository`` (Phase 5)
    to stay within ``docs/rules/architecture.md``'s "Protocols are 1-3
    methods" rule — the same "narrow, single-purpose Protocol" precedent
    ``ReactionFetcher``/``ReactionDedupStore`` already establish rather
    than growing one Protocol past three methods. The approve route
    (US-M4) computes a ``dismissed_finding`` entry's embedding at the
    FastAPI-route layer, via the composition root's own
    ``indexing.embedding.EmbeddingClient`` instance — which means it needs
    the linked finding's fingerprint text (``file``/``rule_id``/``message``)
    *before* calling ``MemoryAdminRepository.set_status``, not just an
    opaque ``entry_id``. Implemented by
    ``adapter/storage/memory_repository.py``'s ``PostgresMemoryRepository``
    (Assumption 8 — one concrete class implements every Memory Protocol).
    """

    async def get_posted_comment(self, *, entry_id: str) -> PostedComment:
        """The ``posted_finding_comment`` row linked to ``entry_id`` (via
        its own ``posted_finding_comment_id`` FK, which is never nullable —
        every ``memory_entry`` row is captured against a real posted
        comment), returned as a ``PostedComment`` so the approve route can
        build ``memory.fingerprint.build_finding_fingerprint``'s input from
        it (US-M4). Raises ``MemoryEntryNotFoundError`` for an unknown
        ``entry_id`` — the same 404 case ``MemoryAdminRepository.set_status``/
        ``delete_entry`` already raise it for — or ``MemoryStoreError`` on
        any other Postgres-level failure.
        """
        ...


class ReactionFetcher(Protocol):
    """Fetches current GitHub reactions for a batch of already-posted
    review comments — US-M3's own read side (Phase 4, Unit D). One method,
    per architecture.md's Protocol-size rule. Implemented by
    ``adapter/github/reactions.py``'s ``GitHubReactionFetcher`` — a GitHub
    API read, not a storage concern, so (unlike every other Protocol in
    this module) it is *not* one of ``PostgresMemoryRepository``'s own
    implemented roles.
    """

    async def fetch_reactions(
        self, *, installation_id: int, repo_full_name: str, comment_ids: Sequence[int]
    ) -> dict[int, tuple[ReactionRecord, ...]]:
        """The current reactions for each id in ``comment_ids``, keyed by
        that same comment id. A comment id present in ``comment_ids`` but
        absent from the returned mapping means that one comment's own fetch
        failed (logged by the implementation at ``warning``) — per the
        plan's "one comment's fetch failing never aborts the batch" design,
        this method itself never raises for a single failed comment; only a
        whole-job-fatal failure (e.g. the installation token exchange
        itself failing) propagates as a real exception.
        """
        ...


class ReactionDedupStore(Protocol):
    """The storage-boundary half of US-M3's reaction capture (Phase 4, Unit
    D) — which ``posted_finding_comment`` rows a poll should even look at,
    and which of a poll's freshly-fetched reactions are genuinely new.
    Implemented by ``adapter/storage/memory_repository.py``'s
    ``PostgresMemoryRepository``.
    """

    async def recent_comment_ids(
        self, *, repo_id: str, pr_number: int, limit: int
    ) -> tuple[int, ...]:
        """The ids of the ``limit`` most-recently-posted
        ``posted_finding_comment`` rows for ``(repo_id, pr_number)``,
        ordered newest first — the plan's own "bounded cost, not just
        bounded per-job" design (``MEMORY_REACTION_POLL_MAX_COMMENTS_PER_POLL``):
        a PR with more posted comments than ``limit`` simply never has its
        oldest comments' reactions polled again, an accepted, logged gap
        rather than an unbounded cost curve against the installation's
        shared GitHub API rate limit. Raises ``MemoryStoreError`` on a
        Postgres-level failure.
        """
        ...

    async def filter_new(
        self, *, posted_finding_comment_id: int, reaction_ids: Sequence[int]
    ) -> tuple[int, ...]:
        """Of ``reaction_ids`` (every reaction currently on
        ``posted_finding_comment_id``, per a fresh ``ReactionFetcher`` call),
        returns only the ones not already recorded in ``captured_reaction``
        — and durably records exactly those returned ids as now-captured in
        this same call (Assumption 9), closing the check-then-record race a
        separate check/record pair would leave open under concurrent polls
        of the same PR. Raises ``MemoryStoreError`` on a Postgres-level
        failure.
        """
        ...
