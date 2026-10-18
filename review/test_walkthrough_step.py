"""Tests for ``review.walkthrough.WalkthroughStep`` (plan-phase-12 Phase 4;
US-W1..W3, W5..W9, W12): the real parsers and renderer; the LLM Gateway, publisher and
default redactor are fakes (default run). The one test using the real vendored Gitleaks
binary is marked ``integration``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

import review.walkthrough as walkthrough_module
from adapter.analyzers.gitleaks import GitleaksTextRedactor
from adapter.github.exceptions import (
    GitHubPermanentAPIError,
    GitHubPostingUnreachableError,
    GitHubWalkthroughCommentLimitError,
    GitHubWalkthroughDedupeError,
)
from findings.models import Finding
from findings.redact import NoOpTextRedactor
from ingestion.diff import Diff, DiffFile
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, CompletionResponse, TokenUsage
from llmgateway.routing import FallbackModelRoutingResolver
from review.step import StepContext
from review.walkthrough import MARKER_LINE, OUTCOMES, WalkthroughOutput, WalkthroughStep
from review.walkthrough_render import FIXED_FOOTER_LINE, MARKER

pytestmark = pytest.mark.asyncio

_SECRET = 'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"'
_SECRET_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"
_NONCE = "n" * 32


def _file(path: str, patch: str | None = "@@ -1 +1 @@\n-a\n+b\n") -> DiffFile:
    return DiffFile(path=path, status="modified", patch=patch, mode="100644")


def _diff(*paths: str) -> Diff:
    return Diff(files=tuple(_file(p) for p in paths))


class _Input:
    def __init__(
        self,
        *,
        diff: Diff | None = None,
        reviewed_diff: Diff | None = None,
        summary: str | None = "Adds a feature",
        findings: list[Finding] | None = None,
    ) -> None:
        self.findings = findings or []
        self.diff = diff
        self.reviewed_diff = reviewed_diff
        self.context_bundle = None
        self.intent_summary = summary
        self.org_id = "org-1"
        self.repo_id = "repo-1"
        self.installation_id = 1
        self.repo_full_name = "o/r"
        self.pr_number = 7
        self.head_sha = "abc123"
        self.base_sha = None


def _response(*groups: tuple[str, list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "groups": [
            {"name": n, "description": f"{n} desc", "files": [{"path": p, "note": t} for p, t in f]}
            for n, f in groups
        ]
    }


_DEFAULT = _response(("Core", [("a.py", "changes a"), ("b.py", "changes b")]))


class _LLM:
    def __init__(self, script: Any = _DEFAULT) -> None:
        self.script = script
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.requests.append(request)
        script = self.script() if callable(self.script) else self.script
        if isinstance(script, BaseException):
            raise script
        content = script if isinstance(script, str) else json.dumps(script)
        usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        return CompletionResponse(content=content, model=request.model, usage=usage)


class _Publisher:
    def __init__(self, result: Any = "created") -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def upsert(self, *, job_id: str, identity: Any, body: str) -> Any:
        self.calls.append({"job_id": job_id, "identity": identity, "body": body})
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            await self.result()
        return self.result if isinstance(self.result, str) else "created"


class _PassRedactor:
    """A real-looking (non-NoOp) redactor that records calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def redact(self, text: str) -> str:
        self.calls += 1
        return text


def _step(
    llm: _LLM,
    publisher: _Publisher,
    *,
    redactor: Any = None,
    internal_timeout: float = 30,
    reserve: float = 5,
    min_files: int = 2,
    max_files: int = 60,
    max_chars: int = 6000,
    resolver: FallbackModelRoutingResolver | None = None,
    nonce: str = _NONCE,
) -> WalkthroughStep:
    return WalkthroughStep(
        llm,
        redactor or _PassRedactor(),
        publisher,
        model="m",
        timeout=5,
        internal_timeout_seconds=internal_timeout,
        min_files=min_files,
        max_files=max_files,
        max_chars=max_chars,
        model_resolver=resolver,
        nonce_factory=lambda: nonce,
        publish_reserve_seconds=reserve,
    )


