"""Tests for review.review_generation.ReviewGenerationStep — renamed from
review.llm_review.LLMReviewStep per
docs/plans/plan-deliver-verification-pipeline.md's Phase 2 section (Unit B)
and docs/specs/spec-verification-pipeline.md's US-V2. Existing coverage
(originally written for the plan-deliver-context-builder.md's Phase 6
section) carries over unchanged under the new module/class names; new
US-V2 cases (the intent-summary prompt section) are added alongside it.

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

import opentelemetry.metrics._internal as metrics_internal
import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from opentelemetry.util._once import Once

from adapter.llm.litellm_client import LiteLLMClient
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse, TokenUsage
from review.context_bundle import ContextBundle, ContextBundleItem, ContextItemKind
from review.diff_prompt import render_diff
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.review_generation import (
    _MAX_CONTEXT_REF_IDS_PER_FINDING,
    REDACTION_FINDING_CEILING,
    ReviewGenerationStep,
    _build_system_prompt,
    _build_user_message,
)
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
    (never imported here — see ``review/review_generation.py``'s own "Import
    boundary" note) closely enough for ``_ReviewGenerationInput.model_validate(...,
    from_attributes=True)`` to read it."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"
    # Added by docs/plans/plan-deliver-context-builder.md's Phase 5 section
    # (US5) — defaults to `None`, matching `_ReviewGenerationInput`'s own default,
    # so every existing test in this file that doesn't set it still gets
    # byte-for-byte the same prompt as before this phase.
    context_bundle: ContextBundle | None = None
    # Added by docs/plans/plan-deliver-verification-pipeline.md's Phase 2
    # section (US-V2) — defaults to `None`, matching `_ReviewGenerationInput`'s
    # own default, so every existing test in this file that doesn't set it
    # still gets byte-for-byte the same prompt as before this phase.
    intent_summary: str | None = None


class _FakeStaticAnalysisStep:
    """Stands in for ``review.static_analysis.StaticAnalysisStep`` so
    ``ReviewGenerationStep`` (which declares ``depends_on = ("static_analysis",)``)
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
    # US3 (docs/specs/spec-observability.md): `None` by default, matching
    # every existing test in this file that never sets it and therefore
    # expects byte-for-byte the same behavior as before US3 — set to a real
    # `TokenUsage` only by this file's own US3 test section below.
    usage: TokenUsage | None = None

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(
            content=self.response_content, model=request.model, usage=self.usage
        )


