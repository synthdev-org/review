"""review.clone_detection — the ``clone_detection`` review step and its consumer-side
contract (``docs/plans/plan-phase-10.md``, Phases 3 and 4).

Phase 3 defined what the Postgres adapter (``adapter/storage/clone_query.py``)
implements: the ``CloneIndexQuery`` Protocol, ``CloneQueryError`` and the candidate-hit
models. Phase 4 adds ``CloneDetectionStep``: it fingerprints the PR's changed files,
compares them with each other and with the default-branch index, and emits at
most a few low-severity ``duplication`` findings for code the PR introduces.
Importable without infra.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from findings.models import Finding
from indexing.chunk import ChunkLanguage, ChunkParseError, language_for_path, parse_file
from indexing.clone import (
    CloneFileIndex,
    CloneIndexError,
    NormalizedTokens,
    build_clone_index,
    is_excluded_path,
    match_regions,
)
from ingestion.diff import Diff, DiffContentMalformedError, DiffContentUnavailableError, DiffFetcher
from review._telemetry import suppress_telemetry_errors
from review.changed_lines import changed_lines
from review.clone_regions import RegionMatch, is_new_code, plan_findings
from review.memory_suppression import SuppressionConfig, suppress_via_memory
from review.step import JobIdentity, Step, StepContext, job_identity_fields

logger = logging.getLogger(__name__)

# Assumption 10: fan-out caps are module constants, not settings.
PER_HASH_CANDIDATE_LIMIT = 20
CANDIDATE_FILE_CAP = 200
MAX_FILES_CONSIDERED = 100
"""Most PR files fingerprinted per job (bounds the ``to_thread`` parses left running)."""


class CloneQueryError(Exception):
    """Raised by ``CloneIndexQuery`` implementations on a database failure or
    timeout — never a raw ``sqlalchemy``/``asyncpg`` exception, mirroring
    ``review.context_builder.ContextQueryError``.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"clone query operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class CloneCandidateHit(BaseModel):
    """One stored fingerprint whose hash equals a queried hash."""

    model_config = ConfigDict(frozen=True)

    hash: int
    file_path: str
    token_offset: int


class CloneCandidates(BaseModel):
    """``find_candidates`` result: the hits (at most ``per_hash_limit`` per
    hash) and how many queried hashes had more matches than that and were cut.
    """

    model_config = ConfigDict(frozen=True)

    hits: tuple[CloneCandidateHit, ...] = ()
    capped_hash_count: int = 0


class CloneIndexQuery(Protocol):
    """Read path over the canonical (``pr_number = 0``) clone index. Both
    methods only see files that still have an ``index_chunk`` row.
    """

    async def find_candidates(
        self,
        *,
        repo_id: str,
        hashes: Sequence[int],
        excluded_paths: Sequence[str],
        per_hash_limit: int,
        timeout_seconds: float,
    ) -> CloneCandidates: ...

    async def load_files(
        self, *, repo_id: str, file_paths: Sequence[str], timeout_seconds: float
    ) -> dict[str, CloneFileIndex]: ...


# ---------------------------------------------------------------------------
# Phase 4 — CloneDetectionStep
# ---------------------------------------------------------------------------

# Telemetry (plan Assumption 12). Literal instrument names so
# scripts/check-dashboard-metrics.py finds them; `outcome` is a closed set.
meter = metrics.get_meter("review.clone_detection")
_outcome_counter = meter.create_counter(
    "review.clone_detection.outcome_total", description="Clone-detection step outcomes."
)
_duration_histogram = meter.create_histogram(
    "review.clone_detection.duration_ms", unit="ms", description="Step wall-clock duration."
)
_candidates_counter = meter.create_counter(
    "review.clone_detection.candidates_total", description="Verified duplicated regions found."
)
_filtered_counter = meter.create_counter(
    "review.clone_detection.filtered_total",
    description="Regions dropped as not new code or import-only.",
)
_findings_counter = meter.create_counter(
    "review.clone_detection.findings_total", description="Clone findings emitted."
)
# Counts findings dropped by the Memory similarity pre-check (plan Phase 5).
_memory_suppressed_counter = meter.create_counter(
    "review.clone_detection.memory_suppressed_total",
    description="Clone findings suppressed by an approved dismissal.",
)
_cap_dropped_counter = meter.create_counter(
    "review.clone_detection.cap_dropped_total",
    description="Merged regions dropped by clone_detection_max_findings.",
)
_candidate_cap_dropped_counter = meter.create_counter(
    "review.clone_detection.candidate_cap_dropped_total",
    description="Files/hashes dropped by the fan-out caps, by `reason`: the cap that fired.",
)
_phase_duration_histogram = meter.create_histogram(
    "review.clone_detection.phase_duration_ms",
    unit="ms",
    description="Wall-clock duration of one step phase, by `phase`; recorded even when the "
    "phase raises or the step times out, so a timeout can be traced to its slow phase.",
)
_files_skipped_counter = meter.create_counter(
    "review.clone_detection.files_skipped_total",
    description="PR files that could not be fingerprinted, by `reason` (unparseable, no_content).",
)
_memory_suppression_failed_counter = meter.create_counter(
    "review.clone_detection.memory_suppression_failed_total",
    description="Jobs whose Memory dismissal check failed, so dismissed findings were kept.",
)

