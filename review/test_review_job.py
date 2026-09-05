"""Integration tests wiring the real, fully-registered Phase 2 macro
registry (pre-filter -> static-analysis -> llm-review -> output, every
macro step a real implementation, not a fake or a single-step test) end to
end against a directly-enqueued test job, via a real Postgres job queue and
the real `apps.worker.dispatch.run_dispatch_loop` — per the PRD's own
Testing table, which names this file specifically for US8/US9: neither
story can be proven by a single-step test file on its own, since each of
those only fakes its neighbors; this file is what proves the isolation
boundary (US9) and the fatality boundary (US8) both hold once every macro
step is real and wired together, matching how `apps/worker/main.py` itself
wires them.

Only the two innermost adapters this file has no reason to make "real"
stay fakes, per the same convention Phase 5's own real-binary
`review/test_static_analysis_step.py` cases already established
(``_FakeDiffFetcher`` there too — this file's own scope is proving the
framework's isolation/fatality boundaries hold for real steps wired
together, not re-proving `adapter/github/diff.py`'s own already-tested
fetch correctness): a fake `DiffFetcher` (in-memory fixture content) and a
fake `LLMClient` (an immediate, typed failure for US8; a trivial
well-formed response for US9). Everything else is real: real
`GitleaksAnalyzer`/`SemgrepAnalyzer` (US4/US9's own "planted secret"
fixture, reused from `review/testdata/`, matching
`review/test_static_analysis_step.py`'s own established fixtures), real
`GitHubFindingsPublisher` posting to a stub GitHub server (`respx`), and a
real Postgres job queue (`testcontainers-python`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.analyzers.gitleaks import GitleaksAnalyzer
from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.posting import GitHubFindingsPublisher
from adapter.storage.engine import create_session_factory
from adapter.storage.models import JobQueueRow, JobStatus, JobType
from adapter.storage.queue import JobQueueRepository
from apps.worker.dispatch import run_dispatch_loop
from findings.models import Finding
from findings.redact import NoOpTextRedactor
from indexing.conftest import FakeEmbeddingClient, FakeRepoTreeFetcher
from indexing.pipeline import IndexingJobDependencies
from ingestion.diff import Diff, DiffFetcher, DiffFile
from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMUnreachableError
from llmgateway.models import CompletionRequest, CompletionResponse
from review.llm_review import LLMReviewStep
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, StepRegistry
from review.prefilter import PrefilterStep
from review.static_analysis import StaticAnalysisStep

pytestmark = pytest.mark.integration

_TESTDATA_DIR = Path(__file__).parent / "testdata"
_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_GITHUB_BASE = "https://api.github.com"
_POLL_INTERVAL_SECONDS = 0.05
_WAIT_TIMEOUT_SECONDS = 10.0


@pytest.fixture(scope="module")
def test_rsa_private_key_pem() -> str:
    """See `review/test_findings_output_step.py`'s own identical fixture
    docstring for why this is defined locally rather than imported."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


class _FakeDiffFetcher:
    """A minimal `DiffFetcher` fake: `fetch()` returns a `Diff` covering
    exactly the fixture files this module was constructed with, and
    `fetch_content()` returns their already-loaded bytes — this file's own
    scope is the framework's isolation/fatality boundaries, not re-proving
    `adapter/github/diff.py`'s own already-tested fetch correctness (see
    this module's own docstring).
    """

    def __init__(self, content: dict[str, bytes]) -> None:
        self._content = content

    async def fetch(self, **kwargs: Any) -> Diff:
        return Diff(
            files=tuple(
                DiffFile(
                    path=name,
                    status="added",
                    mode="100644",
                    blob_sha="f" * 40,
                    blob_size_bytes=len(data),
                )
                for name, data in self._content.items()
            )
        )

    async def fetch_content(self, **kwargs: Any) -> dict[str, bytes]:
        return dict(self._content)


class _RaisingStaticAnalyzer:
    """Stands in for a broken "semgrep" sub-step — the US9 fixture, per
    this module's own docstring, mirroring
    `review/test_static_analysis_step.py`'s own identical fake."""

    name = "semgrep"

    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[Finding]:
        raise RuntimeError("simulated: semgrep crashed")


class _FailingLLMClient:
    """The US8 fixture: every `complete()` call fails immediately,
    standing in for an LLM Gateway timeout — the same failure mode
    `review/test_llm_review_step.py`'s own negative case proves at the
    single-step tier; this file's own concern is that the *real, wired*
    registry turns this into `PipelineFailedError` and still invokes the
    real terminal output step, not re-proving `LLMReviewStep`'s own
    error-translation correctness."""

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        raise LLMUnreachableError(endpoint="http://stub", detail="simulated timeout")


