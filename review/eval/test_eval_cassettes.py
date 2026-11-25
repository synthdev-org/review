"""Integration tests: the real ``LiteLLMClient`` over respx cassettes,
through the real ``ReviewGenerationStep`` and ``VerificationStep``.

Covers the report shape, the cassette-integrity guarantee (a prompt change
that leaves a cassette stale must fail), and that recording never writes
credentials or response headers. No live LLM calls.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from adapter.llm.litellm_client import LiteLLMClient
from review.eval.cassette import (
    CassetteStaleError,
    forward_to_endpoint,
    load_cassette,
    replay,
    save_cassette,
)
from review.eval.corpus import EvalRow, load_corpus
from review.eval.metrics import Metrics, compute_metrics
from review.eval.record import (
    BASELINE_PATH,
    REPLAY_ENDPOINT,
    cassette_path,
    record_version,
    synthetic_producer,
)
from review.eval.run import run_eval

pytestmark = pytest.mark.integration


async def _replay_v1(rows: list[EvalRow]) -> Metrics:
    cassette = load_cassette(cassette_path("v1"))
    client = LiteLLMClient(endpoint=REPLAY_ENDPOINT, api_key="replay-placeholder")
    with replay(cassette):
        outcomes = await run_eval(rows, client, model=cassette.model)
    return compute_metrics(outcomes)


@pytest.mark.asyncio
async def test_v1_cassette_replay_reproduces_the_committed_baseline() -> None:
    baseline = json.loads(BASELINE_PATH.read_text())

    metrics = await _replay_v1(load_corpus())

    assert metrics.model_dump() == baseline["metrics"]
    assert set(baseline["metrics"]) == {
        "precision",
        "recall",
        "findings_per_pr",
        "keep_rate_by_category",
        "kept_ids",
    }
    # Every labelled true positive survives the baseline (the gate Phase 2 compares against).
    assert metrics.recall == 1.0


def test_committed_v1_cassette_is_labelled_synthetic_with_a_rerecord_command() -> None:
    cassette = load_cassette(cassette_path("v1"))
    baseline = json.loads(BASELINE_PATH.read_text())

    assert cassette.synthetic is True
    assert baseline["synthetic"] is True
    assert "SYNTHETIC" in cassette.note
    assert "python -m review.eval.record" in cassette.rerecord


@pytest.mark.asyncio
async def test_a_prompt_change_leaves_the_cassette_stale_and_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "review.review_generation._SYSTEM_PROMPT_PREFIX",
        "You are a different reviewer. " + "x" * 5,
    )

    with pytest.raises(CassetteStaleError, match=r"python -m review\.eval\.record"):
        await _replay_v1(load_corpus())


@pytest.mark.asyncio
async def test_an_interaction_nothing_requests_is_reported_as_stale() -> None:
    rows = load_corpus()
    cassette = load_cassette(cassette_path("v1"))
    client = LiteLLMClient(endpoint=REPLAY_ENDPOINT, api_key="replay-placeholder")

    with pytest.raises(CassetteStaleError, match="never requested"), replay(cassette):
        # Only the first PR's rows: every other interaction stays unused.
        await run_eval(
            [r for r in rows if r.source_pr == rows[0].source_pr], client, model=cassette.model
        )


class _EndpointWithSecrets(BaseHTTPRequestHandler):
    """A stand-in LLM endpoint that echoes credential-like response headers."""

    seen_authorization: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["content-length"]))
        type(self).seen_authorization.append(self.headers.get("authorization", ""))
        body = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"findings": []}'},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "session=cookie-value-1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.asyncio
async def test_recording_forwards_credentials_but_never_writes_them(tmp_path: Path) -> None:
    server = HTTPServer(("127.0.0.1", 0), _EndpointWithSecrets)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
        one_pr = [r for r in load_corpus() if r.diff_excerpt is not None][:1]
        cassette, _ = await record_version(
            one_pr,
            version="scratch",
            produce=forward_to_endpoint,
            endpoint=endpoint,
            api_key="sk-test-recording-credential",
            model="m",
            synthetic=False,
        )
    finally:
        server.shutdown()
        server.server_close()

    path = tmp_path / "scratch.json"
    save_cassette(path, cassette)
    written = path.read_text().lower()

    assert _EndpointWithSecrets.seen_authorization == ["Bearer sk-test-recording-credential"]
    assert "sk-test-recording-credential" not in written
    assert "authorization" not in written
    assert "cookie" not in written


@pytest.mark.asyncio
async def test_synthetic_recording_is_deterministic() -> None:
    rows = load_corpus()
    cassette, _ = await record_version(
        rows,
        version="v1",
        produce=synthetic_producer(rows, load_cassette(cassette_path("v1")).model),
        endpoint=REPLAY_ENDPOINT,
        api_key="synthetic-placeholder",
        model=load_cassette(cassette_path("v1")).model,
        synthetic=True,
    )

    assert cassette == load_cassette(cassette_path("v1"))


@pytest.mark.asyncio
async def test_v2_cassette_replay_keeps_every_baseline_true_positive_at_no_lower_precision() -> (
    None
):
    """Phase 2 acceptance (A4): the v2 prompt, replayed through the real
    steps, loses no labelled true positive and is not less precise than v1."""
    baseline = json.loads(BASELINE_PATH.read_text())["metrics"]
    cassette = load_cassette(cassette_path("v2"))
    client = LiteLLMClient(endpoint=REPLAY_ENDPOINT, api_key="replay-placeholder")

    with replay(cassette):
        outcomes = await run_eval(
            load_corpus(), client, model=cassette.model, review_quality_v2=True
        )
    metrics = compute_metrics(outcomes)

    true_positive_ids = {o.row.id for o in outcomes if o.row.label == "true_positive"}
    assert true_positive_ids <= set(metrics.kept_ids)
    assert metrics.recall >= baseline["recall"]
    assert metrics.precision >= baseline["precision"]
    # The style-only rows the v1 judge let through are now rejected.
    assert metrics.keep_rate_by_category["style"] == 0.0


def test_committed_v2_cassette_is_labelled_synthetic_with_a_rerecord_command() -> None:
    cassette = load_cassette(cassette_path("v2"))

    assert cassette.synthetic is True
    assert "SYNTHETIC" in cassette.note
    assert "--version v2" in cassette.rerecord
    assert "--write-baseline" not in cassette.rerecord


@pytest.mark.asyncio
async def test_synthetic_v2_recording_is_deterministic() -> None:
    rows = load_corpus()
    committed = load_cassette(cassette_path("v2"))
    cassette, _ = await record_version(
        rows,
        version="v2",
        produce=synthetic_producer(rows, committed.model, review_quality_v2=True),
        endpoint=REPLAY_ENDPOINT,
        api_key="synthetic-placeholder",
        model=committed.model,
        synthetic=True,
        review_quality_v2=True,
    )

    assert cassette == committed
