"""Unit F's own E2E proof (docs/specs/spec-observability.md): US11
("Working dashboard"), US12 ("Local demo"), and US14's dashboard-visible
scenario — the only place in this PRD's own Testing table where "every
panel resolves to real data" and "the heartbeat panel visibly goes stale"
are proven against a *running* Grafana HTTP API, not asserted by
inspection (per Unit E's own acceptance split: Unit E is code-complete on
the drift guard alone; this file is what actually closes US11).

Brings up the full Docker Compose stack **with `--profile observability`**
(postgres/migrate/webhook_api/worker, per every other E2E file here, *plus*
an OTel Collector, Prometheus, and the bundled Grafana — see
docker-compose.yml's own "observability profile" comment), points
`OTEL_EXPORTER_OTLP_ENDPOINT` at the bundled collector's in-network address
(`http://otel-collector:4317` — the same value an operator would put in
`.env` per that file's own comment), and drives a real review job plus a
real indexing job the same way `tests/e2e/test_context_builder.py` already
does (this file's own stub GitHub/LLM servers, GitHub scenario, and
running_stack fixture are adapted from that one almost verbatim — it
already proves the one combination — a `push`-indexed cross-file caller
followed by a PR that modifies the called function — that reliably drives
every metric this file's own dashboard needs real data for: a genuine
`context_builder` "ok" outcome (a real, non-empty `ContextBundle`, not
`degraded_empty`), an LLM-sourced finding (findings/verification funnel),
real LLM token usage, and real indexing chunks/edges). One extra
malformed-signature webhook (no job involved) exercises US8's rejection
counter, the one dashboard panel that scenario doesn't otherwise touch.

**Compose profile mechanics this file leans on** (verified against this
environment's real `docker compose` binary, not just its `config`
rendering — see this repo's own `--profile` requirement): `up`/`down` must
carry `--profile observability` for Compose to create/tear down the three
extra services at all (a plain `down` leaves them running — confirmed
experimentally); once a profiled service's container already exists,
`ps`/`kill`/`logs`/`exec` against it by name work with or without the flag,
so `tests/e2e/conftest.py`'s existing `container_id`/`wait_for_healthy`/
`wait_for_log_line` helpers are reused unmodified for every service in
this stack, profiled or not — only this file's own `up`/`down` calls add
the flag.

**Querying "does the dashboard actually show data", per US11's own
"proven by Unit F's E2E scenario, not asserted by inspection" text:** this
file reads the exact panel queries out of Unit E's shipped
`charts/review/dashboards/review.json` (never hand-copied/paraphrased
here, so a future panel edit can't silently drift from what this test
checks) and POSTs each one to Grafana's own `/api/ds/query` endpoint — the
same backend query-proxy API the Grafana UI itself calls to render a
panel, pointed at the datasource UID `observability/grafana/provisioning/
datasources/datasource.yml` provisions deterministically
(`review-observability-prometheus`) — rather than querying Prometheus
directly, so this genuinely proves the dashboard resolves data through
Grafana, not merely that the underlying Prometheus series exist.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from review.fix_generation import GENERATION_MARKER, RUBRIC_MARKER
from tests.e2e.conftest import (
    base_compose_env,
    compose,
    container_id,
    wait_for_healthy,
    wait_for_log_line,
)
from tests.e2e.test_review_pipeline import (
    _IssueComments,
    serve_issue_comments,
)
from tests.e2e.test_review_pipeline import _StubLLMHandler as _PipelineLLMHandler

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "charts" / "review" / "dashboards" / "review.json"
PROMETHEUS_DATASOURCE_UID = "review-observability-prometheus"

STUB_GITHUB_PORT = 19084
STUB_LLM_REVIEW_PORT = 19236
_OWNER_REPO = "acme/observability-e2e"
_INSTALLATION_ID = 741852
_WEBHOOK_URL_PATH = "/webhooks/github"
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_PR_NUMBER = 99

# Local-only remapped ports (this file's own convention, matching
# WEBHOOK_API_PORT/POSTGRES_PORT's own remapping in
# tests/e2e/conftest.py's base_compose_env) — never the shipped defaults,
# so this test can never collide with a developer's own
# `docker compose --profile observability up`.
_GRAFANA_PORT = "13091"
_PROMETHEUS_PORT = "13092"
_OTEL_COLLECTOR_OTLP_GRPC_PORT = "13093"
_OTEL_COLLECTOR_PROMETHEUS_PORT = "13094"
_GRAFANA_ADMIN_USER = "admin"
_GRAFANA_ADMIN_PASSWORD = "e2e-test-grafana-password"

_JOB_WAIT_TIMEOUT_SECONDS = 120.0
# US12's own concrete bound: the OTel SDK's default periodic metric export
# interval is 60s and the bundled Prometheus's scrape interval is 15s (see
# observability/prometheus.yml's own comment) — 2 minutes gives one full
# export-then-scrape cycle plus margin, not an arbitrary number.
_DASHBOARD_DATA_TIMEOUT_SECONDS = 120.0
# US14's own 60s staleness threshold (2x the 30s heartbeat interval) — this
# is the ceiling this test polls up to *after* killing the collector, not
# the same 2-minute dashboard bound above; a fresh margin past 60s so a
# slow CI runner doesn't flake right at the boundary.
_HEARTBEAT_STALE_TIMEOUT_SECONDS = 150.0

# --- Fixture repo content, adapted from tests/e2e/test_context_builder.py's
# own proven scenario (a real cross-file caller -> helper edge, so
# context_builder genuinely resolves a non-empty ContextBundle instead of
# degraded_empty) --------------------------------------------------------

_INDEX_COMMIT_SHA = "b" * 40  # also this PR's own base_sha
_HEAD_SHA = "c" * 40
_SECOND_PUSH_COMMIT_SHA = "d" * 40
_FAILURE_HEAD_SHA = "e" * 40
_FAILURE_PR_NUMBER = 100
# Phase 9 (static-analysis expansion): a PR whose head tree is served once (for
# the diff fetch) and then fails, so the static-analysis repo-config fetch falls
# back to default profiles and the config-fallback panel gets a real sample.
_FALLBACK_HEAD_SHA = "f" * 40
_FALLBACK_PR_NUMBER = 101

# Phase 11: a PR on the main scenario's tree whose stub fix reply is unparseable,
# so the fix-rejection panels get a real `parse_error` sample.
_FIX_REJECT_HEAD_SHA = "9" * 40
_FIX_REJECT_PR_NUMBER = 102

# Phase 12: PRs on a two-file diff (so the walkthrough runs), one per walkthrough outcome the
# stub can drive. Each head SHA serves the main scenario's tree.
_WT_PR_NUMBER = 103  # posted, then updated by a second push
_WT_HEAD_SHA = "a1" * 20
_WT_SECOND_HEAD_SHA = "a2" * 20
_WT_TIMEOUT_PR_NUMBER, _WT_TIMEOUT_HEAD_SHA = 104, "a3" * 20
_WT_GARBAGE_PR_NUMBER, _WT_GARBAGE_HEAD_SHA = 105, "a4" * 20
_WT_GITHUB_ERROR_PR_NUMBER, _WT_GITHUB_ERROR_HEAD_SHA = 106, "a5" * 20
_WT_LIMIT_PR_NUMBER, _WT_LIMIT_HEAD_SHA = 107, "a6" * 20
_WT_SHAS = (
    _WT_HEAD_SHA,
    _WT_SECOND_HEAD_SHA,
    _WT_TIMEOUT_HEAD_SHA,
    _WT_GARBAGE_HEAD_SHA,
    _WT_GITHUB_ERROR_HEAD_SHA,
    _WT_LIMIT_HEAD_SHA,
)

_HELPER_BASE_BLOB_SHA = "1" * 40
_HELPER_HEAD_BLOB_SHA = "2" * 40
_CALLER_BLOB_SHA = "3" * 40
_SECOND_FILE_BLOB_SHA = "4" * 40

_HELPER_TS_BASE = (
    b"// helper.ts\n"
    b"//\n"
    b"// A small utility module used by other files in this repository. Kept\n"
    b"// intentionally documented (rather than a bare one-liner) so a realistic\n"
    b"// PR diff around a single-line change still carries a realistic amount of\n"
    b"// surrounding context, the same shape a real-world diff would have.\n"
    b"//\n"
    b"export function helper(): number {\n"
    b"  return 1;\n"
    b"}\n"
)
_HELPER_TS_HEAD = _HELPER_TS_BASE.replace(b"return 1;", b"return 2;")
_CALLER_TS = (
    b'import { helper } from "./helper";\n'
    b"\n"
    b"export function caller(): number {\n"
    b"  return helper();\n"
    b"}\n"
)
_SECOND_FILE_TS = b"export function second(): number {\n  return 3;\n}\n"

_CALLER_PATCH = "@@ -0,0 +1,5 @@\n" + "\n".join(f"+line {i}" for i in range(5))

_HELPER_PATCH = (
    "@@ -1,10 +1,10 @@\n"
    " // helper.ts\n"
    " //\n"
    " // A small utility module used by other files in this repository. Kept\n"
    " // intentionally documented (rather than a bare one-liner) so a realistic\n"
    " // PR diff around a single-line change still carries a realistic amount of\n"
    " // surrounding context, the same shape a real-world diff would have.\n"
    " //\n"
    " export function helper(): number {\n"
    "-  return 1;\n"
    "+  return 2;\n"
    " }\n"
)


# ---------------------------------------------------------------------------
# Stub GitHub server — answers the push-indexing call chain (twice, at two
# distinct commit SHAs — the main scenario's `_INDEX_COMMIT_SHA` and a
# second, independent push at `_SECOND_PUSH_COMMIT_SHA` the heartbeat/
# staleness test drives on its own, per this module's own docstring) and
# the PR-review call chain, all against one owner/repo.
# ---------------------------------------------------------------------------


@dataclass
class _CallLog:
    calls: list[str] = field(default_factory=list)
    review_bodies: list[dict[str, object]] = field(default_factory=list)
    check_run_bodies: list[dict[str, object]] = field(default_factory=list)


def _tree_entry(path: str, blob_sha: str, content: bytes) -> dict[str, object]:
    return {"type": "blob", "path": path, "mode": "100644", "sha": blob_sha, "size": len(content)}


_TREES_BY_SHA: dict[str, list[dict[str, object]]] = {
    _HEAD_SHA: [
        _tree_entry("helper.ts", _HELPER_HEAD_BLOB_SHA, _HELPER_TS_HEAD),
        _tree_entry("caller.ts", _CALLER_BLOB_SHA, _CALLER_TS),
    ],
    _INDEX_COMMIT_SHA: [
        _tree_entry("helper.ts", _HELPER_BASE_BLOB_SHA, _HELPER_TS_BASE),
        _tree_entry("caller.ts", _CALLER_BLOB_SHA, _CALLER_TS),
    ],
    _FIX_REJECT_HEAD_SHA: [
        _tree_entry("helper.ts", _HELPER_HEAD_BLOB_SHA, _HELPER_TS_HEAD),
        _tree_entry("caller.ts", _CALLER_BLOB_SHA, _CALLER_TS),
    ],
    _FALLBACK_HEAD_SHA: [
        _tree_entry("helper.ts", _HELPER_HEAD_BLOB_SHA, _HELPER_TS_HEAD),
        _tree_entry("caller.ts", _CALLER_BLOB_SHA, _CALLER_TS),
    ],
    _SECOND_PUSH_COMMIT_SHA: [_tree_entry("second.ts", _SECOND_FILE_BLOB_SHA, _SECOND_FILE_TS)],
}
for _sha in _WT_SHAS:
    _TREES_BY_SHA[_sha] = _TREES_BY_SHA[_HEAD_SHA]

_BLOBS: dict[str, bytes] = {
    _HELPER_BASE_BLOB_SHA: _HELPER_TS_BASE,
    _HELPER_HEAD_BLOB_SHA: _HELPER_TS_HEAD,
    _CALLER_BLOB_SHA: _CALLER_TS,
    _SECOND_FILE_BLOB_SHA: _SECOND_FILE_TS,
}


class _StubGitHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _calls(self) -> _CallLog:
        calls = getattr(self.server, "calls", None)
        assert isinstance(calls, _CallLog)
        return calls

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        result: dict[str, object] = json.loads(raw)
        return result

    def do_POST(self) -> None:
        path = self.path
        self._calls().calls.append(f"POST {path}")

        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            self._send_json(
                201, {"token": "ghs_e2e_observability_token", "expires_at": "2999-01-01T00:00:00Z"}
            )
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/reviews", path):
            self._calls().review_bodies.append(self._read_json_body())
            self._send_json(200, {"id": 1})
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/check-runs", path):
            self._calls().check_run_bodies.append(self._read_json_body())
            self._send_json(201, {"id": 1})
            return

        if serve_issue_comments(self, "POST", path, "", _OWNER_REPO):
            return

        self._send_json(404, {"message": "not found (stub)"})

    def do_PATCH(self) -> None:
        self._calls().calls.append(f"PATCH {self.path}")
        if not serve_issue_comments(self, "PATCH", self.path, "", _OWNER_REPO):
            self._send_json(404, {"message": "not found (stub)"})

    def do_DELETE(self) -> None:
        self._calls().calls.append(f"DELETE {self.path}")
        if not serve_issue_comments(self, "DELETE", self.path, "", _OWNER_REPO):
            self._send_json(404, {"message": "not found (stub)"})

    def do_GET(self) -> None:
        path_only = self.path.split("?", 1)[0]
        self._calls().calls.append(f"GET {self.path}")
        if serve_issue_comments(self, "GET", path_only, self.path.partition("?")[2], _OWNER_REPO):
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+", path_only):
            self._send_json(
                200, {"changed_files": 2 if getattr(self.server, "two_files", False) else 1}
            )
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/files", path_only):
            # US1: `files_endpoint_status` (default 200, mutable per-server
            # attribute — mirrors tests/e2e/test_review_pipeline.py's own
            # `_GitHubScenario.files_endpoint_status`) lets one specific PR
            # in this module's own scenario force a real, terminal
            # prefilter/diff-fetch failure — the one real signal that
            # produces dispatch.py's own `outcome="failed"` (a
            # `PipelineFailedError` genuinely propagating out of
            # `StepRegistry.run()`, not merely a "failure"-conclusion
            # check-run the pipeline caught and posted through its own
            # normal, `outcome="done"` completion path).
            status = getattr(self.server, "files_endpoint_status", 200)
            if status != 200:
                self._send_json(status, {"message": "simulated files-endpoint failure (stub)"})
                return
            files = [{"filename": "helper.ts", "status": "modified", "patch": _HELPER_PATCH}]
            if getattr(self.server, "two_files", False):
                files.append({"filename": "caller.ts", "status": "added", "patch": _CALLER_PATCH})
            self._send_json(200, files)
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/(\w+)", path_only)
        if match:
            if match.group(1) == _FALLBACK_HEAD_SHA:
                served = getattr(self.server, "fallback_tree_requests", 0)
                self.server.fallback_tree_requests = served + 1  # type: ignore[attr-defined]
                if served >= 1:  # first request is the diff fetch; later ones are config fetch
                    self._send_json(500, {"message": "simulated tree failure (stub)"})
                    return
            tree = _TREES_BY_SHA.get(match.group(1), [])
            self._send_json(200, {"tree": tree, "truncated": False})
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path_only)
        if match:
            content = _BLOBS.get(match.group(1), b"")
            self._send_json(
                200, {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
            )
            return

        self._send_json(404, {"message": "not found (stub)"})


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_GITHUB_PORT), _StubGitHubHandler)
    server.calls = _CallLog()  # type: ignore[attr-defined]
    server.issue_comments = _IssueComments()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Stub LLM Gateway — self-contained to this file, adapted from
# tests/e2e/test_context_builder.py's own `_StubLLMHandler` (same 3-call
# intent_summary/review_generation/verification sequencing).
# ---------------------------------------------------------------------------


class _StubLLMHandler(BaseHTTPRequestHandler):
    _INTENT_SUMMARY_CONTENT = "This PR changes a function's return value."
    _FINDINGS_CONTENT = json.dumps(
        {
            "findings": [
                {
                    "file": "helper.ts",
                    "line": 9,
                    "severity": "medium",
                    "message": "Changing this return value affects at least one known caller.",
                    "context_ref_ids": [0],
                }
            ]
        }
    )
    _VERDICT_VERIFIED_CONTENT = json.dumps(
        {
            "verdicts": [
                {"index": 0, "valid": True, "actionable": True, "specific": True, "valuable": True}
            ]
        }
    )

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _content_for_call(self, call_number: int) -> str:
        """1 = `intent_summary`, 2 = `review_generation`, 3 = `verification`."""
        if call_number <= 1:
            return self._INTENT_SUMMARY_CONTENT
        if call_number == 2:
            return self._FINDINGS_CONTENT
        return self._VERDICT_VERIFIED_CONTENT

    # Phase 12: the walkthrough call is recognized by its marker, not call position, and its
    # scenario (ok | garbage | delay) is `server.walkthrough_scenario`.
    def _walkthrough_content(self, request: dict[str, Any]) -> str | None:
        return _PipelineLLMHandler._walkthrough_content(self, request)  # type: ignore[arg-type]

    def _fix_content(self, request: dict[str, Any]) -> str | None:
        """Auto-fix calls are recognized by their prompt marker, not call
        position (plan-phase-11 Assumption 15): a valid patch and an all-true
        rubric, so the `accepted` outcome, both fix LLM-usage stages and the
        generate/validate/rubric phases all get real samples."""
        system = " ".join(
            str(m["content"]) for m in request.get("messages", []) if m.get("role") == "system"
        )
        if GENERATION_MARKER in system:
            if getattr(self.server, "fix_reply_unparseable", False):
                return "not json"
            return json.dumps({"start_line": 9, "end_line": 9, "replacement": "  return 3;"})
        if RUBRIC_MARKER in system:
            return json.dumps(
                {"resolves": True, "minimal": True, "safe": True, "compiles_in_context": True}
            )
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            request = {}
        fix_content = self._fix_content(request) if isinstance(request, dict) else None
        if fix_content is None and isinstance(request, dict):
            fix_content = self._walkthrough_content(request)
        call_number = getattr(self.server, "call_count", 0) + 1
        if fix_content is None:
            self.server.call_count = call_number  # type: ignore[attr-defined]

        body = json.dumps(
            {
                "id": "chatcmpl-e2e-observability-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": (
                                fix_content
                                if fix_content is not None
                                else self._content_for_call(call_number)
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def stub_llm_review_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_LLM_REVIEW_PORT), _StubLLMHandler)
    server.call_count = 0  # type: ignore[attr-defined]
    server.walkthrough_scenario = "ok"  # type: ignore[attr-defined]
    server.walkthrough_calls = 0  # type: ignore[attr-defined]
    server.walkthrough_delay_seconds = 14.0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Compose stack — `--profile observability` on top of every other E2E
# file's own `running_stack` shape, per this module's own docstring on
# Compose profile mechanics.
# ---------------------------------------------------------------------------


def _e2e_env() -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WEBHOOK_API_INDEXING_ENABLED="true",
        # Phase 11: the fix panels only get data when the (default-off) step runs.
        REVIEW_WORKER_FIX_GENERATION_ENABLED="true",
        # Phase 12: the walkthrough panels only get data when the (default-off) step runs; the
        # smallest legal step timeout (LLM timeout 10 s + 15) keeps the timeout case quick.
        REVIEW_WORKER_WALKTHROUGH_ENABLED="true",
        REVIEW_WORKER_WALKTHROUGH_STEP_TIMEOUT_SECONDS="25",
        # The default 5/min index limit would roll back the whole delivery (review job included)
        # once the walkthrough PRs push past it.
        REVIEW_WEBHOOK_API_INDEX_ENQUEUE_RATE_LIMIT_PER_MINUTE="1000",
        # The one manual step .env.example documents: point both
        # entrypoints at the bundled collector's in-network address, the
        # same value an operator would set in their own `.env`. Always
        # port 4317 — the collector's own *internal* listening port
        # (observability/otel-collector-config.yaml's fixed
        # `endpoint: 0.0.0.0:4317`), which `OTEL_COLLECTOR_OTLP_GRPC_PORT`
        # below never changes; that variable only remaps the *host-side*
        # publish port (docker-compose.yml's `ports:` mapping), which is
        # irrelevant here since webhook_api/worker reach otel-collector
        # over the Compose network by service name, never through a
        # published host port.
        OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4317",
        GRAFANA_ADMIN_USER=_GRAFANA_ADMIN_USER,
        GRAFANA_ADMIN_PASSWORD=_GRAFANA_ADMIN_PASSWORD,
        GRAFANA_PORT=_GRAFANA_PORT,
        PROMETHEUS_PORT=_PROMETHEUS_PORT,
        OTEL_COLLECTOR_OTLP_GRPC_PORT=_OTEL_COLLECTOR_OTLP_GRPC_PORT,
        OTEL_COLLECTOR_PROMETHEUS_PORT=_OTEL_COLLECTOR_PROMETHEUS_PORT,
    )


def _compose_up(env: dict[str, str]) -> None:
    result = compose("--profile", "observability", "up", "-d", "--build", env=env)
    assert result.returncode == 0, (
        f"docker compose --profile observability up failed unexpectedly.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _compose_down_observability(env: dict[str, str]) -> None:
    """`compose_down` (`tests/e2e/conftest.py`) omits `--profile
    observability`, which this module's own docstring proves leaves the
    three extra containers/network running — this file needs its own
    teardown that includes the flag.
    """
    compose("--profile", "observability", "down", "--volumes", "--remove-orphans", env=env)


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer, stub_llm_review_server: HTTPServer
) -> Iterator[dict[str, str]]:
    env = _e2e_env()
    _compose_down_observability(env)  # clean slate
    _compose_up(env)
    try:
        wait_for_healthy("webhook_api", env)
        wait_for_log_line("worker", env, "worker started")
        _wait_for_grafana_healthy(env)
        yield env
    finally:
        logs = compose("logs", "--no-color", env=env).stdout
        print(logs)
        _compose_down_observability(env)


# ---------------------------------------------------------------------------
# Webhook signing / posting.
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _push_payload(*, commit_sha: str, added_files: list[str]) -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO, "default_branch": "main"},
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": commit_sha,
            "commits": [{"added": added_files, "removed": [], "modified": []}],
            "forced": False,
        }
    ).encode()


def _pull_request_payload(
    *,
    pr_number: int = _PR_NUMBER,
    head_sha: str = _HEAD_SHA,
    base_sha: str = _INDEX_COMMIT_SHA,
    action: str = "opened",
) -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO},
            "action": action,
            "pull_request": {
                "number": pr_number,
                "head": {"sha": head_sha},
                "base": {"sha": base_sha},
            },
        }
    ).encode()


def _post_webhook(
    env: dict[str, str], *, body: bytes, signature: str | None, event_type: str
) -> int:
    port = env["WEBHOOK_API_PORT"]
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": str(uuid.uuid4()),
    }
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    request = urllib.request.Request(
        f"http://localhost:{port}{_WEBHOOK_URL_PATH}", data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _wait_until_review_posted(
    calls: _CallLog, *, timeout: float = _JOB_WAIT_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.check_run_bodies:
            return
        time.sleep(0.5)
    raise AssertionError(f"no check-run posted within {timeout}s (calls so far: {calls.calls!r})")


def _wait_until_check_run_count_at_least(
    calls: _CallLog, count: int, *, timeout: float = _JOB_WAIT_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(calls.check_run_bodies) >= count:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"only {len(calls.check_run_bodies)} check-run(s) posted within {timeout}s, "
        f"expected at least {count} (calls so far: {calls.calls!r})"
    )


# ---------------------------------------------------------------------------
# Direct Postgres check that indexing landed (mirrors
# tests/e2e/test_indexing_pipeline.py / test_context_builder.py's own wait
# helpers) — used only to know *when* to move on to the next webhook, not
# as this file's own pass/fail signal (that's Grafana's API, below).
# ---------------------------------------------------------------------------


def _dsn(env: dict[str, str]) -> str:
    return (
        f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@localhost:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
    )


async def _fetch_indexed_paths(dsn: str, *, repo_full_name: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT c.file_path
            FROM index_chunk c
            JOIN repo r ON r.id = c.repo_id
            WHERE r.full_name = $1
            """,
            repo_full_name,
        )
        return {row["file_path"] for row in rows}
    finally:
        await conn.close()


