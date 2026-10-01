"""Tests for review.intent_summary.IntentSummaryStep, per
docs/plans/plan-deliver-verification-pipeline.md's Phase 1 section and
docs/specs/spec-verification-pipeline.md's US-V1.

Integration cases (per docs/rules/testing.md's "LLM gateway against a
recorded LiteLLM cassette" convention, marked ``@pytest.mark.integration``
per-test, not a module-level ``pytestmark``, matching
``review/test_review_generation_step.py``'s own approach) exercise the real
``adapter.llm.litellm_client.LiteLLMClient`` against a local stub HTTP
server standing in for LM Studio's OpenAI-compatible endpoint — kept small
and self-contained in this file rather than imported from a shared fixture,
the same "stays self-contained" convention ``review/test_review_generation_step.py``
already established for its own equivalent stub server.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adapter.llm.litellm_client import LiteLLMClient
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse
from llmgateway.routing import (
    FallbackModelRoutingResolver,
    ModelRoutingConfigReader,
    ModelRoutingConfigRow,
)
from review.context_bundle import ContextBundle, ContextBundleItem, ContextItemKind
from review.intent_summary import MAX_INTENT_SUMMARY_CHARS, IntentSummaryStep
from review.step import StepContext

# --- Shared fixtures/helpers -------------------------------------------------


@pytest.fixture(autouse=True)
def _reenable_intent_summary_logger() -> None:
    """See `review/test_context_builder_step.py`'s identical fixture
    docstring (`_reenable_context_builder_logger`): Alembic's `env.py`
    (triggered by any earlier-running `migrated_engine`-using test in the
    same `pytest -m integration` process, per `pyproject.toml`'s
    `testpaths` ordering) disables every already-created logger not listed
    in `alembic.ini`, including `review.intent_summary` — the logger every
    ``caplog`` assertion below reads from. Re-enabled here, autouse, so
    these tests stay deterministic regardless of test collection/execution
    order."""
    logging.getLogger("review.intent_summary").disabled = False


def _context() -> StepContext:
    return StepContext(job_id="job-1")


def _diff_with_patch(path: str = "app.py", patch: str = "@@ -1 +1 @@\n-old\n+new\n") -> Diff:
    return Diff(files=(DiffFile(path=path, status="modified", patch=patch),))


@dataclass
class _FakeContextBuilderOutput:
    """Structurally mirrors ``review.context_builder.ContextBuilderOutput``
    (never imported here, per this module's own "Import boundary" note)
    closely enough for ``_IntentSummaryInput.model_validate(...,
    from_attributes=True)`` to read it — see
    ``review/test_review_generation_step.py``'s identical ``_FakeStaticAnalysisOutput``
    for the same pattern."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    context_bundle: ContextBundle | None = None
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"


@dataclass
class _FakeStaticAnalysisOutput:
    """Structurally mirrors ``review.static_analysis.StaticAnalysisOutput``
    — no ``context_bundle`` field of its own."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"


@dataclass
class _FakePrefilterOutput:
    """Structurally mirrors ``review.prefilter.PrefilterOutput`` — no
    ``findings`` or ``context_bundle`` field of its own."""

    diff: Diff
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"


@dataclass
class _FakeLLMClient:
    """A fake ``LLMClient`` recording every request it receives and
    answering with a fixed response content — used to keep the input-shape
    validation cases below pure, with no real I/O."""

    response_content: str = "This PR adds a new feature."
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(content=self.response_content, model=request.model)


def _intent_summary_step(
    *,
    llm_client: LLMClient,
    max_diff_bytes: int = 200_000,
    timeout: float = 5.0,
    model: str = "local-model",
) -> IntentSummaryStep:
    return IntentSummaryStep(
        llm_client,
        model=model,
        timeout=timeout,
        max_diff_bytes=max_diff_bytes,
    )


# =============================================================================
# Integration: real LiteLLMClient against a local stub HTTP server (recorded
# cassette), per testing.md's middle tier.
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
    ``LiteLLMClient`` can be pointed at — self-contained in this file, per
    this file's own docstring."""
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_v1_one_completion_call_made_and_response_forwarded_unchanged(
    stub_llm_server: Callable[[ResponseFactory], str],
) -> None:
    """US-V1: Given a claimed job whose upstream step has produced its
    output, When the ``intent_summary`` step runs, Then exactly one LLM
    Gateway completion call is made and its response is forwarded,
    unchanged, as a new field on this step's own output."""
    call_count = 0

    def respond() -> tuple[int, bytes]:
        nonlocal call_count
        call_count += 1
        return 200, _canned_completion_json("This PR refactors the widget loader.")

    endpoint = stub_llm_server(respond)
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _intent_summary_step(llm_client=llm_client)
    upstream_output = _FakeContextBuilderOutput(diff=_diff_with_patch())

    output = await step.run(upstream_output, _context())

    assert call_count == 1
    assert output.intent_summary == "This PR refactors the widget loader."
    assert output.diff == upstream_output.diff
    assert output.org_id == "org-1"
    assert output.pr_number == 42


@pytest.mark.asyncio
@pytest.mark.integration
async def test_response_exceeding_max_chars_is_truncated_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
    stub_llm_server: Callable[[ResponseFactory], str],
) -> None:
    long_response = "x" * (MAX_INTENT_SUMMARY_CHARS + 1)
    endpoint = stub_llm_server(lambda: (200, _canned_completion_json(long_response)))
    llm_client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")
    step = _intent_summary_step(llm_client=llm_client)

    with caplog.at_level(logging.WARNING, logger="review.intent_summary"):
        output = await step.run(_FakeContextBuilderOutput(diff=_diff_with_patch()), _context())

    assert len(output.intent_summary) == MAX_INTENT_SUMMARY_CHARS
    warnings = [r for r in caplog.records if "truncated" in r.message]
    assert len(warnings) == 1
    assert warnings[0].ceiling_chars == MAX_INTENT_SUMMARY_CHARS  # type: ignore[attr-defined]
    assert warnings[0].original_chars == MAX_INTENT_SUMMARY_CHARS + 1  # type: ignore[attr-defined]
    assert warnings[0].truncated_chars == MAX_INTENT_SUMMARY_CHARS  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_response_at_or_under_max_chars_is_not_truncated_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exact_response = "y" * MAX_INTENT_SUMMARY_CHARS
    llm_client = _FakeLLMClient(response_content=exact_response)
    step = _intent_summary_step(llm_client=llm_client)

    with caplog.at_level(logging.WARNING, logger="review.intent_summary"):
        output = await step.run(_FakeContextBuilderOutput(diff=_diff_with_patch()), _context())

    assert output.intent_summary == exact_response
    assert not [r for r in caplog.records if "truncated" in r.message]


