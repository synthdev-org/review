"""Tests for the worker entrypoint's main() composition root.

`test_main_parses_settings_and_logs_startup` is the Phase 1 unit test
(config parsing + logging only), kept a pure unit test — no real I/O — by
substituting a fake `LLMClient` at `_build_llm_client`, the one seam
`main()` exposes for it (per docs/rules/architecture.md's "wire concrete
implementations only at the composition root" rule: the composition root
is exactly where a test substitutes a fake for the real adapter).

Phase 3 adds a second seam, `_run_dispatch_loop_forever`, for the same
reason: `main()` now hands off to a loop that runs forever, so every test
in this file that reaches that point substitutes a no-op fake for it —
proving `main()`'s own wiring (does it reach the dispatch hand-off with a
real `JobQueueRepository`/`StepRegistry` built from settings) without
actually entering the infinite loop. The loop's own real behavior (claim,
dispatch, mark done/failed) is `apps/worker/test_dispatch.py`'s job, against
a real registry and a real Postgres, not this file's.

The two LLM-reachability tests below exercise the LLM Gateway startup
connectivity check genuinely, through the real `LiteLLMClient`/`litellm`
code path against local-only HTTP servers, per docs/rules/testing.md's
middle tier; both are marked `@pytest.mark.integration` because they cross
the `adapter.llm` boundary, not merely call a fake.

The indexing delivery plan's Phase 4 adds the local embedding model
startup check tests below (not marked `@pytest.mark.integration`,
deliberately): unlike the LLM check, the embedding model needs no network
and no external service to stand up — its real dependency (the Dockerfile-
baked model weights at `/app/models/all-MiniLM-L6-v2`) is already present
in this repo's `dev` image, the same "real, but fully local, baked-in
dependency exercised without mocking" pattern already established here for
the GitHub-private-key (real RS256 signing) and Gitleaks-redaction (real
binary) tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

import adapter.telemetry.heartbeat as heartbeat_module
import apps.worker.main as worker_main
from adapter.analyzers.gitleaks import GitleaksTextRedactor
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.posting import GitHubFindingsPublisher
from adapter.github.tree import GitHubTreeFetcher
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.queue import JobQueueRepository
from adapter.telemetry.sdk import init_telemetry
from apps.worker.main import main
from apps.worker.settings import WorkerSettings
from findings.redact import NoOpTextRedactor, TextRedactor
from indexing.chunk import Chunk
from indexing.embedding import EmbeddingVector
from indexing.pipeline import IndexingJobDependencies
from indexing.tree import RepoTreeEntry
from ingestion.diff import Diff
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse
from llmgateway.routing import FallbackModelRoutingResolver
from memory.reaction_poll import MemoryReactionPollJobDependencies
from review.config import StepEnablement, StepTimeouts
from review.context_builder import CallerHit
from review.pipeline import StepRegistry
from review.step import StepContext


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


class _FakeLLMClient:
    """A fake `LLMClient` (see llmgateway.client.LLMClient) that answers
    every call immediately, with no real I/O — used to keep the plain
    "does main() parse config and log startup" test a pure unit test."""

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        return CompletionResponse(content="pong", model=request.model)


async def _noop_dispatch_loop_forever(
    job_queue: JobQueueRepository,
    registry: StepRegistry,
    indexing_deps: IndexingJobDependencies,
    memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
    settings: WorkerSettings,
    worker_id: str,
    http_client: httpx.AsyncClient,
) -> None:
    """The fake substituted for `_run_dispatch_loop_forever` in every test
    below that reaches it — returns immediately instead of looping forever,
    so `main()` returns and the test can make its assertions. Closes
    `http_client` itself, matching the real function's own responsibility,
    so `main()` doesn't leak the real `httpx.AsyncClient` it constructs
    across these tests."""
    await http_client.aclose()


def test_main_parses_settings_and_logs_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    payloads = [json.loads(line) for line in lines]

    assert any(
        p["message"] == "worker starting" and p.get("environment") == "development"
        for p in payloads
    )
    assert any(p["message"] == "llm endpoint reachable" for p in payloads)
    assert any(p["message"] == "worker started" for p in payloads)


def _unused_port_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _start_stub_llm_server() -> HTTPServer:
    """A minimal local HTTP server standing in for LM Studio's
    OpenAI-compatible endpoint, returning one well-formed completion for
    every request. Kept small and inline here rather than imported from
    `adapter/llm/conftest.py` — `apps/worker`'s tests stay self-contained,
    the same way `adapter/storage`'s do."""
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "local-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    body = json.dumps(payload).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.integration
def test_main_succeeds_when_llm_endpoint_reachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)
    server = _start_stub_llm_server()
    port = server.server_address[1]
    monkeypatch.setenv("REVIEW_WORKER_LLM_ENDPOINT", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("REVIEW_WORKER_LLM_TIMEOUT_SECONDS", "5")

    try:
        main()  # must not raise SystemExit
    finally:
        server.shutdown()
        server.server_close()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(p["message"] == "llm endpoint reachable" for p in payloads)
    assert any(p["message"] == "worker started" for p in payloads)


@pytest.mark.integration
def test_main_exits_non_zero_when_llm_endpoint_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    # Nothing is listening on this port — a genuine connection-refused,
    # exactly the "LM Studio isn't running" case this phase's fail-fast
    # startup check must catch.
    monkeypatch.setenv("REVIEW_WORKER_LLM_ENDPOINT", _unused_port_endpoint())
    monkeypatch.setenv("REVIEW_WORKER_LLM_TIMEOUT_SECONDS", "2")

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(
        p["message"] == "LLM endpoint unreachable at startup; exiting" and p["level"] == "ERROR"
        for p in payloads
    )


# --- Phase 4: GitHub App private-key startup validation --------------------
#
# `apps/worker/conftest.py` sets a real, usable test RSA key as the default
# `REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY` env var, so every test above this
# point already exercises the *valid*-key path implicitly (main() would
# raise SystemExit(1) here too if that default weren't usable). The two
# tests below lock in both branches explicitly, the same way Phase 1's own
# rate-limiter tests lock in both branches of that startup check.


def test_main_succeeds_with_valid_github_app_private_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()  # must not raise SystemExit

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(p["message"] == "github app private key valid" for p in payloads)
    assert any(p["message"] == "worker started" for p in payloads)


def test_main_exits_non_zero_with_malformed_github_app_private_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY", "not-a-real-private-key")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(
        p["message"] == "GitHub App private key is not usable at startup; exiting"
        and p["level"] == "ERROR"
        for p in payloads
    )


