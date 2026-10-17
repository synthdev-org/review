"""adapter.storage.clone_query — Postgres implementation of
``review.clone_detection.CloneIndexQuery`` (``docs/plans/plan-phase-10.md``,
Phase 3), copying ``context_query.PostgresSimilarityQuery``'s shape.

``pr_number = 0`` is hard-coded in every statement, so a PR-scoped (possibly
fork-authored) row can never be a comparison target. Both reads also require
an ``index_chunk`` row for the candidate file: a file deleted by an old
Worker that never touched the clone tables is never matched.

``find_candidates`` caps matches per hash in SQL with a ``LATERAL`` join (as
``PostgresGraphQuery`` does) asking for ``limit + 1`` rows, so "more than the
limit existed" is detectable without counting every match.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import ARRAY, BigInteger, Integer, String, Uuid, bindparam, exists, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import IndexChunkRow, IndexCloneFileRow, IndexCloneFingerprintRow
from adapter.storage.unit_of_work import run_in_session
from indexing.clone import CloneFileIndex, Fingerprint
from review.clone_detection import (
    CloneCandidateHit,
    CloneCandidates,
    CloneIndexQuery,
    CloneQueryError,
)

# Hashes per statement; bounds the unnest()/LATERAL fan-out of one round trip.
HASH_BATCH_SIZE = 500

_FIND_CANDIDATES_QUERY = text(
    """
    SELECT h.hash AS hash, hit.file_path AS file_path, hit.token_offset AS token_offset
    FROM unnest(:hashes) AS h(hash)
    CROSS JOIN LATERAL (
        SELECT f.file_path AS file_path, f.token_offset AS token_offset
        FROM index_clone_fingerprint f
        WHERE f.repo_id = :repo_id
            AND f.pr_number = 0
            AND f.hash = h.hash
            AND f.file_path <> ALL(:excluded)
            AND EXISTS (
                SELECT 1 FROM index_chunk c
                WHERE c.repo_id = f.repo_id AND c.pr_number = 0 AND c.file_path = f.file_path
            )
        ORDER BY f.file_path, f.token_offset
        LIMIT :fetch_limit
    ) hit
    ORDER BY h.hash, hit.file_path, hit.token_offset
    """
).bindparams(
    bindparam("hashes", type_=ARRAY(BigInteger)),
    bindparam("repo_id", type_=Uuid()),
    bindparam("excluded", type_=ARRAY(String)),
    bindparam("fetch_limit", type_=Integer()),
)


class PostgresCloneIndexQuery:
    """Implements ``review.clone_detection.CloneIndexQuery``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _run[T](
        self,
        *,
        operation: str,
        timeout_seconds: float,
        fn: Callable[[AsyncSession], Awaitable[T]],
    ) -> T:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await run_in_session(self._session_factory, None, fn)
        except TimeoutError as exc:
            raise CloneQueryError(
                operation=operation, detail=f"timed out after {timeout_seconds}s"
            ) from exc
        except (SQLAlchemyError, OSError) as exc:
            raise CloneQueryError(operation=operation, detail=str(exc)) from exc

    async def find_candidates(
        self,
        *,
        repo_id: str,
        hashes: Sequence[int],
        excluded_paths: Sequence[str],
        per_hash_limit: int,
        timeout_seconds: float,
    ) -> CloneCandidates:
        distinct_hashes = list(dict.fromkeys(hashes))
        if not distinct_hashes:
            return CloneCandidates()
        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise CloneQueryError(operation="find_candidates", detail=str(exc)) from exc

        async def _read(session: AsyncSession) -> list[Any]:
            rows: list[Any] = []
            for start in range(0, len(distinct_hashes), HASH_BATCH_SIZE):
                result = await session.execute(
                    _FIND_CANDIDATES_QUERY,
                    {
                        "hashes": distinct_hashes[start : start + HASH_BATCH_SIZE],
                        "repo_id": repo_uuid,
                        "excluded": list(excluded_paths),
                        "fetch_limit": per_hash_limit + 1,
                    },
                )
                rows.extend(result.all())
            return rows

        rows = await self._run(
            operation="find_candidates", timeout_seconds=timeout_seconds, fn=_read
        )

        seen_per_hash: dict[int, int] = {}
        hits: list[CloneCandidateHit] = []
        for row in rows:
            count = seen_per_hash.get(row.hash, 0) + 1
            seen_per_hash[row.hash] = count
            if count <= per_hash_limit:
                hits.append(
                    CloneCandidateHit(
                        hash=row.hash, file_path=row.file_path, token_offset=row.token_offset
                    )
                )
        capped = sum(1 for count in seen_per_hash.values() if count > per_hash_limit)
        return CloneCandidates(hits=tuple(hits), capped_hash_count=capped)

    async def load_files(
        self, *, repo_id: str, file_paths: Sequence[str], timeout_seconds: float
    ) -> dict[str, CloneFileIndex]:
        paths = list(dict.fromkeys(file_paths))
        if not paths:
            return {}
        try:
            repo_uuid = uuid.UUID(repo_id)
        except ValueError as exc:
            raise CloneQueryError(operation="load_files", detail=str(exc)) from exc

        async def _read(session: AsyncSession) -> dict[str, CloneFileIndex]:
            has_chunk = exists().where(
                IndexChunkRow.repo_id == repo_uuid,
                IndexChunkRow.pr_number == 0,
                IndexChunkRow.file_path == IndexCloneFileRow.file_path,
            )
            file_rows = (
                (
                    await session.execute(
                        select(IndexCloneFileRow).where(
                            IndexCloneFileRow.repo_id == repo_uuid,
                            IndexCloneFileRow.pr_number == 0,
                            IndexCloneFileRow.file_path.in_(paths),
                            has_chunk,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not file_rows:
                return {}
            fingerprints: dict[str, list[Fingerprint]] = {row.file_path: [] for row in file_rows}
            fingerprint_rows = await session.execute(
                select(
                    IndexCloneFingerprintRow.file_path,
                    IndexCloneFingerprintRow.hash,
                    IndexCloneFingerprintRow.token_offset,
                )
                .where(
                    IndexCloneFingerprintRow.repo_id == repo_uuid,
                    IndexCloneFingerprintRow.pr_number == 0,
                    IndexCloneFingerprintRow.file_path.in_(list(fingerprints)),
                )
                .order_by(IndexCloneFingerprintRow.file_path, IndexCloneFingerprintRow.token_offset)
            )
            for path, hash_value, token_offset in fingerprint_rows.all():
                fingerprints[path].append(Fingerprint(hash=hash_value, token_offset=token_offset))
            return {
                row.file_path: CloneFileIndex(
                    file_path=row.file_path,
                    language=row.language,
                    token_hashes=tuple(row.token_hashes),
                    line_first_token=tuple(row.line_first_token),
                    fingerprints=tuple(fingerprints[row.file_path]),
                )
                for row in file_rows
            }

        return await self._run(operation="load_files", timeout_seconds=timeout_seconds, fn=_read)


_: type[CloneIndexQuery] = PostgresCloneIndexQuery