# =============================================================================
# Input validation off several different upstream shapes, mirroring
# review/test_context_builder_step.py's own pattern for the same concern.
# =============================================================================


@pytest.mark.asyncio
async def test_input_validates_off_context_builder_output_shape() -> None:
    llm_client = _FakeLLMClient()
    step = _intent_summary_step(llm_client=llm_client)
    context_bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/caller.ts",
                symbol_name="bar",
                content="function bar() {}",
                line_range=(1, 1),
            ),
        )
    )
    upstream_output = _FakeContextBuilderOutput(
        diff=_diff_with_patch(), context_bundle=context_bundle
    )

    output = await step.run(upstream_output, _context())

    assert output.context_bundle == context_bundle
    assert output.intent_summary == "This PR adds a new feature."


@pytest.mark.asyncio
async def test_input_validates_off_static_analysis_output_shape() -> None:
    llm_client = _FakeLLMClient()
    step = _intent_summary_step(llm_client=llm_client)
    static_finding = Finding(
        file="app.py", line=1, severity="high", message="hardcoded secret", source="gitleaks"
    )
    upstream_output = _FakeStaticAnalysisOutput(diff=_diff_with_patch(), findings=[static_finding])

    output = await step.run(upstream_output, _context())

    assert output.findings == [static_finding]
    assert output.context_bundle is None


@pytest.mark.asyncio
async def test_input_validates_off_prefilter_output_shape() -> None:
    llm_client = _FakeLLMClient()
    step = _intent_summary_step(llm_client=llm_client)
    upstream_output = _FakePrefilterOutput(diff=_diff_with_patch())

    output = await step.run(upstream_output, _context())

    assert output.findings == []
    assert output.context_bundle is None
    assert output.intent_summary == "This PR adds a new feature."


def test_depends_on_defaults_to_context_builder() -> None:
    step = _intent_summary_step(llm_client=_FakeLLMClient())

    assert step.depends_on == ("context_builder",)


