"""Phase 10 closing E2E (docs/plans/plan-phase-10.md Phase 7, spec Unit F): the
default branch is indexed through the Webhook API's real `push` path (so the
real worker writes the clone fingerprints), then pull requests are reviewed
against it over the full Compose stack, with GitHub and the LLM Gateway
stubbed. Covers a renamed-identifier copy per language, a line edited inside a
pre-existing clone, a renamed file, a clone-index outage (fail-open), the step
switched off (output equals the baseline) and a repo with no fingerprint data yet.

Stub servers, webhook helpers and scenario types are reused from
`test_review_pipeline.py`; the fixture sources are the engine's own
`indexing/testdata/clone` corpus. Test order matters: the no-data case runs
before the push, the outage case needs the push, and the switch-off case recreates
the Worker so it runs last.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Iterator
from http.server import HTTPServer

import asyncpg
import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)
from tests.e2e.test_review_pipeline import (
    _INSTALLATION_ID,
    _OWNER_REPO,
    REPO_ROOT,
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

_REVIEW_TIMEOUT_SECONDS = 180.0
_INDEX_TIMEOUT_SECONDS = 120.0
_CORPUS = REPO_ROOT / "indexing" / "testdata" / "clone"
_LANGUAGES = ("ts", "php", "go")
_LEGACY_COPY = "src/zz_legacy_copy.ts"  # a pre-existing clone of src/original.ts


def _corpus(name: str) -> bytes:
    return (_CORPUS / name).read_bytes()


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def _tree_entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "type": "blob",
        "path": path,
        "mode": "100644",
        "sha": _blob_sha(content),
        "size": len(content),
    }


def _added_patch(content: bytes) -> str:
    lines = content.decode().splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


# The default-branch repo: the original function in each language, plus a
# pre-existing renamed-identifier copy of the TypeScript one.
_BASE: dict[str, bytes] = {
    **{f"src/original.{ext}": _corpus(f"original.{ext}") for ext in _LANGUAGES},
    _LEGACY_COPY: _corpus("renamed.ts"),
}


def _scenario(
    files: list[dict[str, object]], extra: dict[str, bytes], *, with_base: bool = True
) -> _GitHubScenario:
    contents = {**(_BASE if with_base else {}), **extra}
    return _GitHubScenario(
        files=files,
        tree=[_tree_entry(path, content) for path, content in contents.items()],
        blobs={_blob_sha(content): content for content in contents.values()},
    )


def _copies_scenario(*, with_base: bool = True) -> _GitHubScenario:
    extra = {f"src/copy.{ext}": _corpus(f"renamed.{ext}") for ext in _LANGUAGES}
    files: list[dict[str, object]] = [
        {"filename": path, "status": "added", "patch": _added_patch(content)}
        for path, content in extra.items()
    ]
    return _scenario(files, extra, with_base=with_base)


def _edit_in_clone_scenario() -> _GitHubScenario:
    old = _BASE[_LEGACY_COPY].decode()
    new = old.replace("let gross = 0;", "let gross = 1;", 1)
    line = next(i for i, text in enumerate(old.splitlines(), 1) if "let gross = 0;" in text)
    patch = f"@@ -{line},1 +{line},1 @@\n-    let gross = 0;\n+    let gross = 1;"
    files: list[dict[str, object]] = [
        {"filename": _LEGACY_COPY, "status": "modified", "patch": patch}
    ]
    return _scenario(files, {_LEGACY_COPY: new.encode()})


def _rename_scenario() -> _GitHubScenario:
    # Identical to src/original.go, so it would match its own old path's rows.
    # (Real GitHub omits the patch of a pure rename; one is given here so the
    # file is eligible and only the previous_path exclusion keeps it quiet.)
    content = _BASE["src/original.go"]
    files: list[dict[str, object]] = [
        {
            "filename": "src/moved.go",
            "previous_filename": "src/original.go",
            "status": "renamed",
            "patch": _added_patch(content),
        }
    ]
    base = {path: c for path, c in _BASE.items() if path != "src/original.go"}
    return _GitHubScenario(
        files=files,
        tree=[_tree_entry(p, c) for p, c in {**base, "src/moved.go": content}.items()],
        blobs={_blob_sha(c): c for c in [*base.values(), content]},
    )


def _env(*, clone_enabled: bool) -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WEBHOOK_API_INDEXING_ENABLED="true",
        # Every PR webhook also enqueues an index job; the default 5/min would drop the 5th case.
        REVIEW_WEBHOOK_API_INDEX_ENQUEUE_RATE_LIMIT_PER_MINUTE="1000",
        REVIEW_WORKER_CLONE_DETECTION_ENABLED="true" if clone_enabled else "false",
    )


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> Iterator[dict[str, str]]:
    env = _env(clone_enabled=True)
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


def _review(
    env: dict[str, str], github: HTTPServer, llm: HTTPServer, scenario: _GitHubScenario
) -> tuple[list[dict[str, str]], dict[str, object]]:
    _reset_llm_scenario(llm)
    _reset_github_scenario(github, scenario)
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
    match = re.search(r"^_source: (.+)_$", comment["body"], re.MULTILINE)
    assert match, f"comment has no source line: {comment['body']!r}"
    return match.group(1).replace("\\", "")


def _clone_comments(comments: list[dict[str, str]]) -> list[dict[str, str]]:
    return [c for c in comments if _source_of(c) == "clone_detection"]


def _dsn(env: dict[str, str]) -> str:
    return (
        f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@localhost:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
    )


async def _execute(env: dict[str, str], statement: str) -> None:
    conn = await asyncpg.connect(_dsn(env))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


def _index_default_branch(env: dict[str, str], github: HTTPServer) -> None:
    _reset_github_scenario(github, _scenario([], {}))
    commit_sha = "c" * 40
    payload = json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO, "default_branch": "main"},
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": commit_sha,
            "commits": [{"added": sorted(_BASE), "removed": [], "modified": []}],
            "forced": False,
        }
    ).encode()
    assert _post_webhook(env, body=payload, signature=_sign(payload), event_type="push") == 200

    dsn = _dsn(env)

    async def fingerprinted() -> set[str]:
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT f.file_path FROM index_clone_fingerprint f "
                "JOIN repo r ON r.id = f.repo_id WHERE r.full_name = $1 AND f.pr_number = 0",
                _OWNER_REPO,
            )
            return {row["file_path"] for row in rows}
        finally:
            await conn.close()

    deadline = time.monotonic() + _INDEX_TIMEOUT_SECONDS
    seen: set[str] = set()
    while time.monotonic() < deadline:
        seen = asyncio.run(fingerprinted())
        if set(_BASE) <= seen:
            return
        time.sleep(1)
    raise AssertionError(f"clone fingerprints never written for {sorted(_BASE)}; saw {seen}")


def test_repo_without_fingerprint_data_still_gets_a_completed_review(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    # Nothing has been indexed yet: the failure-mode case (testing.md).
    comments, check_run = _review(
        running_stack,
        stub_github_server,
        stub_llm_review_server,
        _copies_scenario(with_base=False),
    )

    assert _clone_comments(comments) == []
    assert check_run["conclusion"] == "neutral"
    logs = compose("logs", "--no-color", "worker", env=running_stack).stdout
    assert "no_matches" in logs


def test_renamed_identifier_copy_in_each_language_is_flagged_with_its_origin(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    _index_default_branch(running_stack, stub_github_server)

    comments, _ = _review(
        running_stack, stub_github_server, stub_llm_review_server, _copies_scenario()
    )

    flagged = {c["path"]: c for c in _clone_comments(comments)}
    assert set(flagged) == {f"src/copy.{ext}" for ext in _LANGUAGES}
    for ext in _LANGUAGES:
        assert f"src/original.{ext}" in flagged[f"src/copy.{ext}"]["body"].replace("\\", "")


def test_editing_a_line_inside_a_pre_existing_clone_is_not_flagged(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    comments, check_run = _review(
        running_stack, stub_github_server, stub_llm_review_server, _edit_in_clone_scenario()
    )

    assert _clone_comments(comments) == []
    assert check_run["conclusion"] == "neutral"


def test_renamed_file_is_not_flagged_against_its_old_path(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    comments, _ = _review(
        running_stack, stub_github_server, stub_llm_review_server, _rename_scenario()
    )

    assert _clone_comments(comments) == []


def test_clone_index_outage_fails_open_and_the_review_still_completes(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    # US-C14 on the real stack: with the fingerprint table unreadable the step
    # must degrade to "no clone findings", never fail the job or the check run.
    asyncio.run(
        _execute(running_stack, "ALTER TABLE index_clone_fingerprint RENAME TO index_clone_fp_off")
    )
    try:
        comments, check_run = _review(
            running_stack, stub_github_server, stub_llm_review_server, _copies_scenario()
        )
        logs = compose("logs", "--no-color", "worker", env=running_stack).stdout
    finally:
        asyncio.run(
            _execute(
                running_stack, "ALTER TABLE index_clone_fp_off RENAME TO index_clone_fingerprint"
            )
        )

    assert _clone_comments(comments) == []
    assert check_run["conclusion"] == "neutral"
    assert "failed_open_query" in logs


def test_step_disabled_reproduces_the_baseline_output(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    enabled, _ = _review(
        running_stack, stub_github_server, stub_llm_review_server, _copies_scenario()
    )
    assert _clone_comments(enabled)

    env = _env(clone_enabled=False)
    result = compose("up", "-d", "--no-deps", "worker", env=env)  # recreates only the Worker
    assert result.returncode == 0, f"worker recreate failed:\n{result.stdout}\n{result.stderr}"
    wait_for_log_line("worker", env, "worker started")
    disabled, _ = _review(env, stub_github_server, stub_llm_review_server, _copies_scenario())

    def key(c: dict[str, str]) -> tuple[str, object, str]:
        return (c["path"], c.get("line"), c["body"])

    baseline = sorted(key(c) for c in enabled if _source_of(c) != "clone_detection")
    assert sorted(key(c) for c in disabled) == baseline
