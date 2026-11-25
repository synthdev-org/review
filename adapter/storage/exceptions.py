"""Typed exceptions for adapter.storage.

Per docs/rules/architecture.md's "specific exception types per domain
concern" rule: callers catch these, never a raw driver exception
(``asyncpg.exceptions.*``/``sqlalchemy.exc.*``), so a startup check or a
queue claim can fail with a message that says what actually went wrong.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every error this adapter raises."""


class PgVectorNotInstalledError(StorageError):
    """Raised when the ``pgvector`` Postgres extension is not present on
    the connected database.

    Per the C4 doc's "Configuration & Secrets" section, the Worker verifies
    this at startup and fails fast with this error rather than surfacing an
    obscure query failure the first time a later phase's indexing code
    tries to use a vector column.
    """

    def __init__(self, *, dsn_summary: str) -> None:
        super().__init__(
            "The 'pgvector' Postgres extension is not installed on the "
            f"connected database ({dsn_summary}). Install it with "
            "'CREATE EXTENSION vector;' (already available on the "
            "pgvector/pgvector Docker image and the bundled Helm subchart) "
            "before starting this service."
        )
        self.dsn_summary = dsn_summary


class StorageConnectionError(StorageError):
    """Raised when Postgres itself could not be reached at all (as opposed
    to being reachable but missing the ``pgvector`` extension).

    Per the plan's Phase 1 section: ``adapter/storage/pgvector_check.py``'s
    ``check_pgvector_installed`` is genuinely extended to catch a
    connection failure at ``engine.connect()`` and re-raise it as this
    error — the same ``dsn_summary``-redacted shape as
    ``PgVectorNotInstalledError``, never a raw driver exception, per
    architecture.md's "specific exception types per domain concern" rule.
    """

    def __init__(self, *, dsn_summary: str, detail: str | None = None) -> None:
        message = (
            f"Could not connect to the configured Postgres database ({dsn_summary})."
            if detail is None
            else f"Postgres database error ({dsn_summary}): {detail}."
        )
        super().__init__(message)
        self.dsn_summary = dsn_summary