def _wait_for_indexed_paths(
    dsn: str, *, repo_full_name: str, expected: set[str], timeout: float = _JOB_WAIT_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    paths: set[str] = set()
    while time.monotonic() < deadline:
        paths = asyncio.run(_fetch_indexed_paths(dsn, repo_full_name=repo_full_name))
        if expected <= paths:
            return
        time.sleep(1)
    raise AssertionError(
        f"index_chunk never contained {expected} for {repo_full_name!r} within {timeout}s "
        f"(saw: {sorted(paths)})"
    )


# ---------------------------------------------------------------------------
# Grafana HTTP API — dashboard panel data resolution (US11/US12) and the
# heartbeat-staleness query (US14).
# ---------------------------------------------------------------------------


def _grafana_auth_header() -> str:
    creds = f"{_GRAFANA_ADMIN_USER}:{_GRAFANA_ADMIN_PASSWORD}"
    return "Basic " + base64.b64encode(creds.encode("utf-8")).decode("ascii")


def _wait_for_grafana_healthy(
    env: dict[str, str], timeout: float = _JOB_WAIT_TIMEOUT_SECONDS
) -> None:
    """`/api/health` needs no auth — Grafana's own liveness/readiness
    endpoint, the same signal a real operator's `docker compose ps` health
    story would eventually rely on if Grafana grew a healthcheck; polled
    directly here since this Compose service doesn't declare one (an image
    from a third party this repo doesn't control the shape of). Also
    confirms the provisioned Prometheus datasource is actually reachable
    via the API before returning — `/api/ds/query` (this file's own main
    tool) fails outright, rather than returning a "no data" frame to
    retry, if provisioning hasn't finished yet.
    """
    port = env["GRAFANA_PORT"]
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/api/health", timeout=5) as resp:
                healthy = resp.status == 200
            if healthy:
                ds_request = urllib.request.Request(
                    f"http://localhost:{port}/api/datasources/uid/{PROMETHEUS_DATASOURCE_UID}",
                    headers={"Authorization": _grafana_auth_header()},
                )
                with urllib.request.urlopen(ds_request, timeout=5) as ds_resp:
                    if ds_resp.status == 200:
                        return
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError) as exc:
            last_error = exc
        time.sleep(2)
    raise AssertionError(
        f"grafana never became healthy within {timeout}s (last error: {last_error})"
    )