class _RecordingTextRedactor:
    """A fake ``TextRedactor`` recording every call, isolating
    ``ReviewGenerationStep``'s own "call every LLM finding" behavior from
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


def _review_generation_step(
    *,
    llm_client: LLMClient,
    redactor: Any,
    max_diff_bytes: int = 200_000,
    timeout: float = 5.0,
    model: str = "local-model",
) -> ReviewGenerationStep:
    return ReviewGenerationStep(
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


def _make_handler(
    respond: ResponseFactory, *, on_request: Callable[[bytes], None] | None = None
) -> type[BaseHTTPRequestHandler]:
    class _StubHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if on_request is not None:
                length = int(self.headers.get("Content-Length", "0"))
                on_request(self.rfile.read(length))
            status, body = respond()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep test output clean; failures surface via assertions

    return _StubHandler


@pytest.fixture
def stub_llm_server() -> Iterator[Callable[..., str]]:
    """Yields a ``start(respond, on_request=None)`` function returning the
    base URL a ``LiteLLMClient`` can be pointed at — see this file's own
    docstring for why this is duplicated here rather than imported from
    ``adapter/llm/conftest.py``. ``on_request``, when given, receives the
    raw request body bytes before ``respond()`` is called — the seam
    US5's own context-bundle-reaches-the-prompt test below uses to inspect
    exactly what ``LiteLLMClient`` sent over the wire, not merely what
    ``ReviewGenerationStep`` built in memory."""
    servers: list[HTTPServer] = []

    def start(
        respond: ResponseFactory, *, on_request: Callable[[bytes], None] | None = None
    ) -> str:
        server = HTTPServer(("127.0.0.1", 0), _make_handler(respond, on_request=on_request))
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
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
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
async def test_us5_non_empty_context_bundle_reaches_the_completion_request(
    stub_llm_server: Callable[..., str],
) -> None:
    """US5/Phase 5: Given a non-empty ``context_bundle`` on the upstream
    step's output, When ``review_generation`` runs, Then the completion
    request actually sent over the wire (not merely built in memory)
    includes a clearly-labeled, distinguishable "Related code in this
    repository" section carrying the bundle's own retrieved content —
    proving the context bundle reaches ``review_generation``'s completion
    request end to end through the real ``LiteLLMClient``, not just
    ``_build_user_message``'s own unit-level behavior."""
    context_bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/caller.ts",
                symbol_name="bar",
                content="function bar() { add(1, 2); }",
                line_range=(1, 1),
            ),
        )
    )
    captured_bodies: list[bytes] = []

    endpoint = stub_llm_server(
        lambda: (200, _canned_completion_json(_findings_response_json(0))),
        on_request=captured_bodies.append,
    )
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(
        diff=_diff_with_patch(), context_bundle=context_bundle
    )

    await step.run(static_output, _context())

    assert len(captured_bodies) == 1
    sent_request = json.loads(captured_bodies[0])
    user_message = sent_request["messages"][-1]["content"]
    assert "Related code in this repository" in user_message
    assert "src/caller.ts" in user_message
    assert "function bar() { add(1, 2); }" in user_message


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_v2_intent_summary_reaches_the_completion_request(
    stub_llm_server: Callable[..., str],
) -> None:
    """US-V2 (first scenario): Given a non-``None`` ``intent_summary`` on the
    upstream step's output, When ``review_generation`` runs, Then the
    completion request actually sent over the wire includes a
    clearly-labeled "What this PR is trying to do" section carrying that
    text, inserted before the static-findings/diff/context-bundle
    sections."""
    captured_bodies: list[bytes] = []

    endpoint = stub_llm_server(
        lambda: (200, _canned_completion_json(_findings_response_json(0))),
        on_request=captured_bodies.append,
    )
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(
        diff=_diff_with_patch(), intent_summary="Refactors the widget loader for clarity."
    )

    await step.run(static_output, _context())

    assert len(captured_bodies) == 1
    sent_request = json.loads(captured_bodies[0])
    user_message = sent_request["messages"][-1]["content"]
    assert "What this PR is trying to do" in user_message
    assert "Refactors the widget loader for clarity." in user_message
    assert user_message.index("What this PR is trying to do") < user_message.index(
        "Static-analysis findings"
    )


def test_build_user_message_separates_intent_summary_from_static_findings_section() -> None:
    """A non-``None`` ``intent_summary`` must not run directly into the
    following "Static-analysis findings" section with no whitespace between
    them — ``_render_intent_summary_section``'s own returned text has to
    end with a separator, mirroring every other section in this prompt
    (``render_context_bundle_section``'s own leading ``\\n\\n``)."""
    user_message = _build_user_message("diff text", [], None, "Fixes the null pointer bug.")

    assert "bug.Static-analysis findings" not in user_message
    assert "bug.\n\nStatic-analysis findings" in user_message


@pytest.mark.asyncio
async def test_us_v2_intent_summary_absent_produces_byte_identical_prompt() -> None:
    """US-V2 (second scenario): Given ``intent_summary=None`` (the default —
    e.g. ``intent_summary_enabled=False``, or any upstream shape that
    predates this phase), When ``review_generation`` runs, Then its prompt
    is byte-for-byte identical to a prompt built with no intent-summary
    section at all — the section contributes exactly the empty string."""
    llm_client = _FakeLLMClient()
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())

    await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    sent_user_message = llm_client.calls[0].messages[-1].content
    assert "What this PR is trying to do" not in sent_user_message
    assert sent_user_message == _build_user_message(render_diff(_diff_with_patch()), [], None, None)

    sent_system_message = llm_client.calls[0].messages[0].content
    assert "What this PR is trying to do" not in sent_system_message
    assert sent_system_message == _build_system_prompt(None)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us6_context_ref_ids_round_trips_json_list_to_finding_tuple(
    stub_llm_server: Callable[..., str],
) -> None:
    """Phase 6's own NFR (deferred by Phase 5, per the plan's Traceability
    table): the LLM's own JSON `context_ref_ids: list[int]` round-trips to
    `findings.models.Finding.context_ref_ids: tuple[int, ...]` — a plain
    JSON list on the wire becomes the frozen, hashable tuple `Finding`
    itself requires — proven against the real `LiteLLMClient`/HTTP round
    trip, not just `_parse_llm_findings`'s own in-memory behavior."""
    findings_response = json.dumps(
        {
            "findings": [
                {
                    "file": "app.py",
                    "line": 1,
                    "severity": "medium",
                    "message": "calls a heavily-used helper",
                    "context_ref_ids": [0, 1],
                }
            ]
        }
    )
    endpoint = stub_llm_server(lambda: (200, _canned_completion_json(findings_response)))
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())

    output = await step.run(static_output, _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == 1
    assert llm_findings[0].context_ref_ids == (0, 1)
    assert isinstance(llm_findings[0].context_ref_ids, tuple)


@pytest.mark.asyncio
async def test_context_ref_ids_absent_from_llm_response_defaults_to_none() -> None:
    """The ordinary case — an LLM response with no `context_ref_ids` field
    at all — leaves `Finding.context_ref_ids` at its own default (`None`),
    never an empty tuple, so `review.citation.render_citation` is never
    even called for a finding that drew on no context."""
    llm_client = _FakeLLMClient(response_content=_findings_response_json(1))
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())

    output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == 1
    assert llm_findings[0].context_ref_ids is None


