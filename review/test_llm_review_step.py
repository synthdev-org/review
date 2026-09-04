"""Tests for review.llm_review.LLMReviewStep, per the plan's Phase 6
section.

Integration cases (per docs/rules/testing.md's "LLM gateway against a
recorded LiteLLM cassette" convention, and the PRD's Testing table, marked
``@pytest.mark.integration`` per-test — not a module-level ``pytestmark``,
since this file mixes tiers the same way ``review/test_static_analysis_step.py``
does per Phase 2's own "Marker note") exercise the real
``adapter.llm.litellm_client.LiteLLMClient`` against a local stub HTTP
server standing in for LM Studio's OpenAI-compatible endpoint — a recorded
cassette, not a mock of litellm's internals, matching
``adapter/llm/test_litellm_client.py``'s own approach. The stub server is
kept small and self-contained in this file rather than imported from
``adapter/llm/conftest.py``, the same "stays self-contained" convention
``apps/worker/test_main.py`` already established for its own equivalent
tests.

Unit cases (fakes at the ``LLMClient``/``TextRedactor`` Protocol
boundaries, no real I/O) cover diff-truncation, the redaction call-every-
LLM-finding behavior, the redaction-failure fatal path, and the redaction
finding-count ceiling — per ``testing.md``'s "new domain logic still needs
unit tests" rule.

The concrete ``TextRedactor`` implementation's own redaction-correctness
and fatal-failure-behavior tests live in Phase 5's
``adapter/analyzers/test_gitleaks.py`` — not duplicated here, since this
phase only adds a second call site against the same already-tested
implementation.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from adapter.llm.litellm_client import LiteLLMClient
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse
from review.llm_review import REDACTION_FINDING_CEILING, LLMReviewStep, _render_diff
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.step import StepContext

# --- Shared fixtures/helpers -------------------------------------------------


@pytest.fixture(autouse=True)
def _reenable_pipeline_logger() -> None:
    """See `review/test_findings_output_step.py`'s own identical fixture
    docstring (`_reenable_output_logger`): Alembic's `env.py` (triggered by
    any `migrated_engine`-using test elsewhere in the same `pytest -m
    integration` process, e.g. `review/test_review_job.py`) disables every
    already-created logger not listed in `alembic.ini`, including
    `review.pipeline` — the logger this file's own
    `test_malformed_llm_response_content_is_a_fatal_step_failure_logged_at_error`/
    `test_llm_gateway_timeout_is_a_fatal_step_failure_logged_at_error` cases
    assert `ERROR` records against. Re-enabled here, autouse, so both stay
    deterministic regardless of test collection/execution order rather than
    only passing by the accident of alphabetical file ordering.
    """
    logging.getLogger("review.pipeline").disabled = False


def _context() -> StepContext:
    return StepContext(job_id="job-1")


@dataclass
class _FakeStaticAnalysisOutput:
    """Structurally mirrors ``review.static_analysis.StaticAnalysisOutput``
    (never imported here — see ``review/llm_review.py``'s own "Import
    boundary" note) closely enough for ``_LLMReviewInput.model_validate(...,
    from_attributes=True)`` to read it."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"


class _FakeStaticAnalysisStep:
    """Stands in for ``review.static_analysis.StaticAnalysisStep`` so
    ``LLMReviewStep`` (which declares ``depends_on = ("static_analysis",)``)
    can be registered into a real, fully-wired outer ``StepRegistry`` — the
    only way to observe ``PipelineFailedError`` propagate and the
    registry's own generic per-step failure log fire, mirroring
    ``review/test_static_analysis_step.py``'s own ``_FakePrefilterStep``.
    """

    name = "static_analysis"
    depends_on: tuple[str, ...] = ()

    def __init__(self, output: _FakeStaticAnalysisOutput) -> None:
        self._output = output

    async def run(self, input: Any, context: StepContext) -> _FakeStaticAnalysisOutput:
        return self._output


