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
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

import apps.worker.main as worker_main
from adapter.analyzers.gitleaks import GitleaksTextRedactor
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.posting import GitHubFindingsPublisher
from adapter.storage.queue import JobQueueRepository
from apps.worker.main import main
from apps.worker.settings import WorkerSettings
from findings.redact import NoOpTextRedactor, TextRedactor
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse
from review.config import StepEnablement, StepTimeouts
from review.pipeline import StepRegistry


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
        redactor: TextRedactor,
        llm_client: LLMClient,
        publisher: GitHubFindingsPublisher,
        settings: WorkerSettings,
    ) -> StepRegistry:
        captured.append(redactor)
        return original(
            enablement, timeouts, diff_fetcher, redactor, llm_client, publisher, settings
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
