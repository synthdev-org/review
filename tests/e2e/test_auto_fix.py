"""Phase 11 closing E2E (docs/plans/plan-phase-11.md Phase 7, spec Unit F): pull
requests go in through the Webhook API's real entrypoint, the full Compose stack
(Webhook API + the worker-only runtime image + Postgres) runs the real analyzers
and the `fix_generation` step, and the stubbed GitHub receives the review. Covers
a suggestion per supported language plus a verified LLM finding, each bad stub
patch (syntax, no-op, secret-shaped, wrong range, re-analysis failure, rubric
false, unparseable) posting its finding with no suggestion, Gitleaks and Trivy
findings getting none, and the step switched off reproducing today's output.

Stub servers, webhook helpers and scenario types are reused from
`test_review_pipeline.py`, whose stub LLM answers the fix calls by their prompt
marker (plan Assumption 15). Test order matters: the switch-off case recreates
the Worker, so it runs last (as in Phase 10).

Not covered here, by design: `clone_detection` and unverified LLM findings get no
fix too, but both are proven at the integration tier
(`review/test_fix_candidates.py`, `review/test_fix_generation_step.py`); a clone
finding needs a pushed index and an unverified LLM finding needs verification
switched off, neither worth another Compose recreate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
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

_REVIEW_TIMEOUT_SECONDS = 180.0  # cold Node/Go/PHP caches on the first job
_SECRET_LINE = 'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"'


@dataclass(frozen=True)
class _Fix:
    start: int
    end: int
    replacement: str

    @property
    def generation(self) -> str:
        return json.dumps(
            {"start_line": self.start, "end_line": self.end, "replacement": self.replacement}
        )


_RUBRIC_OK = json.dumps(
    {"resolves": True, "minimal": True, "safe": True, "compiles_in_context": True}
)
_RUBRIC_NOT_RESOLVED = json.dumps(
    {"resolves": False, "minimal": True, "safe": True, "compiles_in_context": True}
)

# One defect per language, each taken from that engine's own integration-test
# fixtures (adapter/analyzers/test_<engine>.py), with a fix its analyzer accepts.
_PY = b"import os\nx = 1\n"
_ANALYZER_FILES: dict[str, tuple[bytes, _Fix]] = {
    "app.py": (_PY, _Fix(1, 1, "")),
    "web.js": (
        b"export function f() {\n  return undefinedName;\n}\n",
        _Fix(2, 2, "  return 1;"),
    ),
    "pkg.go": (
        b'package pkg\n\nfunc Fill() {\n\tvar m map[string]int\n\tm["a"] = 1\n}\n',
        _Fix(4, 5, '\tm := map[string]int{}\n\tm["a"] = 1'),
    ),
    "index.php": (
        b"<?php\nfunction f() {\n    return $undefined;\n}\n",
        _Fix(3, 3, "    return 1;"),
    ),
}
_LLM_FILE = b"def add(a, b):\n    return a + b\n"
_LLM_FINDING = json.dumps(
    {
        "findings": [
            {
                "file": "llm_target.py",
                "line": 2,
                "severity": "medium",
                "message": "Addition is not commutative for non-numeric operands.",
            }
        ]
    }
)
_LLM_FIX = _Fix(2, 2, "    return b + a")
_INELIGIBLE_FILES = {
    "notes.txt": (_SECRET_LINE + "\n").encode(),  # Gitleaks
    "infra.tf": b'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n  acl    = "public-read"\n}\n',
}

# Bad stub patches, one file each; every finding is Ruff's F401 on line 1.
_BAD_FIXES: dict[str, tuple[_Fix | None, str]] = {
    "bad_syntax.py": (_Fix(1, 1, "import (os"), "syntax_error"),
    "bad_noop.py": (_Fix(1, 1, "import os"), "noop"),
    "bad_secret.py": (_Fix(1, 1, _SECRET_LINE), "redaction"),
    "bad_range.py": (_Fix(2, 2, "x = 2"), "out_of_range"),
    "bad_reanalysis.py": (_Fix(1, 1, "import os, sys"), "reanalysis_failed"),
    "bad_parse.py": (None, "parse_error"),  # no stub entry: the reply is not JSON
}
_BAD_RUBRIC_FILE = "llm_rubric.py"


def _scenario(contents: dict[str, bytes]) -> _GitHubScenario:
    files: list[dict[str, object]] = []
    tree: list[dict[str, object]] = []
    blobs: dict[str, bytes] = {}
    for index, (path, content) in enumerate(contents.items()):
        blob_sha = f"{index + 1:040x}"
        lines = content.decode().splitlines()
        patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
        files.append({"filename": path, "status": "added", "patch": patch})
        tree.append(
            {"type": "blob", "path": path, "mode": "100644", "sha": blob_sha, "size": len(content)}
        )
        blobs[blob_sha] = content
    return _GitHubScenario(files=files, tree=tree, blobs=blobs)


def _supported_contents() -> dict[str, bytes]:
    return {
        **{path: content for path, (content, _) in _ANALYZER_FILES.items()},
        "llm_target.py": _LLM_FILE,
        **_INELIGIBLE_FILES,
    }


def _supported_fixes() -> dict[str, _Fix]:
    return {
        **{path: fix for path, (_, fix) in _ANALYZER_FILES.items()},
        "llm_target.py": _LLM_FIX,
    }


def _fix_scenario(fixes: dict[str, _Fix]) -> dict[str, dict[str, str]]:
    return {
        path: {"generation": fix.generation, "rubric": _RUBRIC_OK} for path, fix in fixes.items()
    }


def _env(*, fix_enabled: bool) -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WORKER_FIX_GENERATION_ENABLED="true" if fix_enabled else "false",
        # Above the default 5 so no case here is cut short by the per-job cap.
        REVIEW_WORKER_FIX_GENERATION_MAX_FIXES_PER_JOB="10",
    )


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> Iterator[dict[str, str]]:
    env = _env(fix_enabled=True)
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


@dataclass(frozen=True)
class _Review:
    comments: list[dict[str, str]]
    check_run: dict[str, object]
    github_calls: list[str]
    fix_calls: list[str]


def _review(
    env: dict[str, str],
    github: HTTPServer,
    llm: HTTPServer,
    contents: dict[str, bytes],
    *,
    fixes: dict[str, dict[str, str]],
    finding: str,
) -> _Review:
    _reset_llm_scenario(llm, review_generation_content=finding, fix_scenario=fixes)
    _reset_github_scenario(github, _scenario(contents))
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
    return _Review(
        comments=comments,
        check_run=calls.check_run_bodies[-1],
        github_calls=list(calls.calls),
        fix_calls=list(llm.fix_calls),  # type: ignore[attr-defined]
    )


def _source_of(comment: dict[str, str]) -> str:
    match = re.search(r"^_source: (.+)_$", comment["body"], re.MULTILINE)
    assert match, f"comment has no source line: {comment['body']!r}"
    return match.group(1).replace("\\", "")


def _has_suggestion(comment: dict[str, str]) -> bool:
    return "```suggestion" in comment["body"]


@pytest.fixture(scope="module")
def supported_review(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> _Review:
    return _review(
        running_stack,
        stub_github_server,
        stub_llm_review_server,
        _supported_contents(),
        fixes=_fix_scenario(_supported_fixes()),
        finding=_LLM_FINDING,
    )


def test_each_supported_language_and_a_verified_llm_finding_get_one_matching_suggestion(
    supported_review: _Review,
) -> None:
    by_path = {c["path"]: c for c in supported_review.comments if _has_suggestion(c)}

    assert set(by_path) == set(_supported_fixes())
    for path, fix in _supported_fixes().items():
        comment: dict[str, object] = dict(by_path[path])
        assert comment["line"] == fix.end, path
        assert comment.get("start_line") == (fix.start if fix.start < fix.end else None), path
        body = str(comment["body"])
        assert body.count("```suggestion") == 1, path
        block = (
            f"```suggestion\n{fix.replacement}\n```" if fix.replacement else "```suggestion\n```"
        )
        assert block in body, path
    assert supported_review.check_run["conclusion"] == "neutral"


def test_gitleaks_and_trivy_findings_get_no_fix(supported_review: _Review) -> None:
    sources = {_source_of(c) for c in supported_review.comments if not _has_suggestion(c)}
    assert {"gitleaks", "trivy"} <= sources
    assert {c["path"] for c in supported_review.comments if _source_of(c) in {"gitleaks", "trivy"}}
    assert not any(
        _has_suggestion(c)
        for c in supported_review.comments
        if _source_of(c) in {"gitleaks", "trivy"}
    )
    attempted = {call.split(":", 1)[1] for call in supported_review.fix_calls}
    assert attempted.isdisjoint(_INELIGIBLE_FILES)


def test_the_stub_github_receives_only_the_review_and_check_run_writes(
    supported_review: _Review,
) -> None:
    writes = [call for call in supported_review.github_calls if not call.startswith("GET ")]
    assert all(
        re.fullmatch(
            r"POST /(app/installations/\d+/access_tokens"
            r"|repos/.+/pulls/\d+/reviews|repos/.+/check-runs)",
            call,
        )
        for call in writes
    ), writes
    assert sum("/reviews" in call for call in writes) == 1
    assert sum(call.endswith("/check-runs") for call in writes) == 1


def test_every_bad_patch_posts_the_finding_without_a_suggestion(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    contents = dict.fromkeys(_BAD_FIXES, _PY) | {_BAD_RUBRIC_FILE: _LLM_FILE}
    fixes = {
        path: {"generation": fix.generation, "rubric": _RUBRIC_OK}
        for path, (fix, _) in _BAD_FIXES.items()
        if fix is not None
    }
    fixes[_BAD_RUBRIC_FILE] = {"generation": _LLM_FIX.generation, "rubric": _RUBRIC_NOT_RESOLVED}
    finding = _LLM_FINDING.replace("llm_target.py", _BAD_RUBRIC_FILE)

    review = _review(
        running_stack,
        stub_github_server,
        stub_llm_review_server,
        contents,
        fixes=fixes,
        finding=finding,
    )

    assert {c["path"] for c in review.comments} == set(contents)
    assert not any(_has_suggestion(c) for c in review.comments)
    assert review.check_run["conclusion"] == "neutral"
    logs = compose("logs", "--no-color", "worker", env=running_stack).stdout
    for _, reason in _BAD_FIXES.values():
        assert reason in logs, reason
    assert "rubric_failed" in logs


def test_step_disabled_reproduces_the_baseline_output(
    running_stack: dict[str, str],
    supported_review: _Review,
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    assert any(_has_suggestion(c) for c in supported_review.comments)

    env = _env(fix_enabled=False)
    result = compose("up", "-d", "--no-deps", "worker", env=env)  # recreates only the Worker
    assert result.returncode == 0, f"worker recreate failed:\n{result.stdout}\n{result.stderr}"
    wait_for_log_line("worker", env, "worker started")
    disabled = _review(
        env,
        stub_github_server,
        stub_llm_review_server,
        _supported_contents(),
        fixes=_fix_scenario(_supported_fixes()),
        finding=_LLM_FINDING,
    )

    assert disabled.fix_calls == []

    def baseline(comment: dict[str, str]) -> tuple[str, str]:
        # What today's output is for the same finding: the body without the block.
        return (
            comment["path"],
            re.sub(r"\n\n```suggestion\n.*?```$", "", comment["body"], flags=re.S),
        )

    assert sorted((c["path"], c["body"]) for c in disabled.comments) == sorted(
        baseline(c) for c in supported_review.comments
    )
    assert all("start_line" not in c for c in disabled.comments)