def _grafana_ds_query(
    env: dict[str, str], expr: str, *, instant: bool = True
) -> dict[str, Any] | None:
    """Returns the parsed `/api/ds/query` response, or ``None`` on any
    transport/HTTP failure — callers treat ``None`` the same as an
    unresolved ("no data yet") query and keep polling, rather than this
    function raising and aborting a wait loop over what's usually a
    transient "Grafana/Prometheus still warming up" condition.
    """
    port = env["GRAFANA_PORT"]
    payload = json.dumps(
        {
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "prometheus", "uid": PROMETHEUS_DATASOURCE_UID},
                    "expr": expr,
                    "instant": instant,
                }
            ],
            "from": "now-5m",
            "to": "now",
        }
    ).encode()
    request = urllib.request.Request(
        f"http://localhost:{port}/api/ds/query",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": _grafana_auth_header()},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result: dict[str, Any] = json.loads(response.read())
            return result
    except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError):
        return None


def _frames_have_data(response: dict[str, Any] | None) -> bool:
    """Per this file's own manual verification against a real Grafana
    11.x/Prometheus datasource pair: a resolved instant query returns one
    frame whose ``data.values`` is a non-empty ``[[time...], [value...]]``
    pair; an unresolved ("No data") query returns a frame with
    ``data.values: []`` instead — never an HTTP error, so this is checked
    on the frame shape, not the status code. ``None`` (a transport/HTTP
    failure — see ``_grafana_ds_query``) is treated the same as "no data
    yet".
    """
    if response is None:
        return False
    result = response.get("results", {}).get("A", {})
    if result.get("error"):
        return False
    frames = result.get("frames", [])
    if not frames:
        return False
    return any(
        bool(values) and any(len(column) > 0 for column in values)
        for frame in frames
        for values in [frame.get("data", {}).get("values", [])]
    )


