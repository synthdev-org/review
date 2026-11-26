"""Integration tests for the smell baseline (docs/specs/spec-smell-and-spec-review.md
US-S1, US-S2, US-S3).

Exercises the real ``adapter.llm.litellm_client.LiteLLMClient`` against a
local stub HTTP server standing in for the LLM endpoint (a recorded
cassette, the same approach ``review/test_review_generation_step.py``
documents), chaining ``ReviewGenerationStep`` into ``VerificationStep``.
The stub is kept self-contained here, matching that file's convention.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adapter.llm.litellm_client import LiteLLMClient
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.review_generation import (
    ReviewGenerationOutput,
    ReviewGenerationStep,
    _build_system_prompt,
)
from review.step import StepContext
from review.verification import VerificationOutput, VerificationStep

pytestmark = pytest.mark.integration


@dataclass
class _Upstream:
    """Structural stand-in for the step upstream of ``review_generation``."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"


class _PassthroughRedactor:
    async def redact(self, text: str) -> str:
        return text


@pytest.fixture
def stub_llm() -> Iterator[Callable[[list[str], list[dict[str, object]]], str]]:
    """``start(contents, requests)`` serves ``contents`` in order (one per
    request) and appends each decoded request body to ``requests``; returns
    the endpoint base URL."""
    servers: list[HTTPServer] = []

    def start(contents: list[str], requests: list[dict[str, object]]) -> str:
        queue = list(contents)

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(length)))
                body = json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "local-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": queue.pop(0)},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


_DUPLICATED_DIFF = Diff(
    files=(
        DiffFile(
            path="a.py", status="modified", patch="@@ -1 +1 @@\n-x\n+total = sum(i.p for i in r)\n"
        ),
        DiffFile(
            path="b.py", status="modified", patch="@@ -1 +1 @@\n-y\n+total = sum(i.p for i in r)\n"
        ),
    )
)


def _smell_response(severity: str = "high", message: str = "Duplicated Code in b.py") -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": severity,
                    "message": message,
                    "category": "smell",
                }
            ]
        }
    )


def _generation_step(endpoint: str, *, smell: bool) -> ReviewGenerationStep:
    return ReviewGenerationStep(
        LiteLLMClient(endpoint=endpoint, api_key="not-needed"),
        redactor=_PassthroughRedactor(),
        model="local-model",
        timeout=5.0,
        max_diff_bytes=200_000,
        smell_baseline_enabled=smell,
    )


def _system_prompt_sent(request: dict[str, object]) -> str:
    messages = request["messages"]
    assert isinstance(messages, list)
    return str(messages[0]["content"])


@pytest.mark.asyncio
async def test_us_s1_prompt_has_smell_section_and_duplicated_code_becomes_smell_finding(
    stub_llm: Callable[[list[str], list[dict[str, object]]], str],
) -> None:
    requests: list[dict[str, object]] = []
    endpoint = stub_llm([_smell_response()], requests)

    output = await _generation_step(endpoint, smell=True).run(
        _Upstream(diff=_DUPLICATED_DIFF), StepContext(job_id="job-1")
    )

    assert "Duplicated Code" in _system_prompt_sent(requests[0])
    [finding] = output.findings
    assert finding.category == "smell"
    assert finding.source == "llm"


@pytest.mark.asyncio
async def test_us_s2_flag_off_sends_the_pre_smell_prompt_and_output_has_no_category(
    stub_llm: Callable[[list[str], list[dict[str, object]]], str],
) -> None:
    requests: list[dict[str, object]] = []
    plain = json.dumps(
        {"findings": [{"file": "a.py", "line": 1, "severity": "medium", "message": "bug"}]}
    )
    endpoint = stub_llm([plain], requests)

    output = await _generation_step(endpoint, smell=False).run(
        _Upstream(diff=_DUPLICATED_DIFF), StepContext(job_id="job-1")
    )

    sent = _system_prompt_sent(requests[0])
    assert sent == _build_system_prompt(None, smell_baseline=False)
    assert "Duplicated Code" not in sent
    assert "category" not in sent
    [finding] = output.findings
    assert finding.category is None
    assert finding.severity == "medium"
    assert finding.message == "bug"


async def _run_generation_then_verification(
    endpoint: str,
) -> tuple[ReviewGenerationOutput, VerificationOutput]:
    context = StepContext(job_id="job-1")
    generated = await _generation_step(endpoint, smell=True).run(
        _Upstream(diff=_DUPLICATED_DIFF), context
    )
    verified = await VerificationStep(
        LiteLLMClient(endpoint=endpoint, api_key="not-needed"),
        model="local-model",
        timeout=5.0,
        max_diff_bytes=200_000,
    ).run(generated, context)
    return generated, verified


@pytest.mark.asyncio
async def test_us_s3_posted_smell_is_labelled_capped_and_keeps_category(
    stub_llm: Callable[[list[str], list[dict[str, object]]], str],
) -> None:
    passing = json.dumps(
        {
            "verdicts": [
                {"index": 0, "valid": True, "actionable": True, "specific": True, "valuable": True}
            ]
        }
    )
    endpoint = stub_llm([_smell_response(severity="critical"), passing], [])

    _, verified = await _run_generation_then_verification(endpoint)

    [finding] = verified.findings
    assert finding.category == "smell"
    assert finding.message.startswith("Possible ")
    assert finding.severity in ("low", "info")
    assert finding.verification_status == "verified"


@pytest.mark.asyncio
async def test_us_s3_smell_rejected_by_verification_is_not_posted(
    stub_llm: Callable[[list[str], list[dict[str, object]]], str],
) -> None:
    failing = json.dumps(
        {
            "verdicts": [
                {"index": 0, "valid": True, "actionable": False, "specific": True, "valuable": True}
            ]
        }
    )
    endpoint = stub_llm([_smell_response(), failing], [])

    generated, verified = await _run_generation_then_verification(endpoint)

    assert len(generated.findings) == 1
    assert verified.findings == []