async def _run(step: WalkthroughStep, inp: Any, *, dry_run: bool = False) -> Any:
    return await step.run(inp, StepContext(job_id="j", dry_run=dry_run))


def _std_input(**kw: Any) -> _Input:
    return _Input(reviewed_diff=_diff("a.py", "b.py"), **kw)


# --- metrics helpers ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reenable_walkthrough_logger() -> None:
    # Alembic's env.py fileConfig() (migrated_engine in earlier tests) disables existing loggers.
    logging.getLogger("review.walkthrough").disabled = False


@pytest.fixture
def reader(reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:
    r = InMemoryMetricReader()
    provider = MeterProvider(resource=Resource.create({"service.name": "t"}), metric_readers=[r])
    metrics.set_meter_provider(provider)
    yield r
    provider.shutdown()


def _outcomes(r: InMemoryMetricReader) -> dict[str, int]:
    data = r.get_metrics_data()
    found: dict[str, int] = {}
    if data is None:
        return found
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "review.walkthrough.outcome_total":
                    for p in m.data.data_points:
                        found[p.attributes["outcome"]] = int(p.value)
    return found


def _points(r: InMemoryMetricReader, name: str, key: str) -> dict[str, int]:
    found: dict[str, int] = {}
    data = r.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    for p in m.data.data_points:
                        found[p.attributes[key]] = int(p.value)
    return found


def _final_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    (record,) = [r for r in caplog.records if r.getMessage() == "walkthrough finished"]
    return record


# --- US-W1 / W2 / W6 / W7 --------------------------------------------------------------------


@pytest.mark.parametrize("carried", ["reviewed_diff", "diff"])
async def test_us_w1_posts_one_comment_and_forwards_the_input(carried: str) -> None:
    llm, pub = _LLM(), _Publisher()
    inp = _Input(**{carried: _diff("a.py", "b.py")})  # type: ignore[arg-type]

    out = await _run(_step(llm, pub), inp)

    assert len(llm.requests) == 1  # US-W7: exactly one routed call
    assert llm.requests[0].messages[0].content.startswith(MARKER_LINE)
    (call,) = pub.calls
    body = call["body"]
    assert body.startswith(MARKER + "\n")
    assert "### Summary\nAdds a feature" in body
    assert "`a.py`" in body and "`b.py`" in body and FIXED_FOOTER_LINE in body
    assert call["identity"].head_sha == "abc123" and call["job_id"] == "j"
    assert isinstance(out, WalkthroughOutput)
    for name in ("findings", "context_bundle", "intent_summary", "org_id", "head_sha", "pr_number"):
        assert getattr(out, name) == getattr(inp, name)
    assert out.reviewed_diff == _diff("a.py", "b.py")


async def test_us_w1_without_a_summary_omits_the_section() -> None:
    pub = _Publisher()
    await _run(_step(_LLM(), pub), _std_input(summary=None))
    assert "### Summary" not in pub.calls[0]["body"]
    assert "`a.py`" in pub.calls[0]["body"]


async def test_us_w1_updated_when_the_publisher_edits() -> None:
    pub = _Publisher("edited")
    await _run(_step(_LLM(), pub), _std_input())
    assert len(pub.calls) == 1


async def test_us_w2_unknown_and_duplicate_paths_dropped_omitted_appended() -> None:
    script = _response(
        ("Core", [("a.py", "x"), ("a.py", "dup"), ("ghost.py", "unknown")]),
    )
    pub = _Publisher()
    await _run(_step(_LLM(script), pub), _Input(reviewed_diff=_diff("a.py", "b.py", "c.py")))
    body = pub.calls[0]["body"]
    assert "ghost.py" not in body and body.count("`a.py`") == 1
    assert "### Other files" in body and "`b.py`" in body and "`c.py`" in body


async def test_us_w6_oversize_pr_sends_a_bounded_list_and_counts_the_rest() -> None:
    paths = [f"f{i}.py" for i in range(5)]
    llm, pub = _LLM(_response(("All", [(p, "n") for p in paths[:2]]))), _Publisher()
    await _run(_step(llm, pub, max_files=2), _Input(reviewed_diff=_diff(*paths)))
    sent = llm.requests[0].messages[1].content
    assert "f1.py" in sent and "f2.py" not in sent
    assert "3 more files not shown" in pub.calls[0]["body"]


async def test_us_w6_body_never_exceeds_the_cap() -> None:
    paths = [f"dir/file_{i}.py" for i in range(40)]
    script = _response(("All", [(p, "a note about this file") for p in paths]))
    pub = _Publisher()
    await _run(_step(_LLM(script), pub, max_chars=800), _Input(reviewed_diff=_diff(*paths)))
    assert len(pub.calls[0]["body"]) <= 800
    assert "more files not shown" in pub.calls[0]["body"]


async def test_us_w7_prompt_input_is_bounded_and_delimited() -> None:
    huge = DiffFile(path="a.py", status="modified", patch="+" + "x" * 50_000)
    llm, pub = _LLM(), _Publisher()
    inp = _Input(reviewed_diff=Diff(files=(huge, _file("b.py"))))
    await _run(_step(llm, pub), inp)
    user = llm.requests[0].messages[1].content
    assert user.startswith("<changed_files>") and user.endswith("</changed_files>")
    assert len(user) < 3000
    assert "+1 -0" in user  # per-file change counts


async def test_us_w7_routes_under_the_walkthrough_stage() -> None:
    class _Resolver:
        def __init__(self) -> None:
            self.stages: list[str] = []

        async def resolve(self, stage: str, org_id: str) -> str:
            self.stages.append(stage)
            return "routed-model"

    resolver = _Resolver()
    llm = _LLM()
    await _run(_step(llm, _Publisher(), resolver=resolver), _std_input())  # type: ignore[arg-type]
    assert resolver.stages == ["walkthrough"] and llm.requests[0].model == "routed-model"


# --- US-W3 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("diff", "posts"),
    [
        (_diff("a.py", "b.py"), True),  # exactly min_files
        (_diff("a.py"), False),  # min_files - 1
        (Diff(files=(_file("a.py"), _file("b.py", patch=None))), False),  # patch=None excluded
    ],
)
async def test_us_w3_min_files_boundaries(
    diff: Diff, posts: bool, reader: InMemoryMetricReader
) -> None:
    llm, pub = _LLM(), _Publisher()
    out = await _run(_step(llm, pub), _Input(reviewed_diff=diff))
    assert bool(pub.calls) is posts and bool(llm.requests) is posts
    assert out.findings == []
    assert _outcomes(reader) == {("posted" if posts else "skipped_small_pr"): 1}


