"""apps.webhook_api.admin_routes — the Memory admin API: list/approve/
reject/delete, per ``docs/plans/plan-deliver-memory.md``'s Phase 5 section
(Unit E, US-M4/US-M5/US-M8/US-M10).

Follows ``apps/webhook_api/manifest_routes.py``'s exact factory-router
shape (``create_admin_router(...) -> APIRouter``), closing over its own
dependencies rather than reading them from module-level state, per
``docs/rules/architecture.md``'s constructor-injection rule.
``apps/webhook_api/main.py`` registers this router **unconditionally**
(plan Assumption 7) — unlike ``manifest_routes.py``'s ``SETUP_MODE`` gate,
there is no "off by default" state for the admin API: every route enforces
its own auth per-request via ``_require_admin_key`` instead.

Every route depends on ``_require_admin_key`` (US-M10) — a single
``Authorization: Bearer <admin_api_key>`` check, ``hmac.compare_digest``'d
against the configured key (never ``==``, the same constant-time-comparison
precedent ``adapter/github/manifest.py``'s own CSRF-state check and
``adapter/github/signature.py``'s webhook-signature check already
establish for a security-sensitive string comparison) — registered once as
a router-level dependency (``APIRouter(dependencies=[...])``) so every
route below it inherits the same check rather than each repeating it.

The approve route (US-M4) is the first place outside the Worker's own job
pipeline that runs local embedding-model inference — bounded to one call
per approve action (an authenticated, human-action-cadence operation, not
attacker-triggerable volume), per the plan's Security review. Wrapped in
``asyncio.to_thread`` since ``indexing.embedding.EmbeddingClient.embed`` is
a plain, synchronous, CPU-bound call (its own docstring), never awaited
directly from an async route handler.

Every approve/reject/delete call is logged at ``info`` (entry id, action) —
the audit trail ``spec-memory.md``'s Observability section names for a
write-capable curation surface with no real per-user identity yet.

Malformed input reaching ``memory.store``'s Protocol methods (a non-UUID
``entry_id`` in a path, or a non-UUID ``org_id``/``repo_id``/``team_id``
filter in a query string) surfaces from the concrete
``PostgresMemoryRepository`` as ``memory.store.MemoryStoreError`` — caught
here and translated to a structured ``400``, never left to propagate as an
unhandled/bare ``500``. A failed embedding computation
(``indexing.embedding.EmbeddingError``, the approve route's own
``embed`` call) is caught the same way and translated to a structured
``500`` (this entrypoint's own local model-inference failure, not the
caller's fault, so not a ``4xx``).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, SecretStr

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
    row in one response."""

    entries: tuple[MemoryEntry, ...]
    total: int


class _DeleteMemoryEntryResponse(BaseModel):
    """A structured body for a successful delete, per this phase's own UX
    section ("every response is structured JSON suitable for a future
    Phase 5 [admin UI] screen to consume directly") — not a bare 204 with
    no body a future screen would have nothing to read."""

    deleted: bool
    entry_id: str


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


def create_admin_router(
    *,
    admin_api_key: SecretStr,
    repository: MemoryAdminRepository,
    posted_comment_reader: PostedCommentReader,
    embedding_client: EmbeddingClient,
) -> APIRouter:
    """Build the always-registered Memory admin router.

    A factory closing over its dependencies, matching
    ``manifest_routes.py``'s ``create_manifest_router`` shape — only called
    by ``apps/webhook_api/main.py``.
    """

    def _require_admin_key(request: Request) -> None:
        """US-M10: reject any admin request without a valid
        ``Authorization: Bearer <admin_api_key>`` header — no Memory entry
        is read, modified, or created for a request that fails this check
        (it runs, as a router-level dependency, before any route body
        below executes).
        """
        authorization = request.headers.get("authorization")
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing or invalid Authorization header")
        provided_key = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(provided_key, admin_api_key.get_secret_value()):
            raise HTTPException(status_code=401, detail="invalid admin API key")

    router = APIRouter(prefix="/admin/memory-entries", dependencies=[Depends(_require_admin_key)])

    @router.get("")
    async def list_memory_entries(
        org_id: str | None = None,
        repo_id: str | None = None,
        team_id: str | None = None,
        status: MemoryStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIST_LIMIT)] = _DEFAULT_LIST_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> _ListMemoryEntriesResponse:
        """US-M8: `org_id`/`repo_id`/`team_id`/`status` are all optional
        filters (never an access-control boundary the key itself enforces
        — the same single static key already reads/writes every org/repo/
        team regardless of what's passed here, per `spec-memory.md`'s own
        "Admin key scope" clarification), newest-first, paginated.
        """
        try:
            entries, total = await repository.list_entries(
                org_id=org_id,
                repo_id=repo_id,
                team_id=team_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except MemoryStoreError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid list request: {exc.detail}"
            ) from exc
        return _ListMemoryEntriesResponse(entries=entries, total=total)

    @router.post("/{entry_id}/approve")
    async def approve_memory_entry(entry_id: str, payload: _ApproveRequest) -> MemoryEntry:
        """US-M4: classify and approve `entry_id`. Only a `dismissed_finding`
        approval computes and stores an embedding — `accepted_fix`/
        `correction` entries are approved with no embedding, since nothing
        reads them yet (per the plan's own out-of-scope list)."""
        embedding: tuple[float, ...] | None = None
        if payload.entry_type == "dismissed_finding":
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
                raise HTTPException(
                    status_code=500, detail=f"embedding computation failed: {exc}"
                ) from exc
            embedding = vectors[0].values

        try:
            entry = await repository.set_status(
                entry_id=entry_id,
                status="approved",
                entry_type=payload.entry_type,
                embedding=embedding,
            )
        except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
            _entry_id_not_found_or_invalid(entry_id, exc)

        logger.info(
            "memory entry approved",
            extra={"entry_id": entry_id, "action": "approve", "entry_type": payload.entry_type},
        )
        return entry

    @router.post("/{entry_id}/reject")
    async def reject_memory_entry(entry_id: str) -> MemoryEntry:
        """US-M5: reject `entry_id` — idempotent (a repeat reject of an
        already-rejected entry is a plain 200, not an error) and reversible
        (a later approve re-classifies exactly as a fresh entry would).
        Clears any prior `entry_type`/`embedding`, per `memory.models
        .MemoryEntryType`'s own "reject clears any prior classification"
        docstring.
        """
        try:
            entry = await repository.set_status(
                entry_id=entry_id, status="rejected", entry_type=None, embedding=None
            )
        except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
            _entry_id_not_found_or_invalid(entry_id, exc)

        logger.info("memory entry rejected", extra={"entry_id": entry_id, "action": "reject"})
        return entry

    @router.delete("/{entry_id}")
    async def delete_memory_entry(
        entry_id: str, confirm: bool = False
    ) -> _DeleteMemoryEntryResponse:
        """US-M5: irreversibly remove `entry_id`'s row — requires an
        explicit `confirm=true` query parameter (`400` without it, "the
        closest a JSON API gets to a confirmation dialog's role in a UI",
        per the plan's UX section); `404` for an already-deleted or
        never-existing id, on either call shape.
        """
        if not confirm:
            raise HTTPException(status_code=400, detail="delete requires confirm=true")

        try:
            await repository.delete_entry(entry_id=entry_id)
        except (MemoryEntryNotFoundError, MemoryStoreError) as exc:
            _entry_id_not_found_or_invalid(entry_id, exc)

        logger.info("memory entry deleted", extra={"entry_id": entry_id, "action": "delete"})
        return _DeleteMemoryEntryResponse(deleted=True, entry_id=entry_id)

    return router