class _EmptyFindingsLLMClient:
    """The US9 fixture's own llm-review dependency: llm-review itself must
    *succeed* for US9 (only the static-analysis sub-step fails), so this
    returns a trivially well-formed, empty-findings response."""

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        return CompletionResponse(content='{"findings": []}', model="fake-model")


def _mock_github(*, reviews_url: str, check_runs_url: str) -> None:
    respx.post(f"{_GITHUB_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )
    respx.post(reviews_url).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(check_runs_url).mock(return_value=httpx.Response(201, json={"id": 1}))


def _registry(
    *,
    diff_fetcher: DiffFetcher,
    semgrep: Any,
    llm_client: LLMClient,
    publisher: Any,
    llm_review_enabled: bool = True,
) -> StepRegistry:
    """Builds the same four-macro-step registry `apps/worker/main.py`
    itself constructs, `terminal_step="output"` included — see that
    module's own `_build_review_registry`, including its own
    `llm_review_enabled`-dependent `FindingsOutputStep(depends_on=...)`
    wiring (below)."""
    redactor = NoOpTextRedactor()
    enabled = {
        "prefilter": True,
        "static_analysis": True,
        "llm_review": llm_review_enabled,
        "output": True,
        "gitleaks": True,
        "semgrep": True,
    }
    timeouts = {
        "prefilter": 10.0,
        "static_analysis": 30.0,
        "llm_review": 10.0,
        "output": 10.0,
        "gitleaks": 10.0,
        "semgrep": 10.0,
    }
    return StepRegistry(
        steps=[
            PrefilterStep(diff_fetcher),
            StaticAnalysisStep(
                diff_fetcher,
                gitleaks_analyzer=GitleaksAnalyzer(),
                semgrep_analyzer=semgrep,
                redactor=redactor,
                enabled=enabled,
                timeouts=timeouts,
            ),
            LLMReviewStep(
                llm_client,
                redactor=redactor,
                model="fake-model",
                timeout=10.0,
                max_diff_bytes=200_000,
            ),
            FindingsOutputStep(
                publisher,
                depends_on="llm_review" if llm_review_enabled else "static_analysis",
            ),
        ],
        enabled=enabled,
        timeouts=timeouts,
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )


def _seed_payload() -> dict[str, object]:
    return {
        "org_id": "org-1",
        "repo_id": "repo-1",
        "installation_id": _INSTALLATION_ID,
        "repo_full_name": _OWNER_REPO,
        "pr_number": _PR_NUMBER,
        "head_sha": _HEAD_SHA,
    }


class _NullIndexRepository:
    """A trivial `IndexRepository`/`IndexCommitTracker` stand-in — this
    file's own tests dispatch only `REVIEW` jobs, never `INDEX`, so
    `run_dispatch_loop`'s now-required `indexing_deps` argument never needs
    to do real work here (see `apps/worker/test_dispatch.py`'s identical
    fake for the dedicated `INDEX`-job dispatch tests)."""

    async def upsert_file(self, **kwargs: object) -> None:
        raise AssertionError("_NullIndexRepository.upsert_file should never be called")

    async def delete_file(self, **kwargs: object) -> None:
        raise AssertionError("_NullIndexRepository.delete_file should never be called")

    async def get_last_indexed_commit(self, **kwargs: object) -> str | None:
        raise AssertionError("_NullIndexRepository.get_last_indexed_commit should never be called")

    async def set_last_indexed_commit(self, **kwargs: object) -> None:
        raise AssertionError("_NullIndexRepository.set_last_indexed_commit should never be called")


def _null_indexing_deps() -> IndexingJobDependencies:
    null_repo = _NullIndexRepository()
    return IndexingJobDependencies(
        tree_fetcher=FakeRepoTreeFetcher(),
        embedding_client=FakeEmbeddingClient(),
        index_repository=null_repo,
        commit_tracker=null_repo,
    )


@contextlib.asynccontextmanager
async def _running_dispatch_loop(
    *, job_queue: JobQueueRepository, review_registry: StepRegistry
) -> AsyncIterator[None]:
    task: asyncio.Task[None] = asyncio.create_task(
        run_dispatch_loop(
            job_queue=job_queue,
            review_registry=review_registry,
            indexing_deps=_null_indexing_deps(),
            worker_id="test-worker",
            poll_interval_seconds=_POLL_INTERVAL_SECONDS,
            stuck_job_warning_threshold_seconds=30.0,
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_for_status(
    engine: AsyncEngine, job_id: uuid.UUID, expected: JobStatus, *, timeout: float
) -> None:
    session_factory = create_session_factory(engine)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_status: JobStatus | None = None
    while loop.time() < deadline:
        async with session_factory() as session:
            row = await session.get(JobQueueRow, job_id)
            assert row is not None, f"job {job_id} vanished from job_queue"
            last_status = row.status
            if row.status == expected:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not reach status {expected!r} within {timeout}s (last: {last_status!r})"
    )


# --- US8: a real macro-step failure marks the job FAILED --------------------


@respx.mock
@pytest.mark.asyncio
async def test_us8_llm_gateway_failure_marks_job_failed_and_posts_failure_check_run(
    migrated_engine: AsyncEngine, test_rsa_private_key_pem: str
) -> None:
    """US8: Given the llm-review macro step fails unrecoverably, When the
    Worker handles the failed job, Then it marks the job `FAILED` — and
    (per Phase 7's own fix to a real gap in an earlier draft of US-F6) the
    real terminal output step still posts a failure-conclusion check-run,
    proving `review.pipeline.StepRegistry`'s terminal-step mechanism holds
    for the real, fully-wired registry, not only a fake one
    (`review/test_step_registry.py`'s own equivalent unit case)."""
    reviews_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
    check_runs_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/check-runs"
    _mock_github(reviews_url=reviews_url, check_runs_url=check_runs_url)
    check_run_route = respx.post(check_runs_url)

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        semgrep=GitleaksAnalyzer(),  # unused: llm_review fails before it matters which
        llm_client=_FailingLLMClient(),
        publisher=GitHubFindingsPublisher(token_provider=token_provider),
    )

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, _seed_payload())

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(
            migrated_engine, job_id, JobStatus.FAILED, timeout=_WAIT_TIMEOUT_SECONDS
        )

    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "failure"