# --- US-W5: the trust boundary --------------------------------------------------------------


@pytest.mark.integration  # real vendored Gitleaks binary
async def test_us_w5_secrets_in_path_note_and_summary_never_reach_the_publisher() -> None:
    secret_path = f"{_SECRET_TOKEN}.py"
    script = _response(("Core", [(secret_path, _SECRET), ("b.py", "ok")]))
    pub = _Publisher()
    inp = _Input(reviewed_diff=_diff(secret_path, "b.py"), summary=f"Uses {_SECRET}")
    await _run(_step(_LLM(script), pub, redactor=GitleaksTextRedactor()), inp)
    (call,) = pub.calls
    assert _SECRET_TOKEN not in call["body"]


async def test_us_w5_redaction_is_a_single_call() -> None:
    redactor = _PassRedactor()
    await _run(_step(_LLM(), _Publisher(), redactor=redactor), _std_input())
    assert redactor.calls == 1


@pytest.mark.parametrize("where", ["path", "note", "summary"])
async def test_us_w5_nonce_planted_in_a_field_posts_nothing(
    where: str, reader: InMemoryMetricReader
) -> None:
    path = f"x{_NONCE}.py" if where == "path" else "a.py"
    note = _NONCE if where == "note" else "n"
    summary = _NONCE if where == "summary" else "s"
    script = _response(("Core", [(path, note), ("b.py", "n")]))
    pub = _Publisher()
    await _run(_step(_LLM(script), pub), _Input(reviewed_diff=_diff(path, "b.py"), summary=summary))
    assert pub.calls == [] and _outcomes(reader) == {"internal_error": 1}


