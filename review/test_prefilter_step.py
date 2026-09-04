"""Tests for review.prefilter — US3, per the plan's Phase 4 section and
docs/adr/0005-pure-no-io-logic-unit-tested.md (US3 is Unit-only per the
PRD's own Testing table: pure filtering logic with no I/O boundary of its
own — `DiffFetcher` is faked, never a live GitHub call).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ingestion.diff import Diff, DiffFile
from review.prefilter import (
    AGGREGATE_BYTES_CEILING,
    MATERIALIZATION_FILE_CEILING,
    PrefilterStep,
)
from review.step import StepContext

_INPUT = {
    "org_id": "org-1",
    "repo_id": "repo-1",
    "installation_id": 555,
    "repo_full_name": "acme/widgets",
    "pr_number": 1,
    "head_sha": "a" * 40,
}


class _FakeDiffFetcher:
    def __init__(self, diff: Diff) -> None:
        self._diff = diff

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        return self._diff

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        raise NotImplementedError("review/prefilter.py must never call fetch_content()")


def _file(path: str, *, size: int = 100) -> DiffFile:
    return DiffFile(
        path=path, status="modified", mode="100644", blob_sha="sha", blob_size_bytes=size
    )


def _context() -> StepContext:
    return StepContext(job_id="job-1")


async def _run(diff: Diff) -> tuple[str, ...]:
    step = PrefilterStep(_FakeDiffFetcher(diff))
    output = await step.run(dict(_INPUT), _context())
    return tuple(f.path for f in output.diff.files)


# ---------------------------------------------------------------------------
# US3's own Given/When/Then
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lockfile_is_filtered_but_source_file_is_not() -> None:
    diff = Diff(files=(_file("package-lock.json"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


# ---------------------------------------------------------------------------
# Observability — routine per-job `info` log, unconditional on every job,
# per the plan's Phase 4 "Observability & rollback" section: counts filtered
# vs. passed through, plus the fetched-diff file count and byte size.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_log_fires_at_info_on_every_job_with_byte_size(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = Diff(files=(_file("app.py", size=123), _file("lib.py", size=77)))

    with caplog.at_level("INFO", logger="review.prefilter"):
        step = PrefilterStep(_FakeDiffFetcher(diff))
        await step.run(dict(_INPUT), _context())

    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_records) == 1
    assert info_records[0].getMessage() == "prefilter completed"
    assert info_records[0].fetched_diff_bytes == 200  # type: ignore[attr-defined]
    assert info_records[0].total_files == 2  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Each default ignore-pattern category, as its own case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendored_path_is_filtered() -> None:
    diff = Diff(files=(_file("vendor/some_lib/main.go"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


@pytest.mark.asyncio
async def test_generated_file_is_filtered() -> None:
    diff = Diff(files=(_file("static/bundle.min.js"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


@pytest.mark.asyncio
async def test_binary_file_is_filtered() -> None:
    diff = Diff(files=(_file("assets/logo.png"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


# ---------------------------------------------------------------------------
# Materialization file-count ceiling (default 500) — boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exactly_the_ceiling_count_all_survive() -> None:
    files = tuple(_file(f"f{i}.py", size=1) for i in range(MATERIALIZATION_FILE_CEILING))
    diff = Diff(files=files)

    surviving = await _run(diff)

    assert len(surviving) == MATERIALIZATION_FILE_CEILING


@pytest.mark.asyncio
async def test_one_over_the_ceiling_drops_exactly_one(caplog: pytest.LogCaptureFixture) -> None:
    files = tuple(_file(f"f{i}.py", size=1) for i in range(MATERIALIZATION_FILE_CEILING + 1))
    diff = Diff(files=files)

    with caplog.at_level("WARNING", logger="review.prefilter"):
        surviving = await _run(diff)

    assert len(surviving) == MATERIALIZATION_FILE_CEILING
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "materialization" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# Aggregate-bytes ceiling (default ~200MB) — boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_total_landing_exactly_at_the_byte_ceiling_all_survive() -> None:
    diff = Diff(
        files=(
            _file("a.py", size=AGGREGATE_BYTES_CEILING - 1),
            _file("b.py", size=1),
        )
    )

    surviving = await _run(diff)

    assert surviving == ("a.py", "b.py")


@pytest.mark.asyncio
async def test_one_byte_over_the_ceiling_drops_the_crossing_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = Diff(
        files=(
            _file("a.py", size=AGGREGATE_BYTES_CEILING - 1),
            _file("b.py", size=2),
        )
    )

    with caplog.at_level("WARNING", logger="review.prefilter"):
        surviving = await _run(diff)

    assert surviving == ("a.py",)
    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and "aggregate" in r.getMessage()
    ]
    assert len(warnings) == 1