@pytest.mark.asyncio
async def test_one_malformed_finding_is_dropped_not_the_whole_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single finding whose `context_ref_ids` exceeds
    `_MAX_CONTEXT_REF_IDS_PER_FINDING` must not fail the whole LLM
    response — it's dropped and logged at `warning`, while every other,
    well-formed finding in the same response still survives. Under the
    pre-fix code (a single `_LLMFindingsResponse.model_validate` call over
    the whole `findings` list) this exact response would have raised
    `LLMResponseParseError` for the entire batch instead, escalating to a
    fatal `PipelineFailedError` under `FailurePolicy.FATAL` and discarding
    the other 2 well-formed findings along with it."""
    response = json.dumps(
        {
            "findings": [
                {"file": "a.py", "line": 1, "severity": "medium", "message": "finding a"},
                {
                    "file": "b.py",
                    "line": 2,
                    "severity": "high",
                    "message": "finding b",
                    # One over the cap — invalid on its own, must not sink
                    # the other two findings in this same response.
                    "context_ref_ids": list(range(_MAX_CONTEXT_REF_IDS_PER_FINDING + 1)),
                },
                {"file": "c.py", "line": 3, "severity": "low", "message": "finding c"},
            ]
        }
    )
    llm_client = _FakeLLMClient(response_content=response)
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    # `_RecordingTextRedactor` wraps every surviving finding's own message
    # as `REDACTED[...]` (see its own docstring) — this test's own subject
    # is which *findings* survive, not redaction, so it asserts against
    # that wrapped form rather than the raw LLM-authored text.
    assert {f.message for f in llm_findings} == {"REDACTED[finding a]", "REDACTED[finding c]"}
    dropped_warnings = [r for r in caplog.records if "dropping one malformed finding" in r.message]
    assert len(dropped_warnings) == 1
    assert dropped_warnings[0].finding_index == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_every_finding_failing_validation_logs_error_not_just_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When *every* item in a non-empty `findings` list fails per-item
    validation, each drop still logs its own `warning` (unchanged), but
    this must additionally log once at `error` — otherwise a broken LLM
    integration or schema regression that fails every finding is
    indistinguishable, from the step's own successful, empty-findings
    return value, from a genuinely clean diff with nothing to report."""
    response = json.dumps(
        {
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": "medium",
                    "message": "finding a",
                    "context_ref_ids": list(range(_MAX_CONTEXT_REF_IDS_PER_FINDING + 1)),
                },
                {
                    "file": "b.py",
                    "line": 2,
                    "severity": "high",
                    "message": "finding b",
                    "context_ref_ids": list(range(_MAX_CONTEXT_REF_IDS_PER_FINDING + 1)),
                },
            ]
        }
    )
    llm_client = _FakeLLMClient(response_content=response)
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    assert [f for f in output.findings if f.source == "llm"] == []
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    assert "every finding" in error_records[0].message
    assert error_records[0].total_items == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_findings_key_missing_from_an_otherwise_valid_json_object_is_a_parse_error() -> None:
    """A distinct malformed-*response* shape from
    `test_malformed_llm_response_content_is_a_fatal_step_failure_logged_at_error`
    below (which covers non-JSON content): valid JSON, but missing the
    top-level `findings` list entirely — still a whole-response parse
    failure, never silently treated as zero findings, since the two-stage
    (JSON-shape-check, then per-item) rewrite of `_parse_llm_findings` must
    keep rejecting a response with no `findings` list to work on at all."""
    llm_client = _FakeLLMClient(response_content=json.dumps({"not_findings": []}))
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "review_generation": True},
        timeouts={"static_analysis": 5.0, "review_generation": 5.0},
        on_failure=FailurePolicy.FATAL,
    )

    with pytest.raises(PipelineFailedError):
        await registry.run(seed_input=None, context=_context())


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
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "review_generation": True},
        timeouts={"static_analysis": 5.0, "review_generation": 5.0},
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
        if r.levelname == "ERROR" and getattr(r, "step_name", None) == "review_generation"
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
    step = _review_generation_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), timeout=2.0
    )
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "review_generation": True},
        timeouts={"static_analysis": 5.0, "review_generation": 5.0},
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
        if r.levelname == "ERROR" and getattr(r, "step_name", None) == "review_generation"
    ]
    assert len(error_records) == 1