def _diff_with_patch(path: str = "app.py", patch: str = "@@ -1 +1 @@\n-old\n+new\n") -> Diff:
    return Diff(files=(DiffFile(path=path, status="modified", patch=patch),))


def _findings_response_json(count: int, *, severity: str = "medium") -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "file": f"file{i}.py",
                    "line": i + 1,
                    "severity": severity,
                    "message": f"llm finding {i}",
                }
                for i in range(count)
            ]
        }
    )


@dataclass
class _FakeLLMClient:
    """A fake ``LLMClient`` (see llmgateway.client.LLMClient) that records
    every request it receives and answers with a fixed response content —
    used to keep the Unit-tier cases below pure, with no real I/O."""

    response_content: str = '{"findings": []}'
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(content=self.response_content, model=request.model)


class _RecordingTextRedactor:
    """A fake ``TextRedactor`` recording every call, isolating
    ``LLMReviewStep``'s own "call every LLM finding" behavior from
    ``GitleaksTextRedactor``'s own redaction-correctness test
    (``adapter/analyzers/test_gitleaks.py``) — per the plan's Phase 5 Tests
    section, reused here for this step's own second call site.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def redact(self, text: str) -> str:
        self.calls.append(text)
        return f"REDACTED[{text}]"


class _RaisingTextRedactor:
    async def redact(self, text: str) -> str:
        raise RuntimeError("redaction backend unavailable")


def _llm_review_step(
    *,
    llm_client: LLMClient,
    redactor: Any,
    max_diff_bytes: int = 200_000,
    timeout: float = 5.0,
    model: str = "local-model",
) -> LLMReviewStep:
    return LLMReviewStep(
        llm_client,
        redactor=redactor,
        model=model,
        timeout=timeout,
        max_diff_bytes=max_diff_bytes,
    )


# =============================================================================
# Integration: real LiteLLMClient against a local stub HTTP server (recorded
# cassette), per testing.md's middle tier and the PRD's own Testing table.
# =============================================================================

ResponseFactory = Callable[[], tuple[int, bytes]]


def _make_handler(respond: ResponseFactory) -> type[BaseHTTPRequestHandler]:
    class _StubHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            status, body = respond()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep test output clean; failures surface via assertions

    return _StubHandler


@pytest.fixture
def stub_llm_server() -> Iterator[Callable[[ResponseFactory], str]]:
    """Yields a ``start(respond)`` function returning the base URL a
    ``LiteLLMClient`` can be pointed at — see this file's own docstring for
    why this is duplicated here rather than imported from
    ``adapter/llm/conftest.py``."""
    servers: list[HTTPServer] = []

    def start(respond: ResponseFactory) -> str:
        server = HTTPServer(("127.0.0.1", 0), _make_handler(respond))
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def _canned_completion_json(content: str) -> bytes:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "local-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    return json.dumps(payload).encode()


def _unused_port_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us5_one_completion_call_made_with_diff_and_static_findings_as_context(
    stub_llm_server: Callable[[ResponseFactory], str],
) -> None:
    """US5: Given the static-analysis step's output for a claimed job, When
    the llm-review step runs, Then it makes exactly one LLM Gateway
    completion call supplying the bare diff plus the static-analysis
    findings as context, and parses the response against the fixed
    structured findings schema (file, line, severity, message)."""
    static_finding = Finding(
        file="app.py", line=1, severity="high", message="hardcoded secret", source="gitleaks"
    )
    call_count = 0

    def respond() -> tuple[int, bytes]:
        nonlocal call_count
        call_count += 1
        return 200, _canned_completion_json(_findings_response_json(1))

    endpoint = stub_llm_server(respond)
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _llm_review_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(
        diff=_diff_with_patch(patch="@@ -1 +1 @@\n-foo\n+bar\n"), findings=[static_finding]
    )

    output = await step.run(static_output, _context())

    assert call_count == 1
    # the combined output carries the forwarded static finding plus the new
    # LLM-sourced one:
    assert static_finding in output.findings
    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == 1
    assert llm_findings[0].file == "file0.py"
    assert llm_findings[0].severity == "medium"
    assert llm_findings[0].rule_id is None
    assert output.org_id == "org-1"
    assert output.pr_number == 42


@pytest.mark.asyncio
@pytest.mark.integration
async def test_malformed_llm_response_content_is_a_fatal_step_failure_logged_at_error(
    stub_llm_server: Callable[[ResponseFactory], str], caplog: pytest.LogCaptureFixture
) -> None:
    """Negative case (1): a malformed/unparseable LLM response asserts a
    typed failure, not an unhandled exception, and that this failure logs
    at ``error`` per Phase 2's macro-step-fatal-failure default — a
    job-failing case, not an isolated sub-step case."""
    endpoint = stub_llm_server(lambda: (200, _canned_completion_json("not valid json at all {{{")))
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _llm_review_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "llm_review": True},
        timeouts={"static_analysis": 5.0, "llm_review": 5.0},
        on_failure=FailurePolicy.FATAL,
    )

    with (
        caplog.at_level(logging.ERROR, logger="review.pipeline"),
        pytest.raises(PipelineFailedError),
    ):
        await registry.run(seed_input=None, context=_context())

    error_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and getattr(r, "step_name", None) == "llm_review"
    ]
    assert len(error_records) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_llm_gateway_timeout_is_a_fatal_step_failure_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Negative case (2), the PRD's own US8 scenario: an LLM Gateway
    timeout asserts the same typed failure path via a cassette recorded
    with a timeout/connection-error response, and — matching case (1) —
    that this also logs at ``error`` per the same Phase 2 default."""
    llm_client = LiteLLMClient(endpoint=_unused_port_endpoint(), api_key="not-needed")
    step = _llm_review_step(llm_client=llm_client, redactor=_RecordingTextRedactor(), timeout=2.0)
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "llm_review": True},
        timeouts={"static_analysis": 5.0, "llm_review": 5.0},
        on_failure=FailurePolicy.FATAL,
    )

    with (
        caplog.at_level(logging.ERROR, logger="review.pipeline"),
        pytest.raises(PipelineFailedError),
    ):
        await registry.run(seed_input=None, context=_context())

    error_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and getattr(r, "step_name", None) == "llm_review"
    ]
    assert len(error_records) == 1