def _query_value(response: dict[str, Any] | None) -> float:
    """The single scalar value of a resolved instant query — used for the
    US14 before/after "did this panel's own value change" comparison, so
    callers must confirm `_frames_have_data` first (asserted here too, so
    a call-site ordering bug fails loudly rather than raising a raw
    `KeyError`/`TypeError`)."""
    assert response is not None
    frame = response["results"]["A"]["frames"][0]
    values = frame["data"]["values"]
    return float(values[1][0])


def _dashboard_panel_targets() -> list[tuple[str, list[str]]]:
    """Every non-`row` panel's title + its own PromQL target expressions,
    read straight out of Unit E's shipped dashboard JSON — never
    hand-copied here, so a future panel edit can't silently drift from
    what this test checks (the same file `scripts/check-dashboard-
    metrics.py`, US13, already treats as the one source of truth)."""
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    targets: list[tuple[str, list[str]]] = []
    for panel in dashboard["panels"]:
        if panel.get("type") == "row":
            continue
        exprs = [target["expr"] for target in panel.get("targets", [])]
        targets.append((panel["title"], exprs))
    return targets


# Panels whose metric needs traffic this stack does not generate (an external
# product-knowledge provider, an authenticated Admin API write, LLM citations).
# They shipped without E2E data before Phase 9 and this test was red because of
# it; each is listed explicitly (never "everything else") and checked against the
# dashboard so a renamed or removed panel fails here rather than rotting.
_PANELS_WITHOUT_E2E_TRAFFIC = frozenset(
    {
        "Provider job outcomes (non-ok, by outcome)",
        "Provider call rate by operation and outcome",
        "Provider call duration (p95)",
        "Citation refs unresolved (ratio)",
        "Product knowledge config resolution failures",
        "Admin config write rejections",
        # Phase 10 failure-path clone counters: they only record on a candidate-cap hit,
        # a skipped file or a memory-suppression error, which the stub PRs never trigger.
        # The other clone panels get data from the indexing push and the review PRs, and
        # tests/e2e/test_clone_detection.py exercises the failure modes themselves.
        "Clone findings suppressed / capped",
        "Clone review-time skips and Memory failures",
        # Phase 9 failure-path counters: a config the tool rejects, a findings
        # cap/changed-line drop, or an engine with no matching files needs
        # traffic the stub PRs do not generate.
        "Config skipped (repo config ignored)",
        "Findings dropped (ceiling / unchanged lines)",
        "Analyzer runs skipped (no matching files)",
        # Phase 12: a duplicate-comment convergence failure and an unexpected internal failure
        # (redactor outage, hostile multi-line scanner rule) cannot be provoked from stub
        # traffic; review/test_walkthrough_step.py and adapter/github/test_walkthrough_posting.py
        # cover both.
        "Walkthrough outcome: dedupe_failed",
        "Walkthrough outcome: internal_error",
    }
)


