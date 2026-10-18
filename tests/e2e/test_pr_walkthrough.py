"""Phase 12 closing E2E (docs/plans/plan-phase-12.md Phase 5, spec Unit D): pull
requests go in through the Webhook API's real entrypoint, the full Compose stack
(Webhook API + Worker + Postgres) runs the real review pipeline with the
`walkthrough` step on, and the stub GitHub holds the PR's issue comments.

Covers the goal (a push creates one walkthrough comment, the next push edits that
same comment), US-W8 (an LLM timeout, a bad LLM shape, a GitHub 5xx and the
500-comment limit leave the findings and check-run exactly as a successful
walkthrough run leaves them, and an existing comment untouched), US-W3 (a one-file
PR makes no call and no write) and US-W9 (the flag off: the stub receives only the
review and check-run writes).

Stub servers, webhook helpers and scenario types are reused from
`test_review_pipeline.py`, whose shared `_StubGitHubHandler` serves the issue-comment
routes and whose stub LLM answers the walkthrough call by its prompt marker. Test
order matters: the flag-off case recreates the Worker, so it runs last (as in
Phase 11).
"""

from __future__ import annotations

import time
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
    _IssueComments,
    _post_webhook,
    _reset_github_scenario,
    _reset_llm_scenario,
    _sign,
    _webhook_payload,
    stub_github_server,  # noqa: F401  (pytest fixture, re-exported into this module)
    stub_llm_review_server,  # noqa: F401  (pytest fixture, re-exported into this module)
)

_REVIEW_TIMEOUT_SECONDS = 180.0
_MARKER = "<!-- review-walkthrough:v1 -->"
_FILES = {
    "app.py": b"import os\nx = 1\n",  # Ruff F401: gives the review a finding
    "util.py": b"def add(a, b):\n    return a + b\n",
}


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


def _env(*, walkthrough_enabled: bool) -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WORKER_WALKTHROUGH_ENABLED="true" if walkthrough_enabled else "false",
        # The smallest legal value (LLM timeout 10 s + 15), so the timeout case is quick.
        REVIEW_WORKER_WALKTHROUGH_STEP_TIMEOUT_SECONDS="25",
    )


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> Iterator[dict[str, str]]:
    env = _env(walkthrough_enabled=True)
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
class _Outcome:
    review: list[tuple[object, object, object]]
    check_run: tuple[object, object]
    comments: list[str]  # walkthrough comment bodies on the PR afterwards
    comment_writes: list[str]
    github_calls: list[str]
    walkthrough_calls: int


def _push(
    env: dict[str, str],
    github: HTTPServer,
    llm: HTTPServer,
    *,
    pr_number: int,
    head_sha: str,
    action: str,
    check_runs_before: int,
) -> _Outcome:
    # The stub answers intent/review/verification by call position within a push.
    llm.call_count = 0  # type: ignore[attr-defined]
    body = _webhook_payload(action=action, pr_number=pr_number, head_sha=head_sha)
    assert _post_webhook(env, body=body, signature=_sign(body)) == 200
    calls: _CallLog = github.calls  # type: ignore[attr-defined]
    deadline = time.monotonic() + _REVIEW_TIMEOUT_SECONDS
    while len(calls.check_run_bodies) <= check_runs_before:
        assert time.monotonic() < deadline, f"no check-run in time (calls: {calls.calls!r})"
        time.sleep(0.5)
    store: _IssueComments = github.issue_comments  # type: ignore[attr-defined]
    # One review per push: a push that posted none would otherwise read another push's.
    assert len(calls.review_bodies) == check_runs_before + 1, calls.calls
    review = calls.review_bodies[check_runs_before]
    raw = review.get("comments", [])
    assert isinstance(raw, list)
    check_run = calls.check_run_bodies[check_runs_before]
    return _Outcome(
        review=sorted((c["path"], c.get("line"), c["body"]) for c in raw),
        check_run=(check_run["conclusion"], check_run["output"]),
        comments=store.platform_bodies(),
        comment_writes=list(store.writes),
        github_calls=list(calls.calls),
        walkthrough_calls=llm.walkthrough_calls,  # type: ignore[attr-defined]
    )


def _fresh_review(
    env: dict[str, str],
    github: HTTPServer,
    llm: HTTPServer,
    contents: dict[str, bytes],
    *,
    walkthrough: str = "ok",
    comments_mode: str = "normal",
) -> tuple[_Outcome, int]:
    """One `opened` push on a brand-new PR with empty comments; returns the outcome and PR."""
    _reset_llm_scenario(llm, walkthrough=walkthrough)
    _reset_github_scenario(github, _scenario(contents))
    github.issue_comments = _IssueComments(mode=comments_mode)  # type: ignore[attr-defined]
    pr_number, head_sha = _fresh_pr()
    outcome = _push(
        env,
        github,
        llm,
        pr_number=pr_number,
        head_sha=head_sha,
        action="opened",
        check_runs_before=0,
    )
    return outcome, pr_number


