"""iam.models — the domain models the Admin GUI's
authentication and session handling reads and writes.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule — every field here is a plain pydantic/``typing`` type, never a
SQLAlchemy column; ids are plain ``str`` (the domain-uses-``str``-ids,
adapter-converts-to-``uuid.UUID`` convention ``tenancy.resolution``'s
``OrgRepoIds``/``review/context_builder.py``'s Protocols already
establish), and ``Role`` is a ``Literal`` here — the storage layer's own
mirror (``adapter/storage/models.py``'s ``AdminRole``) is a real
``enum.StrEnum``, the same domain-``Literal``-vs-storage-``enum.StrEnum``
split ``memory.models.MemoryStatus``/``adapter/storage/models.py``'s
``MemoryEntryStatus`` already establish.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["admin", "viewer"]
"""US-A5's two roles: ``admin`` can perform every mutating action the Admin GUI
adds; ``viewer`` has read-only parity (every ``GET`` route ``require_session``
alone gates, every mutating route additionally gates behind
``require_admin_role``)."""


class User(BaseModel):
    """One row of ``admin_user`` — a human operator who can sign in to the
    Admin UI. Frozen, matching every other row-shaped domain model in this
    codebase (``memory.models.MemoryEntry``, ``indexing.chunk.Chunk``):
    every mutation goes through a ``UserRepository`` method that returns a
    fresh instance, never an in-place field update.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    username: str
    # An already-computed Argon2id hash (adapter/auth/argon2_hasher.py),
    # never a plaintext password — included here (unlike a typical
    # API-facing response model) because apps/admin_api/auth_routes.py's
    # login route genuinely needs it to call
    # Argon2PasswordHasher.verify(); every HTTP-facing response model
    # defined here (_SessionInfoResponse) excludes it explicitly, so this
    # field never round-trips into an API response.
    password_hash: str
    role: Role
    is_active: bool
    created_at: datetime
    updated_at: datetime


class Session(BaseModel):
    """One row of ``admin_session`` — an opaque, hash-lookup credential,
    never a JWT. ``id`` is this session row's own surrogate
    id, not the raw session token itself (the raw token is never persisted
    or modeled here — only its SHA-256 hash, ``adapter/storage/iam.py``'s
    own storage-layer concern) — it's what the CSRF token is
    HMAC'd against, and what ``SessionStore.revoke``/``touch_last_seen``
    address a session by.

    ``last_seen_at`` backs US-A2's sliding-window idle expiry
    (``iam.auth.is_session_expired``) — updated on every authenticated
    request via ``SessionStore.touch_last_seen``, never on session creation
    alone.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    user_id: str
    created_at: datetime
    last_seen_at: datetime


class AuthenticatedIdentity(BaseModel):
    """What ``iam.auth.authenticate_session`` resolves a valid, non-expired
    session token to: the session itself (its ``id`` is what a CSRF check
    HMACs against) plus the active user behind it. Frozen, like every other
    model in this module.
    """

    model_config = ConfigDict(frozen=True)

    session: Session
    user: User
