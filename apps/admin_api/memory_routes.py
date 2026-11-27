"""apps.admin_api.memory_routes — the Memory admin API: list/approve/
reject/delete, migrated off the shared static token onto real per-user
sessions (US-A7, US-A8-A11).

Moved wholesale from ``apps/webhook_api/admin_routes.py`` — same route
bodies, same ``memory.store`` Protocols, same 400/404 translation for a
malformed/unknown ``entry_id`` — with two changes, both required by US-A7:

- Auth swaps from ``_require_admin_key``'s single static
  ``Authorization: Bearer <admin_api_key>`` check to this entrypoint's own
  ``require_session``/``require_admin_role`` dependency chain
  (``apps/admin_api/dependencies.py``). The list route depends on
  ``require_session`` alone (Viewer parity, per US-A5's second clause —
  reading the queue is not a mutating action); approve/reject/delete each
  depend on ``require_admin_role``.
- Every mutating handler (approve/reject/delete) now writes a real
  ``admin_audit_log`` row via ``PostgresAuditLogRepository``,
  attributed to the acting session's user id — the "someone with the
  shared key did this" audit gap US-A7 exists to close. Approve
  additionally attributes ``memory_entry.approved_by_user_id`` itself,
  not just the audit log.

Follows ``apps/admin_api/user_routes.py``'s exact factory-router shape
(``create_memory_router(...) -> APIRouter``), closing over its own
dependencies rather than reading them from module-level state, per
``docs/rules/architecture.md``'s constructor-injection rule.
``apps/admin_api/main.py`` registers this router unconditionally, the same
as every other route module here — there is no "off by default" state for
the admin API.

The approve route (US-A9/US-M4) is the first place in ``apps/admin_api``
that runs local embedding-model inference — bounded to one call per
approve action (an authenticated, human-action-cadence operation, not
attacker-triggerable volume).
Wrapped in ``asyncio.to_thread`` since
``indexing.embedding.EmbeddingClient.embed`` is a plain, synchronous,
CPU-bound call (its own docstring), never awaited directly from an async
route handler.

Malformed input reaching ``memory.store``'s Protocol methods (a non-UUID
``entry_id`` in a path, or a non-UUID ``org_id``/``repo_id``/``team_id``
filter in a query string) surfaces from the concrete
``PostgresMemoryRepository`` as ``memory.store.MemoryStoreError`` — caught
here and translated to a structured ``400``, never left to propagate as an
unhandled/bare ``500``. A failed embedding computation
(``indexing.embedding.EmbeddingError``, the approve route's own ``embed``
call) is caught the same way and translated to a structured ``500`` (this
entrypoint's own local model-inference failure, not the caller's fault, so
not a ``4xx``).

US-A23 (Could-priority) adds
``POST /api/v1/memory-entries/bulk-approve`` and ``.../bulk-reject`` — each
executes this module's own ``approve_memory_entry``/``reject_memory_entry``
handler once per selected id (never a new bulk-specific domain method),
collecting each id's own success/failure into the response rather than
rolling back the whole batch on one failure.
The shared per-item collection logic lives in the module-level
``_run_bulk_operation`` helper, kept pure (no FastAPI/DB dependency of its
own beyond the operation it's handed) so it can be unit-tested directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from opentelemetry import metrics
from pydantic import BaseModel, Field

from adapter.storage.iam import PostgresAuditLogRepository
from apps.admin_api.dependencies import (
    AuthenticatedSession,
    RequireAdminRoleDependency,
    RequireSessionDependency,
)
from indexing.embedding import EmbeddingClient, EmbeddingError
from memory.fingerprint import build_finding_fingerprint
from memory.models import MemoryEntry, MemoryStatus
from memory.store import (
    MemoryAdminRepository,
    MemoryEntryNotFoundError,
    MemoryStoreError,
    PostedCommentReader,
)

# Deliberately narrower than `memory.models.MemoryEntryType` (which is
# `Literal[...] | None`, since `None` is a valid *stored* value for a
# `pending`/`rejected` entry): the approve request body must reject a
# `None`/missing `entry_type` with a 422 (US-M4's own "approval without
# classification is never allowed" requirement) rather than silently
# accepting `{"entry_type": null}` as if it were a valid classification.
_ApproveEntryType = Literal["dismissed_finding", "accepted_fix", "correction"]

logger = logging.getLogger(__name__)

meter = metrics.get_meter("review.memory")

# `{operation}` -> `"approve"`/`"reject"` (2 values). `_run_bulk_operation`
# folds a per-item failure into that item's
# `_BulkItemResult.success=False` in the HTTP response body alone — no log
# line, no metric — so an operator bulk-acting on the Memory queue has
# today's only signal in a browser network tab, not a dashboard/alert.
_bulk_operation_item_failed_counter = meter.create_counter(
    "review.memory.bulk_operation_item_failed_total",
    description="Count of individual entry failures inside a bulk-approve/bulk-reject call, "
    "labeled by operation — these are swallowed into the per-item response body and would "
    "otherwise have no server-side signal at all.",
)

# Unlabeled: this is the admin API's one call site of local embedding-model
# inference (approve's `dismissed_finding` path) — a degraded embedding
# runtime (OOM, model load failure, contention with a concurrently-running
# indexing job) would otherwise only be visible as a structured 500 an
# operator has to notice and report, never as a trend.
_embedding_failed_counter = meter.create_counter(
    "review.memory.embedding_failed_total",
    description="Count of embedding computation failures during a dismissed_finding approval.",
)

# US-M8: "default limit=50, capped at 200" — enforced as a hard `Query`
# upper bound (a request for more than this many rows is rejected with a
# 422, the same "a concrete ceiling, not silently clamped" posture this
# codebase's other numeric ceilings already take — e.g.
# `findings/redact.py`'s `apply_redaction_ceiling`), not silently clamped
# down to 200.
_MAX_LIST_LIMIT = 200
_DEFAULT_LIST_LIMIT = 50


class _ApproveRequest(BaseModel):
    """US-M4's own request body — `entry_type` is required, with no
    default: a missing key, a `null` value, or a value outside the three
    known literals fails pydantic validation before this module's own
    route code ever runs, which is exactly what "approval without
    classification is never allowed" (US-M4's second Given/When/Then)
    needs — FastAPI turns that validation failure into a 422
    automatically, and the entry this call named is never touched.
    """

    entry_type: _ApproveEntryType


class _ListMemoryEntriesResponse(BaseModel):
    """US-M8's own response shape — every matching page of entries plus the
    total matching count (ignoring `limit`/`offset`), never every matching
    row in one response.
    """

    entries: tuple[MemoryEntry, ...]
    total: int


class _DeleteMemoryEntryResponse(BaseModel):
    """A structured body for a successful delete — structured JSON
    suitable for the Admin UI to consume directly, not a bare 204
    with no body a screen would have nothing to read.
    """

    deleted: bool
    entry_id: str


class _BulkApproveRequest(BaseModel):
    """US-A23's first Given/When/Then: one explicit ``entry_type`` applied
    to every selected id — not a per-id override — mirroring
    ``_ApproveRequest``'s own "missing/invalid entry_type fails validation
    before any entry is touched" posture, now for the whole batch rather
    than one entry.
    """

    entry_ids: Annotated[tuple[str, ...], Field(min_length=1)]
    entry_type: _ApproveEntryType


class _BulkRejectRequest(BaseModel):
    """US-A23's second Given/When/Then: reject every selected id, no
    ``entry_type`` — rejecting always clears any prior classification,
    exactly like the single-entry reject route.
    """

    entry_ids: Annotated[tuple[str, ...], Field(min_length=1)]


class _BulkItemResult(BaseModel):
    """One selected id's own outcome — ``error`` is the same detail string
    the single-entry route would have returned in its HTTP response body,
    so a caller sees the identical failure reason either way.
    """

    entry_id: str
    success: bool
    error: str | None = None


class _BulkOperationResponse(BaseModel):
    """US-A23's own "response shape distinguishes [successes and failures]"
    requirement — one result per requested id, in request order, never a
    single aggregate success/failure flag for the whole batch.
    """

    results: tuple[_BulkItemResult, ...]


def _entry_id_not_found_or_invalid(entry_id: str, exc: Exception) -> NoReturn:
    """Shared translation for the `MemoryEntryNotFoundError` -> 404 /
    `MemoryStoreError` -> 400 pair every entry-id-scoped route below
    (approve/reject/delete) needs — the exact same two-exception mapping
    was previously duplicated verbatim in each route's own try/except.

    Raises (rather than returns) so it can preserve each branch's original
    exception-chaining behavior itself: the 404 case intentionally suppresses
    the internal `MemoryEntryNotFoundError` from the traceback (`from None`
    — nothing about "which repository lookup failed" belongs in a 404's
    chain), while the 400 case still chains `from exc` so the originating
    `MemoryStoreError` remains visible for debugging. A single
    `raise ... from exc` at every call site would flatten that distinction.
    """
    if isinstance(exc, MemoryEntryNotFoundError):
        raise HTTPException(
            status_code=404, detail=f"memory entry {entry_id!r} not found"
        ) from None
    if isinstance(exc, MemoryStoreError):
        raise HTTPException(status_code=400, detail=f"invalid entry_id: {exc.detail}") from exc
    raise exc


async def _run_bulk_operation(
    entry_ids: Sequence[str],
    operation: Callable[[str], Awaitable[object]],
    *,
    operation_name: Literal["approve", "reject"],
) -> _BulkOperationResponse:
    """US-A23: run ``operation`` once per id in ``entry_ids``,
    collecting each id's own success/failure into the response rather than
    letting one id's ``HTTPException`` (e.g. a 404 for an already-deleted
    id) abort the loop or roll back ids already processed — the per-entry handlers
    are iterated individually, collecting per-item success/failure rather
    than rolling back the batch on one failure.

    Deliberately a plain module-level function, not a closure inside
    ``create_memory_router`` — it needs no dependency of its own beyond the
    ``operation`` callable it's handed, so it can be unit-tested directly
    against a fake ``operation`` with no FastAPI app or database involved.
    Any non-``HTTPException`` raised by ``operation`` is left to propagate
    (an unexpected error is a real 5xx, not a per-item result to swallow).
    """
    results: list[_BulkItemResult] = []
    for entry_id in entry_ids:
        try:
            await operation(entry_id)
        except HTTPException as exc:
            _bulk_operation_item_failed_counter.add(1, {"operation": operation_name})
            results.append(_BulkItemResult(entry_id=entry_id, success=False, error=str(exc.detail)))
        else:
            results.append(_BulkItemResult(entry_id=entry_id, success=True, error=None))
    return _BulkOperationResponse(results=tuple(results))


async def _dismissed_finding_embedding(
    entry_id: str,
    posted_comment_reader: PostedCommentReader,
    embedding_client: EmbeddingClient,
) -> tuple[float, ...]:
    """Embed the fingerprint of the posted comment behind ``entry_id``."""
    try:
        posted_comment = await posted_comment_reader.get_posted_comment(entry_id=entry_id)
    except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
        _entry_id_not_found_or_invalid(entry_id, exc)
    fingerprint_text = build_finding_fingerprint(
        file=posted_comment.file,
        rule_id=posted_comment.rule_id,
        message=posted_comment.message,
    )
    try:
        vectors = await asyncio.to_thread(embedding_client.embed, (fingerprint_text,))
    except EmbeddingError as exc:
        _embedding_failed_counter.add(1)
        raise HTTPException(status_code=500, detail=f"embedding computation failed: {exc}") from exc
    return vectors[0].values


@dataclass(frozen=True)
class _MemoryDeps:
    repository: MemoryAdminRepository
    posted_comment_reader: PostedCommentReader
    embedding_client: EmbeddingClient
    audit_log_repository: PostgresAuditLogRepository


async def _list_entries(
    deps: _MemoryDeps,
    org_id: str | None,
    repo_id: str | None,
    team_id: str | None,
    status: MemoryStatus | None,
    limit: int,
    offset: int,
) -> _ListMemoryEntriesResponse:
    """US-M8: `org_id`/`repo_id`/`team_id`/`status` are all optional
    filters (never an access-control boundary — every session,
    Admin or Viewer, reads/filters every org/repo/team, per
    `spec-memory.md`'s own "Admin key scope" clarification, now applied
    to `require_session` instead of the retired static key). Gated by
    `require_session` alone (Viewer parity, US-A5's second clause) —
    reading the queue is not a mutating action.
    """
    try:
        entries, total = await deps.repository.list_entries(
            org_id=org_id,
            repo_id=repo_id,
            team_id=team_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    except MemoryStoreError as exc:
        raise HTTPException(status_code=400, detail=f"invalid list request: {exc.detail}") from exc
    return _ListMemoryEntriesResponse(entries=entries, total=total)


async def _approve_entry(
    deps: _MemoryDeps,
    entry_id: str,
    payload: _ApproveRequest,
    authenticated: AuthenticatedSession,
) -> MemoryEntry:
    """US-A9/US-M4: classify and approve `entry_id`, attributed to the
    acting Admin's identity (US-A7). Only a `dismissed_finding`
    approval computes and stores an embedding — `accepted_fix`/
    `correction` entries are approved with no embedding, since nothing
    reads them yet.
    """
    embedding: tuple[float, ...] | None = None
    if payload.entry_type == "dismissed_finding":
        embedding = await _dismissed_finding_embedding(
            entry_id, deps.posted_comment_reader, deps.embedding_client
        )

    try:
        entry = await deps.repository.set_status(
            entry_id=entry_id,
            status="approved",
            entry_type=payload.entry_type,
            embedding=embedding,
            approved_by_user_id=authenticated.user.id,
        )
    except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
        _entry_id_not_found_or_invalid(entry_id, exc)

    await deps.audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="memory_entry_approved",
        resource_type="memory_entry",
        resource_id=entry_id,
        metadata={"entry_type": payload.entry_type},
    )
    logger.info(
        "memory entry approved",
        extra={
            "entry_id": entry_id,
            "action": "approve",
            "entry_type": payload.entry_type,
            "actor": authenticated.user.username,
        },
    )
    return entry


async def _reject_entry(
    deps: _MemoryDeps, entry_id: str, authenticated: AuthenticatedSession
) -> MemoryEntry:
    """US-A10/US-M5: reject `entry_id`, attributed to the acting Admin
    (US-A7) — idempotent (a repeat reject of an already-rejected entry
    is a plain 200, not an error) and reversible (a later approve
    re-classifies exactly as a fresh entry would). Clears any prior
    `entry_type`/`embedding`, per `memory.models.MemoryEntryType`'s own
    "reject clears any prior classification" docstring.
    """
    try:
        entry = await deps.repository.set_status(
            entry_id=entry_id,
            status="rejected",
            entry_type=None,
            embedding=None,
            approved_by_user_id=None,
        )
    except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
        _entry_id_not_found_or_invalid(entry_id, exc)

    await deps.audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="memory_entry_rejected",
        resource_type="memory_entry",
        resource_id=entry_id,
    )
    logger.info(
        "memory entry rejected",
        extra={"entry_id": entry_id, "action": "reject", "actor": authenticated.user.username},
    )
    return entry


async def _delete_entry(
    deps: _MemoryDeps, entry_id: str, confirm: bool, authenticated: AuthenticatedSession
) -> _DeleteMemoryEntryResponse:
    """US-A11/US-M5: irreversibly remove `entry_id`'s row, attributed to
    the acting Admin (US-A7) — requires an explicit `confirm=true` query
    parameter (`400` without it, "the closest a JSON API gets to a
    confirmation dialog's role in a UI"); `404` for an already-deleted or never-existing id, on
    either call shape.
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="delete requires confirm=true")

    try:
        await deps.repository.delete_entry(entry_id=entry_id)
    except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
        _entry_id_not_found_or_invalid(entry_id, exc)

    await deps.audit_log_repository.record(
        actor_user_id=authenticated.user.id,
        action="memory_entry_deleted",
        resource_type="memory_entry",
        resource_id=entry_id,
    )
    logger.info(
        "memory entry deleted",
        extra={"entry_id": entry_id, "action": "delete", "actor": authenticated.user.username},
    )
    return _DeleteMemoryEntryResponse(deleted=True, entry_id=entry_id)


async def _bulk_approve(
    deps: _MemoryDeps, payload: _BulkApproveRequest, authenticated: AuthenticatedSession
) -> _BulkOperationResponse:
    """US-A23's first Given/When/Then: approve every id in
    ``payload.entry_ids`` with the one ``entry_type`` applied to all of
    them, by calling this router's own ``approve_memory_entry`` handler
    once per id — including its own embedding computation where
    applicable, attributed to this same acting Admin — never a new
    bulk-specific domain method. A single id's failure (e.g.
    already-deleted) is collected into that id's own result and never
    blocks or rolls back the rest of the batch.
    """
    return await _run_bulk_operation(
        payload.entry_ids,
        lambda entry_id: _approve_entry(
            deps, entry_id, _ApproveRequest(entry_type=payload.entry_type), authenticated
        ),
        operation_name="approve",
    )


async def _bulk_reject(
    deps: _MemoryDeps, payload: _BulkRejectRequest, authenticated: AuthenticatedSession
) -> _BulkOperationResponse:
    """US-A23's second Given/When/Then: reject every id in
    ``payload.entry_ids`` by calling this router's own
    ``reject_memory_entry`` handler once per id — same partial-failure
    handling as ``bulk_approve_memory_entries``.
    """
    return await _run_bulk_operation(
        payload.entry_ids,
        lambda entry_id: _reject_entry(deps, entry_id, authenticated),
        operation_name="reject",
    )


def _register_read_routes(
    router: APIRouter, deps: _MemoryDeps, require_session: RequireSessionDependency
) -> None:
    @router.get("")
    async def list_memory_entries(
        org_id: str | None = None,
        repo_id: str | None = None,
        team_id: str | None = None,
        status: MemoryStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIST_LIMIT)] = _DEFAULT_LIST_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
        authenticated: AuthenticatedSession = Depends(require_session),
    ) -> _ListMemoryEntriesResponse:
        _ = authenticated
        return await _list_entries(deps, org_id, repo_id, team_id, status, limit, offset)


