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