# =============================================================================
# Unit: fakes at the LLMClient/TextRedactor Protocol boundaries, no real I/O.
# =============================================================================

# --- Diff-truncation boundary cases -----------------------------------------


def _diff_of_patch_byte_length(length: int) -> Diff:
    """A single-file ``Diff`` whose patch text is ``length`` repeated ``x``
    bytes — the actual rendered prompt byte count (patch text plus
    ``_render_diff``'s own per-file header) is computed per-test via
    ``_render_diff`` directly, rather than assumed, so these boundary tests
    assert against the real rendered byte count rather than the raw patch
    length."""
    return Diff(files=(DiffFile(path="app.py", status="modified", patch="x" * length),))


@pytest.mark.asyncio
async def test_diff_exactly_at_ceiling_is_not_truncated() -> None:
    diff = _diff_of_patch_byte_length(1_000)
    rendered_bytes = len(_render_diff(diff).encode("utf-8"))
    llm_client = _FakeLLMClient()
    step = _llm_review_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), max_diff_bytes=rendered_bytes
    )

    await step.run(_FakeStaticAnalysisOutput(diff=diff), _context())

    sent_user_message = llm_client.calls[0].messages[-1].content
    assert "x" * 1_000 in sent_user_message


@pytest.mark.asyncio
async def test_diff_one_byte_over_ceiling_is_truncated_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = _diff_of_patch_byte_length(1_000)
    rendered_bytes = len(_render_diff(diff).encode("utf-8"))
    llm_client = _FakeLLMClient()
    step = _llm_review_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), max_diff_bytes=rendered_bytes - 1
    )

    with caplog.at_level(logging.WARNING, logger="review.llm_review"):
        await step.run(_FakeStaticAnalysisOutput(diff=diff), _context())

    sent_user_message = llm_client.calls[0].messages[-1].content
    assert "x" * 1_000 not in sent_user_message  # truncated away
    warnings = [r for r in caplog.records if "diff truncated" in r.message]
    assert len(warnings) == 1
    assert warnings[0].original_bytes == rendered_bytes  # type: ignore[attr-defined]
    assert warnings[0].truncated_bytes == rendered_bytes - 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_diff_comfortably_under_ceiling_is_not_truncated_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = _diff_of_patch_byte_length(10)
    llm_client = _FakeLLMClient()
    step = _llm_review_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), max_diff_bytes=1_000
    )

    with caplog.at_level(logging.WARNING, logger="review.llm_review"):
        await step.run(_FakeStaticAnalysisOutput(diff=diff), _context())

    assert not [r for r in caplog.records if "diff truncated" in r.message]


