"""Phase 9 closing E2E (docs/plans/plan-phase-9.md Phase 8, spec Unit H): one
defect per language goes in through the Webhook API's real entrypoint, the
full Compose stack (Webhook API + the worker-only `worker-runtime` image +
Postgres) runs the five new engines for real, and exactly the expected
inline comments come out against a stubbed GitHub. A second case recreates
only the Worker with every new engine switched off and proves the output is
what it was before the expansion (no new-engine comments; the
Gitleaks-detected secret is still posted).

Stub servers, webhook helpers and scenario types are reused from
`test_review_pipeline.py` rather than copied; only the Compose environment and
the multi-language scenario are specific to this file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from http.server import HTTPServer

import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)
from tests.e2e.test_review_pipeline import (
    STUB_GITHUB_PORT,
    STUB_LLM_REVIEW_PORT,
    _CallLog,
    _fresh_pr,
    _GitHubScenario,
    _post_webhook,
    _reset_github_scenario,
    _reset_llm_scenario,
    _sign,
    _wait_until_calls_received,
    _webhook_payload,
    stub_github_server,  # noqa: F401  (pytest fixture, re-exported into this module)
    stub_llm_review_server,  # noqa: F401  (pytest fixture, re-exported into this module)
)

_REVIEW_TIMEOUT_SECONDS = 180.0  # cold Node/Go/PHP/Trivy caches on the first job

_ENGINE_FLAGS = (
    "REVIEW_WORKER_STATIC_ANALYSIS_RUFF_ENABLED",
    "REVIEW_WORKER_STATIC_ANALYSIS_ESLINT_ENABLED",
    "REVIEW_WORKER_STATIC_ANALYSIS_GOLANGCI_LINT_ENABLED",
    "REVIEW_WORKER_STATIC_ANALYSIS_TRIVY_ENABLED",
    "REVIEW_WORKER_STATIC_ANALYSIS_PHPSTAN_ENABLED",
)

# One correctness defect per language, each taken from that engine's own
# integration-test fixtures (adapter/analyzers/test_<engine>.py).
_SOURCES: dict[str, bytes] = {
    "app.py": b"import os\n",
    "web.js": b"export function f() {\n  return undefinedName;\n}\n",
    "pkg.go": b'package pkg\n\nfunc Fill() {\n\tvar m map[string]int\n\tm["a"] = 1\n}\n',
    "infra.tf": b'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n  acl    = "public-read"\n}\n',
    "index.php": b"<?php\nfunction f() {\n    return $undefined;\n}\n",
}
# Reported by Gitleaks regardless of language; present in both cases below.
_SECRET = ("notes.txt", b'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"\n')

# (path -> (engine source tag, a rule id that engine must report there))
_EXPECTED_RULES = {
    "app.py": ("ruff", "F401"),
    "web.js": ("eslint", "no-undef"),
    "pkg.go": ("golangci_lint", "SA5000"),
    "index.php": ("phpstan", "variable.undefined"),
}
_TERRAFORM = "infra.tf"


def _multi_language_scenario() -> _GitHubScenario:
    files: list[dict[str, object]] = []
    tree: list[dict[str, object]] = []
    blobs: dict[str, bytes] = {}
    for index, (path, content) in enumerate([*_SOURCES.items(), _SECRET]):
        blob_sha = f"{index + 1:040x}"
        lines = content.decode().splitlines()
        patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
        files.append({"filename": path, "status": "added", "patch": patch})
        tree.append(
            {"type": "blob", "path": path, "mode": "100644", "sha": blob_sha, "size": len(content)}
        )
        blobs[blob_sha] = content
    return _GitHubScenario(files=files, tree=tree, blobs=blobs)


def _env(*, engines_enabled: bool) -> dict[str, str]:
    overrides = {
        "REVIEW_WORKER_LLM_ENDPOINT": f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        "REVIEW_WORKER_GITHUB_API_BASE_URL": f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        "REVIEW_WORKER_POLL_INTERVAL_SECONDS": "0.2",
    }
    if not engines_enabled:
        overrides.update(dict.fromkeys(_ENGINE_FLAGS, "false"))
    return base_compose_env(**overrides)


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> Iterator[dict[str, str]]:
    env = _env(engines_enabled=True)
    compose_down(env)
    result = compose("up", "-d", "--build", env=env)
    assert result.returncode == 0, f"compose up failed:\n{result.stdout}\n{result.stderr}"
    try:
        wait_for_healthy("webhook_api", env)
        wait_for_log_line("worker", env, "worker started")
        yield env
    finally:
        print(compose("logs", "--no-color", env=env).stdout)
        compose_down(env)


def _review_one_pr(
    env: dict[str, str], github: HTTPServer, llm: HTTPServer
) -> tuple[list[dict[str, str]], dict[str, object]]:
    _reset_llm_scenario(llm)
    _reset_github_scenario(github, _multi_language_scenario())
    pr_number, head_sha = _fresh_pr()
    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)
    assert _post_webhook(env, body=body, signature=_sign(body)) == 200

    calls: _CallLog = github.calls  # type: ignore[attr-defined]
    _wait_until_calls_received(calls, timeout=_REVIEW_TIMEOUT_SECONDS)
    comments: list[dict[str, str]] = []
    if calls.review_bodies:
        raw = calls.review_bodies[-1]["comments"]
        assert isinstance(raw, list)
        comments = raw
    return comments, calls.check_run_bodies[-1]


def _source_of(comment: dict[str, str]) -> str:
    # Rendered as "_source: golangci\\_lint_" (italic wrapper plus escaped underscores).
    match = re.search(r"^_source: (.+)_$", comment["body"], re.MULTILINE)
    assert match, f"comment has no source line: {comment['body']!r}"
    return match.group(1).replace("\\", "")


def test_one_defect_per_language_posts_exactly_the_expected_comments(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    comments, check_run = _review_one_pr(running_stack, stub_github_server, stub_llm_review_server)

    by_path: dict[str, list[dict[str, str]]] = {}
    for comment in comments:
        by_path.setdefault(comment["path"], []).append(comment)

    # Exactly the files with a defect (plus the secret) get comments, no others.
    assert set(by_path) == {*_EXPECTED_RULES, _TERRAFORM, _SECRET[0]}
    for path, (source, rule) in _EXPECTED_RULES.items():
        assert {_source_of(c) for c in by_path[path]} == {source}, path
        assert any(f"({rule})" in c["body"].replace("\\", "") for c in by_path[path]), (path, rule)
    assert {_source_of(c) for c in by_path[_TERRAFORM]} == {"trivy"}
    assert {_source_of(c) for c in by_path[_SECRET[0]]} == {"gitleaks"}
    assert check_run["conclusion"] == "neutral"


def test_all_new_engines_disabled_reproduces_the_pre_expansion_output(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    env = _env(engines_enabled=False)
    result = compose("up", "-d", "--no-deps", "worker", env=env)  # recreates only the Worker
    assert result.returncode == 0, f"worker recreate failed:\n{result.stdout}\n{result.stderr}"
    wait_for_log_line("worker", env, "worker started")

    comments, check_run = _review_one_pr(env, stub_github_server, stub_llm_review_server)

    # Only Gitleaks/Semgrep-attributable output remains: the planted secret.
    assert {c["path"] for c in comments} == {_SECRET[0]}
    assert {_source_of(c) for c in comments} == {"gitleaks"}
    assert check_run["conclusion"] == "neutral"
