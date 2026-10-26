"""Integration tests for the CI stories (docs/plans/plan-local-mode.md, Phase 5): US-C1 (one
pull-request review), US-C2 (optionally fail the build) and US-C3 (a new push refreshes the review).

The whole local pipeline runs for real, as in ``test_local_review.py`` (real git repository, the
recorded model on a local stub endpoint, real ``gitleaks``). GitHub is a ``respx`` cassette: every
call the run makes to ``api.github.com`` is answered by a route the test controls, and the tests
assert on the exact requests made. Needs ``git`` and ``gitleaks`` on PATH.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from adapter.github.ci_review import CI_COMMENT_MARKER, OUTDATED_MARKER
from adapter.llm.conftest import (
    CapturedRequest,
    RequestResponder,
    StartCapturingStub,
    canned_completion_json,
)
from adapter.local.conftest import Repo, feature_branch_repo, requires_git
from apps.local_cli.main import (
    EXIT_GATE_TRIPPED,
    EXIT_OK,
    EXIT_RUNTIME_FAILURE,
    EXIT_USAGE,
    run_review,
)
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

CANARY = "ghs_CANARYtoken0123456789abcdefghijklmnopqrstuv"
API = "https://api.github.com"
BOT = "github-actions[bot]"
_FINDING_LINE_2 = {
    "file": "app.py",
    "line": 2,
    "severity": "high",
    "message": "result starts as an int, so a list of floats mixes types",
}
_FINDING_LINE_4 = {**_FINDING_LINE_2, "line": 4, "message": "the loop adds without a type check"}


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _model(findings: list[dict[str, Any]]) -> RequestResponder:
    """The recorded model: reports exactly ``findings`` and verifies every one of them."""

    def respond(request: CapturedRequest) -> tuple[int, bytes]:
        system = request.json()["messages"][0]["content"]
        if "verification assistant" in system:
            verdict = {"valid": True, "actionable": True, "specific": True, "valuable": True}
            content = json.dumps(
                {"verdicts": [{"index": i, **verdict} for i in range(len(findings))]}
            )
        elif "summary (2-4 sentences)" in system:
            content = "Rewrites total() as an explicit accumulation loop."
        else:
            content = json.dumps({"findings": findings})
        return 200, canned_completion_json(content=content)

    return respond


class FakeGitHub:
    """The GitHub endpoints one run touches, with healthy defaults a test overrides."""

    def __init__(self, router: respx.MockRouter) -> None:
        repo = "/repos/acme/widgets"
        self.pull = router.get(path=f"{repo}/pulls/7").respond(
            200, json={"head": {"sha": "a" * 40}}
        )
        self.list = router.get(path=f"{repo}/pulls/7/comments").respond(200, json=[])
        self.review = router.post(path=f"{repo}/pulls/7/reviews").respond(200, json={"id": 99})
        self.provenance = router.get(path=f"{repo}/pulls/7/reviews/99/comments").respond(
            200, json=[]
        )
        self.reviews = router.get(path=f"{repo}/pulls/7/reviews").respond(200, json=[])
        self.patch = router.patch(path__regex=rf"{repo}/pulls/comments/\d+").respond(200, json={})
        self.graphql = router.post(path="/graphql").respond(200, json={"data": {}})

    def posted_review(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.review.calls.last.request.content)
        return body


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        # The recorded model is a local stub server; only api.github.com is faked.
        router.route(host="127.0.0.1").pass_through()
        yield FakeGitHub(router)


def _event(tmp_path: Path, *, pull_request: bool = True) -> Path:
    path = tmp_path / "event.json"
    payload: dict[str, Any] = {"action": "synchronize"}
    if pull_request:
        payload["pull_request"] = {"number": 7, "head": {"sha": "a" * 40}}
    path.write_text(json.dumps(payload))
    return path


def _settings(endpoint: str, tmp_path: Path, **overrides: Any) -> LocalSettings:
    values: dict[str, Any] = {
        "llm_endpoint": endpoint,
        "llm_model": "local-model",
        "static_analysis_enabled": False,
        "github_actions": True,
        "github_event_name": "pull_request",
        "github_token": SecretStr(CANARY),
        "github_repository": "acme/widgets",
        "github_event_path": overrides.pop("github_event_path", None) or _event(tmp_path),
        "github_max_retry_attempts": 0,
    }
    return LocalSettings(**{**values, **overrides})


async def _run(
    repo: Repo, settings: LocalSettings, *, output: Path | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(
        settings, base="main", repo_dir=repo.path, out=out, err=err, output=output
    )
    return status, out.getvalue(), err.getvalue()


def _bot_comment(
    comment_id: int, body: str, *, line: int = 2, login: str = BOT, **extra: Any
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "node_id": f"PRRC_{comment_id}",
        "user": {"login": login},
        "path": "app.py",
        "line": line,
        "body": body,
        **extra,
    }


def _assert_no_token(*texts: str) -> None:
    for text in texts:
        assert CANARY not in text


# --- US-C1: one review comment on the pull request ------------------------------------------------


@pytest.mark.asyncio
async def test_us_c1_posts_one_review_anchored_to_the_diff_line_and_logs_the_summary(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)

    status, out, err = await _run(repo, settings)

    assert status == EXIT_OK
    assert gh.review.call_count == 1
    review = gh.posted_review()
    assert review["event"] == "COMMENT"
    [comment] = review["comments"]
    assert (comment["path"], comment["line"]) == ("app.py", 2)
    assert "result starts as an int" in comment["body"]
    assert comment["body"].rstrip().endswith(CI_COMMENT_MARKER)
    assert gh.review.calls.last.request.headers["authorization"] == f"Bearer {CANARY}"
    # The job log has the finding and the run summary, including the CI comment counts.
    assert "app.py:2: high: result starts as an int" in out
    assert "ci comments: 1 posted, 0 updated, 0 outdated, 0 skipped" in err
    _assert_no_token(out, err)


@pytest.mark.asyncio
async def test_a_comment_outside_the_diff_loses_only_that_finding(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    # The whole review is refused, then the first finding alone, then the second is accepted.
    gh.review.side_effect = [
        httpx.Response(422),
        httpx.Response(422),
        httpx.Response(200, json={"id": 99}),
    ]
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2, _FINDING_LINE_4])), tmp_path)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_OK
    assert gh.review.call_count == 3
    assert "1 rejected by GitHub (HTTP 422)" in err
    assert "Traceback" not in err


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", ["list", "review"])
async def test_us_c1_missing_permission_or_a_fork_token_exits_non_zero_with_findings_logged(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub, refused: str
) -> None:
    """A fork pull request's read-only token can read comments but not post (403 on the review);
    a token without access to the pull request fails earlier (403 on the listing)."""
    repo = feature_branch_repo(tmp_path / "repo")
    {"list": gh.list, "review": gh.review}[refused].respond(
        403, json={"message": f"Resource not accessible by integration {CANARY}"}
    )
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)

    status, out, err = await _run(repo, settings)

    assert status == EXIT_RUNTIME_FAILURE
    assert "error: no permission to comment" in err
    assert "pull-requests: write" in err
    assert "Traceback" not in err
    assert "app.py:2: high: result starts as an int" in out  # still in the job log
    _assert_no_token(out, err)


@pytest.mark.asyncio
async def test_us_c1_a_workflow_that_is_not_a_pull_request_event_is_a_usage_error(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(
        stub_llm_capture(_model([])),
        tmp_path,
        github_event_path=_event(tmp_path, pull_request=False),
    )

    status, _, err = await _run(repo, settings)

    assert status == EXIT_USAGE, err
    assert "pull_request" in err
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_c1_actions_without_a_token_is_a_usage_error_before_any_model_call(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model([])), tmp_path, github_token=None)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_USAGE
    assert "GITHUB_TOKEN" in err
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_c1_hostile_finding_text_is_sanitised_before_it_is_posted(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    hostile = {
        **_FINDING_LINE_2,
        "message": "see https://evil.example/x ![i](https://evil.example/p.png) @maintainer "
        "<img src=https://evil.example/b> " + "x" * 5000,
    }
    settings = _settings(stub_llm_capture(_model([hostile])), tmp_path)

    status, _, _ = await _run(repo, settings)

    assert status == EXIT_OK
    body = gh.posted_review()["comments"][0]["body"]
    assert "https://" not in body and "://" not in body.replace(":​//", "")
    assert "![i]" not in body and "<img" not in body
    assert "@maintainer" not in body  # a zero-width space breaks the mention
    assert len(body) < 2500


# --- US-C3: a new push updates the review, not duplicates it -------------------------------------


async def _first_run_body(
    repo: Repo, tmp_path: Path, endpoint: str, gh: FakeGitHub
) -> dict[str, Any]:
    """Run 1 on an empty pull request; returns the comment as GitHub would list it next time."""
    status, _, _ = await _run(repo, _settings(endpoint, tmp_path))
    assert status == EXIT_OK
    posted = gh.posted_review()["comments"][0]
    gh.review.reset()
    return _bot_comment(501, posted["body"], line=posted["line"])


@pytest.mark.asyncio
async def test_us_c3_a_finding_still_present_on_the_same_lines_is_not_posted_again(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model([_FINDING_LINE_2]))
    earlier = await _first_run_body(repo, tmp_path, endpoint, gh)
    gh.list.respond(200, json=[earlier])

    status, out, err = await _run(repo, _settings(endpoint, tmp_path))

    assert status == EXIT_OK
    assert gh.review.call_count == 0
    assert gh.patch.call_count == 0 and gh.graphql.call_count == 0
    assert "ci comments: 0 posted, 0 updated, 0 outdated, 1 skipped" in err
    assert "app.py:2: high:" in out


@pytest.mark.asyncio
async def test_us_c3_a_resolved_finding_is_marked_outdated_and_a_new_one_is_posted_once(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    earlier = await _first_run_body(repo, tmp_path, stub_llm_capture(_model([_FINDING_LINE_2])), gh)
    gh.list.respond(200, json=[earlier])
    endpoint = stub_llm_capture(_model([_FINDING_LINE_4]))

    status, _, err = await _run(repo, _settings(endpoint, tmp_path))

    assert status == EXIT_OK
    assert gh.review.call_count == 1
    [new] = gh.posted_review()["comments"]
    assert new["line"] == 4
    # The line-2 comment is minimised as OUTDATED by its node id, and nothing else is touched.
    assert gh.graphql.call_count == 1
    sent = json.loads(gh.graphql.calls.last.request.content)
    assert sent["variables"] == {"id": "PRRC_501"}
    assert "OUTDATED" in sent["query"]
    # ...and the minimised comment is tagged so the next push does not minimise it again.
    assert gh.patch.call_count == 1
    assert json.loads(gh.patch.calls.last.request.content)["body"].endswith(OUTDATED_MARKER)
    assert "ci comments: 1 posted, 0 updated, 1 outdated, 0 skipped" in err


@pytest.mark.asyncio
async def test_us_c3_a_run_with_no_findings_still_marks_the_old_comment_outdated(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    earlier = await _first_run_body(repo, tmp_path, stub_llm_capture(_model([_FINDING_LINE_2])), gh)
    gh.list.respond(200, json=[earlier])

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model([])), tmp_path))

    assert status == EXIT_OK
    assert "No findings." in out
    assert gh.review.call_count == 0
    assert gh.graphql.call_count == 1
    assert "0 posted, 0 updated, 1 outdated" in err


@pytest.mark.asyncio
async def test_us_c3_a_finding_on_the_same_lines_with_new_text_updates_the_comment(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    earlier = await _first_run_body(repo, tmp_path, stub_llm_capture(_model([_FINDING_LINE_2])), gh)
    gh.list.respond(200, json=[earlier])
    reworded = {**_FINDING_LINE_2, "message": "the accumulator is an int but items may be floats"}

    status, _, err = await _run(repo, _settings(stub_llm_capture(_model([reworded])), tmp_path))

    assert status == EXIT_OK
    assert gh.review.call_count == 0
    assert gh.patch.call_count == 1
    assert gh.patch.calls.last.request.url.path.endswith("/pulls/comments/501")
    assert "the accumulator is an int" in json.loads(gh.patch.calls.last.request.content)["body"]
    assert "0 posted, 1 updated, 0 outdated, 0 skipped" in err


@pytest.mark.asyncio
async def test_us_c3_a_marker_copied_by_another_author_is_never_updated_or_minimised(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    spoof = _bot_comment(
        666, f"harmless\n\n{CI_COMMENT_MARKER}", login="mallory"
    )  # right marker, wrong author
    gh.list.respond(200, json=[spoof])

    # Same line as the spoof: the finding must be posted, not matched to it.
    status, _, _ = await _run(
        repo, _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)
    )
    assert status == EXIT_OK
    assert gh.review.call_count == 1
    # And with the finding gone, the spoof is still left alone.
    gh.review.reset()
    status, _, _ = await _run(repo, _settings(stub_llm_capture(_model([])), tmp_path))
    assert status == EXIT_OK
    assert gh.patch.call_count == 0 and gh.graphql.call_count == 0


# --- US-C3 failure handling: 429 / 5xx on lookup, update and outdate ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text=f"upstream {CANARY}"),
        httpx.Response(429, headers={"Retry-After": "60"}),
    ],
    ids=["5xx", "429"],
)
async def test_us_c3_a_failed_lookup_posts_nothing_exits_non_zero_and_logs_the_findings(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    gh: FakeGitHub,
    response: httpx.Response,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    gh.list.mock(return_value=response)
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)

    status, out, err = await _run(repo, settings)

    assert status == EXIT_RUNTIME_FAILURE
    assert "error: the pull-request review could not be posted or refreshed" in err
    assert "dedupe failed: listing review comments" in err  # counted in the summary
    assert gh.review.call_count == 0  # posting blind would duplicate the earlier review
    assert "app.py:2: high:" in out
    _assert_no_token(out, err)


@pytest.mark.asyncio
async def test_us_c3_a_failed_update_is_counted_and_exits_non_zero(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    earlier = await _first_run_body(repo, tmp_path, stub_llm_capture(_model([_FINDING_LINE_2])), gh)
    gh.list.respond(200, json=[earlier])
    gh.patch.respond(500)
    reworded = {**_FINDING_LINE_2, "message": "different text on the same line"}

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model([reworded])), tmp_path))

    assert status == EXIT_RUNTIME_FAILURE
    assert "dedupe failed: updating a review comment: HTTP 500" in err
    assert "app.py:2: high:" in out


@pytest.mark.asyncio
async def test_us_c3_a_failed_outdate_is_counted_and_exits_non_zero(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    earlier = await _first_run_body(repo, tmp_path, stub_llm_capture(_model([_FINDING_LINE_2])), gh)
    gh.list.respond(200, json=[earlier])
    gh.graphql.respond(502)

    status, out, err = await _run(
        repo, _settings(stub_llm_capture(_model([_FINDING_LINE_4])), tmp_path)
    )

    assert status == EXIT_RUNTIME_FAILURE
    assert "dedupe failed: marking a review comment outdated: HTTP 502" in err
    assert gh.review.call_count == 1  # the new finding was still posted
    assert "app.py:4: high:" in out


# --- US-C2: optionally fail the build ------------------------------------------------------------


@pytest.mark.asyncio
async def test_us_c2_a_high_finding_at_a_high_threshold_exits_with_the_gate_code(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(
        stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path, fail_on_severity="high"
    )

    status, out, err = await _run(repo, settings)

    assert status == EXIT_GATE_TRIPPED
    assert EXIT_GATE_TRIPPED not in (EXIT_OK, EXIT_RUNTIME_FAILURE, EXIT_USAGE)
    assert "gate: 1 finding(s) at or above severity high" in err
    assert gh.review.call_count == 1  # the review is still posted
    assert "app.py:2: high:" in out


@pytest.mark.asyncio
async def test_us_c2_a_finding_below_the_threshold_does_not_trip_the_gate(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(
        stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path, fail_on_severity="critical"
    )

    status, _, _ = await _run(repo, settings)

    assert status == EXIT_OK


@pytest.mark.asyncio
async def test_us_c2_with_no_threshold_findings_never_fail_the_job_locally_or_in_ci(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model([_FINDING_LINE_2]))

    in_ci, _, _ = await _run(repo, _settings(endpoint, tmp_path))
    locally, _, _ = await _run(repo, _settings(endpoint, tmp_path, github_actions=False))

    assert (in_ci, locally) == (EXIT_OK, EXIT_OK)


@pytest.mark.asyncio
async def test_us_c2_a_tool_error_is_never_reported_as_a_tripped_gate(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    gh.review.respond(403)
    settings = _settings(
        stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path, fail_on_severity="info"
    )

    status, _, _ = await _run(repo, settings)

    assert status == EXIT_RUNTIME_FAILURE


# --- the token never leaks ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_planted_github_token_is_absent_from_every_output_and_log(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    gh: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    caplog.set_level(logging.DEBUG)
    endpoint = stub_llm_capture(_model([_FINDING_LINE_2]))
    target = tmp_path / "review.json"

    ok = await _run(repo, _settings(endpoint, tmp_path), output=target)
    gh.review.respond(403, text=CANARY)
    denied = await _run(repo, _settings(endpoint, tmp_path))
    gh.list.respond(503, text=CANARY)
    down = await _run(repo, _settings(endpoint, tmp_path))

    assert [ok[0], denied[0], down[0]] == [EXIT_OK, EXIT_RUNTIME_FAILURE, EXIT_RUNTIME_FAILURE]
    _assert_no_token(*ok[1:], *denied[1:], *down[1:], target.read_text(), caplog.text)
    assert repr(_settings(endpoint, tmp_path)).count(CANARY) == 0


# --- audit fixes ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", ["pull_request_target", "push", None])
async def test_us_c1_only_the_pull_request_event_runs_even_with_a_pull_request_payload(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    gh: FakeGitHub,
    event_name: str | None,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model([])), tmp_path, github_event_name=event_name)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_USAGE
    assert "never pull_request_target" in err
    assert llm_requests == [] and gh.review.call_count == 0


@pytest.mark.asyncio
async def test_us_c1_staged_is_refused_in_actions_instead_of_using_head_as_the_base(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    gh: FakeGitHub,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    repo.write("staged.py", "x = 1\n")
    repo.git("add", "staged.py")
    out, err = io.StringIO(), io.StringIO()

    status = await run_review(
        _settings(stub_llm_capture(_model([])), tmp_path),
        staged=True,
        repo_dir=repo.path,
        out=out,
        err=err,
    )

    assert status == EXIT_USAGE
    assert "--staged is not available in GitHub Actions" in err.getvalue()
    assert llm_requests == [] and gh.review.call_count == 0 and gh.list.call_count == 0


@pytest.mark.asyncio
async def test_us_c3_a_range_comment_is_keyed_by_its_whole_range_so_a_one_line_finding_is_new(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    earlier = await _first_run_body(repo, tmp_path, stub_llm_capture(_model([_FINDING_LINE_2])), gh)
    gh.list.respond(200, json=[{**earlier, "start_line": 1, "line": 2}])  # a lines 1-2 comment

    status, _, err = await _run(
        repo, _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)
    )

    assert status == EXIT_OK
    assert "ci comments: 1 posted, 0 updated, 1 outdated, 0 skipped" in err
    sent = json.loads(gh.graphql.calls.last.request.content)
    assert sent["variables"] == {"id": "PRRC_501"}


@pytest.mark.asyncio
async def test_us_c3_a_comment_whose_range_collapses_to_one_line_is_still_the_same_finding(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model([_FINDING_LINE_2]))
    earlier = await _first_run_body(repo, tmp_path, endpoint, gh)
    gh.list.respond(200, json=[{**earlier, "start_line": 2, "line": 2}])  # GitHub's own shape

    status, _, err = await _run(repo, _settings(endpoint, tmp_path))

    assert status == EXIT_OK
    assert gh.review.call_count == 0 and gh.graphql.call_count == 0
    assert "0 posted, 0 updated, 0 outdated, 1 skipped" in err


@pytest.mark.asyncio
async def test_us_c3_a_github_call_that_times_out_fails_the_run_visibly_with_the_findings_logged(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    gh.list.mock(side_effect=httpx.ReadTimeout("timed out"))
    settings = _settings(
        stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path, github_timeout_seconds=0.75
    )

    status, out, err = await _run(repo, settings)

    assert status == EXIT_RUNTIME_FAILURE
    assert "error: the pull-request review could not be posted or refreshed" in err
    assert "dedupe failed" in err and gh.review.call_count == 0
    assert "app.py:2: high:" in out
    # The configured timeout is what the client was given, not httpx's default.
    assert gh.list.calls.last.request.extensions["timeout"]["read"] == 0.75


_FINDING_FILE_LEVEL = {
    "file": "app.py",
    "severity": "high",
    "message": "the module never validates its input",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_commit", "expected_posts"),
    [("a" * 40, 0), ("b" * 40, 1)],
    ids=["same-head", "new-head"],
)
async def test_a_file_level_finding_is_deduped_only_against_reviews_of_the_same_head(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    gh: FakeGitHub,
    review_commit: str,
    expected_posts: int,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model([_FINDING_FILE_LEVEL]))
    status, _, _ = await _run(repo, _settings(endpoint, tmp_path))
    assert status == EXIT_OK
    earlier = {
        "id": 7,
        "user": {"login": BOT},
        "body": gh.posted_review()["body"],
        "commit_id": review_commit,
    }
    gh.review.reset()
    gh.reviews.respond(200, json=[earlier])

    status, _, _ = await _run(repo, _settings(endpoint, tmp_path))

    assert status == EXIT_OK
    assert gh.review.call_count == expected_posts


@pytest.mark.asyncio
async def test_a_refused_batch_is_bisected_so_good_findings_share_a_review(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    third = {**_FINDING_LINE_2, "line": 3, "message": "the accumulator is never reset"}
    # Whole batch refused; the first half is accepted; the second half is refused, then its first
    # finding is refused alone and its second accepted.
    gh.review.side_effect = [
        httpx.Response(422),
        httpx.Response(200, json={"id": 99}),
        httpx.Response(422),
        httpx.Response(422),
        httpx.Response(200, json={"id": 99}),
    ]
    settings = _settings(
        stub_llm_capture(_model([_FINDING_LINE_2, third, _FINDING_LINE_4])), tmp_path
    )

    status, _, err = await _run(repo, settings)

    assert status == EXIT_OK
    assert gh.review.call_count == 5  # whole, left half, right half, then the right one by one
    assert "1 rejected by GitHub (HTTP 422)" in err


@pytest.mark.asyncio
async def test_every_comment_of_a_batch_refused_is_a_failure_not_a_quiet_pass(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    gh.review.respond(422)  # e.g. the head commit is not part of the pull request
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2, _FINDING_LINE_4])), tmp_path)

    status, _, err = await _run(repo, settings)

    assert status != EXIT_OK
    assert "refused every comment" in err


@pytest.mark.asyncio
async def test_a_lone_finding_refused_while_the_head_is_current_is_only_that_finding(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    gh.review.respond(422)  # one line outside the diff
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_OK
    assert "1 rejected by GitHub (HTTP 422)" in err


@pytest.mark.asyncio
async def test_a_lone_finding_refused_after_the_head_moved_is_a_failure_not_a_quiet_pass(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    gh.review.respond(422)
    gh.pull.respond(200, json={"head": {"sha": "b" * 40}})  # a push landed since the job started
    settings = _settings(stub_llm_capture(_model([_FINDING_LINE_2])), tmp_path)

    status, _, err = await _run(repo, settings)

    assert status != EXIT_OK
    assert "refused every comment" in err


@pytest.mark.asyncio
async def test_a_refused_batch_on_a_moved_head_is_not_bisected(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, gh: FakeGitHub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    third = {**_FINDING_LINE_2, "line": 3, "message": "the accumulator is never reset"}
    gh.review.respond(422)
    gh.pull.respond(200, json={"head": {"sha": "b" * 40}})
    settings = _settings(
        stub_llm_capture(_model([_FINDING_LINE_2, third, _FINDING_LINE_4])), tmp_path
    )

    status, _, err = await _run(repo, settings)

    assert status != EXIT_OK
    assert gh.review.call_count == 1
    assert "no longer the commit under review" in err