# --- Redaction: called on every LLM finding, and its own fatal-failure path -


@pytest.mark.asyncio
async def test_every_llm_finding_message_is_redacted_before_merge() -> None:
    """Proves the "call every source: llm finding's message" behavior in
    isolation from ``GitleaksTextRedactor``'s own redaction-correctness
    test (``adapter/analyzers/test_gitleaks.py``)."""
    llm_client = _FakeLLMClient(response_content=_findings_response_json(2))
    redactor = _RecordingTextRedactor()
    step = _llm_review_step(llm_client=llm_client, redactor=redactor)

    output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    assert redactor.calls == ["llm finding 0", "llm finding 1"]
    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert {f.message for f in llm_findings} == {
        "REDACTED[llm finding 0]",
        "REDACTED[llm finding 1]",
    }


@pytest.mark.asyncio
async def test_redaction_failure_produces_the_fatal_pipeline_failed_error_path() -> None:
    """Mirrors ``review/test_static_analysis_step.py``'s equivalent test for
    Phase 5's own call site into the same mechanism — this macro step's own
    redaction-failure propagation is proven directly, not left as an
    unverified extrapolation."""
    llm_client = _FakeLLMClient(response_content=_findings_response_json(1))
    step = _llm_review_step(llm_client=llm_client, redactor=_RaisingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "llm_review": True},
        timeouts={"static_analysis": 5.0, "llm_review": 5.0},
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError):
        await registry.run(seed_input=None, context=_context())


# --- Redaction finding-count ceiling boundary cases -------------------------


@pytest.mark.asyncio
async def test_redaction_ceiling_exactly_at_cap_all_findings_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_client = _FakeLLMClient(response_content=_findings_response_json(REDACTION_FINDING_CEILING))
    redactor = _RecordingTextRedactor()
    step = _llm_review_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.llm_review"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == REDACTION_FINDING_CEILING
    assert len(redactor.calls) == REDACTION_FINDING_CEILING
    assert not [r for r in caplog.records if "ceiling" in r.message]


@pytest.mark.asyncio
async def test_redaction_ceiling_one_over_drops_exactly_one_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_client = _FakeLLMClient(
        response_content=_findings_response_json(REDACTION_FINDING_CEILING + 1)
    )
    redactor = _RecordingTextRedactor()
    step = _llm_review_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.llm_review"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == REDACTION_FINDING_CEILING
    assert len(redactor.calls) == REDACTION_FINDING_CEILING
    ceiling_warnings = [r for r in caplog.records if "ceiling" in r.message]
    assert len(ceiling_warnings) == 1
    assert ceiling_warnings[0].dropped_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_redaction_ceiling_comfortably_under_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_client = _FakeLLMClient(response_content=_findings_response_json(3))
    redactor = _RecordingTextRedactor()
    step = _llm_review_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.llm_review"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == 3
    assert not [r for r in caplog.records if "ceiling" in r.message]