# Closed `reason`/`phase` label sets (no path, code or message is ever an attribute).
_CAP_FILES: Final = "files"
_CAP_PER_HASH: Final = "per_hash"
_CAP_CANDIDATE_FILES: Final = "candidate_files"
_SKIP_UNPARSEABLE: Final = "unparseable"
_SKIP_NO_CONTENT: Final = "no_content"

_OUTCOME_COMPLETED: Final = "completed"
_OUTCOME_NO_MATCHES: Final = "no_matches"
_OUTCOME_NO_ELIGIBLE: Final = "no_eligible_files"
_OUTCOME_FAILED_QUERY: Final = "failed_open_query"
_OUTCOME_FAILED_CONTENT: Final = "failed_open_content"
_OUTCOME_FAILED_TIMEOUT: Final = "failed_open_timeout"


class _CloneDetectionInput(JobIdentity):
    """Mirrors ``StaticAnalysisOutput``/``PrefilterOutput``'s shape without
    importing them (the step-to-step import boundary); ``findings`` defaults
    to empty so the step can sit directly behind ``prefilter``."""

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    findings: list[Finding] = Field(default_factory=list)


class CloneDetectionOutput(JobIdentity):
    """The incoming diff and findings plus this step's clone findings.
    ``base_sha`` and the product-knowledge fields are forwarded explicitly
    (``JobIdentity``'s docstring: optional fields are never auto-carried)."""

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    diff: Diff
    findings: list[Finding]


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    path: str
    index: CloneFileIndex
    tokens: NormalizedTokens
    added_lines: frozenset[int]


@dataclass
class _Result:
    outcome: str = _OUTCOME_COMPLETED
    findings: tuple[Finding, ...] = ()
    files_considered: int = 0
    files_skipped: int = 0
    candidate_count: int = 0
    filtered_count: int = 0
    cap_dropped_count: int = 0
    memory_suppressed_count: int = 0
    candidate_cap_dropped: dict[str, int] = field(default_factory=dict)
    files_skipped_by_reason: dict[str, int] = field(default_factory=dict)
    memory_suppression_failed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@contextmanager
def _timed_phase(phase: str) -> Iterator[None]:
    start = time.monotonic()
    try:
        yield
    finally:  # also on error/cancellation: a timed-out step is when the slow phase matters
        with suppress_telemetry_errors(
            logger, "clone_detection telemetry recording failed (review unaffected)", phase=phase
        ):
            _phase_duration_histogram.record(
                int((time.monotonic() - start) * 1000), {"phase": phase}
            )


def _count_dropped(result: _Result, reason: str, count: int) -> None:
    if count:
        result.candidate_cap_dropped[reason] = result.candidate_cap_dropped.get(reason, 0) + count


def _count_skipped(result: _Result, reason: str) -> None:
    result.files_skipped_by_reason[reason] = result.files_skipped_by_reason.get(reason, 0) + 1


def _prepare(
    language: ChunkLanguage, path: str, content: bytes, patch: str | None
) -> _PreparedFile:
    """Parse and tokenize one PR file (CPU-bound; runs in a thread)."""
    index, tokens = build_clone_index(language, path, parse_file(language, path, content))
    return _PreparedFile(path, index, tokens, changed_lines(patch))