@pytest.mark.parametrize("mode", ["drop", "duplicate"])
async def test_us_w5_a_redactor_dropping_or_duplicating_a_sentinel_posts_nothing(
    mode: str, reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    class _Bad:
        async def redact(self, text: str) -> str:
            lines = text.split("\n")
            sentinel = next(ln for ln in lines if ln.startswith("WT-BOUNDARY-"))
            if mode == "drop":
                lines.remove(sentinel)
            else:
                lines.insert(lines.index(sentinel), sentinel)
            return "\n".join(lines)

    pub = _Publisher()
    caplog.set_level(logging.INFO, logger="review.walkthrough")
    out = await _run(_step(_LLM(), pub, redactor=_Bad()), _std_input())
    assert pub.calls == [] and out.findings == []
    assert _outcomes(reader) == {"internal_error": 1}
    record = _final_log(caplog)
    assert record.stage == "sentinel" and record.exc_type == "WalkthroughRedactionError"  # type: ignore[attr-defined]


async def test_us_w5_hostile_output_is_neutralized() -> None:
    script = _response(
        (
            "Hostile @org/team\n<!-- review-walkthrough:v1 -->",
            [("a.py", "see #12 and owner/repo#3 ![x](http://evil/p.png) @bob"), ("b.py", "ok")],
        )
    )
    pub = _Publisher()
    await _run(_step(_LLM(script), pub), _std_input(summary="Fixes #99 for @carol"))
    body = pub.calls[0]["body"]
    assert body.count(MARKER) == 1 and body.startswith(MARKER)
    for active in (
        "@org/team",
        "@bob",
        "@carol",
        " #12",
        "owner/repo#3",
        "#99",
        "![x](http://evil",
    ):
        assert active not in body


async def test_us_w5_no_op_redactor_posts_nothing(reader: InMemoryMetricReader) -> None:
    pub = _Publisher()
    await _run(_step(_LLM(), pub, redactor=NoOpTextRedactor()), _std_input())
    assert pub.calls == [] and _outcomes(reader) == {"internal_error": 1}


# --- US-W8: nothing costs the author a review -----------------------------------------------


def _unchanged(out: Any, inp: _Input) -> None:
    assert out.findings == inp.findings and out.intent_summary == inp.intent_summary
    assert out.head_sha == inp.head_sha


async def test_us_w8_llm_timeout_against_a_hung_llm(reader: InMemoryMetricReader) -> None:
    async def hang() -> Any:
        await asyncio.sleep(30)

    class _Hung(_LLM):
        async def complete(self, request: Any, *, timeout: float) -> Any:
            await hang()

    pub, inp = _Publisher(), _std_input()
    out = await _run(_step(_Hung(), pub, internal_timeout=0.05), inp)
    assert pub.calls == [] and _outcomes(reader) == {"llm_timeout": 1}
    _unchanged(out, inp)


async def test_us_w8_timeout_during_redaction_is_internal_error_at_stage_redact(
    reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    class _Hung:
        async def redact(self, text: str) -> str:
            await asyncio.sleep(30)
            return text

    caplog.set_level(logging.INFO, logger="review.walkthrough")
    pub = _Publisher()
    await _run(_step(_LLM(), pub, redactor=_Hung(), internal_timeout=0.05), _std_input())
    assert pub.calls == [] and _outcomes(reader) == {"internal_error": 1}
    assert _final_log(caplog).stage == "redact"  # type: ignore[attr-defined]


async def test_us_w8_publish_deadline_is_the_steps_own(reader: InMemoryMetricReader) -> None:
    async def hang() -> None:
        await asyncio.sleep(30)

    pub, inp = _Publisher(hang), _std_input()
    out = await _run(_step(_LLM(), pub, reserve=0.05), inp)
    assert _outcomes(reader) == {"github_error": 1}
    _unchanged(out, inp)


@pytest.mark.parametrize(
    ("llm_script", "publisher_result", "expected"),
    [
        (LLMUnreachableError(endpoint="m", detail="down"), "created", "llm_timeout"),
        (LLMMalformedResponseError(endpoint="m", detail="bad"), "created", "parse_failure"),
        ("not json at all", "created", "parse_failure"),
        ({"groups": [{"junk": 1}, "x"]}, "created", "parse_failure"),
        ({"groups": []}, "created", "parse_failure"),
        (_DEFAULT, GitHubPostingUnreachableError(detail="x"), "github_error"),
        (_DEFAULT, GitHubWalkthroughCommentLimitError(detail="x"), "skipped_comment_limit"),
        (_DEFAULT, GitHubWalkthroughDedupeError(detail="x"), "dedupe_failed"),
        (_DEFAULT, RuntimeError("boom"), "github_error"),
    ],
)
async def test_us_w8_every_failure_passes_the_input_through(
    llm_script: Any, publisher_result: Any, expected: str, reader: InMemoryMetricReader
) -> None:
    pub, inp = _Publisher(publisher_result), _std_input()
    out = await _run(_step(_LLM(llm_script), pub), inp)
    _unchanged(out, inp)
    assert _outcomes(reader) == {expected: 1}


async def test_us_w8_redaction_failure_is_internal_error(reader: InMemoryMetricReader) -> None:
    class _Raising:
        async def redact(self, text: str) -> str:
            raise RuntimeError("gitleaks crashed")

    pub = _Publisher()
    await _run(_step(_LLM(), pub, redactor=_Raising()), _std_input())
    assert pub.calls == [] and _outcomes(reader) == {"internal_error": 1}


async def test_us_w8_input_validation_failure_returns_the_input_untouched(
    reader: InMemoryMetricReader,
) -> None:
    junk = object()
    out = await _run(_step(_LLM(), _Publisher()), junk)
    assert out is junk and _outcomes(reader) == {"internal_error": 1}


async def test_us_w8_input_without_a_diff_is_internal_error(reader: InMemoryMetricReader) -> None:
    await _run(_step(_LLM(), _Publisher()), _Input())
    assert _outcomes(reader) == {"internal_error": 1}


async def test_us_w8_cancel_during_publish_still_records_its_outcome(
    reader: InMemoryMetricReader,
) -> None:
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(_run(_step(_LLM(), _Publisher(hang)), _std_input()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _outcomes(reader) == {"github_error": 1}


async def test_us_w8_dry_run_publishes_nothing_and_leaves_the_input() -> None:
    pub, inp = _Publisher(), _std_input()
    out = await _run(_step(_LLM(), pub), inp, dry_run=True)
    assert pub.calls == []
    _unchanged(out, inp)


# --- US-W9 / W12 ----------------------------------------------------------------------------


async def test_us_w12_exported_counter_name_and_the_nine_outcomes() -> None:
    assert len(OUTCOMES) == 9 and len(set(OUTCOMES)) == 9
    name = "review.walkthrough.outcome_total"
    assert name.replace(".", "_") == "review_walkthrough_outcome_total"


async def test_us_w12_all_nine_outcomes_are_counted(reader: InMemoryMetricReader) -> None:
    cases: list[tuple[WalkthroughStep, _Input]] = [
        (_step(_LLM(), _Publisher("created")), _std_input()),
        (_step(_LLM(), _Publisher("edited")), _std_input()),
        (_step(_LLM(), _Publisher()), _Input(reviewed_diff=_diff("a.py"))),
        (_step(_LLM(), _Publisher(GitHubWalkthroughCommentLimitError(detail="x"))), _std_input()),
        (_step(_LLM(LLMUnreachableError(endpoint="m", detail="d")), _Publisher()), _std_input()),
        (_step(_LLM("nope"), _Publisher()), _std_input()),
        (_step(_LLM(), _Publisher(GitHubPostingUnreachableError(detail="x"))), _std_input()),
        (_step(_LLM(), _Publisher(GitHubWalkthroughDedupeError(detail="x"))), _std_input()),
        (
            _step(_LLM(), _Publisher(), nonce="a"),
            _Input(reviewed_diff=_diff("a.py", "b.py"), summary="a"),
        ),
    ]
    for step, inp in cases:
        await _run(step, inp)
    assert _outcomes(reader) == dict.fromkeys(OUTCOMES, 1)


async def test_us_w12_logs_carry_no_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_summary = "SUMMARY-TEXT-MARKER"
    await _run(_step(_LLM(), _Publisher()), _std_input(summary=secret_summary))
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert secret_summary not in logged and "a.py" not in logged
    record = _final_log(caplog)
    assert record.outcome == "posted" and record.files_in == 2  # type: ignore[attr-defined]


async def test_us_w12_telemetry_failure_is_logged_and_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Broken:
        def add(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("meter down")

    monkeypatch.setattr(walkthrough_module, "_outcome_counter", _Broken())
    pub = _Publisher()
    out = await _run(_step(_LLM(), pub), _std_input())
    assert len(pub.calls) == 1 and isinstance(out, WalkthroughOutput)
    assert any("telemetry recording failed" in r.getMessage() for r in caplog.records)


async def test_files_not_shown_is_counted_by_reason(reader: InMemoryMetricReader) -> None:
    paths = [f"f{i}.py" for i in range(5)]
    step = _step(_LLM(), _Publisher(), max_files=3)
    await _run(step, _Input(reviewed_diff=_diff(*paths)))
    got = _points(reader, "review.walkthrough.files_not_shown_total", "reason")
    assert got == {"max_files": 2}


async def test_reconciled_counts_model_path_errors(reader: InMemoryMetricReader) -> None:
    script = _response(("Core", [("a.py", "n"), ("ghost.py", "n"), ("a.py", "n")]))
    await _run(_step(_LLM(script), _Publisher()), _std_input())
    got = _points(reader, "review.walkthrough.reconciled_total", "kind")
    assert got == {"unknown_path": 1, "duplicate_path": 1, "omitted": 1}


async def test_internal_error_counter_carries_the_stage(reader: InMemoryMetricReader) -> None:
    await _run(_step(_LLM(), _Publisher()), _Input(reviewed_diff=None, diff=None))
    got = _points(reader, "review.walkthrough.outcome_total", "stage")
    assert got == {"validate": 1}


async def test_publish_failure_log_carries_exc_type_and_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cause = GitHubPermanentAPIError(status_code=403, detail="forbidden")
    err = GitHubPostingUnreachableError(detail="x")
    err.__cause__ = cause
    caplog.set_level(logging.INFO, logger="review.walkthrough")
    await _run(_step(_LLM(), _Publisher(err)), _std_input())
    record = _final_log(caplog)
    assert record.outcome == "github_error"  # type: ignore[attr-defined]
    assert record.exc_type == "GitHubPostingUnreachableError"  # type: ignore[attr-defined]
    assert record.status_code == 403  # type: ignore[attr-defined]


async def test_a_response_without_usage_still_posts() -> None:
    class _NoUsageLLM(_LLM):
        async def complete(
            self, request: CompletionRequest, *, timeout: float
        ) -> CompletionResponse:
            return CompletionResponse(content=json.dumps(_DEFAULT), model=request.model, usage=None)

    pub = _Publisher()
    await _run(_step(_NoUsageLLM(), pub), _std_input())
    assert len(pub.calls) == 1