# --- Phase 4: GitHub API retry-disabled startup log -------------------------
#
# Locks in the third leg of this plan's three-way startup-log-level
# distinction (the rate limiter's `warning`, redaction's `warning`, and
# this one's `info` — see the plan's "Assumptions this plan makes" section):
# this log fires only when an operator has actively deviated away from an
# active-by-default protection (github_retry_max_attempts's own shipped
# default is `3`, not `0`), unlike the rate limiter's own `warning`, which
# fires for *this phase's own shipped default*.


def test_github_retry_disabled_logs_info_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_GITHUB_RETRY_MAX_ATTEMPTS", "0")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = [
        p
        for p in payloads
        if p["message"] == "GitHub API retries are disabled (github_retry_max_attempts=0)"
    ]
    assert len(matching) == 1
    assert matching[0]["level"] == "INFO"


def test_main_succeeds_with_loadable_embedding_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default `embedding_model_path` (the Dockerfile-baked, real
    bundled model) loads and infers successfully — no mocking, the same
    "real dependency baked into the dev image, exercised for real" pattern
    the GitHub-private-key and Gitleaks-redaction tests above already
    establish for this file."""
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()  # must not raise SystemExit

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(p["message"] == "local embedding model loaded" for p in payloads)
    assert any(p["message"] == "worker started" for p in payloads)


def test_main_exits_non_zero_with_unloadable_embedding_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_EMBEDDING_MODEL_PATH", "/nonexistent/model/path")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(
        p["message"] == "local embedding model is not usable at startup; exiting"
        and p["level"] == "ERROR"
        for p in payloads
    )


def test_github_retry_default_does_not_log_disabled_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert not [p for p in payloads if "GitHub API retries are disabled" in p["message"]]


# --- Phase 5: secret redaction / static-analysis-gitleaks-enabled decoupling ---
#
# Two cases proving the decoupling ``findings/redact.py``'s own module
# docstring describes, rather than the coupled behavior an earlier draft
# would have tested — per the plan's Phase 5 Tests section. Both monkeypatch
# `_build_review_registry`, the one seam where the wired `TextRedactor`
# instance is passed to something this test can inspect, without needing to
# reach into the constructed `StepRegistry`'s own private state.


def _capture_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> list[TextRedactor]:
    captured: list[TextRedactor] = []
    original = worker_main._build_review_registry

    def _capturing(
        enablement: StepEnablement,
        timeouts: StepTimeouts,
        diff_fetcher: GitHubDiffFetcher,
        tree_fetcher: GitHubTreeFetcher,
        graph_query: PostgresGraphQuery,
        similarity_query: PostgresSimilarityQuery,
        embedding_client: LocalONNXEmbeddingClient,
        redactor: TextRedactor,
        llm_client: LLMClient,
        publisher: GitHubFindingsPublisher,
        settings: WorkerSettings,
        memory_matcher: PostgresMemoryRepository,
        model_resolver: FallbackModelRoutingResolver | None,
    ) -> StepRegistry:
        captured.append(redactor)
        return original(
            enablement,
            timeouts,
            diff_fetcher,
            tree_fetcher,
            graph_query,
            similarity_query,
            embedding_client,
            redactor,
            llm_client,
            publisher,
            settings,
            memory_matcher,
            model_resolver,
        )

    monkeypatch.setattr(worker_main, "_build_review_registry", _capturing)
    return captured


def test_secret_redaction_disabled_wires_noop_redactor_and_logs_warning_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_SECRET_REDACTION_ENABLED", "false")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)
    captured_redactors = _capture_redactor(monkeypatch)

    main()

    assert len(captured_redactors) == 1
    assert isinstance(captured_redactors[0], NoOpTextRedactor)

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = [p for p in payloads if p["message"] == "secret redaction is disabled"]
    assert len(matching) == 1
    assert matching[0]["level"] == "WARNING"


def test_secret_redaction_default_wires_real_gitleaks_backed_redactor_no_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)
    captured_redactors = _capture_redactor(monkeypatch)

    main()

    assert len(captured_redactors) == 1
    assert isinstance(captured_redactors[0], GitleaksTextRedactor)

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert not [p for p in payloads if "secret redaction is disabled" in p["message"]]


def test_gitleaks_scanner_disabled_does_not_disable_redaction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves the exact gap this plan's own review caught and corrected:
    disabling the Gitleaks *scanner* sub-step (an ordinary US-F4 noise-
    reduction toggle) must never silently disable redaction of a secret the
    LLM (Phase 6) echoes back verbatim into its own finding prose.
    ``secret_redaction_enabled`` is left at its default (``True``).
    """
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_STATIC_ANALYSIS_GITLEAKS_ENABLED", "false")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)
    captured_redactors = _capture_redactor(monkeypatch)

    main()

    assert len(captured_redactors) == 1
    assert isinstance(captured_redactors[0], GitleaksTextRedactor)

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert not [p for p in payloads if "secret redaction is disabled" in p["message"]]


