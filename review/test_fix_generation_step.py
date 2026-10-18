"""Integration tests for ``review.fix_generation.FixGenerationStep`` (plan-phase-11
Phase 5; US-AF1..AF8, AF11, AF12): real parsers, the real Gitleaks redactor and
the real Ruff binary through ``AnalyzerRerunner``; only GitHub content and the
LLM Gateway are faked (scripted by the prompt's fixed marker line).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import pytest

import review.fix_generation as fix_generation_module
from adapter.analyzers.gitleaks import GitleaksTextRedactor
from adapter.analyzers.ruff import RUFF_CONFIG_FILENAMES, RuffAnalyzer
from findings.merge import merge_findings
from findings.models import Finding
from findings.redact import NoOpTextRedactor, TextRedactor
from indexing.embedding import EmbeddingVector
from ingestion.diff import Diff, DiffContentUnavailableError, DiffFile
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, CompletionResponse, TokenUsage
from llmgateway.routing import (
    FallbackModelRoutingResolver,
    ModelRoutingConfigReader,
    ModelRoutingConfigRow,
)
from review.analyzer_rerun import AnalyzerRerunner
from review.file_matchers import match_python
from review.fix_generation import (
    GENERATION_MARKER,
    RUBRIC_MARKER,
    FixGenerationOutput,
    FixGenerationStep,
)
from review.memory_suppression import MemorySuppressionConfig, suppress_via_memory
from review.static_analysis import AnalyzerRegistration
from review.step import StepContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ORIGINAL = "import os\n\n\ndef f():\n    return 1\n"
_PATCH = "@@ -0,0 +1,5 @@\n+import os\n+\n+\n+def f():\n+    return 1\n"
_DELETE_IMPORT = {"start_line": 1, "end_line": 1, "replacement": ""}
_RUBRIC_OK = {"resolves": True, "minimal": True, "safe": True, "compiles_in_context": True}


def _ruff(line: int | None = 1, **overrides: Any) -> Finding:
    fields: dict[str, Any] = {
        "file": "a.py",
        "line": line,
        "severity": "medium",
        "message": "`os` imported but unused",
        "source": "ruff",
        "rule_id": "F401",
    }
    return Finding.model_validate({**fields, **overrides})


def _llm(**overrides: Any) -> Finding:
    return _ruff(
        1, source="llm", rule_id=None, verification_status="verified", message="unused", **overrides
    )


class _Fetcher:
    def __init__(self, content: dict[str, bytes] | None = None, *, fail: bool = False) -> None:
        self.content = {"a.py": _ORIGINAL.encode()} if content is None else content
        self.fail = fail
        self.fail_diff = False
        self.fetch_content_calls: list[list[str]] = []
        self.fetch_calls = 0

    async def fetch(self, **_: Any) -> Diff:
        self.fetch_calls += 1
        if self.fail_diff:
            raise DiffContentUnavailableError(repo_full_name="o/r", detail="no diff")
        return _diff()

    async def fetch_content(self, *, paths: Any, **_: Any) -> dict[str, bytes]:
        self.fetch_content_calls.append(list(paths))
        if self.fail:
            raise DiffContentUnavailableError(repo_full_name="o/r", detail="boom")
        return self.content


class _LLM:
    """Answers by the system prompt's marker. A value may be a JSON-able object,
    a raw string, an exception to raise, or a callable returning one."""

    def __init__(self, generation: Any = _DELETE_IMPORT, rubric: Any = _RUBRIC_OK) -> None:
        self.generation = generation
        self.rubric = rubric
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.requests.append(request)
        system = request.messages[0].content
        script = self.generation if system.startswith(GENERATION_MARKER) else self.rubric
        assert system.startswith((GENERATION_MARKER, RUBRIC_MARKER))
        if callable(script):
            script = script()
        if isinstance(script, Exception):
            raise script
        content = script if isinstance(script, str) else json.dumps(script)
        usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        return CompletionResponse(content=content, model=request.model, usage=usage)

    def count(self, marker: str) -> int:
        return sum(r.messages[0].content.startswith(marker) for r in self.requests)


class _Reanalyzer:
    def __init__(self, verdict: str = "passed") -> None:
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    async def rerun(
        self, analyzer_name: str, path: str, original: bytes, patched: bytes, **kw: Any
    ) -> str:
        self.calls.append({"analyzer": analyzer_name, "patched": patched, **kw})
        return self.verdict


class _RaisingRedactor:
    async def redact(self, text: str) -> str:
        raise RuntimeError("gitleaks crashed")


def _diff(patch: str | None = _PATCH, path: str = "a.py") -> Diff:
    return Diff(files=(DiffFile(path=path, status="added", patch=patch, mode="100644"),))


class _Input:
    def __init__(
        self, findings: list[Finding], diff: Diff | None = None, *, no_diff: bool = False
    ) -> None:
        self.findings = findings
        self.diff = None if no_diff else (diff if diff is not None else _diff())
        self.org_id = "org-1"
        self.repo_id = "repo-1"
        self.installation_id = 1
        self.repo_full_name = "o/r"
        self.pr_number = 7
        self.head_sha = "abc"
        self.base_sha = None


def _step(
    llm: _LLM,
    *,
    fetcher: _Fetcher | None = None,
    reanalyzer: Any = None,
    redactor: TextRedactor | None = None,
    resolver: FallbackModelRoutingResolver | None = None,
    verification_enabled: bool = True,
    internal_timeout: float = 30,
    cap: int = 5,
    max_range: int = 20,
    max_replacement: int = 40,
    window: int = 60,
) -> FixGenerationStep:
    return FixGenerationStep(
        fetcher or _Fetcher(),
        llm,
        reanalyzer or _Reanalyzer(),
        redactor or NoOpTextRedactor(),
        model="m",
        timeout=5,
        internal_timeout_seconds=internal_timeout,
        max_fixes_per_job=cap,
        max_range_lines=max_range,
        max_replacement_lines=max_replacement,
        window_lines=window,
        verification_enabled=verification_enabled,
        model_resolver=resolver,
    )


async def _run(step: FixGenerationStep, inp: _Input) -> FixGenerationOutput:
    return await step.run(inp, StepContext(job_id="j"))


def _outcome(output: FixGenerationOutput, plain: list[Finding]) -> None:
    """Every incoming finding comes back, none changed apart from ``suggestion``."""
    assert [f.model_copy(update={"suggestion": None}) for f in output.findings] == plain


# --- US-AF1 / AF3 / AF6: accepted fix, real Ruff ---------------------------------------------


def _real_rerunner() -> AnalyzerRerunner:
    registration = AnalyzerRegistration(
        "ruff", RuffAnalyzer(), matches=match_python, config_filenames=RUFF_CONFIG_FILENAMES
    )
    return AnalyzerRerunner([registration], asyncio.Semaphore(2), {"static_analysis_analyzer": 60})


async def test_analyzer_finding_gets_one_validated_suggestion_via_real_ruff() -> None:
    llm = _LLM()
    other = _ruff(5, source="trivy", message="other finding must not reach the prompt")
    finding = _ruff()

    output = await _run(_step(llm, reanalyzer=_real_rerunner()), _Input([finding, other]))

    fixed = next(f for f in output.findings if f.line == 1)
    assert fixed.suggestion is not None
    assert (fixed.suggestion.start_line, fixed.suggestion.end_line) == (1, 1)
    assert fixed.suggestion.replacement == ""
    assert fixed.message == finding.message
    assert next(f for f in output.findings if f.line == 5).suggestion is None
    assert llm.count(GENERATION_MARKER) == 1
    assert llm.count(RUBRIC_MARKER) == 0  # analyzer findings use the re-run, not the rubric
    prompt = llm.requests[0].messages[1].content
    assert "other finding must not reach" not in prompt
    assert "1| import os" in prompt


async def test_real_ruff_rejects_a_fix_that_leaves_the_finding() -> None:
    llm = _LLM(generation={"start_line": 1, "end_line": 1, "replacement": "import os  # keep"})

    output = await _run(_step(llm, reanalyzer=_real_rerunner()), _Input([_ruff()]))

    assert output.findings[0].suggestion is None


# --- US-AF2 / AF11: eligibility and cap -----------------------------------------------------


async def test_ineligible_findings_make_no_llm_call() -> None:
    llm = _LLM()
    findings = [
        _ruff(source="gitleaks"),
        _ruff(severity="info"),
        _ruff(line=None),
        _ruff(file="gone.py"),
        _ruff(source="llm", verification_status=None),
    ]

    output = await _run(_step(llm), _Input(findings))

    assert llm.requests == []
    assert output.findings == findings


async def test_memory_suppressed_llm_finding_never_reaches_fix_generation() -> None:
    """Suppression runs upstream in verification and drops the finding, so the
    step is handed nothing and makes no generation call (US-AF13)."""

    class _Matcher:
        async def best_effective_similarity(self, **_: Any) -> float:
            return 0.99

    class _Embedder:
        def embed(self, texts: Any) -> tuple[EmbeddingVector, ...]:
            return tuple(EmbeddingVector(values=(1.0,), dimension=1) for _ in texts)

    kept, suppressed = await suppress_via_memory(
        [_llm()],
        config=MemorySuppressionConfig(_Matcher(), _Embedder(), 0.9, 180.0, True),
        repo_id="repo-1",
    )
    llm = _LLM()

    output = await _run(_step(llm), _Input(kept))

    assert suppressed == 1
    assert llm.requests == []
    assert output.findings == []


async def test_unverified_pipeline_makes_llm_findings_ineligible() -> None:
    llm = _LLM()
    await _run(_step(llm, verification_enabled=False), _Input([_llm()]))
    assert llm.requests == []


class _Counter:
    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, str]]] = []

    def add(self, value: int, attributes: dict[str, str] | None = None) -> None:
        self.adds.append((value, attributes or {}))

    def total(self) -> int:
        return sum(v for v, _ in self.adds)


async def test_cap_attempts_best_ranked_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    skipped, outcomes = _Counter(), _Counter()
    monkeypatch.setattr(fix_generation_module, "_skipped_cap_counter", skipped)
    monkeypatch.setattr(fix_generation_module, "_outcome_counter", outcomes)
    patch = "@@ -0,0 +1,8 @@\n" + "".join(f"+x{i} = {i}\n" for i in range(8))
    content = "".join(f"x{i} = {i}\n" for i in range(8)).encode()
    severities = ["low", "critical", "low", "high", "low", "medium", "low", "low"]
    findings = [
        _ruff(i + 1, severity=sev, rule_id=f"R{i}", file="b.py") for i, sev in enumerate(severities)
    ]
    llm = _LLM(generation=lambda: {"start_line": 1, "end_line": 1, "replacement": "y = 0"})
    step = _step(llm, fetcher=_Fetcher({"b.py": content}), cap=5)

    with caplog.at_level(logging.INFO, logger=fix_generation_module.logger.name):
        await _run(step, _Input(findings, _diff(patch, "b.py")))

    assert llm.count(GENERATION_MARKER) == 5
    generation_prompts = [
        r.messages[1].content
        for r in llm.requests
        if r.messages[0].content.startswith(GENERATION_MARKER)
    ]
    attempted_severities = {
        sev
        for sev in set(severities)
        if any(f'"severity": "{sev}"' in p for p in generation_prompts)
    }
    assert {"critical", "high", "medium"} <= attempted_severities
    assert sum('"severity": "low"' in p for p in generation_prompts) == 2
    assert skipped.total() == 3
    assert outcomes.total() == 5
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.__dict__["skipped_count"] == 3
    [record] = [r for r in caplog.records if getattr(r, "skipped_by_cap", None) == 3]
    assert record.__dict__["attempts"] == 5


async def test_telemetry_and_token_usage_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes, candidates, attempts = _Counter(), _Counter(), _Counter()
    monkeypatch.setattr(fix_generation_module, "_outcome_counter", outcomes)
    monkeypatch.setattr(fix_generation_module, "_candidates_counter", candidates)
    monkeypatch.setattr(fix_generation_module, "_attempts_counter", attempts)
    usage_stages: list[str] = []
    monkeypatch.setattr(
        fix_generation_module,
        "record_llm_token_usage",
        lambda usage, *, model, stage, logger: usage_stages.append(stage),
    )

    await _run(_step(_LLM()), _Input([_llm()]))

    assert outcomes.adds == [(1, {"outcome": "accepted", "source": "llm"})]
    assert (candidates.total(), attempts.total()) == (1, 1)
    assert usage_stages == ["fix_generation", "fix_verification"]


# --- US-AF4 / AF5 / AF8: gate rejections ----------------------------------------------------


@pytest.mark.parametrize(
    "generation",
    [
        "not json",
        {"start_line": 1, "end_line": 1},
        {"start_line": 1, "end_line": 1, "replacement": "x", "extra": 1},
        {"start_line": "1", "end_line": 1, "replacement": ""},
        {"start_line": 3, "end_line": 3, "replacement": ""},  # does not contain the finding line
        {"start_line": 1, "end_line": 40, "replacement": ""},  # outside the hunk
        {"start_line": 1, "end_line": 1, "replacement": "import os"},  # noop
        {"start_line": 1, "end_line": 1, "replacement": "a\r"},  # carriage return
        {"start_line": 1, "end_line": 1, "replacement": "x = (\n"},  # syntax error
        {"start_line": 1, "end_line": 1, "replacement": "a\nb\nc\nd"},  # oversized (cap 3)
    ],
)
async def test_gate_failures_leave_the_finding_unchanged(generation: Any) -> None:
    reanalyzer = _Reanalyzer()
    finding = _ruff()

    output = await _run(
        _step(_LLM(generation=generation), reanalyzer=reanalyzer, max_replacement=3),
        _Input([finding]),
    )

    assert output.findings == [finding]
    assert reanalyzer.calls == []  # a gate failure never reaches the analyzer


async def test_real_gitleaks_redactor_rejects_a_secret_bearing_replacement() -> None:
    secret = 'token = "ghp_' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5" + '"'
    llm = _LLM(generation={"start_line": 1, "end_line": 1, "replacement": secret})

    output = await _run(_step(llm, redactor=GitleaksTextRedactor()), _Input([_ruff()]))

    assert output.findings[0].suggestion is None


# --- US-AF6 / AF7 / AF12 ---------------------------------------------------------------------


async def test_reanalysis_must_pass_and_receives_the_originating_analyzer_and_rule() -> None:
    reanalyzer = _Reanalyzer("passed")
    output = await _run(_step(_LLM(), reanalyzer=reanalyzer), _Input([_ruff()]))
    assert output.findings[0].suggestion is not None
    assert reanalyzer.calls[0]["analyzer"] == "ruff"
    assert reanalyzer.calls[0]["rule_id"] == "F401"
    assert reanalyzer.calls[0]["patched"] == b"\n\ndef f():\n    return 1\n"

    for verdict in ("rejected", "failed"):
        output = await _run(_step(_LLM(), reanalyzer=_Reanalyzer(verdict)), _Input([_ruff()]))
        assert output.findings[0].suggestion is None


async def test_analyzer_finding_without_a_rule_id_is_rejected() -> None:
    output = await _run(_step(_LLM()), _Input([_ruff(rule_id=None)]))
    assert output.findings[0].suggestion is None


async def test_reanalysis_results_are_counted_per_analyzer_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reanalysis = _Counter()
    monkeypatch.setattr(fix_generation_module, "_reanalysis_counter", reanalysis)

    for verdict in ("passed", "rejected", "failed"):
        await _run(_step(_LLM(), reanalyzer=_Reanalyzer(verdict)), _Input([_ruff()]))
    await _run(_step(_LLM()), _Input([_ruff(rule_id=None)]))

    assert [attrs for _, attrs in reanalysis.adds] == [
        {"analyzer": "ruff", "result": result}
        for result in ("passed", "rejected", "failed", "no_rule")
    ]


async def test_rejected_outcome_carries_the_source_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = _Counter()
    monkeypatch.setattr(fix_generation_module, "_outcome_counter", outcomes)

    await _run(_step(_LLM(), reanalyzer=_Reanalyzer("rejected")), _Input([_ruff()]))

    assert outcomes.adds == [(1, {"outcome": "reanalysis_failed", "source": "analyzer"})]


class _Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def get(self, *, org_id: str | None, stage: str) -> ModelRoutingConfigRow | None:
        self.calls.append((org_id, stage))
        return ModelRoutingConfigRow(org_id=org_id, stage=stage, model_name=f"model-for-{stage}")


async def test_verified_llm_finding_passes_only_on_four_true_criteria() -> None:
    reader = _Reader()
    assert isinstance(reader, ModelRoutingConfigReader)
    resolver = FallbackModelRoutingResolver(reader, static_default_model="d")
    llm = _LLM()

    output = await _run(_step(llm, resolver=resolver), _Input([_llm()]))

    assert output.findings[0].suggestion is not None
    assert llm.count(GENERATION_MARKER) == 1 and llm.count(RUBRIC_MARKER) == 1
    assert [r.model for r in llm.requests] == ["model-for-fix_generation", "model-for-verification"]
    assert [stage for _, stage in reader.calls] == ["fix_generation", "verification"]


@pytest.mark.parametrize(
    "rubric",
    [
        {**_RUBRIC_OK, "resolves": False},
        {**_RUBRIC_OK, "minimal": False},
        {**_RUBRIC_OK, "safe": False},
        {**_RUBRIC_OK, "compiles_in_context": False},
        {"resolves": True, "minimal": True, "safe": True},  # missing verdict field
        {**_RUBRIC_OK, "safe": "yes"},
        "unparseable",
        LLMUnreachableError(endpoint="e", detail="d"),
        LLMMalformedResponseError(endpoint="e", detail="d"),
    ],
)
async def test_rubric_failures_fail_closed(rubric: Any) -> None:
    output = await _run(_step(_LLM(rubric=rubric)), _Input([_llm()]))
    assert output.findings[0].suggestion is None


@pytest.mark.parametrize(
    "generation",
    [
        LLMUnreachableError(endpoint="e", detail="d"),
        LLMMalformedResponseError(endpoint="e", detail="d"),
    ],
)
async def test_llm_generation_failure_is_isolated(generation: Exception) -> None:
    finding = _ruff()
    output = await _run(_step(_LLM(generation=generation)), _Input([finding]))
    assert output.findings == [finding]


async def test_fetch_failure_and_redactor_crash_leave_findings_unchanged() -> None:
    finding = _ruff()
    output = await _run(_step(_LLM(), fetcher=_Fetcher(fail=True)), _Input([finding]))
    assert output.findings == [finding]

    output = await _run(_step(_LLM(), fetcher=_Fetcher({})), _Input([finding]))
    assert output.findings == [finding]

    output = await _run(_step(_LLM(), redactor=_RaisingRedactor()), _Input([finding]))
    assert output.findings == [finding]


async def test_an_unexpected_error_in_one_attempt_does_not_escape_the_step() -> None:
    finding = _ruff()
    output = await _run(_step(_LLM(), reanalyzer=object()), _Input([finding]))
    assert output.findings == [finding]


async def test_step_timeout_keeps_completed_fixes(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = _Counter()
    monkeypatch.setattr(fix_generation_module, "_outcome_counter", outcomes)
    patch = "@@ -0,0 +1,2 @@\n+import os\n+import sys\n"
    content = b"import os\nimport sys\n"
    fast, slow = _ruff(1, rule_id="F401"), _ruff(2, severity="low", rule_id="F401")

    class _SlowSecond(_Reanalyzer):
        async def rerun(
            self, analyzer_name: str, path: str, original: bytes, patched: bytes, **kw: Any
        ) -> str:
            if kw["patched_span"][0] == 2:
                await asyncio.sleep(10)
            return "passed"

    def generation() -> dict[str, Any]:
        generation.n += 1  # type: ignore[attr-defined]
        line = generation.n  # type: ignore[attr-defined]
        return {"start_line": line, "end_line": line, "replacement": ""}

    generation.n = 0  # type: ignore[attr-defined]
    step = _step(
        _LLM(generation=generation),
        fetcher=_Fetcher({"a.py": content}),
        reanalyzer=_SlowSecond(),
        internal_timeout=1,
    )

    output = await _run(step, _Input([fast, slow], _diff(patch)))

    assert sum(f.suggestion is not None for f in output.findings) == 1
    assert (1, {"outcome": "timeout", "source": "analyzer"}) in outcomes.adds


async def test_step_timeout_still_resolves_overlapping_accepted_fixes() -> None:
    patch = "@@ -0,0 +1,3 @@\n+import os\n+# c\n+import sys\n"
    content = b"import os\n# c\nimport sys\n"
    high, low = _ruff(1, severity="high", rule_id="F401"), _ruff(2, severity="low", rule_id="F401")
    third = _ruff(3, severity="low", rule_id="F401")

    class _HangThird(_Reanalyzer):
        async def rerun(
            self, analyzer_name: str, path: str, original: bytes, patched: bytes, **kw: Any
        ) -> str:
            if kw["patched_span"][0] == 3:
                await asyncio.sleep(10)
            return "passed"

    step = _step(
        _LLM(generation={"start_line": 1, "end_line": 2, "replacement": "import os\n# c\n"}),
        fetcher=_Fetcher({"a.py": content}),
        reanalyzer=_HangThird(),
        internal_timeout=1,
    )

    output = await _run(step, _Input([high, low, third], _diff(patch)))

    assert sum(f.suggestion is not None for f in output.findings) <= 1


# --- merge interaction (plan Assumption 8) and overlap ---------------------------------------


async def test_suggestion_lands_on_the_near_duplicate_that_survives_the_merge() -> None:
    # The merge keeps the first occurrence (the verified LLM finding); candidate
    # selection alone would have preferred the better-ranked analyzer finding.
    first = _ruff(1, source="llm", rule_id=None, verification_status="verified")
    duplicate = _ruff(1)
    llm, reanalyzer = _LLM(), _Reanalyzer()

    output = await _run(_step(llm, reanalyzer=reanalyzer), _Input([first, duplicate]))

    assert len(output.findings) == 2
    assert output.findings[0].suggestion is not None
    assert output.findings[1].suggestion is None
    assert llm.count(RUBRIC_MARKER) == 1
    assert reanalyzer.calls == []
    [survivor] = merge_findings(output.findings).findings
    assert survivor.suggestion is not None


async def test_overlapping_accepted_fixes_keep_only_the_higher_ranked() -> None:
    patch = "@@ -0,0 +1,3 @@\n+a = 1\n+b = 2\n+c = 3\n"
    content = b"a = 1\nb = 2\nc = 3\n"
    high, low = _ruff(1, severity="high", rule_id="A"), _ruff(2, severity="low", rule_id="B")
    llm = _LLM(generation={"start_line": 1, "end_line": 2, "replacement": "a = 1"})

    output = await _run(
        _step(llm, fetcher=_Fetcher({"a.py": content})), _Input([high, low], _diff(patch))
    )

    assert [f.suggestion is not None for f in output.findings] == [True, False]


async def test_prompt_window_is_bounded_and_only_candidate_files_are_fetched() -> None:
    lines = [f"x{i} = {i}\n" for i in range(200)]
    patch = "@@ -0,0 +1,200 @@\n" + "".join(f"+{ln}" for ln in lines)
    diff = Diff(
        files=(
            DiffFile(path="b.py", status="added", patch=patch, mode="100644"),
            DiffFile(path="c.py", status="added", patch="@@ -0,0 +1 @@\n+y = 1\n", mode="100644"),
        )
    )
    fetcher = _Fetcher({"b.py": "".join(lines).encode()})
    llm = _LLM(generation=lambda: {"start_line": 100, "end_line": 100, "replacement": "z = 0"})

    await _run(
        _step(llm, fetcher=fetcher, window=30),
        _Input([_ruff(100, file="b.py", rule_id="R1")], diff),
    )

    assert fetcher.fetch_content_calls == [["b.py"]]
    prompt = llm.requests[0].messages[1].content
    assert 0 < len(re.findall(r"^\d+\| ", prompt, flags=re.MULTILINE)) <= 30


async def test_missing_diff_is_fetched_once_and_a_fetch_failure_fails_open() -> None:
    fetcher = _Fetcher()
    output = await _run(_step(_LLM(), fetcher=fetcher), _Input([_ruff()], no_diff=True))
    assert fetcher.fetch_calls == 1
    assert output.findings[0].suggestion is not None

    broken, llm = _Fetcher(), _LLM()
    broken.fail_diff = True
    finding = _ruff()
    output = await _run(_step(llm, fetcher=broken), _Input([finding], no_diff=True))
    assert output.findings == [finding]
    assert llm.requests == []


def test_window_is_centered_clamped_and_never_larger_than_the_file() -> None:
    from review.fix_generation import _window_bounds

    assert _window_bounds(50, 200, 60) == (20, 79)
    assert _window_bounds(2, 200, 60) == (1, 60)
    assert _window_bounds(199, 200, 60) == (141, 200)
    assert _window_bounds(3, 5, 60) == (1, 5)
