"""review.prefilter — the pre-filter pipeline step (US3): fetches a PR's diff and
narrows it to the subset of changed files that should proceed to static-analysis/LLM review.

First real ``review.step.Step`` implementation registered into the
pipeline-step framework — proves the framework genuinely plugs in a real step
with zero changes to ``review/step.py``/``review/pipeline.py``. ``PrefilterStep`` depends only on
``ingestion.diff.DiffFetcher`` (a Protocol, accepted via constructor
injection) — never on ``adapter.github`` directly, per architecture.md's
"domain packages: zero infra imports" rule; ``apps/worker/main.py`` (the
composition root) wires the concrete ``adapter.github.diff.GitHubDiffFetcher``
in.

Three filtering passes, applied in order, each preserving the surviving
files' relative order from ``Diff.files``:

1. **Ignore patterns** (``_is_ignored``) — a plain Python constant, not a
   bootstrap-config override (the PRD marks operator-configurable ignore
   patterns Could-have, not Must-have): lockfiles, vendored paths,
   generated files, binary files (extension-based, not content-sniffed —
   an accepted limitation, see this module's own docstring below).
2. **Materialization ceiling** (default 500 files) — files beyond it are
   dropped, logged at ``warning`` with the count dropped, never fetched.
3. **Aggregate-bytes ceiling** (default 200MB) — closes a cross-tenant
   disk-exhaustion vector the file-count ceiling alone leaves open: files are dropped, in the same
   surviving order,
   once the running total of each survivor's own ``Diff File.blob_size_bytes``
   would exceed it, logged at ``warning`` with the byte total dropped.

Both ceilings are non-fatal, capped-and-logged drops — never a step
failure. Scoped to ``fetch()`` only: this step never calls
``DiffFetcher.fetch_content()`` itself (the static-analysis step does,
after this step has already narrowed the path set) — running the aggregate-bytes check against
``blob_size_bytes`` (already returned by ``fetch()``) rather than actual
fetched content is what keeps this step's own I/O to exactly one ``fetch()``
call per job.

**Accepted limitation, named rather than left implicit:** binary-file
exclusion here is extension/pattern-based (matching on filename), not
content-based — a compiled binary or archive committed under a
non-ignored extension bypasses this filter and reaches Gitleaks, Semgrep,
and the LLM diff prompt like any other file. Bounded downstream by the
existing size/count materialization ceilings and per-step timeouts
rather than caught here; a content-sniffing filter (magic-byte detection)
is a reasonable future enhancement, not built here.

**US7** (docs/specs/spec-observability.md): the five-count funnel below is
also exported as five OTel counters, recorded at the same point as the
existing ``logger.info`` call — see this module's own ``meter``/counter
definitions.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath

from opentelemetry import metrics
from pydantic import ConfigDict

from ingestion.diff import Diff, DiffFetcher, DiffFile
from review._telemetry import suppress_telemetry_errors
from review.step import JobIdentity, Step, StepContext

logger = logging.getLogger(__name__)

# US7 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) — imports
# only the OTel API, same `meter = metrics.get_meter(__name__)` style
# `adapter/telemetry/heartbeat.py` uses in domain/adapter code alike;
# `__name__` here is literally `"review.prefilter"`, the catalog's mandated
# meter name, so no separate string literal is needed. A no-op when no
# `MeterProvider` has been registered (`OTEL_EXPORTER_OTLP_ENDPOINT` unset),
# per US10 — every `.add()` call below is then OTel's own no-op.
meter = metrics.get_meter(__name__)

# Catalog names, verbatim — five counters, one `.add(count)` each per job
# run, no attributes (the funnel is a per-job scalar breakdown, not sliced
# by any further dimension per the catalog). No source code/diff
# content/finding text is ever recorded — only these five plain counts.
_total_files_counter = meter.create_counter(
    "review.prefilter.total_files_total",
    description="Total files in the fetched diff, before any filtering.",
)
_passed_pattern_filter_counter = meter.create_counter(
    "review.prefilter.passed_pattern_filter_total",
    description="Files surviving the ignore-pattern filter.",
)
_dropped_by_materialization_ceiling_counter = meter.create_counter(
    "review.prefilter.dropped_by_materialization_ceiling_total",
    description="Files dropped by the materialization file-count ceiling.",
)
_dropped_by_aggregate_bytes_ceiling_counter = meter.create_counter(
    "review.prefilter.dropped_by_aggregate_bytes_ceiling_total",
    description="Files dropped by the aggregate materialized-bytes ceiling.",
)
_final_file_count_counter = meter.create_counter(
    "review.prefilter.final_file_count_total",
    description="Files surviving all filters, forwarded to the next step.",
)

MATERIALIZATION_FILE_CEILING = 500
AGGREGATE_BYTES_CEILING = 200_000_000  # ~200MB

# Lockfiles: matched by basename, regardless of directory depth.
_IGNORED_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "Gemfile.lock",
        "go.sum",
        "Cargo.lock",
        "composer.lock",
        "mix.lock",
    }
)

# Vendored paths: matched as a path segment, anywhere in the path.
_IGNORED_DIRECTORY_SEGMENTS = frozenset(
    {
        "vendor",
        "node_modules",
        ".venv",
        "venv",
        "third_party",
        "dist",
        "build",
    }
)

# Generated files: matched via fnmatch glob against the full path.
_IGNORED_GENERATED_GLOBS = (
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
)

# Binary files: extension-based, not content-sniffed (see module docstring).
_IGNORED_BINARY_EXTENSION_GLOBS = (
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.webp",
    "*.pdf",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.gz",
    "*.7z",
    "*.exe",
    "*.dll",
    "*.dylib",
    "*.so",
    "*.bin",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.class",
    "*.jar",
    "*.wasm",
    "*.mp4",
    "*.mov",
    "*.mp3",
)


def _is_ignored(path: str) -> bool:
    """Whether ``path`` matches one of this step's default ignore-pattern
    categories (lockfile, vendored path, generated file, binary file).
    """
    basename = posixpath.basename(path)
    if basename in _IGNORED_LOCKFILE_NAMES:
        return True
    if any(segment in _IGNORED_DIRECTORY_SEGMENTS for segment in path.split("/")):
        return True
    if any(fnmatch.fnmatch(path, pattern) for pattern in _IGNORED_GENERATED_GLOBS):
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in _IGNORED_BINARY_EXTENSION_GLOBS)


class PrefilterInput(JobIdentity):
    """The job-identifying fields this step needs to call ``DiffFetcher.fetch()``
    — validated at this step's own boundary from the job's raw payload
    dict (``apps/worker/dispatch.py``'s ``seed_input``), per
    architecture.md's "validate every boundary ... job payloads ... with
    pydantic models" rule. Mirrors ``apps/webhook_api/webhook_routes.py``'s
    ``_review_enqueue_payload`` shape exactly — the one place this payload
    is built.
    """

    model_config = ConfigDict(frozen=True)


class PrefilterOutput(JobIdentity):
    """This step's own output: the filtered ``Diff`` plus the same
    job-identifying context ``PrefilterInput`` carried, forwarded so
    downstream steps (the static-analysis and LLM review steps) that
    only ever receive *this* step's output as their own ``input`` (per
    ``review.pipeline.StepRegistry``'s single-dependency-input design)
    still have what they need to call ``DiffFetcher.fetch_content()``/
    build their own GitHub-scoped calls, without re-deriving it from the
    raw job payload a second time.
    """

    model_config = ConfigDict(frozen=True)

    diff: Diff


def _record_prefilter_counters(
    job_id: str,
    *,
    fetched_diff_bytes: int,
    total: int,
    passed: int,
    count_dropped: int,
    bytes_dropped: int,
    final: int,
) -> None:
    logger.info(
        "prefilter completed",
        extra={
            "job_id": job_id,
            "total_files": total,
            "fetched_diff_bytes": fetched_diff_bytes,
            "passed_pattern_filter": passed,
            "dropped_by_materialization_ceiling": count_dropped,
            "dropped_by_aggregate_bytes_ceiling": bytes_dropped,
            "final_file_count": final,
        },
    )
    # US7 — five plain counts, no attributes; failures never affect the result.
    with suppress_telemetry_errors(
        logger,
        "prefilter telemetry recording failed (prefilter result unaffected)",
        job_id=job_id,
    ):
        _total_files_counter.add(total)
        _passed_pattern_filter_counter.add(passed)
        _dropped_by_materialization_ceiling_counter.add(count_dropped)
        _dropped_by_aggregate_bytes_ceiling_counter.add(bytes_dropped)
        _final_file_count_counter.add(final)


class PrefilterStep:
    """``review.step.Step`` implementation for US3 — see this module's own
    docstring for the full filtering design.
    """

    name = "prefilter"
    depends_on: tuple[str, ...] = ()

    def __init__(self, diff_fetcher: DiffFetcher) -> None:
        self._diff_fetcher = diff_fetcher

    async def run(self, step_input: dict[str, object], context: StepContext) -> PrefilterOutput:
        parsed = PrefilterInput.model_validate(step_input)

        diff = await self._diff_fetcher.fetch(
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
        )

        pattern_filtered = [file for file in diff.files if not _is_ignored(file.path)]

        count_filtered, count_dropped = self._apply_materialization_ceiling(pattern_filtered)
        bytes_filtered, bytes_dropped_count, _bytes_dropped_total = (
            self._apply_aggregate_bytes_ceiling(count_filtered)
        )

        fetched_diff_bytes = sum(file.blob_size_bytes or 0 for file in diff.files)

        _record_prefilter_counters(
            context.job_id,
            fetched_diff_bytes=fetched_diff_bytes,
            total=len(diff.files),
            passed=len(pattern_filtered),
            count_dropped=count_dropped,
            bytes_dropped=bytes_dropped_count,
            final=len(bytes_filtered),
        )

        return PrefilterOutput(
            diff=Diff(files=tuple(bytes_filtered)),
            org_id=parsed.org_id,
            repo_id=parsed.repo_id,
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            pr_number=parsed.pr_number,
            head_sha=parsed.head_sha,
            base_sha=parsed.base_sha,
            pr_title=parsed.pr_title,
            pr_head_ref=parsed.pr_head_ref,
            pr_issue_keys=parsed.pr_issue_keys,
            pr_is_fork=parsed.pr_is_fork,
            private=parsed.private,
        )

    def _apply_materialization_ceiling(self, files: list[DiffFile]) -> tuple[list[DiffFile], int]:
        if len(files) <= MATERIALIZATION_FILE_CEILING:
            return files, 0

        kept = files[:MATERIALIZATION_FILE_CEILING]
        dropped_count = len(files) - MATERIALIZATION_FILE_CEILING
        logger.warning(
            "prefilter: materialization file-count ceiling exceeded; dropping excess files",
            extra={"ceiling": MATERIALIZATION_FILE_CEILING, "dropped_count": dropped_count},
        )
        return kept, dropped_count

    def _apply_aggregate_bytes_ceiling(
        self, files: list[DiffFile]
    ) -> tuple[list[DiffFile], int, int]:
        kept: list[DiffFile] = []
        dropped: list[DiffFile] = []
        running_total = 0
        for file in files:
            size = file.blob_size_bytes or 0
            if running_total + size > AGGREGATE_BYTES_CEILING:
                dropped.append(file)
                continue
            running_total += size
            kept.append(file)

        if dropped:
            dropped_bytes_total = sum(f.blob_size_bytes or 0 for f in dropped)
            logger.warning(
                "prefilter: aggregate materialized-bytes ceiling exceeded; dropping excess files",
                extra={
                    "ceiling_bytes": AGGREGATE_BYTES_CEILING,
                    "dropped_count": len(dropped),
                    "dropped_bytes_total": dropped_bytes_total,
                },
            )
            return kept, len(dropped), dropped_bytes_total

        return kept, 0, 0


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[dict[str, object], PrefilterOutput]] = PrefilterStep