# --- Context Builder delivery plan's Phase 5: composition-root wiring ------
#
# Exercises `_build_review_registry` directly (not through `main()`) with
# fakes at every adapter Protocol boundary — a real `StepRegistry`, real
# `ContextBuilderStep`/`LLMReviewStep`/etc. wiring, but zero real I/O
# (network, subprocess, database) — proving the complete, four-combination
# `depends_on` rule this phase's own plan section calls out, including the
# specific combination (`context_builder_enabled=True`,
# `static_analysis_can_produce_findings=False`) round two found unhandled:
# the job completes successfully, not a `RuntimeError`/`PipelineFailedError`
# from an unsatisfiable dependency.
#
# `GitleaksAnalyzer`/`SemgrepAnalyzer` are monkeypatched at the
# `worker_main` module level (`_build_review_registry` resolves them via
# that module's own global namespace at call time) so a combination with
# `static_analysis_can_produce_findings=True` never shells out to the real
# vendored binaries — this test is about composition-root wiring, not
# analyzer behavior (already covered elsewhere).


class _EmptyDiffFetcher:
    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        return Diff(files=())

    async def fetch_content(
        self, *, installation_id: int, repo_full_name: str, diff: Diff, paths: list[str]
    ) -> dict[str, bytes]:
        return {}


class _EmptyTreeFetcher:
    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        return ()

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: list[RepoTreeEntry],
    ) -> dict[str, bytes]:
        return {}


