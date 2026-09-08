"""Tests for review.verification.VerificationStep — per
docs/plans/plan-deliver-verification-pipeline.md's Phase 3 section (Unit C)
and docs/specs/spec-verification-pipeline.md's US-V3 through US-V6.

Integration cases (per docs/rules/testing.md's "LLM gateway against a
recorded LiteLLM cassette" convention, marked ``@pytest.mark.integration``
per-test, mirroring ``review/test_review_generation_step.py``'s own mixed-tier
approach) exercise the real ``adapter.llm.litellm_client.LiteLLMClient``
against a local stub HTTP server standing in for LM Studio's
OpenAI-compatible endpoint. The stub server is kept small and
self-contained in this file, mirroring
``review/test_review_generation_step.py``'s own identical convention.

Unit cases (a fake ``LLMClient`` Protocol implementation, no real I/O) cover
the same US-V3/US-V4/US-V5/US-V6 behavior more directly where a cassette
adds no extra coverage.

**Note on ``verification_status``:** ``findings.models.Finding`` does not
yet declare this field (Phase 4 of the plan above adds it) — pydantic's
``model_copy(update=...)`` still sets it as a dynamic instance attribute
even though it is not (yet) part of the model's own schema, so assertions
against ``finding.verification_status`` below already pass; they will
continue to pass unchanged once Phase 4 lands the real field.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Literal

import pytest

from adapter.llm.litellm_client import LiteLLMClient
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse
from review.context_bundle import ContextBundle, ContextBundleItem, ContextItemKind
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.step import StepContext
from review.verification import VerificationResponseParseError, VerificationStep

# --- Shared fixtures/helpers -------------------------------------------------


@pytest.fixture(autouse=True)
def _reenable_pipeline_logger() -> None:
    """See ``review/test_review_generation_step.py``'s own identical fixture
    docstring — Alembic's ``env.py`` (triggered by any migrated-engine-using
    test elsewhere in the same ``pytest -m integration`` process) disables
    every already-created logger not listed in ``alembic.ini``, including
    ``review.pipeline`` — the logger this file's own fatal-failure case
    asserts an ``ERROR`` record against."""
    logging.getLogger("review.pipeline").disabled = False


def _context() -> StepContext:
    return StepContext(job_id="job-1")


def _diff_with_patch(path: str = "app.py", patch: str = "@@ -1 +1 @@\n-old\n+new\n") -> Diff:
    return Diff(files=(DiffFile(path=path, status="modified", patch=patch),))


@dataclass
class _FakeReviewGenerationOutput:
    """Structurally mirrors ``review.review_generation.ReviewGenerationOutput``
    (never imported here — see ``review/verification.py``'s own "Import
    boundary" note), extended with the ``diff`` field
    ``_VerificationInput`` also requires, closely enough for
    ``_VerificationInput.model_validate(..., from_attributes=True)`` to
    read it."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"
    context_bundle: ContextBundle | None = None


class _FakeReviewGenerationStep:
    """Stands in for ``review.review_generation.ReviewGenerationStep`` so
    ``VerificationStep`` (which declares ``depends_on = ("review_generation",)``)
    can be registered into a real, fully-wired outer ``StepRegistry`` —
    mirrors ``review/test_review_generation_step.py``'s own
    ``_FakeStaticAnalysisStep``."""

    name = "review_generation"
    depends_on: tuple[str, ...] = ()

    def __init__(self, output: _FakeReviewGenerationOutput) -> None:
        self._output = output

    async def run(self, input: Any, context: StepContext) -> _FakeReviewGenerationOutput:
        return self._output


def _llm_finding(index: int, *, context_ref_ids: tuple[int, ...] | None = None) -> Finding:
    return Finding(
        file=f"file{index}.py",
        line=index + 1,
        severity="medium",
        message=f"llm finding {index}",
        source="llm",
        context_ref_ids=context_ref_ids,
    )


def _static_finding(source: Literal["gitleaks", "semgrep"] = "gitleaks") -> Finding:
    return Finding(
        file="secrets.py", line=1, severity="high", message="hardcoded secret", source=source
    )


def _verdicts_response_json(verdicts: list[dict[str, object]]) -> str:
    return json.dumps({"verdicts": verdicts})


def _passing_verdict(index: int) -> dict[str, object]:
    return {"index": index, "valid": True, "actionable": True, "specific": True, "valuable": True}


def _failing_verdict(index: int, **overrides: bool) -> dict[str, object]:
    verdict: dict[str, object] = {
        "index": index,
        "valid": True,
        "actionable": True,
        "specific": True,
        "valuable": True,
    }
    verdict.update(overrides)
    return verdict


@dataclass
class _FakeLLMClient:
    """A fake ``LLMClient`` (see ``llmgateway.client.LLMClient``) that
    records every request it receives and answers with a fixed response
    content — keeps the Unit-tier cases below pure, with no real I/O."""

    response_content: str = '{"verdicts": []}'
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(content=self.response_content, model=request.model)


def _verification_step(
    *,
    llm_client: LLMClient,
    max_diff_bytes: int = 200_000,
    timeout: float = 5.0,
    model: str = "local-model",
) -> VerificationStep:
    return VerificationStep(llm_client, model=model, timeout=timeout, max_diff_bytes=max_diff_bytes)


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
    ``adapter/llm/conftest.py``."""
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_v3_one_batched_completion_call_judges_every_candidate_finding(
    stub_llm_server: Callable[..., str],
) -> None:
    """US-V3 (first scenario): Given review_generation's output contains
    more than one ``source="llm"`` finding, When ``verification`` runs,
    Then exactly one LLM Gateway completion call is made, supplying every
    candidate finding (each with its own explicit ``index``) plus the diff."""
    call_count = 0
    captured_bodies: list[bytes] = []

    def respond() -> tuple[int, bytes]:
        nonlocal call_count
        call_count += 1
        return 200, _canned_completion_json(
            _verdicts_response_json([_passing_verdict(0), _passing_verdict(1)])
        )

    endpoint = stub_llm_server(respond, on_request=captured_bodies.append)
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0), _llm_finding(1)]
    )

    output = await step.run(upstream_output, _context())

    assert call_count == 1
    assert len(output.findings) == 2
    sent_request = json.loads(captured_bodies[0])
    user_message = sent_request["messages"][-1]["content"]
    assert '"index": 0' in user_message
    assert '"index": 1' in user_message
    assert output.head_sha == "deadbeef"


@pytest.mark.asyncio
async def test_us_v3_zero_llm_findings_skips_the_call_entirely() -> None:
    """US-V3 (second scenario): Given review_generation's output contains
    zero ``source="llm"`` findings, When ``verification`` runs, Then no LLM
    Gateway call is made at all, and any static findings still pass
    through unchanged."""
    llm_client = _FakeLLMClient()
    step = _verification_step(llm_client=llm_client)
    static_finding = _static_finding()
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[static_finding]
    )

    output = await step.run(upstream_output, _context())

    assert llm_client.calls == []
    assert output.findings == [static_finding]


@pytest.mark.asyncio
async def test_us_v4_finding_failing_one_criterion_is_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """US-V4: Given a candidate finding fails at least one of the four
    rubric criteria, When verification completes, Then that finding is
    excluded from this step's output entirely, and the drop is logged at
    ``warning``."""
    llm_client = _FakeLLMClient(
        response_content=_verdicts_response_json(
            [_passing_verdict(0), _failing_verdict(1, actionable=False)]
        )
    )
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0), _llm_finding(1)]
    )

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        output = await step.run(upstream_output, _context())

    assert [f.file for f in output.findings] == ["file0.py"]
    summary_warnings = [r for r in caplog.records if "batch judgment completed" in r.message]
    assert len(summary_warnings) == 1
    assert summary_warnings[0].rejected_count == 1  # type: ignore[attr-defined]
    assert summary_warnings[0].actionable_failures == 1  # type: ignore[attr-defined]
    assert summary_warnings[0].valid_failures == 0  # type: ignore[attr-defined]
    assert summary_warnings[0].unmatched_count == 0  # type: ignore[attr-defined]
    # Never a rejected finding's own raw message text.
    assert "llm finding 1" not in summary_warnings[0].message


@pytest.mark.asyncio
async def test_us_v5_finding_passing_all_four_is_marked_verified_context_ref_ids_preserved() -> (
    None
):
    """US-V5: Given a candidate finding passes all four criteria, When
    verification completes, Then it is included with
    ``verification_status="verified"``, every other field (including
    ``context_ref_ids``) unchanged."""
    llm_client = _FakeLLMClient(response_content=_verdicts_response_json([_passing_verdict(0)]))
    step = _verification_step(llm_client=llm_client)
    finding = _llm_finding(0, context_ref_ids=(2, 4))
    upstream_output = _FakeReviewGenerationOutput(diff=_diff_with_patch(), findings=[finding])

    output = await step.run(upstream_output, _context())

    assert len(output.findings) == 1
    verified = output.findings[0]
    assert verified.verification_status == "verified"
    assert verified.context_ref_ids == (2, 4)
    assert verified.file == finding.file
    assert verified.message == finding.message


@pytest.mark.asyncio
async def test_us_v6_static_findings_bypass_the_batch_forwarded_unchanged() -> None:
    """US-V6: Given review_generation's output contains findings with
    ``source`` in ``{"gitleaks", "semgrep"}``, When verification runs, Then
    those findings are forwarded completely unchanged
    (``verification_status`` stays ``None``) and are never included in the
    rubric-judged batch."""
    gitleaks_finding = _static_finding(source="gitleaks")
    semgrep_finding = _static_finding(source="semgrep")
    llm_client = _FakeLLMClient(response_content=_verdicts_response_json([_passing_verdict(0)]))
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(),
        findings=[gitleaks_finding, semgrep_finding, _llm_finding(0)],
    )

    output = await step.run(upstream_output, _context())

    assert gitleaks_finding in output.findings
    assert semgrep_finding in output.findings
    assert getattr(gitleaks_finding, "verification_status", None) is None
    assert getattr(semgrep_finding, "verification_status", None) is None
    # Only the one llm finding was sent for judgment.
    sent_request = llm_client.calls[0]
    user_message = sent_request.messages[-1].content
    assert "hardcoded secret" not in user_message


@pytest.mark.asyncio
async def test_unmatched_index_finding_is_rejected_not_silently_verified() -> None:
    """Fail-closed correlation (Assumption 5), proven at the step level: a
    verdict list missing an entry for one candidate finding results in
    that finding being excluded, not silently kept."""
    llm_client = _FakeLLMClient(response_content=_verdicts_response_json([_passing_verdict(0)]))
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0), _llm_finding(1)]
    )

    output = await step.run(upstream_output, _context())

    assert [f.file for f in output.findings] == ["file0.py"]


@pytest.mark.asyncio
async def test_non_empty_context_bundle_reaches_the_verification_prompt() -> None:
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
    llm_client = _FakeLLMClient(response_content=_verdicts_response_json([_passing_verdict(0)]))
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0)], context_bundle=context_bundle
    )

    await step.run(upstream_output, _context())

    user_message = llm_client.calls[0].messages[-1].content
    assert "Related code in this repository" in user_message
    assert "src/caller.ts" in user_message


@pytest.mark.asyncio
async def test_findings_key_missing_from_an_otherwise_valid_json_object_is_a_parse_error() -> None:
    """A distinct malformed-*response* shape from the fatal-failure
    integration case below (which covers non-JSON content): valid JSON,
    but missing the top-level ``verdicts`` list entirely."""
    llm_client = _FakeLLMClient(response_content=json.dumps({"not_verdicts": []}))
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0)]
    )

    with pytest.raises(VerificationResponseParseError):
        await step.run(upstream_output, _context())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_malformed_verification_response_content_is_a_fatal_step_failure_logged_at_error(
    stub_llm_server: Callable[[ResponseFactory], str], caplog: pytest.LogCaptureFixture
) -> None:
    """Mirrors ``review/test_review_generation_step.py``'s own
    ``test_malformed_llm_response_content_is_a_fatal_step_failure_logged_at_error``:
    a malformed/unparseable verification response raises
    ``VerificationResponseParseError``, uncaught within ``run()``,
    propagating as this step's own fatal failure under
    ``FailurePolicy.FATAL`` — a job-failing case, not an isolated
    sub-step case."""
    endpoint = stub_llm_server(lambda: (200, _canned_completion_json("not valid json at all {{{")))
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _verification_step(llm_client=llm_client)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0)]
    )
    registry = StepRegistry(
        [_FakeReviewGenerationStep(upstream_output), step],
        enabled={"review_generation": True, "verification": True},
        timeouts={"review_generation": 5.0, "verification": 5.0},
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
        if r.levelname == "ERROR" and getattr(r, "step_name", None) == "verification"
    ]
    assert len(error_records) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_llm_gateway_timeout_is_a_fatal_step_failure_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors ``review/test_review_generation_step.py``'s own equivalent
    negative case: an LLM Gateway timeout also fails the job, logged at
    ``error``."""

    def _unused_port_endpoint() -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        return f"http://127.0.0.1:{port}/v1"

    llm_client = LiteLLMClient(endpoint=_unused_port_endpoint(), api_key="not-needed")
    step = _verification_step(llm_client=llm_client, timeout=2.0)
    upstream_output = _FakeReviewGenerationOutput(
        diff=_diff_with_patch(), findings=[_llm_finding(0)]
    )
    registry = StepRegistry(
        [_FakeReviewGenerationStep(upstream_output), step],
        enabled={"review_generation": True, "verification": True},
        timeouts={"review_generation": 5.0, "verification": 5.0},
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
        if r.levelname == "ERROR" and getattr(r, "step_name", None) == "verification"
    ]
    assert len(error_records) == 1


# --- Diff-truncation boundary case (shares review_generation's own logic) ---


@pytest.mark.asyncio
async def test_diff_one_byte_over_ceiling_is_truncated_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from review.diff_prompt import render_diff

    diff = Diff(files=(DiffFile(path="app.py", status="modified", patch="x" * 1_000),))
    rendered_bytes = len(render_diff(diff).encode("utf-8"))
    llm_client = _FakeLLMClient(response_content=_verdicts_response_json([_passing_verdict(0)]))
    step = _verification_step(llm_client=llm_client, max_diff_bytes=rendered_bytes - 1)
    upstream_output = _FakeReviewGenerationOutput(diff=diff, findings=[_llm_finding(0)])

    with caplog.at_level(logging.WARNING, logger="review.verification"):
        await step.run(upstream_output, _context())

    sent_user_message = llm_client.calls[0].messages[-1].content
    assert "x" * 1_000 not in sent_user_message
    warnings = [r for r in caplog.records if "diff truncated" in r.message]
    assert len(warnings) == 1