# --- US9: one analyzer's failure stays isolated, the review still completes


@respx.mock
@pytest.mark.asyncio
async def test_us9_one_analyzer_failure_stays_isolated_review_still_completes(
    migrated_engine: AsyncEngine, test_rsa_private_key_pem: str
) -> None:
    """US9: Given one analyzer sub-step fails, When static-analysis runs,
    Then the review still completes using the other analyzer's and the
    LLM's findings — real Gitleaks against the planted-secret fixture; a
    broken fake standing in for Semgrep. Proves the sub-step isolation
    boundary (US-F5) doesn't just hold at the single-step tier
    (`review/test_static_analysis_step.py`) but survives being wired into
    the real, full four-macro-step registry, ending in a normal (non-
    `FAILED`) posted result — the opposite outcome from US8 above, not to
    be confused with it."""
    reviews_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
    check_runs_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/check-runs"
    _mock_github(reviews_url=reviews_url, check_runs_url=check_runs_url)
    reviews_route = respx.post(reviews_url)
    check_run_route = respx.post(check_runs_url)

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        semgrep=_RaisingStaticAnalyzer(),
        llm_client=_EmptyFindingsLLMClient(),
        publisher=GitHubFindingsPublisher(token_provider=token_provider),
    )

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, _seed_payload())

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(
            migrated_engine, job_id, JobStatus.DONE, timeout=_WAIT_TIMEOUT_SECONDS
        )

    # Gitleaks' own planted-secret finding still made it all the way
    # through to a real posted review, despite Semgrep having crashed.
    assert reviews_route.call_count == 1
    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "neutral"


# --- Rollback: disabling llm_review falls back to static-analysis-only ------


@respx.mock
@pytest.mark.asyncio
async def test_llm_review_disabled_falls_back_to_static_analysis_only_output(
    migrated_engine: AsyncEngine, test_rsa_private_key_pem: str
) -> None:
    """Phase 6's own "Rollback" claim ('disabling llm_review via per-step
    config falls back to static-analysis-only output') proven against the
    real, fully-wired registry — a bug this plan's own review caught: with a
    fixed `FindingsOutputStep.depends_on=("llm_review",)`, disabling
    llm_review left `output` with no satisfiable dependency, and
    `StepRegistry._resolve_input`'s own generic `RuntimeError` propagated as
    an unhandled exception, bypassing the terminal-step failure-signal
    mechanism entirely (no check-run posted at all). `llm_client` is the
    same `_FailingLLMClient` US8 uses above — if `output` were still wired to
    `llm_review` (or if the disabled step were invoked anyway), this job
    would fail instead of completing normally."""
    reviews_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
    check_runs_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/check-runs"
    _mock_github(reviews_url=reviews_url, check_runs_url=check_runs_url)
    reviews_route = respx.post(reviews_url)
    check_run_route = respx.post(check_runs_url)

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        semgrep=GitleaksAnalyzer(),
        llm_client=_FailingLLMClient(),
        publisher=GitHubFindingsPublisher(token_provider=token_provider),
        llm_review_enabled=False,
    )

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, _seed_payload())

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(
            migrated_engine, job_id, JobStatus.DONE, timeout=_WAIT_TIMEOUT_SECONDS
        )

    # static-analysis's own findings made it through to a real posted
    # review with no llm-review step ever having run.
    assert reviews_route.call_count == 1
    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "neutral"