def _register_write_routes(
    router: APIRouter, deps: _MemoryDeps, require_admin_role: RequireAdminRoleDependency
) -> None:
    @router.post("/{entry_id}/approve")
    async def approve_memory_entry(
        entry_id: str,
        payload: _ApproveRequest,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> MemoryEntry:
        return await _approve_entry(deps, entry_id, payload, authenticated)

    @router.post("/{entry_id}/reject")
    async def reject_memory_entry(
        entry_id: str,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> MemoryEntry:
        return await _reject_entry(deps, entry_id, authenticated)

    @router.delete("/{entry_id}")
    async def delete_memory_entry(
        entry_id: str,
        confirm: bool = False,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> _DeleteMemoryEntryResponse:
        return await _delete_entry(deps, entry_id, confirm, authenticated)

    @router.post("/bulk-approve")
    async def bulk_approve_memory_entries(
        payload: _BulkApproveRequest,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> _BulkOperationResponse:
        return await _bulk_approve(deps, payload, authenticated)

    @router.post("/bulk-reject")
    async def bulk_reject_memory_entries(
        payload: _BulkRejectRequest,
        authenticated: AuthenticatedSession = Depends(require_admin_role),
    ) -> _BulkOperationResponse:
        return await _bulk_reject(deps, payload, authenticated)


# Build the always-registered Memory admin router.
#
# A factory closing over its dependencies, matching
# ``apps/admin_api/user_routes.py``'s ``create_user_router`` shape — only
# called by ``apps/admin_api/main.py``. The handler bodies (and their
# docstrings) live in the module-level ``_*_entry``/``_bulk_*`` helpers above.
def create_memory_router(
    *,
    repository: MemoryAdminRepository,
    posted_comment_reader: PostedCommentReader,
    embedding_client: EmbeddingClient,
    audit_log_repository: PostgresAuditLogRepository,
    require_session: RequireSessionDependency,
    require_admin_role: RequireAdminRoleDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memory-entries")
    deps = _MemoryDeps(repository, posted_comment_reader, embedding_client, audit_log_repository)
    _register_read_routes(router, deps, require_session)
    _register_write_routes(router, deps, require_admin_role)
    return router