def _is_import_boilerplate(tokens: NormalizedTokens, start: int, end: int) -> bool:
    """Whether the region is (almost) only import/use/require tokens (US-C8). Not
    ``indexing.clone.region_is_import_only``'s strict "every token": a copied import block
    still drags in the file's ``<?php``/``package``/``namespace`` header, which would otherwise
    defeat the rule for PHP and Go.
    """
    return sum(tokens.in_import[start:end]) * 10 >= (end - start) * 9


def _match_all(
    prepared: Sequence[_PreparedFile],
    stored_files: dict[str, CloneFileIndex],
    shared_hashes: dict[str, set[int]],
    *,
    min_tokens: int,
    min_lines: int,
) -> tuple[list[RegionMatch], int, int]:
    """PR-internal pairs and PR-vs-stored matches, filtered to new code.
    Returns ``(matches, candidate_count, filtered_count)``. CPU-bound."""
    pr_hashes = {item.path: {fp.hash for fp in item.index.fingerprints} for item in prepared}
    matches: list[RegionMatch] = []
    candidates = filtered = 0

    def consider(item: _PreparedFile, stored: CloneFileIndex) -> None:
        nonlocal candidates, filtered
        for region in match_regions(item.index, stored, min_tokens=min_tokens, min_lines=min_lines):
            candidates += 1
            if _is_import_boilerplate(
                item.tokens, region.query_token_start, region.query_token_end
            ) or not is_new_code(region.query_start_line, region.query_end_line, item.added_lines):
                filtered += 1
                continue
            matches.append(
                RegionMatch(
                    file=item.path,
                    start_line=region.query_start_line,
                    end_line=region.query_end_line,
                    other_path=region.stored_file_path,
                    other_start_line=region.stored_start_line,
                    other_end_line=region.stored_end_line,
                )
            )

    # PR-internal: each unordered pair once, reported in the later-sorted path.
    for later_pos, later in enumerate(prepared):
        for earlier in prepared[:later_pos]:
            if pr_hashes[later.path] & pr_hashes[earlier.path]:
                consider(later, earlier.index)
    for item in prepared:
        for stored_path, hashes in shared_hashes.items():
            if pr_hashes[item.path] & hashes:
                consider(item, stored_files[stored_path])
    return matches, candidates, filtered