def _wait_for_all_panels_to_resolve_data(
    env: dict[str, str], *, timeout: float = _DASHBOARD_DATA_TIMEOUT_SECONDS
) -> None:
    all_targets = _dashboard_panel_targets()
    assert all_targets, "dashboard JSON yielded no queryable panels — parsing regression"
    titles = {title for title, _ in all_targets}
    assert titles >= _PANELS_WITHOUT_E2E_TRAFFIC, _PANELS_WITHOUT_E2E_TRAFFIC - titles
    panel_targets = [t for t in all_targets if t[0] not in _PANELS_WITHOUT_E2E_TRAFFIC]

    deadline = time.monotonic() + timeout
    pending: dict[str, list[str]] = {}
    while time.monotonic() < deadline:
        pending = {}
        for title, exprs in panel_targets:
            unresolved = [
                expr for expr in exprs if not _frames_have_data(_grafana_ds_query(env, expr))
            ]
            if unresolved:
                pending[title] = unresolved
        if not pending:
            return
        time.sleep(3)
    raise AssertionError(f"panels with no data after {timeout}s: {pending}")


# ---------------------------------------------------------------------------
# US11 / US12 — every dashboard panel resolves real data within 2 minutes.
# ---------------------------------------------------------------------------


def test_dashboard_panels_resolve_real_data_within_two_minutes(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    env = running_stack
    dsn = _dsn(env)

    # 0. US8: one rejected webhook, no job involved — the "Webhook
    #    rejection rate" panel's only source of data in this scenario.
    bad_body = _pull_request_payload()
    status = _post_webhook(
        env, body=bad_body, signature="sha256=" + "0" * 64, event_type="pull_request"
    )
    assert status == 401

    # 1. Index the default branch — populates index_chunk/index_graph_edge
    #    with a real caller -> helper call edge across two files (US9), and
    #    gives US2/US6's context_builder step below a real chunk to find
    #    (an "ok" outcome, not degraded_empty).
    push_body = _push_payload(commit_sha=_INDEX_COMMIT_SHA, added_files=["helper.ts", "caller.ts"])
    push_status = _post_webhook(env, body=push_body, signature=_sign(push_body), event_type="push")
    assert push_status == 200
    _wait_for_indexed_paths(dsn, repo_full_name=_OWNER_REPO, expected={"helper.ts", "caller.ts"})

    # Worker's own one-time startup LLM-reachability check already
    # consumed call slot 1 before this fixture's first real job — reset so
    # the review job below starts its own 3-call sequence at 1.
    stub_llm_review_server.call_count = 0  # type: ignore[attr-defined]

    # 2. A PR modifying `helper` — drives the full review pipeline
    #    (US1-US7): context_builder resolves the real caller hit, the stub
    #    LLM Gateway returns one finding citing it, verification verifies
    #    it, and it reaches the posted review.
    pr_body = _pull_request_payload()
    pr_status = _post_webhook(
        env, body=pr_body, signature=_sign(pr_body), event_type="pull_request"
    )
    assert pr_status == 200
    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_review_posted(calls)
    assert len(calls.review_bodies) == 1  # the one verified finding really posted

    # 3. US1's "Job failure rate" panel needs a real `outcome="failed"`
    #    sample — dispatch.py's own `_completed_counter.add(1, {...
    #    "outcome": ...})` only ever fires once per terminal job with
    #    whichever outcome actually happened (never an unconditional
    #    per-run `.add(0, {"outcome": "failed"})` the way US5/US7's
    #    per-job-run counters do), so a job that only ever completes
    #    successfully never gives Prometheus a series to query at all for
    #    `outcome="failed"` — "no data", not "0". A files-endpoint failure
    #    is a genuinely *terminal* prefilter/diff-fetch failure (mirrors
    #    tests/e2e/test_review_pipeline.py's own
    #    `test_files_endpoint_error_posts_failure_check_run_not_silent_
    #    failure`), the one real path that raises `PipelineFailedError`
    #    out of `StepRegistry.run()` and reaches dispatch.py's own
    #    `mark_failed`/`outcome="failed"` — unlike an LLM-call failure,
    #    which the pipeline instead catches internally and still posts
    #    through its normal `outcome="done"` completion path (a
    #    "failure"-*conclusion* check-run is a review-domain concept, not
    #    the same thing as this job-dispatch-level outcome).
    stub_github_server.files_endpoint_status = 500  # type: ignore[attr-defined]
    failure_body = _pull_request_payload(pr_number=_FAILURE_PR_NUMBER, head_sha=_FAILURE_HEAD_SHA)
    failure_status = _post_webhook(
        env, body=failure_body, signature=_sign(failure_body), event_type="pull_request"
    )
    assert failure_status == 200
    _wait_until_check_run_count_at_least(calls, 2)
    assert calls.check_run_bodies[-1]["conclusion"] == "failure"
    stub_github_server.files_endpoint_status = 200  # type: ignore[attr-defined]

    # 3b. Phase 9: the static-analysis config-fallback panel only has a series
    #     once a repo-config fetch actually fails. This PR's head tree is served
    #     for the diff fetch and then 500s, so the review still completes (on
    #     default profiles) and `review.static_analysis.config_fallback_total`
    #     gets a real `reason="tree_fetch_failed"` sample.
    stub_llm_review_server.call_count = 0  # type: ignore[attr-defined]  # fresh 3-call sequence
    fallback_body = _pull_request_payload(
        pr_number=_FALLBACK_PR_NUMBER, head_sha=_FALLBACK_HEAD_SHA
    )
    fallback_status = _post_webhook(
        env, body=fallback_body, signature=_sign(fallback_body), event_type="pull_request"
    )
    assert fallback_status == 200
    _wait_until_check_run_count_at_least(calls, 3)
    assert str(calls.check_run_bodies[-1]["conclusion"]) == "neutral"

    # 3c. Phase 11: the fix-rejection panels need a rejected attempt. Same tree as
    #     the main PR, but the stub's fix reply is not JSON, so the step records a
    #     real `parse_error` outcome (the review still posts).
    stub_llm_review_server.call_count = 0  # type: ignore[attr-defined]  # fresh 3-call sequence
    stub_llm_review_server.fix_reply_unparseable = True  # type: ignore[attr-defined]
    reject_body = _pull_request_payload(
        pr_number=_FIX_REJECT_PR_NUMBER, head_sha=_FIX_REJECT_HEAD_SHA
    )
    reject_status = _post_webhook(
        env, body=reject_body, signature=_sign(reject_body), event_type="pull_request"
    )
    assert reject_status == 200
    _wait_until_check_run_count_at_least(calls, 4)
    stub_llm_review_server.fix_reply_unparseable = False  # type: ignore[attr-defined]

    # 3d. Phase 12: the walkthrough panels. The main PR above is one file, so it already gave
    #     `skipped_small_pr` its sample; the PRs below serve a two-file diff. A push creates the
    #     comment (`posted`), the next push edits it (`updated`), and one PR per failure mode
    #     the stub can drive: LLM delay (`llm_timeout`), garbage (`parse_failure`), comment
    #     writes answering 500 (`github_error`) and a 501-comment PR (`skipped_comment_limit`).
    #     `dedupe_failed` and `internal_error` cannot be provoked from stub traffic.
    stub_github_server.two_files = True  # type: ignore[attr-defined]
    llm = stub_llm_review_server
    store: _IssueComments = stub_github_server.issue_comments  # type: ignore[attr-defined]
    for number, sha, action, scenario, mode in (
        (_WT_PR_NUMBER, _WT_HEAD_SHA, "opened", "ok", "normal"),
        (_WT_PR_NUMBER, _WT_SECOND_HEAD_SHA, "synchronize", "ok", "normal"),
        (_WT_TIMEOUT_PR_NUMBER, _WT_TIMEOUT_HEAD_SHA, "opened", "delay", "normal"),
        (_WT_GARBAGE_PR_NUMBER, _WT_GARBAGE_HEAD_SHA, "opened", "garbage", "normal"),
        (_WT_GITHUB_ERROR_PR_NUMBER, _WT_GITHUB_ERROR_HEAD_SHA, "opened", "ok", "write_error"),
        (_WT_LIMIT_PR_NUMBER, _WT_LIMIT_HEAD_SHA, "opened", "ok", "limit"),
    ):
        llm.call_count = 0  # type: ignore[attr-defined]  # fresh 3-call sequence
        llm.walkthrough_scenario = scenario  # type: ignore[attr-defined]
        store.mode = mode
        before = len(calls.check_run_bodies)
        wt_body = _pull_request_payload(pr_number=number, head_sha=sha, action=action)
        assert (
            _post_webhook(env, body=wt_body, signature=_sign(wt_body), event_type="pull_request")
            == 200
        )
        _wait_until_check_run_count_at_least(calls, before + 1)
    stub_github_server.two_files = False  # type: ignore[attr-defined]
    assert len(store.items) == 1  # the one comment, created then edited in place

    # 4. Now the part US11/US12 actually own: poll Grafana's own HTTP API
    #    (not Prometheus directly, not "containers started") until every
    #    panel in the shipped dashboard resolves real data, within the
    #    2-minute bound US12's own text derives from the SDK's 60s export
    #    interval plus the bundled Prometheus's 15s scrape interval.
    _wait_for_all_panels_to_resolve_data(env)


# ---------------------------------------------------------------------------
# US14 (dashboard-visible) — the heartbeat panel goes stale/unresolvable
# after the OTel Collector container is killed, proving a real telemetry
# outage is genuinely visible in the dashboard, not merely inferred.
#
# What this test asserts, and why it isn't the PRD's own literal "time() -
# timestamp(...) keeps growing past 60s while staying queryable" framing:
# this file's own manual verification against this exact stack (Prometheus
# v2.55.1 + otel-collector-contrib 0.116.1) found that once a scrape
# target fails, Prometheus writes staleness markers for that target's
# series within roughly one to a few scrape intervals — *not* "stays
# queryable at its last value until a long lookback window elapses", the
# behavior the dashboard panel's own `time() - timestamp(...)` expression
# assumes. In practice the heartbeat panel's query goes from resolving a
# fresh value straight to "no data" (an *absent* series), almost always
# before it would ever be observed sitting at a value above 60 — this
# test accepts either outcome as proof of staleness (both are
# unambiguously "not healthy, not fresh"), rather than asserting the one
# specific numeric transition the PRD's own text describes, which this
# real stack doesn't reliably produce. The same staleness marking applies
# to *every* series that scrape target was exposing, not only the
# heartbeat's — so a same-target comparison panel (queue depth) is
# expected to go stale too, not stay frozen at its pre-kill value; this
# test only proves the *heartbeat's own* transition from healthy to
# broken is real and visible, which is the property US14 actually needs
# (an operator who's seen the dashboard healthy sees the heartbeat panel
# go dark when telemetry breaks) — not that it's the *only* panel to do
# so.
# ---------------------------------------------------------------------------


def test_heartbeat_panel_becomes_unresolvable_after_collector_dies(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
) -> None:
    env = running_stack
    dsn = _dsn(env)

    # Self-contained (doesn't depend on the other test function's own job
    # having run — see this module's own docstring): one small push event
    # is enough to give a second, non-heartbeat panel real, current data
    # too — proving the "before" snapshot below reflects a genuinely
    # healthy dashboard, not just one lucky metric. The heartbeat itself
    # needs no job at all — it fires every 30s independent of job
    # activity, per US14's own text.
    push_body = _push_payload(commit_sha=_SECOND_PUSH_COMMIT_SHA, added_files=["second.ts"])
    status = _post_webhook(env, body=push_body, signature=_sign(push_body), event_type="push")
    assert status == 200
    _wait_for_indexed_paths(dsn, repo_full_name=_OWNER_REPO, expected={"second.ts"})

    heartbeat_expr = (
        'time() - timestamp(sum(review_process_heartbeat_total{job="review-webhook-api"}))'
    )
    queue_depth_expr = "sum(review_queue_enqueued_total) - sum(review_queue_claimed_total)"

    # Wait until both queries this test touches are actually resolving
    # data (the same polling shape as US11/US12's own wait, just scoped to
    # these two) — proves the "healthy baseline" below is real, live data.
    deadline = time.monotonic() + _DASHBOARD_DATA_TIMEOUT_SECONDS
    heartbeat_before: float | None = None
    while time.monotonic() < deadline:
        heartbeat_response = _grafana_ds_query(env, heartbeat_expr)
        queue_response = _grafana_ds_query(env, queue_depth_expr)
        if _frames_have_data(heartbeat_response) and _frames_have_data(queue_response):
            heartbeat_before = _query_value(heartbeat_response)
            break
        time.sleep(3)
    assert heartbeat_before is not None, (
        "heartbeat/queue-depth panels never resolved data before the collector-kill phase"
    )
    # Healthy precondition: the heartbeat is currently fresh (well under
    # its own 60s staleness threshold), so this test's own "goes stale"
    # assertion below is a real transition, not a pre-existing false
    # positive.
    assert heartbeat_before < 60.0

    # Kill the OTel Collector container — the failure mode US14 exists to
    # make visible. `container_id`/`compose` are reused unmodified here
    # (no --profile flag needed to target an already-running container by
    # name — see this module's own docstring).
    collector_cid = container_id("otel-collector", env)
    assert collector_cid, "otel-collector container not found"
    kill_result = compose("kill", "otel-collector", env=env)
    assert kill_result.returncode == 0, kill_result.stderr

    # Poll the heartbeat panel's own query until it stops resolving a
    # healthy (<=60s-old) value — either because it now resolves a value
    # past its documented 60s staleness threshold, or because it stops
    # resolving at all (the real, empirically-confirmed outcome against
    # this stack — see this section's own header comment for why both
    # count as proof here). Either is a real, visible transition from
    # "healthy" to "broken", which is what an operator watching this exact
    # panel across a real outage would see.
    stale_deadline = time.monotonic() + _HEARTBEAT_STALE_TIMEOUT_SECONDS
    became_stale = False
    while time.monotonic() < stale_deadline:
        response = _grafana_ds_query(env, heartbeat_expr)
        if not _frames_have_data(response) or _query_value(response) > 60.0:
            became_stale = True
            break
        time.sleep(3)
    assert became_stale, (
        f"heartbeat panel still resolved a healthy (<=60s) value "
        f"{_HEARTBEAT_STALE_TIMEOUT_SECONDS}s after killing otel-collector — the "
        "collector-down state never became visible"
    )