def test_first_push_creates_one_comment_and_second_push_edits_it(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    first, pr_number = _fresh_review(
        running_stack, stub_github_server, stub_llm_review_server, _FILES
    )

    assert len(first.comments) == 1
    body = first.comments[0]
    assert body.startswith(_MARKER)
    assert "`app.py`" in body
    assert "`util.py`" in body
    assert first.review, "the review still carries its finding"
    created = stub_github_server.issue_comments.items[0]["id"]  # type: ignore[attr-defined]

    second_sha = _fresh_pr()[1]
    second = _push(
        running_stack,
        stub_github_server,
        stub_llm_review_server,
        pr_number=pr_number,
        head_sha=second_sha,
        action="synchronize",
        check_runs_before=1,
    )

    items = stub_github_server.issue_comments.items  # type: ignore[attr-defined]
    assert len(items) == 1
    assert items[0]["id"] == created
    assert f"Describes commit `{second_sha}`" in second.comments[0]
    assert any(w.startswith("PATCH ") for w in second.comment_writes)
    assert not any(w.startswith("DELETE ") for w in second.comment_writes)


@pytest.mark.parametrize(
    ("llm_scenario", "comments_mode"),
    [
        ("garbage", "normal"),  # bad shape -> parse_failure
        ("delay", "normal"),  # LLM timeout -> llm_timeout
        ("ok", "write_error"),  # GitHub 5xx -> github_error
    ],
)
def test_a_failed_regeneration_leaves_comment_findings_and_check_run_unchanged(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
    llm_scenario: str,
    comments_mode: str,
) -> None:
    stub_llm_review_server.walkthrough_delay_seconds = 14.0  # type: ignore[attr-defined]
    first, pr_number = _fresh_review(
        running_stack, stub_github_server, stub_llm_review_server, _FILES
    )
    assert len(first.comments) == 1
    before = first.comments[0]

    stub_llm_review_server.walkthrough_scenario = llm_scenario  # type: ignore[attr-defined]
    stub_github_server.issue_comments.mode = comments_mode  # type: ignore[attr-defined]
    failed = _push(
        running_stack,
        stub_github_server,
        stub_llm_review_server,
        pr_number=pr_number,
        head_sha=_fresh_pr()[1],
        action="synchronize",
        check_runs_before=1,
    )

    assert failed.comments == [before]
    assert failed.review == first.review
    assert failed.check_run == first.check_run
    # The second push really reached the walkthrough step (the counter is not reset between
    # pushes; a timed-out call may be retried, so "at least one more").
    assert failed.walkthrough_calls > first.walkthrough_calls
    if comments_mode == "write_error":
        new_writes = failed.comment_writes[len(first.comment_writes) :]
        assert any(w.startswith("PATCH ") for w in new_writes), new_writes


def test_a_pr_with_more_than_500_comments_still_gets_its_review(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    baseline, _ = _fresh_review(running_stack, stub_github_server, stub_llm_review_server, _FILES)
    limited, pr_number = _fresh_review(
        running_stack, stub_github_server, stub_llm_review_server, _FILES, comments_mode="limit"
    )

    # The step ran (one LLM call) and walked exactly the adapter's five pages, no sixth:
    # this is what distinguishes skipped_comment_limit from the step never running.
    assert limited.walkthrough_calls == 1  # the counter is reset per fresh review
    listing = f"/issues/{pr_number}/comments?"
    pages = [c for c in limited.github_calls if c.startswith("GET ") and listing in c]
    assert len(pages) == 5, pages
    assert not any("page=6" in c for c in pages), pages
    assert limited.comments == []
    assert limited.comment_writes == []
    # The step finished with the limit outcome (not some other path that also writes nothing).
    deadline = time.monotonic() + 30
    while True:
        logs = compose("logs", "--no-color", "worker", env=running_stack).stdout
        if any(
            "walkthrough finished" in line and "skipped_comment_limit" in line
            for line in logs.splitlines()
        ):
            break
        assert time.monotonic() < deadline, f"no skipped_comment_limit log line:\n{logs}"
        time.sleep(1)
    assert limited.review == baseline.review
    assert limited.check_run == baseline.check_run


def test_a_one_file_pr_makes_no_walkthrough_call_and_no_comment_write(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    outcome, _ = _fresh_review(
        running_stack,
        stub_github_server,
        stub_llm_review_server,
        {"app.py": _FILES["app.py"]},
    )

    assert outcome.walkthrough_calls == 0
    assert outcome.comment_writes == []
    assert outcome.comments == []
    assert outcome.review


def test_flag_off_the_stub_receives_only_the_review_and_check_run_writes(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,  # noqa: F811
    stub_llm_review_server: HTTPServer,  # noqa: F811
) -> None:
    on, _ = _fresh_review(running_stack, stub_github_server, stub_llm_review_server, _FILES)
    assert on.comments

    env = _env(walkthrough_enabled=False)
    result = compose("up", "-d", "--no-deps", "worker", env=env)  # recreates only the Worker
    assert result.returncode == 0, f"worker recreate failed:\n{result.stdout}\n{result.stderr}"
    wait_for_log_line("worker", env, "worker started")
    off, _ = _fresh_review(env, stub_github_server, stub_llm_review_server, _FILES)

    assert off.walkthrough_calls == 0
    assert off.comments == []
    assert off.comment_writes == []
    writes = [c for c in off.github_calls if not c.startswith("GET ")]
    assert all(
        "/access_tokens" in c or c.endswith("/reviews") or c.endswith("/check-runs") for c in writes
    ), writes
    assert not any("/issues/" in c for c in off.github_calls)
    assert off.review == on.review
    assert off.check_run == on.check_run