class CloneDetectionStep:
    """``review.step.Step`` for US-C4..C10 and C14 — Type-2 clone detection on
    the code a PR adds.

    **Fail open (US-C14).** The whole body runs inside one internal
    ``asyncio.wait_for`` (``internal_timeout_seconds``, derived by the
    composition root as the step timeout minus 5 s, because a macro step's
    registry timeout is fatal). ``CloneQueryError``,
    ``DiffContentUnavailableError``/``DiffContentMalformedError`` and the
    timeout each return the incoming findings unchanged with a named
    ``failed_open_*`` outcome; ``ChunkParseError``/``CloneIndexError`` are
    caught per file so one unparseable PR file never hides a copy elsewhere.
    Anything else is a bug and propagates. A timeout cancels the awaiting
    task, but a ``to_thread`` parse already running finishes in the
    background (bounded by ``MAX_FILES_CONSIDERED``).

    ``no_matches`` means no candidate row matched (an empty or
    not-yet-backfilled index, or simply no overlapping code) and no
    PR-internal duplicate was found. It cannot tell those apart, so backfill
    coverage is read from ``review.indexing.clone_backfill_total``, not from
    this outcome.
    """

    name = "clone_detection"

    def __init__(
        self,
        diff_fetcher: DiffFetcher,
        clone_query: CloneIndexQuery,
        *,
        internal_timeout_seconds: float,
        min_tokens: int,
        min_lines: int,
        max_findings: int,
        depends_on: str = "static_analysis",
        # None (or disabled) skips the Memory similarity pre-check entirely (US-M12).
        memory_suppression: SuppressionConfig | None = None,
    ) -> None:
        self._diff_fetcher = diff_fetcher
        self._clone_query = clone_query
        self._internal_timeout_seconds = internal_timeout_seconds
        self._min_tokens = min_tokens
        self._min_lines = min_lines
        self._max_findings = max_findings
        self._memory_suppression = memory_suppression
        self.depends_on: tuple[str, ...] = (depends_on,)

    async def _detect(self, parsed: _CloneDetectionInput) -> _Result:
        result = _Result()
        eligible = sorted(
            (
                file
                for file in parsed.diff.files
                if file.status != "removed"
                and file.patch
                and language_for_path(file.path) is not None
                and not is_excluded_path(file.path)
            ),
            key=lambda file: file.path,
        )
        if not eligible:
            result.outcome = _OUTCOME_NO_ELIGIBLE
            return result
        considered = eligible[:MAX_FILES_CONSIDERED]
        result.files_skipped = len(eligible) - len(considered)
        _count_dropped(result, _CAP_FILES, result.files_skipped)

        with _timed_phase("fetch_content"):
            content = await self._diff_fetcher.fetch_content(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                diff=parsed.diff,
                paths=[file.path for file in considered],
            )

        async def prepare(path: str, patch: str | None) -> _PreparedFile | None:
            language = language_for_path(path)
            if language is None or path not in content:
                _count_skipped(result, _SKIP_NO_CONTENT)
                return None
            try:
                return await asyncio.to_thread(_prepare, language, path, content[path], patch)
            except (ChunkParseError, CloneIndexError) as exc:
                _count_skipped(result, _SKIP_UNPARSEABLE)
                logger.warning(
                    "clone_detection skipped file",
                    extra={"path": path, "error_type": type(exc).__name__, "error": str(exc)},
                )
                return None

        with _timed_phase("prepare"):
            prepared_all = await asyncio.gather(*(prepare(f.path, f.patch) for f in considered))
        prepared = [item for item in prepared_all if item is not None]
        result.files_considered = len(prepared)
        result.files_skipped += len(considered) - len(prepared)

        # Every path the PR changes, deletes or renames: their default-branch
        # rows describe code the PR itself changes (US-C6).
        excluded = sorted(
            {file.path for file in parsed.diff.files}
            | {file.previous_path for file in parsed.diff.files if file.previous_path}
        )
        hashes = sorted({fp.hash for item in prepared for fp in item.index.fingerprints})
        stored_files: dict[str, CloneFileIndex] = {}
        shared_hashes: dict[str, set[int]] = {}
        if hashes:
            with _timed_phase("find_candidates"):
                found = await self._clone_query.find_candidates(
                    repo_id=parsed.repo_id,
                    hashes=hashes,
                    excluded_paths=excluded,
                    per_hash_limit=PER_HASH_CANDIDATE_LIMIT,
                    timeout_seconds=self._internal_timeout_seconds,
                )
            _count_dropped(result, _CAP_PER_HASH, found.capped_hash_count)
            by_file: dict[str, set[int]] = defaultdict(set)
            for hit in found.hits:
                if not is_excluded_path(hit.file_path):
                    by_file[hit.file_path].add(hit.hash)
            ranked = sorted(by_file, key=lambda path: (-len(by_file[path]), path))
            kept = ranked[:CANDIDATE_FILE_CAP]
            _count_dropped(result, _CAP_CANDIDATE_FILES, len(ranked) - len(kept))
            if kept:
                with _timed_phase("load_files"):
                    stored_files = await self._clone_query.load_files(
                        repo_id=parsed.repo_id,
                        file_paths=kept,
                        timeout_seconds=self._internal_timeout_seconds,
                    )
                shared_hashes = {path: by_file[path] for path in stored_files}
            result.extra["candidate_files"] = len(kept)

        with _timed_phase("match"):
            matches, result.candidate_count, result.filtered_count = await asyncio.to_thread(
                _match_all,
                prepared,
                stored_files,
                shared_hashes,
                min_tokens=self._min_tokens,
                min_lines=self._min_lines,
            )
        if result.candidate_count == 0 and not stored_files:
            result.outcome = _OUTCOME_NO_MATCHES
        plan = plan_findings(
            matches,
            {item.path: item.added_lines for item in prepared},
            max_findings=self._max_findings,
        )
        findings = list(plan.findings)
        config = self._memory_suppression
        if config is not None and config.enabled and findings:
            try:
                with _timed_phase("memory_suppression"):
                    findings, result.memory_suppressed_count = await suppress_via_memory(
                        findings, config=config, repo_id=parsed.repo_id
                    )
            except Exception as exc:  # MemoryStoreError from the matcher or an embed failure
                # Fail open like the rest of the step: a noisy duplicate beats a lost review.
                result.memory_suppression_failed = True
                logger.warning(
                    "clone_detection memory suppression failed (findings kept)",
                    extra={"error_type": type(exc).__name__, "error": str(exc)},
                )
        result.findings = tuple(findings)
        result.cap_dropped_count = plan.cap_dropped_count
        return result

    async def run(self, step_input: Any, context: StepContext) -> CloneDetectionOutput:
        # Any: the registry hands over the raw upstream output (StaticAnalysisOutput
        # or PrefilterOutput); from_attributes enforces the shape at runtime.
        parsed = _CloneDetectionInput.model_validate(step_input, from_attributes=True)
        start = time.monotonic()
        result = _Result()
        error_extra: dict[str, Any] = {}
        try:
            result = await asyncio.wait_for(
                self._detect(parsed), timeout=self._internal_timeout_seconds
            )
        except CloneQueryError as exc:
            result.outcome = _OUTCOME_FAILED_QUERY
            error_extra = {"query_operation": exc.operation, "query_detail": exc.detail}
        except (DiffContentUnavailableError, DiffContentMalformedError) as exc:
            result.outcome = _OUTCOME_FAILED_CONTENT
            error_extra = {"error_type": type(exc).__name__, "error": str(exc)}
        except TimeoutError:
            result.outcome = _OUTCOME_FAILED_TIMEOUT
        duration_ms = int((time.monotonic() - start) * 1000)
        self._record_and_log(parsed, context.job_id, result, duration_ms, error_extra)
        return CloneDetectionOutput(
            diff=parsed.diff,
            findings=[*parsed.findings, *result.findings],
            **job_identity_fields(parsed),
        )

    @staticmethod
    def _record_and_log(
        parsed: _CloneDetectionInput,
        job_id: str,
        result: _Result,
        duration_ms: int,
        error_extra: dict[str, Any],
    ) -> None:
        attributes = {"outcome": result.outcome}
        with suppress_telemetry_errors(
            logger, "clone_detection telemetry recording failed (review unaffected)", job_id=job_id
        ):
            _outcome_counter.add(1, attributes)
            _duration_histogram.record(duration_ms, attributes)
            _candidates_counter.add(result.candidate_count)
            _filtered_counter.add(result.filtered_count)
            _findings_counter.add(len(result.findings))
            _cap_dropped_counter.add(result.cap_dropped_count)
            _memory_suppressed_counter.add(result.memory_suppressed_count)
            for reason, count in result.candidate_cap_dropped.items():
                _candidate_cap_dropped_counter.add(count, {"reason": reason})
            for reason, count in result.files_skipped_by_reason.items():
                _files_skipped_counter.add(count, {"reason": reason})
            if result.memory_suppression_failed:
                _memory_suppression_failed_counter.add(1)
        candidate_cap_dropped_count = sum(result.candidate_cap_dropped.values())
        log_extra: dict[str, Any] = {
            "job_id": job_id,
            "org_id": parsed.org_id,
            "repo_id": parsed.repo_id,
            "repo_full_name": parsed.repo_full_name,
            "pr_number": parsed.pr_number,
            "outcome": result.outcome,
            "duration_ms": duration_ms,
            "candidate_count": result.candidate_count,
            "filtered_count": result.filtered_count,
            "dropped_by_cap_count": result.cap_dropped_count,
            "suppressed_by_memory_count": result.memory_suppressed_count,
            "candidate_cap_dropped_count": candidate_cap_dropped_count,
            "finding_count": len(result.findings),
            "files_considered": result.files_considered,
            "files_skipped": result.files_skipped,
            **result.extra,
            **error_extra,
        }
        if result.outcome.startswith("failed_open"):
            logger.warning("clone_detection failed open", extra=log_extra)
        elif result.cap_dropped_count or candidate_cap_dropped_count:
            logger.warning("clone_detection dropped matches at a cap", extra=log_extra)
        else:
            logger.info("clone_detection completed", extra=log_extra)


# Runtime confirmation that this class satisfies the Step Protocol.
_clone_detection_step: type[Step[Any, CloneDetectionOutput]] = CloneDetectionStep