def test_depends_on_is_configurable() -> None:
    step = IntentSummaryStep(
        _FakeLLMClient(),
        model="local-model",
        timeout=5.0,
        max_diff_bytes=200_000,
        depends_on="prefilter",
    )

    assert step.depends_on == ("prefilter",)


# =============================================================================
# Model routing (docs/plans/plan-deliver-admin-gui.md's Phase 5 section,
# Unit F, Assumption 3): model_resolver injection.
# =============================================================================


class _StaticReader:
    """A ``ModelRoutingConfigReader`` fake returning one fixed row for every
    call — enough to prove the resolved model (not the constructor's own
    ``model`` fallback) reaches the completion request."""

    def __init__(self, row: ModelRoutingConfigRow) -> None:
        self._row = row
        self.calls: list[tuple[str | None, str]] = []

    async def get(self, *, org_id: str | None, stage: str) -> ModelRoutingConfigRow | None:
        self.calls.append((org_id, stage))
        return self._row


def _assert_is_reader(reader: object) -> None:
    assert isinstance(reader, ModelRoutingConfigReader)


@pytest.mark.asyncio
async def test_model_resolver_when_set_resolves_model_at_dispatch_time() -> None:
    reader = _StaticReader(
        ModelRoutingConfigRow(org_id="org-1", stage="intent_summary", model_name="routed-model")
    )
    _assert_is_reader(reader)
    resolver = FallbackModelRoutingResolver(reader, static_default_model="local-model")
    llm_client = _FakeLLMClient()
    step = IntentSummaryStep(
        llm_client,
        model="local-model",
        timeout=5.0,
        max_diff_bytes=200_000,
        model_resolver=resolver,
    )
    upstream_output = _FakeContextBuilderOutput(diff=_diff_with_patch(), org_id="org-1")

    await step.run(upstream_output, _context())

    assert len(llm_client.calls) == 1
    assert llm_client.calls[0].model == "routed-model"
    # the resolver is called with this step's own registry name
    # ("intent_summary") and the job's own org_id, per Assumption 3's exact
    # `resolve(stage, org_id)` signature — the org tier already returned a
    # row, so the platform tier is never queried.
    assert reader.calls == [("org-1", "intent_summary")]


@pytest.mark.asyncio
async def test_model_resolver_absent_uses_constructor_model_unchanged() -> None:
    """``model_resolver=None`` (the default) restores byte-for-byte this
    step's prior behavior — ``model`` is used directly, with zero calls to
    any resolver."""
    llm_client = _FakeLLMClient()
    step = IntentSummaryStep(llm_client, model="local-model", timeout=5.0, max_diff_bytes=200_000)
    upstream_output = _FakeContextBuilderOutput(diff=_diff_with_patch())

    await step.run(upstream_output, _context())

    assert len(llm_client.calls) == 1
    assert llm_client.calls[0].model == "local-model"


# =============================================================================
# Unit: diff-truncation-into-prompt behavior, fake at the LLMClient boundary.
# =============================================================================


@pytest.mark.asyncio
async def test_diff_truncated_to_fit_prompt_byte_ceiling_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = Diff(files=(DiffFile(path="app.py", status="modified", patch="x" * 1_000),))
    llm_client = _FakeLLMClient()
    step = _intent_summary_step(llm_client=llm_client, max_diff_bytes=10)

    with caplog.at_level(logging.WARNING, logger="review.intent_summary"):
        await step.run(_FakeContextBuilderOutput(diff=diff), _context())

    sent_user_message = llm_client.calls[0].messages[-1].content
    assert "x" * 1_000 not in sent_user_message
    warnings = [r for r in caplog.records if "diff truncated" in r.message]
    assert len(warnings) == 1
    assert warnings[0].ceiling_bytes == 10  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_success_logs_only_the_char_count_never_the_summary_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_looking_summary = "adds handling for API_KEY=super-secret-value"
    llm_client = _FakeLLMClient(response_content=secret_looking_summary)
    step = _intent_summary_step(llm_client=llm_client)

    with caplog.at_level(logging.INFO, logger="review.intent_summary"):
        output = await step.run(_FakeContextBuilderOutput(diff=_diff_with_patch()), _context())

    assert output.intent_summary == secret_looking_summary
    completed_records = [r for r in caplog.records if "completed" in r.message]
    assert len(completed_records) == 1
    assert completed_records[0].intent_summary_chars == len(  # type: ignore[attr-defined]
        secret_looking_summary
    )
    for record in caplog.records:
        assert secret_looking_summary not in record.getMessage()