class _UnusedGraphQuery:
    """Never actually called in this test's fixture (an empty diff resolves
    no changed symbols), but still needs to structurally satisfy
    `review.context_builder.GraphQuery` for `ContextBuilderStep`'s own
    constructor."""

    async def find_callers(
        self, *, repo_id: str, symbol_names: list[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        raise AssertionError("find_callers should never be called for an empty diff")


class _UnusedSimilarityQuery:
    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        raise AssertionError("find_similar should never be called for an empty diff")


class _UnusedEmbeddingClient:
    def embed(self, texts: list[str]) -> tuple[EmbeddingVector, ...]:
        raise AssertionError("embed should never be called for an empty diff")


class _UnusedMemoryMatcher:
    """Never actually called in this test's fixture (an empty diff produces
    zero findings, so ``verification``'s own memory-suppression pre-check
    never runs — its own early-return for zero candidate findings, per
    ``review/verification.py``'s ``VerificationStep.run``, skips it
    entirely) — same shape as ``_UnusedGraphQuery``/``_UnusedSimilarityQuery``/
    ``_UnusedEmbeddingClient`` above."""

    async def best_effective_similarity(
        self, *, repo_id: str, embedding: tuple[float, ...], half_life_days: float, now: datetime
    ) -> float | None:
        raise AssertionError("best_effective_similarity should never be called for an empty diff")


class _NoOpGitleaksAnalyzer:
    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[object]:
        return []


class _NoOpSemgrepAnalyzer:
    async def run(self, diff: Diff, content: dict[str, bytes], *, timeout: float) -> list[object]:
        return []


class _EmptyFindingsLLMClient:
    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        return CompletionResponse(content='{"findings": []}', model=request.model)


class _NoOpPublisher:
    async def post_review(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        findings: list[object],
    ) -> None:
        raise AssertionError("post_review should never be called for zero findings")

    async def post_check_run(
        self, *, installation_id: int, repo_full_name: str, head_sha: str, finding_count: int
    ) -> None:
        return None

    async def post_failure_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        failed_step_name: str,
        error: str,
    ) -> None:
        raise AssertionError("post_failure_check_run should never be called on the happy path")


def _wiring_test_settings(monkeypatch: pytest.MonkeyPatch) -> WorkerSettings:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    return WorkerSettings(_env_file=None)


async def _run_review_registry(registry: StepRegistry) -> dict[str, object]:
    seed_input = {
        "org_id": "org-1",
        "repo_id": "repo-1",
        "installation_id": 1,
        "repo_full_name": "acme/widgets",
        "pr_number": 7,
        "head_sha": "deadbeef",
    }
    outcomes = await registry.run(seed_input=seed_input, context=StepContext(job_id="job-1"))
    return {name: outcome.status for name, outcome in outcomes.items()}


@pytest.mark.parametrize(
    ("context_builder_enabled", "sa_can_produce_findings", "expected_context_builder_depends_on"),
    [
        (True, True, "static_analysis"),
        (True, False, "prefilter"),
        (False, True, "static_analysis"),
        (False, False, "prefilter"),
    ],
)
@pytest.mark.asyncio
async def test_all_four_context_builder_enablement_combinations_complete_successfully(
    monkeypatch: pytest.MonkeyPatch,
    context_builder_enabled: bool,
    sa_can_produce_findings: bool,
    expected_context_builder_depends_on: str,
) -> None:
    monkeypatch.setattr(worker_main, "GitleaksAnalyzer", _NoOpGitleaksAnalyzer)
    monkeypatch.setattr(worker_main, "SemgrepAnalyzer", _NoOpSemgrepAnalyzer)
    settings = _wiring_test_settings(monkeypatch)

    enablement = StepEnablement(
        context_builder_enabled=context_builder_enabled,
        static_analysis_gitleaks_enabled=sa_can_produce_findings,
        static_analysis_semgrep_enabled=sa_can_produce_findings,
    )
    timeouts = StepTimeouts()

    registry = worker_main._build_review_registry(
        enablement,
        timeouts,
        cast("GitHubDiffFetcher", _EmptyDiffFetcher()),
        cast("GitHubTreeFetcher", _EmptyTreeFetcher()),
        cast("PostgresGraphQuery", _UnusedGraphQuery()),
        cast("PostgresSimilarityQuery", _UnusedSimilarityQuery()),
        cast("LocalONNXEmbeddingClient", _UnusedEmbeddingClient()),
        NoOpTextRedactor(),
        _EmptyFindingsLLMClient(),
        cast("GitHubFindingsPublisher", _NoOpPublisher()),
        settings,
        cast("PostgresMemoryRepository", _UnusedMemoryMatcher()),
        None,
    )

    context_builder_step = registry._by_name["context_builder"]
    intent_summary_step = registry._by_name["intent_summary"]
    review_generation_step = registry._by_name["review_generation"]
    verification_step = registry._by_name["verification"]
    assert context_builder_step.depends_on == (expected_context_builder_depends_on,)
    # `intent_summary_enabled`/`review_generation_enabled` are both at their
    # default (`True`) in this parametrization, so `intent_summary` is
    # always effectively enabled — it inherits exactly the same
    # `context_builder`-or-fallback rule `review_generation` used to have
    # directly, one link earlier in the chain.
    expected_intent_summary_depends_on = (
        "context_builder" if context_builder_enabled else expected_context_builder_depends_on
    )
    assert intent_summary_step.depends_on == (expected_intent_summary_depends_on,)
    # `review_generation` always depends on `intent_summary` here, since
    # `intent_summary` is always effectively enabled in this parametrization
    # regardless of `context_builder_enabled`/`sa_can_produce_findings`.
    assert review_generation_step.depends_on == ("intent_summary",)
    assert verification_step.depends_on == ("review_generation",)

    statuses = await _run_review_registry(registry)

    assert statuses["output"] == "success"
    assert statuses["intent_summary"] == "success"
    assert statuses["review_generation"] == "success"
    assert statuses["verification"] == "success"
    assert statuses["context_builder"] == ("success" if context_builder_enabled else "disabled")


@pytest.mark.asyncio
async def test_context_builder_enabled_but_review_generation_disabled_skips_context_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`review.config.context_builder_can_produce_value`'s own regression
    case: `context_builder_enabled=True`'s raw flag default must not be
    enough to actually run the step when its one real consumer,
    `review_generation`, is disabled — running it anyway would pay the full
    diff/tree/graph/embedding cost for a `ContextBundle` nothing reads.
    `intent_summary` (`review.config.intent_summary_can_produce_value`'s own
    identical case) is skipped for the same reason. `verification` still
    runs — its own enablement is not gated by `review_generation_enabled` —
    falling back all the way to `static_analysis`'s output.
    `static_analysis_gitleaks_enabled=True` keeps this a viable
    configuration (`review_generation_enabled=False` alone would otherwise
    trip `review.config.check_step_configuration_viable`'s own fail-fast
    rule)."""
    monkeypatch.setattr(worker_main, "GitleaksAnalyzer", _NoOpGitleaksAnalyzer)
    monkeypatch.setattr(worker_main, "SemgrepAnalyzer", _NoOpSemgrepAnalyzer)
    settings = _wiring_test_settings(monkeypatch)

    enablement = StepEnablement(
        context_builder_enabled=True,
        review_generation_enabled=False,
        static_analysis_gitleaks_enabled=True,
        static_analysis_semgrep_enabled=True,
    )
    timeouts = StepTimeouts()

    registry = worker_main._build_review_registry(
        enablement,
        timeouts,
        cast("GitHubDiffFetcher", _EmptyDiffFetcher()),
        cast("GitHubTreeFetcher", _EmptyTreeFetcher()),
        cast("PostgresGraphQuery", _UnusedGraphQuery()),
        cast("PostgresSimilarityQuery", _UnusedSimilarityQuery()),
        cast("LocalONNXEmbeddingClient", _UnusedEmbeddingClient()),
        NoOpTextRedactor(),
        _EmptyFindingsLLMClient(),
        cast("GitHubFindingsPublisher", _NoOpPublisher()),
        settings,
        cast("PostgresMemoryRepository", _UnusedMemoryMatcher()),
        None,
    )

    statuses = await _run_review_registry(registry)

    assert statuses["context_builder"] == "disabled"
    assert statuses["intent_summary"] == "disabled"
    assert statuses["review_generation"] == "disabled"
    assert statuses["verification"] == "success"
    assert statuses["output"] == "success"


@pytest.mark.asyncio
async def test_context_builder_disabled_produces_byte_for_byte_phase_1_review_generation_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`context_builder_enabled=False` regression case: `review_generation`'s
    own completion request carries no context-bundle section — byte-for-byte
    the same prompt Phase 1/2 always produced — even though it now depends
    on `intent_summary` rather than directly on `static_analysis` (that
    link's own `depends_on` fallback, one step earlier in the chain, is what
    changed by this phase)."""
    monkeypatch.setattr(worker_main, "GitleaksAnalyzer", _NoOpGitleaksAnalyzer)
    monkeypatch.setattr(worker_main, "SemgrepAnalyzer", _NoOpSemgrepAnalyzer)
    settings = _wiring_test_settings(monkeypatch)

    enablement = StepEnablement(context_builder_enabled=False)
    timeouts = StepTimeouts()

    captured_requests: list[CompletionRequest] = []

    class _RecordingLLMClient:
        async def complete(
            self, request: CompletionRequest, *, timeout: float
        ) -> CompletionResponse:
            captured_requests.append(request)
            return CompletionResponse(content='{"findings": []}', model=request.model)

    registry = worker_main._build_review_registry(
        enablement,
        timeouts,
        cast("GitHubDiffFetcher", _EmptyDiffFetcher()),
        cast("GitHubTreeFetcher", _EmptyTreeFetcher()),
        cast("PostgresGraphQuery", _UnusedGraphQuery()),
        cast("PostgresSimilarityQuery", _UnusedSimilarityQuery()),
        cast("LocalONNXEmbeddingClient", _UnusedEmbeddingClient()),
        NoOpTextRedactor(),
        _RecordingLLMClient(),
        cast("GitHubFindingsPublisher", _NoOpPublisher()),
        settings,
        cast("PostgresMemoryRepository", _UnusedMemoryMatcher()),
        None,
    )

    intent_summary_step = registry._by_name["intent_summary"]
    review_generation_step = registry._by_name["review_generation"]
    assert intent_summary_step.depends_on == ("static_analysis",)
    assert review_generation_step.depends_on == ("intent_summary",)

    statuses = await _run_review_registry(registry)
    assert statuses["output"] == "success"
    assert statuses["context_builder"] == "disabled"

    # `_RecordingLLMClient` is shared across `intent_summary`/
    # `review_generation`/`verification` (mirroring the real composition
    # root's own single-`llm_client` wiring) — `review_generation`'s own
    # request is the one whose user message names the static-findings
    # section only `_build_user_message` (review/review_generation.py)
    # builds, distinguishing it from `intent_summary`'s bare-diff prompt.
    review_generation_requests = [
        request
        for request in captured_requests
        if "Static-analysis findings" in request.messages[-1].content
    ]
    assert len(review_generation_requests) == 1
    user_message = review_generation_requests[0].messages[-1].content
    assert "Related code in this repository" not in user_message


# --- Phase 5 (verification pipeline plan): deprecated settings env vars ----
#
# `_check_deprecated_settings_env_vars` (Assumption 7/8 of
# docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section) — an
# operator's own, externally-owned deployment config still setting one of
# the renamed `llm_review_*` names must get a one-time `warning`, not a
# silent no-op, and the Worker must still start successfully either way.


def _deprecated_warning_payloads(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        p
        for p in payloads
        if p["message"] == "deprecated setting name is still set and no longer has any effect"
    ]


def test_deprecated_llm_review_enabled_env_var_warns_and_worker_still_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_LLM_REVIEW_ENABLED", "true")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()  # must not raise SystemExit

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = _deprecated_warning_payloads(payloads)
    assert len(matching) == 1
    assert matching[0]["deprecated_name"] == "REVIEW_WORKER_LLM_REVIEW_ENABLED"
    assert matching[0]["current_name"] == "REVIEW_WORKER_REVIEW_GENERATION_ENABLED"
    assert matching[0]["level"] == "WARNING"
    assert any(p["message"] == "worker started" for p in payloads)


def test_deprecated_timeout_env_var_set_only_via_dotenv_file_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves the `.env`-file path is actually read, not just `os.environ` —
    the deprecated name is set only in a `.env` file at the (monkeypatched)
    current working directory, never via `monkeypatch.setenv`."""
    (tmp_path / ".env").write_text("REVIEW_WORKER_LLM_REVIEW_STEP_TIMEOUT_SECONDS=45\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.delenv("REVIEW_WORKER_LLM_REVIEW_STEP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()  # must not raise SystemExit

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = _deprecated_warning_payloads(payloads)
    assert len(matching) == 1
    assert matching[0]["deprecated_name"] == "REVIEW_WORKER_LLM_REVIEW_STEP_TIMEOUT_SECONDS"
    assert matching[0]["current_name"] == "REVIEW_WORKER_REVIEW_GENERATION_STEP_TIMEOUT_SECONDS"
    assert any(p["message"] == "worker started" for p in payloads)


def test_deprecated_env_var_detected_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("review_worker_llm_review_enabled", "true")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()  # must not raise SystemExit

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = _deprecated_warning_payloads(payloads)
    assert len(matching) == 1
    assert matching[0]["deprecated_name"] == "REVIEW_WORKER_LLM_REVIEW_ENABLED"
    assert any(p["message"] == "worker started" for p in payloads)


def test_no_deprecated_env_vars_set_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert _deprecated_warning_payloads(payloads) == []


# --- Phase 5 (verification pipeline plan): verification_enabled=False ------
#
# Mirrors the existing `secret_redaction_enabled=False` test case above
# exactly, per US-V9's second scenario.


def test_verification_disabled_logs_warning_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_VERIFICATION_ENABLED", "false")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = [
        p
        for p in payloads
        if p["message"]
        == "verification is disabled: unverified LLM findings will be posted directly"
    ]
    assert len(matching) == 1
    assert matching[0]["level"] == "WARNING"


def test_verification_default_enabled_logs_no_disabled_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert not [p for p in payloads if "verification is disabled" in p["message"]]


# --- Phase 6 (memory plan): memory_suppression_enabled=False -----------------
#
# Mirrors the `verification_enabled=False` test pair immediately above,
# exactly — per US-M12's own "visible, not silent" deviation-from-default
# requirement.


def test_memory_suppression_disabled_logs_warning_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_MEMORY_SUPPRESSION_ENABLED", "false")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = [
        p
        for p in payloads
        if p["message"] == "memory suppression is disabled: no similarity-based pre-check will run "
        "before the rubric call"
    ]
    assert len(matching) == 1
    assert matching[0]["level"] == "WARNING"


def test_memory_suppression_default_enabled_logs_no_disabled_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert not [p for p in payloads if "memory suppression is disabled" in p["message"]]


# --- Phase 5 (admin GUI plan): model_routing_config_enabled=False -----------
#
# Mirrors the `memory_suppression_enabled=False` test pair immediately
# above, exactly — per docs/plans/plan-deliver-admin-gui.md's Phase 5
# section's own "visible, not silent" deviation-from-default requirement.


def test_model_routing_disabled_logs_warning_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("REVIEW_WORKER_MODEL_ROUTING_CONFIG_ENABLED", "false")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = [
        p
        for p in payloads
        if p["message"] == "model routing is disabled: every stage uses the static default "
        "model, regardless of any ModelRoutingConfig row"
    ]
    assert len(matching) == 1
    assert matching[0]["level"] == "WARNING"


def test_model_routing_default_enabled_logs_no_disabled_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert not [p for p in payloads if "model routing is disabled" in p["message"]]


# --- Phase 5 (verification pipeline plan): resolved depends_on in startup log


def test_step_configuration_valid_log_includes_resolved_depends_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    matching = [p for p in payloads if p["message"] == "step configuration valid"]
    assert len(matching) == 1
    assert matching[0]["review_pipeline_depends_on"] == {
        "context_builder": "static_analysis",
        "intent_summary": "context_builder",
        "review_generation": "intent_summary",
        "verification": "review_generation",
        "output": "verification",
    }


# --- docs/specs/spec-observability.md's Unit A: US10 (no-op default) and
# US14 (heartbeat) ---------------------------------------------------------


async def _noop_heartbeat_loop(*, interval_seconds: float = 30.0) -> None:
    """Fast-completing fake substituted for `run_heartbeat_loop` — mirrors
    `_noop_dispatch_loop_forever`'s own role above: `main()` gathers both
    loops together when telemetry is enabled, and this repo's `main()` has
    no graceful-shutdown seam of its own (a real process only stops via a
    signal, which cancels everything `asyncio.run()` is still running) —
    so a unit test needs both gathered coroutines to actually return for
    `main()` to return at all.
    """
    return None


def test_main_is_a_telemetry_noop_when_otlp_endpoint_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer_provider_before = trace.get_tracer_provider()
    meter_provider_before = metrics.get_meter_provider()
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _noop_dispatch_loop_forever)

    main()

    # US10: no SDK/exporter constructed at all — the global providers are
    # untouched, proven by identity.
    assert trace.get_tracer_provider() is tracer_provider_before
    assert metrics.get_meter_provider() is meter_provider_before


@pytest.mark.usefixtures("reset_otel_global_providers")
def test_main_starts_heartbeat_loop_when_otlp_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_calls: list[float] = []
    heartbeat_started = asyncio.Event()

    async def _recording_heartbeat_loop(*, interval_seconds: float = 30.0) -> None:
        heartbeat_calls.append(interval_seconds)
        heartbeat_started.set()

    async def _dispatch_loop_forever_after_heartbeat_starts(
        job_queue: JobQueueRepository,
        registry: StepRegistry,
        indexing_deps: IndexingJobDependencies,
        memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
        settings: WorkerSettings,
        worker_id: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        # The heartbeat task (a plain `asyncio.create_task`, per
        # `apps/worker/main.py`'s own docstring) only actually starts
        # running once this coroutine yields control back to the event
        # loop — in real operation, `run_dispatch_loop`'s own first
        # `await job_queue.claim_next(...)` (real I/O) always does that
        # well before `run_heartbeat_loop` could be starved of a turn;
        # this fake reproduces that yield deterministically (waiting for
        # the heartbeat fake's own signal) instead of racing the
        # scheduler on a fake that returns with no real suspension point
        # at all.
        await heartbeat_started.wait()
        await http_client.aclose()

    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())
    monkeypatch.setattr(
        worker_main, "_run_dispatch_loop_forever", _dispatch_loop_forever_after_heartbeat_starts
    )
    monkeypatch.setattr(heartbeat_module, "run_heartbeat_loop", _recording_heartbeat_loop)

    try:
        main()  # must not raise SystemExit, must not hang
    finally:
        tracer_provider = trace.get_tracer_provider()
        meter_provider = metrics.get_meter_provider()
        assert isinstance(tracer_provider, TracerProvider)
        assert isinstance(meter_provider, MeterProvider)
        tracer_provider.shutdown()
        meter_provider.shutdown()

    assert len(heartbeat_calls) == 1


@pytest.mark.usefixtures("reset_otel_global_providers")
@pytest.mark.asyncio
async def test_amain_propagates_dispatch_loop_exception_unwrapped_and_cancels_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat task now runs as a plain `asyncio.create_task`,
    cancelled in a `finally` block around awaiting the dispatch loop — not
    `asyncio.TaskGroup` — specifically so a failing dispatch loop's own
    exception propagates out of `_amain` completely unwrapped (no
    `ExceptionGroup`/`BaseExceptionGroup`, no unwrap helper needed), and so
    the heartbeat task is reliably cancelled rather than left running.
    """
    heartbeat_started = asyncio.Event()
    heartbeat_cancelled = asyncio.Event()

    async def _long_running_heartbeat_loop(*, interval_seconds: float = 30.0) -> None:
        heartbeat_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            heartbeat_cancelled.set()
            raise

    async def _failing_dispatch_loop_forever(
        job_queue: JobQueueRepository,
        registry: StepRegistry,
        indexing_deps: IndexingJobDependencies,
        memory_reaction_poll_deps: MemoryReactionPollJobDependencies,
        settings: WorkerSettings,
        worker_id: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        # Wait for the heartbeat task to actually start running before
        # raising — see `test_main_starts_heartbeat_loop_when_otlp_endpoint_set`'s
        # identical fake for why this determinism matters instead of
        # racing the scheduler.
        await heartbeat_started.wait()
        await http_client.aclose()
        raise RuntimeError("dispatch loop exploded")

    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.setattr(worker_main, "_run_dispatch_loop_forever", _failing_dispatch_loop_forever)
    monkeypatch.setattr(heartbeat_module, "run_heartbeat_loop", _long_running_heartbeat_loop)

    settings = WorkerSettings()
    logger = logging.getLogger(__name__)
    client = _FakeLLMClient()
    telemetry_enabled = init_telemetry(
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        service_name="review-worker-test",
        otlp_headers=None,
    )

    try:
        with pytest.raises(RuntimeError, match="dispatch loop exploded") as excinfo:
            await worker_main._amain(settings, logger, client, telemetry_enabled=telemetry_enabled)
        assert not isinstance(excinfo.value, BaseExceptionGroup)
    finally:
        tracer_provider = trace.get_tracer_provider()
        meter_provider = metrics.get_meter_provider()
        assert isinstance(tracer_provider, TracerProvider)
        assert isinstance(meter_provider, MeterProvider)
        tracer_provider.shutdown()
        meter_provider.shutdown()

    assert heartbeat_cancelled.is_set()