# =============================================================================
# Unit: fakes at the LLMClient/TextRedactor Protocol boundaries, no real I/O.
# =============================================================================

# --- Diff-truncation boundary cases -----------------------------------------


def _diff_of_patch_byte_length(length: int) -> Diff:
    """A single-file ``Diff`` whose patch text is ``length`` repeated ``x``
    bytes — the actual rendered prompt byte count (patch text plus
    ``render_diff``'s own per-file header) is computed per-test via
    ``render_diff`` directly, rather than assumed, so these boundary tests
    assert against the real rendered byte count rather than the raw patch
    length."""
    return Diff(files=(DiffFile(path="app.py", status="modified", patch="x" * length),))


@pytest.mark.asyncio
async def test_diff_exactly_at_ceiling_is_not_truncated() -> None:
    diff = _diff_of_patch_byte_length(1_000)
    rendered_bytes = len(render_diff(diff).encode("utf-8"))
    llm_client = _FakeLLMClient()
    step = _review_generation_step(
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
    rendered_bytes = len(render_diff(diff).encode("utf-8"))
    llm_client = _FakeLLMClient()
    step = _review_generation_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), max_diff_bytes=rendered_bytes - 1
    )

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
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
    step = _review_generation_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), max_diff_bytes=1_000
    )

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
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
    step = _review_generation_step(llm_client=llm_client, redactor=redactor)

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
    step = _review_generation_step(llm_client=llm_client, redactor=_RaisingTextRedactor())
    static_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch())
    registry = StepRegistry(
        [_FakeStaticAnalysisStep(static_output), step],
        enabled={"static_analysis": True, "review_generation": True},
        timeouts={"static_analysis": 5.0, "review_generation": 5.0},
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
    step = _review_generation_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
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
    step = _review_generation_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == REDACTION_FINDING_CEILING
    assert len(redactor.calls) == REDACTION_FINDING_CEILING
    ceiling_warnings = [r for r in caplog.records if "ceiling" in r.message]
    assert len(ceiling_warnings) == 1
    assert ceiling_warnings[0].dropped_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_redaction_ceiling_keeps_critical_finding_placed_after_the_ceiling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A completion listing a critical finding after
    ``REDACTION_FINDING_CEILING`` low-severity ones must not have that
    critical finding silently dropped — the ceiling sorts by severity first
    (see ``review.review_generation``'s own module docstring), mirroring
    ``findings.merge.merge_findings``'s own severity-aware ceiling (US-V8)
    one stage downstream.
    """
    findings = [
        {
            "file": f"file{i}.py",
            "line": i + 1,
            "severity": "low",
            "message": f"low finding {i}",
        }
        for i in range(REDACTION_FINDING_CEILING)
    ]
    findings.append(
        {
            "file": "critical.py",
            "line": 1,
            "severity": "critical",
            "message": "critical finding placed last in the response",
        }
    )
    llm_client = _FakeLLMClient(response_content=json.dumps({"findings": findings}))
    redactor = _RecordingTextRedactor()
    step = _review_generation_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == REDACTION_FINDING_CEILING
    assert any(f.severity == "critical" for f in llm_findings)
    assert sum(1 for f in llm_findings if f.severity == "low") == REDACTION_FINDING_CEILING - 1


@pytest.mark.asyncio
async def test_redaction_ceiling_comfortably_under_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_client = _FakeLLMClient(response_content=_findings_response_json(3))
    redactor = _RecordingTextRedactor()
    step = _review_generation_step(llm_client=llm_client, redactor=redactor)

    with caplog.at_level(logging.WARNING, logger="review.review_generation"):
        output = await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    llm_findings = [f for f in output.findings if f.source == "llm"]
    assert len(llm_findings) == 3
    assert not [r for r in caplog.records if "ceiling" in r.message]


# =============================================================================
# US3 — LLM token usage (docs/specs/spec-observability.md, Unit B).
# =============================================================================
#
# Uses OTel's own in-memory metric reader (no I/O, per testing.md's Unit
# tier), registered as the *global* MeterProvider so
# review.llm_telemetry's module-level `_llm_tokens_counter` (created once,
# at import time, against whatever provider was global then) picks this
# test's provider up via OTel's proxy-instrument migration mechanism — see
# adapter/telemetry/test_heartbeat.py's own identical fixture docstring for
# the full mechanism explanation. Defined locally in this file (not added to
# review/conftest.py) to avoid touching shared test infra other Unit B
# slices (review/pipeline.py, findings/merge.py, etc.) may be adding to
# concurrently.


@pytest.fixture
def _reset_otel_global_meter_provider() -> Iterator[None]:
    previous_provider = metrics_internal._METER_PROVIDER
    previous_once = metrics_internal._METER_PROVIDER_SET_ONCE
    metrics_internal._METER_PROVIDER = None
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()
    try:
        yield
    finally:
        metrics_internal._METER_PROVIDER = previous_provider
        metrics_internal._METER_PROVIDER_SET_ONCE = previous_once


@pytest.fixture
def in_memory_metric_reader(
    _reset_otel_global_meter_provider: None,
) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _llm_token_data_points(reader: InMemoryMetricReader) -> list[tuple[float, dict[str, object]]]:
    metrics_data = reader.get_metrics_data()
    # `get_metrics_data()` returns `None` (not an empty-but-present
    # structure) when the provider has recorded no data points at all yet —
    # the "no usage on the response" case below hits this, not just "found
    # the metric with zero data points".
    if metrics_data is None:
        return []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "review.llm.tokens_total":
                    continue
                assert isinstance(metric.data, Sum)
                return [(dp.value, dict(dp.attributes or {})) for dp in metric.data.data_points]
    return []


@pytest.mark.asyncio
async def test_us3_completion_with_usage_records_three_token_counters(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """US3: Given a completion call returns a response with `usage` set,
    When `review_generation` runs, Then `review.llm.tokens_total` records
    three `.add()` calls (prompt/completion/total) tagged by this step's
    fixed stage name and the response's own model."""
    llm_client = _FakeLLMClient(
        response_content=_findings_response_json(0),
        usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )
    step = _review_generation_step(
        llm_client=llm_client, redactor=_RecordingTextRedactor(), model="local-model"
    )

    await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    data_points = {
        attrs["token_type"]: value
        for value, attrs in _llm_token_data_points(in_memory_metric_reader)
    }
    assert data_points == {"prompt": 11.0, "completion": 7.0, "total": 18.0}

    for _, attributes in _llm_token_data_points(in_memory_metric_reader):
        assert attributes["stage"] == "review-generation"
        assert attributes["model"] == "local-model"
        # Privacy/Compliance NFR: the attribute set is a fixed, reviewed
        # allowlist — never a finding's/diff's free text.
        assert set(attributes) == {"stage", "model", "token_type"}


@pytest.mark.asyncio
async def test_us3_completion_without_usage_records_no_token_metrics(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """When the LLM Gateway response carries no `usage` (provider didn't
    report it), no `review.llm.tokens_total` data point is recorded at
    all — the catalog only requires recording "a response with usage
    set"."""
    llm_client = _FakeLLMClient(response_content=_findings_response_json(0))
    step = _review_generation_step(llm_client=llm_client, redactor=_RecordingTextRedactor())

    await step.run(_FakeStaticAnalysisOutput(diff=_diff_with_patch()), _context())

    assert _llm_token_data_points(in_memory_metric_reader) == []
