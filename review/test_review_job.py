"""Integration tests wiring the real, fully-registered Phase 2 macro
registry (pre-filter -> static-analysis -> review-generation -> output,
every macro step a real implementation, not a fake or a single-step test)
end to end against a directly-enqueued test job, via a real Postgres job
queue and the real `apps.worker.dispatch.run_dispatch_loop` — per the PRD's
own Testing table, which names this file specifically for US8/US9: neither
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

docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section extends
this file with US-V7: the same US8 mechanism (a raising fake `LLMClient`),
now injected at the `verification` step's own call site instead of
`review_generation`'s, proving the real, wired six-step chain's fatality
boundary holds for `verification` too.
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
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, StepRegistry
from review.prefilter import PrefilterStep
from review.review_generation import ReviewGenerationStep
from review.static_analysis import StaticAnalysisStep
from review.step import Step
from review.verification import VerificationStep

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
    """The US8/US-V7 fixture: every `complete()` call fails immediately,
    standing in for an LLM Gateway timeout — the same failure mode
    `review/test_review_generation_step.py`'s own negative case proves at
    the single-step tier; this file's own concern is that the *real, wired*
    registry turns this into `PipelineFailedError` and still invokes the
    real terminal output step, not re-proving `ReviewGenerationStep`'s/
    `VerificationStep`'s own error-translation correctness. Used both as
    US8's own `review_generation`-step client (this class's original role)
    and as US-V7's `verification`-step client (Phase 5 of
    docs/plans/plan-deliver-verification-pipeline.md) — the same failure
    mode, injected at a different step's own call site.
    """

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        raise LLMUnreachableError(endpoint="http://stub", detail="simulated timeout")


class _EmptyFindingsLLMClient:
    """The US9 fixture's own review-generation dependency: review-generation
    itself must *succeed* for US9 (only the static-analysis sub-step
    fails), so this returns a trivially well-formed, empty-findings
    response."""

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        return CompletionResponse(content='{"findings": []}', model="fake-model")


class _SingleFindingLLMClient:
    """US-V7's own `review_generation`-step client: unlike
    `_EmptyFindingsLLMClient`, this returns exactly one well-formed
    `source="llm"` finding, so `verification` (this test's own target step)
    actually has a non-empty `llm_findings` batch to judge — an empty batch
    would skip `verification`'s own `LLMClient.complete()` call entirely
    (US-V3's second scenario), never reaching the failure this test needs to
    prove.
    """

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        content = (
            '{"findings": [{"file": "app.py", "line": 1, "severity": "medium", '
            '"message": "a candidate finding for verification to judge"}]}'
        )
        return CompletionResponse(content=content, model="fake-model")


def _mock_github(*, reviews_url: str, check_runs_url: str) -> tuple[respx.Route, respx.Route]:
    """Returns the ``(reviews_route, check_run_route)`` pair every caller
    below needs a handle to (for its own ``call_count``/``calls.last``
    assertions) — **the caller must use these returned routes, never
    re-declare its own via a second ``respx.post(reviews_url)``/
    ``respx.post(check_runs_url)`` call**: respx resets a route's mocked
    response the moment ``respx.post(...)`` is called again for the same
    pattern without a fresh ``.mock(...)``, silently downgrading it to a
    default empty-200 response. That regressed from harmless to a real
    `JSONDecodeError` the moment Phase 2 (US-M11) started actually reading
    the create-review response body (``response.json()["id"]``, for the
    follow-up provenance-capture GET) — a real bug in this file's own
    pre-existing pattern that a plain ``call_count``-only assertion had
    never been able to surface.
    """
    respx.post(f"{_GITHUB_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )
    reviews_route = respx.post(reviews_url).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(check_runs_url).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    # Phase 2 (US-M11)'s follow-up provenance-capture GET, issued whenever a
    # posted review has at least one line-attributed finding (US9's and
    # "review_generation disabled"'s own real Gitleaks findings below) — see
    # `review/test_findings_output_step.py`'s identical mock's own docstring
    # for why an empty response is sufficient here: this file's own scope is
    # the macro-registry's isolation/fatality boundaries, not Phase 2's
    # correlation logic (`adapter/github/test_posting.py`'s job).
    respx.get(f"{reviews_url}/1/comments").mock(return_value=httpx.Response(200, json=[]))
    return reviews_route, check_run_route


class _FakeProvenanceRecorder:
    """A no-op `memory.store.MemorySignalRecorder` fake — see
    `review/test_findings_output_step.py`'s identical fake's own docstring."""

    async def record_comment_provenance(
        self, *, repo_id: str, pr_number: int, commit_sha: str, comments: object
    ) -> None:
        return None

    async def record_signal(
        self,
        *,
        posted_finding_comment_id: int,
        raw_signal_type: str,
        raw_signal_detail: dict[str, object],
        github_comment_id: int | None,
    ) -> None:
        raise AssertionError("record_signal is not exercised by this test file")


def _registry(
    *,
    diff_fetcher: DiffFetcher,
    semgrep: Any,
    llm_client: LLMClient,
    publisher: Any,
    review_generation_enabled: bool = True,
    verification_llm_client: LLMClient | None = None,
) -> StepRegistry:
    """Builds the same macro-step registry `apps/worker/main.py` itself
    constructs, `terminal_step="output"` included — see that module's own
    `_build_review_registry`, including its own
    `review_generation_enabled`-dependent `FindingsOutputStep(depends_on=...)`
    wiring (below).

    `verification_llm_client` (Phase 5 of
    docs/plans/plan-deliver-verification-pipeline.md, US-V7) is `None` by
    default — this file's own pre-existing US8/US9 cases predate
    `verification`'s existence and don't need it wired in at all — a
    four-macro-step registry exactly like before this phase. Passing a
    client inserts `VerificationStep` between `review_generation` and
    `output`, mirroring the real composition root's own six-step chain for
    exactly the one test (US-V7, below) that needs it.
    """
    redactor = NoOpTextRedactor()
    verification_enabled = verification_llm_client is not None
    enabled = {
        "prefilter": True,
        "static_analysis": True,
        "review_generation": review_generation_enabled,
        "verification": verification_enabled,
        "output": True,
        "gitleaks": True,
        "semgrep": True,
    }
    timeouts = {
        "prefilter": 10.0,
        "static_analysis": 30.0,
        "review_generation": 10.0,
        "verification": 10.0,
        "output": 10.0,
        "gitleaks": 10.0,
        "semgrep": 10.0,
    }
    steps: list[Step[Any, Any]] = [
        PrefilterStep(diff_fetcher),
        StaticAnalysisStep(
            diff_fetcher,
            gitleaks_analyzer=GitleaksAnalyzer(),
            semgrep_analyzer=semgrep,
            redactor=redactor,
            enabled=enabled,
            timeouts=timeouts,
        ),
        ReviewGenerationStep(
            llm_client,
            redactor=redactor,
            model="fake-model",
            timeout=10.0,
            max_diff_bytes=200_000,
        ),
    ]
    output_depends_on = "review_generation" if review_generation_enabled else "static_analysis"
    if verification_llm_client is not None:
        steps.append(
            VerificationStep(
                verification_llm_client,
                model="fake-model",
                timeout=10.0,
                max_diff_bytes=200_000,
            )
        )
        output_depends_on = "verification"
    steps.append(FindingsOutputStep(publisher, depends_on=output_depends_on))

    return StepRegistry(
        steps=steps,
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
    """US8: Given the review-generation macro step fails unrecoverably, When
    the Worker handles the failed job, Then it marks the job `FAILED` — and
    (per Phase 7's own fix to a real gap in an earlier draft of US-F6) the
    real terminal output step still posts a failure-conclusion check-run,
    proving `review.pipeline.StepRegistry`'s terminal-step mechanism holds
    for the real, fully-wired registry, not only a fake one
    (`review/test_step_registry.py`'s own equivalent unit case)."""
    reviews_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
    check_runs_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/check-runs"
    _reviews_route, check_run_route = _mock_github(
        reviews_url=reviews_url, check_runs_url=check_runs_url
    )

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        semgrep=GitleaksAnalyzer(),  # unused: review_generation fails before it matters which
        llm_client=_FailingLLMClient(),
        publisher=GitHubFindingsPublisher(
            token_provider=token_provider, provenance_recorder=_FakeProvenanceRecorder()
        ),
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
    reviews_route, check_run_route = _mock_github(
        reviews_url=reviews_url, check_runs_url=check_runs_url
    )

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        semgrep=_RaisingStaticAnalyzer(),
        llm_client=_EmptyFindingsLLMClient(),
        publisher=GitHubFindingsPublisher(
            token_provider=token_provider, provenance_recorder=_FakeProvenanceRecorder()
        ),
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


# --- Rollback: disabling review_generation falls back to static-analysis-only -


@respx.mock
@pytest.mark.asyncio
async def test_review_generation_disabled_falls_back_to_static_analysis_only_output(
    migrated_engine: AsyncEngine, test_rsa_private_key_pem: str
) -> None:
    """Phase 6's own "Rollback" claim ('disabling review_generation via
    per-step config falls back to static-analysis-only output') proven
    against the real, fully-wired registry — a bug this plan's own review
    caught: with a fixed `FindingsOutputStep.depends_on=("review_generation",)`,
    disabling review_generation left `output` with no satisfiable
    dependency, and `StepRegistry._resolve_input`'s own generic
    `RuntimeError` propagated as an unhandled exception, bypassing the
    terminal-step failure-signal mechanism entirely (no check-run posted at
    all). `llm_client` is the same `_FailingLLMClient` US8 uses above — if
    `output` were still wired to `review_generation` (or if the disabled
    step were invoked anyway), this job would fail instead of completing
    normally."""
    reviews_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
    check_runs_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/check-runs"
    reviews_route, check_run_route = _mock_github(
        reviews_url=reviews_url, check_runs_url=check_runs_url
    )

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        semgrep=GitleaksAnalyzer(),
        llm_client=_FailingLLMClient(),
        publisher=GitHubFindingsPublisher(
            token_provider=token_provider, provenance_recorder=_FakeProvenanceRecorder()
        ),
        review_generation_enabled=False,
    )

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, _seed_payload())

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(
            migrated_engine, job_id, JobStatus.DONE, timeout=_WAIT_TIMEOUT_SECONDS
        )

    # static-analysis's own findings made it through to a real posted
    # review with no review-generation step ever having run.
    assert reviews_route.call_count == 1
    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "neutral"


# --- US-V7: a real verification-step failure marks the job FAILED ----------
#
# docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section — the
# same US8 mechanism above, moved one step later in the chain: a raising
# fake `LLMClient` injected at `verification`'s own call site (not
# `review_generation`'s), against the real `StepRegistry`
# (`FailurePolicy.FATAL`), proving the job is marked `FAILED`, no findings
# are posted, and a failure check-run is posted — mirroring
# `test_us8_llm_gateway_failure_marks_job_failed_and_posts_failure_check_run`
# above exactly, adapted to target `verification` instead.


@respx.mock
@pytest.mark.asyncio
async def test_us_v7_verification_gateway_failure_marks_job_failed_and_posts_failure_check_run(
    migrated_engine: AsyncEngine, test_rsa_private_key_pem: str
) -> None:
    """US-V7: Given the verification macro step fails unrecoverably (an LLM
    Gateway timeout at verification's own call site, distinct from
    review_generation's), When the Worker handles the failed job, Then it
    marks the job `FAILED`, posts no findings, and the real terminal output
    step still posts a failure-conclusion check-run — proving
    `review.pipeline.StepRegistry`'s terminal-step mechanism holds for
    `verification` too, once it's wired into the real, fully-wired registry,
    not only a fake one."""
    reviews_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
    check_runs_url = f"{_GITHUB_BASE}/repos/{_OWNER_REPO}/check-runs"
    reviews_route, check_run_route = _mock_github(
        reviews_url=reviews_url, check_runs_url=check_runs_url
    )

    content = {"planted_secret.py": (_TESTDATA_DIR / "planted_secret.py").read_bytes()}
    token_provider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem=test_rsa_private_key_pem
    )
    registry = _registry(
        diff_fetcher=_FakeDiffFetcher(content),
        # Real Gitleaks against the planted-secret fixture — a non-empty,
        # source != "llm" finding forwarded unchanged by verification
        # (US-V6), unaffected by verification's own failure below;
        # `reviews_route.call_count == 0` still holds because the *job*
        # fails fatally before `output` ever reaches its own happy path.
        semgrep=GitleaksAnalyzer(),
        # review_generation must succeed and produce at least one
        # source="llm" finding, or verification would skip its own
        # LLMClient.complete() call entirely (US-V3's second scenario) and
        # never reach the failure this test needs to prove.
        llm_client=_SingleFindingLLMClient(),
        publisher=GitHubFindingsPublisher(
            token_provider=token_provider, provenance_recorder=_FakeProvenanceRecorder()
        ),
        verification_llm_client=_FailingLLMClient(),
    )

    session_factory = create_session_factory(migrated_engine)
    job_queue = JobQueueRepository(session_factory)
    job_id = await job_queue.enqueue(JobType.REVIEW, _seed_payload())

    async with _running_dispatch_loop(job_queue=job_queue, review_registry=registry):
        await _wait_for_status(
            migrated_engine, job_id, JobStatus.FAILED, timeout=_WAIT_TIMEOUT_SECONDS
        )

    # No findings ever posted — the terminal output step's own
    # failure-path never calls `post_review`, only `post_failure_check_run`.
    assert reviews_route.call_count == 0
    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "failure"
