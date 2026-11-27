"""Integration tests for spec-review-context.md (US-X1 .. US-X11) and the
batching gaps of spec-diff-batching.md (US-B10 through the step, hub threshold,
import-over-call priority): review_generation, verification, spec_review and
output over fakes at the LLMClient, DiffFetcher, CommitSource, TextRedactor and
FindingsPublisher Protocol boundaries. Tree-sitter parsing is real.

US-X10 (the adapters) is covered by adapter/github/test_commits.py and
adapter/localgit/test_commits.py; US-X11 by apps/worker/test_settings.py,
apps/worker/test_main.py and apps/local_review/test_main.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import pytest

from adapter.github.diff import GitHubDiffFetcher
from adapter.localgit.diff import LocalGitDiffFetcher
from findings.models import Finding
from indexing.conftest import FakeRepoTreeFetcher
from ingestion.commits import CommitInfo, CommitsUnavailableError
from ingestion.diff import (
    Diff,
    DiffContentMalformedError,
    DiffContentUnavailableError,
    DiffFile,
)
from review.diff_batching import plan_batches
from review.diff_classify import ShrinkConfig
from review.diff_expand import ExpandConfig
from review.diff_links import build_file_links
from review.diff_prompt import INERT_REFERENCE_DATA_FRAMING, file_block_size
from review.output import FindingsOutputStep
from review.review_generation import (
    DiffBatchingConfig,
    ReviewContextConfig,
    ReviewGenerationStep,
)
from review.spec_review import SpecReviewStep
from review.test_diff_batching_steps import (
    _LLM,
    _ctx,
    _diff,
    _empty,
    _Fetcher,
    _file,
    _finding_json,
    _In,
    _Publisher,
    _Redactor,
)
from review.test_spec_review_step import (
    _SPEC_PATH,
    _SPEC_TEXT,
    _FakeDiffFetcher,
    _FakeLLM,
    _Upstream,
)
from review.verification import VerificationFileContext, VerificationStep
from review.verification_context import FileContextConfig

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BIG = 100_000  # a diff that always fits


def _config(**overrides: Any) -> ReviewContextConfig:
    values: dict[str, Any] = {
        "shrink": ShrinkConfig(),
        "expand": None,
        "pr_map_max_bytes": 0,
        "risk_ordering": False,
        "commits": None,
        "commit_max_commits": 60,
        "commit_fetch_timeout_seconds": 5.0,
    }
    return ReviewContextConfig(**{**values, **overrides})


def _gen(
    llm: _LLM,
    fetcher: _Fetcher | None = None,
    *,
    context: ReviewContextConfig | None = None,
    max_bytes: int = BIG,
    max_chunks: int = 8,
) -> ReviewGenerationStep:
    return ReviewGenerationStep(
        llm,
        redactor=_Redactor(),
        model="m",
        timeout=5.0,
        max_diff_bytes=max_bytes,
        batching=None if fetcher is None else DiffBatchingConfig(fetcher, max_chunks, 4, 5.0),  # type: ignore[arg-type]
        context=context,
    )


def _added(n: int) -> str:
    return f"@@ -0,0 +1,{n} @@\n" + "\n".join(f"+{'x' * 40} {i}" for i in range(n))


def _deleted(n: int) -> str:
    return f"@@ -1,{n} +0,0 @@\n" + "\n".join(f"-gone {i}" for i in range(n))


CASSETTE = "review/testdata/eval/cassettes/v2.json"
SMALL_FIXTURE = "review/testdata/small.json"
SMALL_PATCH = '@@ -0,0 +1,1 @@\n+{"ok": true}'
SRC_PATCH = "@@ -1 +1 @@\n-a\n+b"


# --- US-X4: data costs one line and is listed ---------------------------------


async def test_us_x4_cassette_is_stubbed_listed_and_still_in_the_pipeline_diff() -> None:
    diff = Diff(
        files=(
            DiffFile(path="app/svc.py", status="modified", patch=SRC_PATCH),
            DiffFile(path=CASSETTE, status="added", patch=_added(500)),
            DiffFile(path=SMALL_FIXTURE, status="added", patch=SMALL_PATCH),
        )
    )
    llm = _LLM(_empty)

    out = await _gen(llm, context=_config()).run(_In(diff), _ctx())

    prompt = llm.users[0]
    assert "(data file: +500 -0 lines" in prompt and "x" * 40 + " 3" not in prompt
    assert SMALL_PATCH in prompt  # under the threshold: rendered in full
    assert out.unreviewed_files == (f"{CASSETTE} (data file stubbed; not reviewed in detail)",)
    assert out.diff == diff  # static analysis and inline anchoring still see the full patch
    publisher = _Publisher()
    await FindingsOutputStep(publisher).run(
        _In(out.diff, unreviewed_files=out.unreviewed_files), _ctx()
    )
    assert publisher.reviews[0]["unreviewed_files"] == out.unreviewed_files


async def test_a_small_pr_with_nothing_stubbed_is_byte_for_byte_unchanged() -> None:
    """Byte-for-byte invariant: default-on shrink, ordering and map change nothing."""
    diff = Diff(
        files=(
            DiffFile(path="app/svc.py", status="modified", patch=SRC_PATCH),
            DiffFile(path=SMALL_FIXTURE, status="added", patch=SMALL_PATCH),
            DiffFile(path="docs/notes.md", status="modified", patch="@@ -1 +1 @@\n-x\n+y"),
        )
    )
    static = [Finding(file="app/svc.py", line=1, severity="low", message="s", source="semgrep")]
    base_llm, ctx_llm = _LLM(_empty), _LLM(_empty)
    fetcher = _Fetcher()

    base = await _gen(base_llm, fetcher).run(_In(diff, findings=static), _ctx())
    shaped = await _gen(
        ctx_llm,
        fetcher,
        context=_config(pr_map_max_bytes=8000, risk_ordering=True),
    ).run(_In(diff, findings=static), _ctx())

    assert ctx_llm.users == base_llm.users and len(ctx_llm.users) == 1
    assert shaped.model_dump() == base.model_dump()
    assert shaped.unreviewed_files == () and shaped.review_batches == ()
    assert fetcher.calls == 0  # nothing to expand, no batching decision to make
    assert "PR map" not in ctx_llm.users[0]


# --- US-X5: bulk deletions collapse -------------------------------------------


async def test_us_x5_bulk_deletion_collapses_small_and_mixed_hunks_stay() -> None:
    diff = Diff(
        files=(
            DiffFile(path="old/plan.py", status="removed", patch=_deleted(25)),
            DiffFile(path="old/small.py", status="modified", patch=_deleted(19)),
            DiffFile(
                path="old/mixed.py",
                status="modified",
                patch="@@ -1,25 +1,1 @@\n" + "\n".join(f"-gone {i}" for i in range(25)) + "\n+new",
            ),
        )
    )
    llm = _LLM(_empty)

    out = await _gen(llm, context=_config()).run(_In(diff), _ctx())

    prompt = llm.users[0]
    assert (
        "(deleted 25 lines; not shown)" in prompt and "-gone 24" not in prompt.split("old/small")[0]
    )
    assert "-gone 18" in prompt  # 19 deleted lines: shown in full
    assert prompt.count("-gone 24") == 1  # only the mixed hunk keeps its lines
    assert out.unreviewed_files == (
        "old/plan.py (deleted lines collapsed; not reviewed in detail)",
    )


# --- US-X6: prose is trimmed, spec review still works -------------------------


PLAN = "docs/notes/plan.md"


def _prose(kb: int) -> str:
    return "@@ -0,0 +1,900 @@\n" + "\n".join(
        f"+prose line number {i} " + "w" * 60 for i in range(kb * 12)
    )


async def test_us_x6_prose_is_trimmed_but_spec_review_keeps_it() -> None:
    diff = Diff(
        files=(
            DiffFile(path="app.py", status="modified", patch=SRC_PATCH),
            DiffFile(path=_SPEC_PATH, status="added", patch=_prose(70)),
            DiffFile(path=CASSETTE, status="added", patch=_added(500)),
        )
    )
    llm = _LLM(_empty)
    out = await _gen(llm, context=_config()).run(_In(diff), _ctx())
    assert "prose truncated" in llm.users[0]
    assert f"{_SPEC_PATH} (prose truncated; not reviewed in detail)" in out.unreviewed_files

    spec_llm = _FakeLLM()
    step = SpecReviewStep(
        spec_llm,
        _FakeDiffFetcher({_SPEC_PATH: _SPEC_TEXT.encode()}),
        FakeRepoTreeFetcher(),
        redactor=_Redactor(),
        model="m",
        timeout=5.0,
        max_diff_bytes=10_000_000,
        internal_timeout_seconds=5.0,
        shrink=ShrinkConfig(),
    )
    spec_out = await step.run(_Upstream(diff=diff), _ctx())

    sent = spec_llm.calls[0].messages[-1].content
    assert spec_out.spec is not None and spec_out.spec.path == _SPEC_PATH
    assert "prose truncated" not in sent and _prose(70) in sent  # prose kept whole
    assert "(data file: +500 -0 lines" in sent  # data is still stubbed here


# --- US-X3: a hunk shows its enclosing function -------------------------------

SAVE_HEAD = (
    b"def save(rec):\n    a = 1\n    b = 2\n    rec.value = a + b + 1\n    c = 3\n    return c\n"
)
SAVE_PATCH = (
    "@@ -3,3 +3,3 @@\n     b = 2\n-    rec.value = a + b\n+    rec.value = a + b + 1\n     c = 3"
)


def _save_diff(extra: Sequence[DiffFile] = ()) -> Diff:
    return Diff(files=(DiffFile(path="app/save.py", status="modified", patch=SAVE_PATCH), *extra))


async def test_us_x3_hunk_is_shown_with_its_enclosing_function() -> None:
    llm, fetcher = _LLM(_empty), _Fetcher({"app/save.py": SAVE_HEAD})

    await _gen(llm, fetcher, context=_config(expand=ExpandConfig())).run(_In(_save_diff()), _ctx())

    prompt = llm.users[0]
    assert "     a = 1\n" in prompt and "     return c" in prompt  # unchanged lines, as context
    assert "@@ -1,6 +1,6 @@ save" in prompt  # header counts rewritten, symbol named
    assert fetcher.calls == 1


async def test_us_x3_flag_off_is_identical_and_never_fetches() -> None:
    base_llm, off_llm = _LLM(_empty), _LLM(_empty)
    fetcher = _Fetcher({"app/save.py": SAVE_HEAD})

    await _gen(base_llm, fetcher).run(_In(_save_diff()), _ctx())
    await _gen(off_llm, fetcher, context=_config()).run(_In(_save_diff()), _ctx())

    assert off_llm.users == base_llm.users and fetcher.calls == 0


@pytest.mark.parametrize("case", ["fetch_error", "timeout", "mismatch", "unsupported", "added"])
async def test_us_x3_every_degradation_leaves_the_patch_unchanged(case: str) -> None:
    extra: tuple[DiffFile, ...] = ()
    files: dict[str, bytes] = {"app/save.py": SAVE_HEAD}
    fetcher: _Fetcher = _Fetcher(files)
    if case == "fetch_error":
        fetcher = _Fetcher(error=DiffContentUnavailableError(repo_full_name="a/b", detail="x"))
    elif case == "timeout":

        class _Slow(_Fetcher):
            async def fetch_content(self, **_: Any) -> dict[str, bytes]:
                await asyncio.sleep(1)
                return files

        fetcher = _Slow(files)
    elif case == "mismatch":
        fetcher = _Fetcher({"app/save.py": b"completely\ndifferent\nfile\nhere\n"})
    elif case == "unsupported":
        extra = (DiffFile(path="notes.cfg", status="modified", patch=SRC_PATCH),)
        files = {}
    base_llm, llm = _LLM(_empty), _LLM(_empty)
    diff = _save_diff(extra)
    if case == "added":
        diff = Diff(files=(DiffFile(path="app/save.py", status="added", patch=SAVE_PATCH),))
    if case == "unsupported":
        diff = Diff(files=extra)

    await _gen(base_llm, _Fetcher(), max_bytes=BIG).run(_In(diff), _ctx())
    cfg = _config(expand=ExpandConfig())
    batching = DiffBatchingConfig(fetcher, 8, 4, 0.05)  # type: ignore[arg-type]
    step = ReviewGenerationStep(
        llm, redactor=_Redactor(), model="m", timeout=5.0, max_diff_bytes=BIG,
        batching=batching, context=cfg,
    )  # fmt: skip
    out = await step.run(_In(diff), _ctx())

    assert llm.users == base_llm.users and out.findings == []


# --- US-X7: every batch knows the whole PR ------------------------------------

MAP_MAX = 1000
BODIES = {
    "d0/f.py": b"def alpha():\n    return 1\n",
    "d1/f.py": b"def beta():\n    return 1\n",
    "d2/f.py": b"def gamma():\n    return 1\n",
}


def _map_step(llm: _LLM, fetcher: _Fetcher) -> ReviewGenerationStep:
    return _gen(llm, fetcher, context=_config(pr_map_max_bytes=MAP_MAX), max_bytes=1400)


def _section(prompt: str) -> str:
    return prompt.split("PR map:", 1)[1].split("\n", 1)[1]


async def test_us_x7_every_batch_carries_the_same_map_with_its_own_label() -> None:
    paths = [*BODIES, "d3/f.py", "d4/f.py", "d5/f.py"]  # d3..d5: no content, no symbols
    llm = _LLM(_empty)

    out = await _map_step(llm, _Fetcher(BODIES)).run(_In(_diff(*paths)), _ctx())

    assert len(out.review_batches) == len(llm.users) == 6
    for prompt in llm.users:
        assert INERT_REFERENCE_DATA_FRAMING in prompt.split("PR map:", 1)[1]
        assert len(prompt.split("PR map:", 1)[1].encode()) <= MAP_MAX
        own = next(b[0] for b in out.review_batches if f"--- {b[0]} " in prompt)
        lines = _section(prompt).splitlines()
        assert [ln for ln in lines if "this batch" in ln] == [
            next(ln for ln in lines if ln.startswith(own))
        ]
    anonymised = {
        "|".join(
            ln.replace("this batch", "batch").split(" [")[0] for ln in _section(p).splitlines()
        )
        for p in llm.users
    }
    assert len(anonymised) == 1  # identical apart from the labels
    assert any(
        ln.startswith("d0/f.py") and ln.endswith("alpha")
        for ln in _section(llm.users[0]).splitlines()
    )
    assert any(
        ln.startswith("d4/f.py") and "alpha" not in ln for ln in _section(llm.users[0]).splitlines()
    )


async def test_us_x7_a_diff_that_fits_gets_no_map() -> None:
    llm = _LLM(_empty)
    await _map_step(llm, _Fetcher(BODIES)).run(_In(_diff(*BODIES)), _ctx())
    assert len(llm.users) == 1 and "PR map" not in llm.users[0]


async def test_us_x7_files_cut_by_the_cap_are_labelled_not_reviewed() -> None:
    llm = _LLM(_empty)
    paths = [f"d{i}/f.py" for i in range(6)]
    step = _gen(
        llm, _Fetcher(), context=_config(pr_map_max_bytes=MAP_MAX), max_bytes=1400, max_chunks=2
    )

    out = await step.run(_In(_diff(*paths)), _ctx())

    assert out.unreviewed_files
    for dropped in out.unreviewed_files:
        assert any(
            f"{dropped} (modified +1/-1) [not reviewed]" in ln
            for ln in _section(llm.users[0]).splitlines()
        )


# --- US-X8: the cap drops the least valuable files first ----------------------


async def test_us_x8_cap_drops_docs_and_data_before_source_and_lists_them() -> None:
    paths = ["docs/plan.md", "testdata/data.json", "pkg/a.py", "other/b.py"]
    diff = _diff(*paths)
    llm, off_llm = _LLM(_empty), _LLM(_empty)
    on = _gen(
        llm,
        _Fetcher(),
        context=_config(risk_ordering=True, shrink=None),
        max_bytes=400,
        max_chunks=2,
    )
    off = _gen(off_llm, _Fetcher(), context=_config(shrink=None), max_bytes=400, max_chunks=2)

    out = await on.run(_In(diff), _ctx())
    off_out = await off.run(_In(diff), _ctx())

    assert set(out.unreviewed_files) == {"docs/plan.md", "testdata/data.json"}
    assert {p for b in out.review_batches for p in b} == {"pkg/a.py", "other/b.py"}
    # Flag off: exactly the diff-order plan.
    sizes = {p: file_block_size(_file(p)) for p in paths}
    assert (
        off_out.unreviewed_files
        == plan_batches(paths, sizes, {}, max_bytes=400, max_chunks=2).dropped
    )


# --- US-X9: commit-by-commit --------------------------------------------------


class _Commits:
    """Linear history base <- c1 <- ... : commit i touches exactly file i."""

    def __init__(
        self, paths: Sequence[str], *, patch: dict[str, str], broken: bool = False
    ) -> None:
        self.paths, self.patch, self.broken = list(paths), patch, broken
        self.shas = [f"c{i + 1}" for i in range(len(paths) - 1)] + ["abc"]  # abc: the job head
        self.range_calls = 0

    def commits(self, *, nonlinear: bool = False) -> tuple[CommitInfo, ...]:
        parents = ["base", *self.shas[:-1]]
        if nonlinear:
            parents[2] = "elsewhere"
        return tuple(
            CommitInfo(sha=s, parent_shas=(p,)) for s, p in zip(self.shas, parents, strict=True)
        )

    async def list_commits(self, **_: Any) -> tuple[CommitInfo, ...]:
        if self.broken:
            raise CommitsUnavailableError(repo_full_name="a/b", detail="boom")
        return self.commits()

    async def fetch_range_diff(self, *, base_sha: str, head_sha: str, **_: Any) -> Diff:
        self.range_calls += 1
        order = ["base", *self.shas]
        picked = self.paths[order.index(base_sha) : order.index(head_sha)]
        return Diff(
            files=tuple(DiffFile(path=p, status="added", patch=self.patch[p]) for p in picked)
        )


COMMIT_PATHS = [f"d{i}/f.py" for i in range(6)]
COMMIT_PATCH = {
    "d0/f.py": "@@ -0,0 +1,1 @@\n+result = compute_value(0)",  # moved to head line 2
    "d1/f.py": "@@ -0,0 +1,1 @@\n+result = compute_value(1)",  # gone at head
    "d2/f.py": "@@ -0,0 +1,1 @@\n+pass",  # trivial: file-level
    "d3/f.py": "@@ -0,0 +1,1 @@\n+result = compute_value(3)",  # unchanged line
    "d4/f.py": "@@ -0,0 +1,1 @@\n+result = compute_value(4)",
    "d5/f.py": "@@ -0,0 +1,1 @@\n+result = compute_value(5)",
}
COMMIT_HEAD = {
    "d0/f.py": b"x = 0\nresult = compute_value(0)\n",
    "d1/f.py": b"x = 0\nsomething_else = 1\n",
    "d2/f.py": b"pass\n",
    "d3/f.py": b"result = compute_value(3)\n",
}


def _commit_llm() -> _LLM:
    findings = [_finding_json(p, f"bug in {p}", 1) for p in COMMIT_PATHS[:4]]
    return _LLM(lambda _: json.dumps({"findings": findings}))


def _commit_step(
    llm: _LLM, fetcher: _Fetcher, source: _Commits, **cfg: Any
) -> ReviewGenerationStep:
    context = _config(commits=source, shrink=None, **cfg)
    return _gen(llm, fetcher, context=context, max_bytes=600, max_chunks=2)


async def test_us_x9_commit_slices_cover_the_pr_and_findings_are_anchored_at_head() -> None:
    llm, source = _commit_llm(), _Commits(COMMIT_PATHS, patch=COMMIT_PATCH)

    out = await _commit_step(llm, _Fetcher(COMMIT_HEAD), source).run(
        _In(_diff(*COMMIT_PATHS)), _ctx()
    )

    assert len(llm.users) == 1 <= 2  # one slice, within the cap
    assert out.unreviewed_files == () and [p for b in out.review_batches for p in b] == COMMIT_PATHS
    by_file = {f.file: f.line for f in out.findings}
    assert by_file == {"d0/f.py": 2, "d2/f.py": None, "d3/f.py": 1}  # d1 is gone at head


async def test_us_x9_unreachable_head_content_keeps_findings_file_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FlakyFetcher(_Fetcher):
        async def fetch_content(self, **_: Any) -> dict[str, bytes]:
            self.calls += 1
            if self.calls > 1:  # the planning fetch works, the anchoring fetch does not
                raise DiffContentUnavailableError(repo_full_name="a/b", detail="x")
            return self.contents

    llm, source = _commit_llm(), _Commits(COMMIT_PATHS, patch=COMMIT_PATCH)
    caplog.set_level(logging.WARNING)

    out = await _commit_step(llm, _FlakyFetcher(COMMIT_HEAD), source).run(
        _In(_diff(*COMMIT_PATHS)), _ctx()
    )

    assert {f.file: f.line for f in out.findings} == dict.fromkeys(COMMIT_PATHS[:4])
    assert any("findings kept file-level" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("case", ["list_fails", "too_many_commits", "non_linear"])
async def test_us_x9_falls_back_to_batching(case: str) -> None:
    source = _Commits(COMMIT_PATHS, patch=COMMIT_PATCH, broken=case == "list_fails")
    if case == "non_linear":
        source.commits = lambda **kw: _Commits.commits(source, nonlinear=True)  # type: ignore[method-assign]
    llm = _commit_llm()
    extra = {"commit_max_commits": 3} if case == "too_many_commits" else {}

    out = await _commit_step(llm, _Fetcher(COMMIT_HEAD), source, **extra).run(
        _In(_diff(*COMMIT_PATHS)), _ctx()
    )

    assert len(llm.users) == 2  # the batch plan, capped
    assert out.unreviewed_files and len(out.review_batches) == 2


async def test_us_x9_off_by_default_even_when_batching_drops_files() -> None:
    llm = _LLM(_empty)
    source = _Commits(COMMIT_PATHS, patch=COMMIT_PATCH)
    step = _gen(llm, _Fetcher(), context=_config(shrink=None), max_bytes=600, max_chunks=2)

    out = await step.run(_In(_diff(*COMMIT_PATHS)), _ctx())

    assert source.range_calls == 0 and out.unreviewed_files


# --- US-X1 / US-X2: verification with head-file context -----------------------

TEST_FILE = "tests/test_x.py"
TEST_HEAD = (
    b"import pytest\npytestmark = pytest.mark.integration\n\n\nasync def test_a():\n    pass\n"
)
MARK = "pytestmark = pytest.mark.integration"


def _refuting(user: str) -> str:
    valid = MARK not in user  # the module-level mark refutes "missing integration mark"
    verdict = {"index": 0, "valid": valid, "actionable": True, "specific": True, "valuable": True}
    return json.dumps({"verdicts": [verdict]})


def _all_valid(_: str) -> str:
    verdict = {"valid": True, "actionable": True, "specific": True, "valuable": True}
    return json.dumps({"verdicts": [{"index": i, **verdict} for i in range(3)]})


def _llm_finding(file: str, line: int = 5, message: str = "missing integration mark") -> Finding:
    return Finding(file=file, line=line, severity="high", message=message, source="llm")


def _verify(
    llm: _LLM,
    fetcher: _Fetcher | None,
    *,
    timeout: float = 1.0,
    config: FileContextConfig | None = None,
) -> VerificationStep:
    context = (
        None
        if fetcher is None
        else VerificationFileContext(fetcher, config or FileContextConfig(), timeout)  # type: ignore[arg-type]
    )
    return VerificationStep(
        llm, model="m", timeout=5.0, max_diff_bytes=BIG, batch_concurrency=2, file_context=context
    )


def _test_diff(*extra: str) -> Diff:
    return Diff(
        files=tuple(
            DiffFile(path=p, status="modified", patch=SRC_PATCH) for p in (TEST_FILE, *extra)
        )
    )


async def test_us_x1_a_claim_refuted_by_code_outside_the_diff_is_rejected() -> None:
    plain_llm, ctx_llm = _LLM(_refuting), _LLM(_refuting)
    finding = _llm_finding(TEST_FILE)

    plain = await _verify(plain_llm, None).run(_In(_test_diff(), findings=[finding]), _ctx())
    judged = await _verify(ctx_llm, _Fetcher({TEST_FILE: TEST_HEAD})).run(
        _In(_test_diff(), findings=[finding]), _ctx()
    )

    assert len(plain.findings) == 1  # diff-only: the false positive survives
    assert judged.findings == []  # with head context: refuted
    prompt = ctx_llm.users[0]
    assert f"--- {TEST_FILE}\n" in prompt and INERT_REFERENCE_DATA_FRAMING in prompt
    assert prompt.index(MARK) > prompt.index("Diff:")


async def test_us_x1_context_appears_once_per_file_and_large_files_get_windows() -> None:
    big = "\n".join(f"line_{i} = {i}" for i in range(1, 401)).encode()
    llm = _LLM(_all_valid)
    findings = [_llm_finding(TEST_FILE, 200, "one"), _llm_finding(TEST_FILE, 201, "two")]
    config = FileContextConfig(max_bytes_per_file=2000, surrounding_lines=5)

    await _verify(llm, _Fetcher({TEST_FILE: big}), config=config).run(
        _In(_test_diff(), findings=findings), _ctx()
    )

    prompt = llm.users[0]
    assert prompt.count(f"--- {TEST_FILE}\n") == 1
    assert "line_200 = 200" in prompt and "line_1 = 1\n" not in prompt  # a window, not the file
    assert "200| line_200" in prompt  # with line numbers


async def test_us_x1_batched_groups_get_only_their_own_files_context() -> None:
    heads = {"a.py": b"a_marker = 1\n", "b.py": b"b_marker = 1\n"}
    llm = _LLM(_all_valid)
    diff = _test_diff("a.py", "b.py")

    await _verify(llm, _Fetcher(heads)).run(
        _In(
            diff,
            findings=[_llm_finding("a.py"), _llm_finding("b.py")],
            review_batches=(("a.py",), ("b.py",)),
        ),
        _ctx(),
    )

    a_call = next(u for u in llm.users if "a_marker" in u)
    b_call = next(u for u in llm.users if "b_marker" in u)
    assert "b_marker" not in a_call and "a_marker" not in b_call and len(llm.users) == 2


class _Rec(_Fetcher):
    async def fetch_content(self, **kwargs: Any) -> dict[str, bytes]:
        self.paths = list(kwargs["paths"])
        return await super().fetch_content(**kwargs)


@pytest.mark.parametrize(
    "case", ["unavailable", "malformed", "token", "timeout", "binary", "secret_path", "off_diff"]
)
async def test_us_x2_every_fetch_problem_degrades_to_diff_only(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    cited, files = TEST_FILE, {TEST_FILE: TEST_HEAD}
    diff = _test_diff()
    fetcher: _Fetcher = _Rec(files)
    timeout = 1.0
    if case == "unavailable":
        fetcher = _Fetcher(error=DiffContentUnavailableError(repo_full_name="a/b", detail="x"))
    elif case == "malformed":
        fetcher = _Fetcher(error=DiffContentMalformedError(repo_full_name="a/b", detail="x"))
    elif case == "token":  # an adapter error the domain cannot name (GitHubTokenExchangeError)
        fetcher = _Fetcher(error=RuntimeError("token exchange failed"))
    elif case == "timeout":
        timeout = 0.05

        class _Slow(_Fetcher):
            async def fetch_content(self, **_: Any) -> dict[str, bytes]:
                await asyncio.sleep(1)
                return files

        fetcher = _Slow()
    elif case == "binary":
        fetcher = _Rec({TEST_FILE: b"\x00\x01binary" + TEST_HEAD})
    elif case == "secret_path":
        cited, diff = (
            ".env",
            Diff(files=(DiffFile(path=".env", status="modified", patch=SRC_PATCH),)),
        )
        fetcher = _Rec({".env": b"TOKEN=abc"})
    elif case == "off_diff":
        cited = "elsewhere.py"
        fetcher = _Rec({"elsewhere.py": b"x = 1\n"})
    llm = _LLM(_all_valid)
    caplog.set_level(logging.WARNING)

    out = await _verify(llm, fetcher, timeout=timeout).run(
        _In(diff, findings=[_llm_finding(cited)]), _ctx()
    )

    assert len(out.findings) == 1 and "Current (head) version" not in llm.users[0]
    if case in {"secret_path", "off_diff"}:
        assert fetcher.calls == 0  # never even asked for
    if case in {"unavailable", "malformed", "token", "timeout"}:
        assert any("head file context unavailable" in r.getMessage() for r in caplog.records)


async def test_us_x2_flag_off_is_byte_identical_and_never_fetches() -> None:
    base_llm, off_llm = _LLM(_all_valid), _LLM(_all_valid)
    fetcher = _Fetcher({TEST_FILE: TEST_HEAD})
    job = _In(_test_diff(), findings=[_llm_finding(TEST_FILE)])

    await VerificationStep(base_llm, model="m", timeout=5.0, max_diff_bytes=BIG).run(job, _ctx())
    await _verify(off_llm, None).run(job, _ctx())

    assert off_llm.users == base_llm.users and fetcher.calls == 0


# --- US-X10 -------------------------------------------------------------------


async def test_us_x10_the_existing_fetchers_gain_no_commit_methods() -> None:
    for fetcher in (GitHubDiffFetcher, LocalGitDiffFetcher):
        assert not hasattr(fetcher, "list_commits") and not hasattr(fetcher, "fetch_range_diff")


# --- spec-diff-batching.md gaps: US-B10, hub threshold, import-over-call ------


def _py(name: str, body: str) -> dict[str, bytes]:
    return {name: body.encode()}


async def test_us_b10_python_files_are_linked_through_the_step_by_a_call() -> None:
    contents = {
        **_py("y9/orders.py", "def place_order():\n    return reconcile_ledger()\n"),
        **_py("b0/ledger.py", "def reconcile_ledger():\n    return 1\n"),
    }
    paths = ["b0/ledger.py", "c1/f.py", "c2/f.py", "c3/f.py", "c4/f.py", "y9/orders.py"]

    out = await _gen(_LLM(_empty), _Fetcher(contents), max_bytes=600).run(
        _In(_diff(*paths)), _ctx()
    )

    together = [b for b in out.review_batches if "y9/orders.py" in b]
    assert len(together) == 1 and "b0/ledger.py" in together[0]


async def test_hub_files_do_not_pull_their_importers_together() -> None:
    leaves = {
        f"d{i}/leaf{i}.py": (
            f"from hub.core import core_thing\n\ndef leaf_{i}_main():\n    core_thing()\n"
        ).encode()
        for i in range(9)
    }
    contents = {**leaves, "hub/core.py": b"def core_thing():\n    return 1\n"}
    paths = ["hub/core.py", *leaves]
    sizes = {p: file_block_size(_file(p)) for p in paths}
    links = build_file_links(contents).links
    assert any("hub/core.py" in v for v in links.values())  # the edges exist ...

    out = await _gen(_LLM(_empty), _Fetcher(contents), max_bytes=600).run(
        _In(_diff(*paths)), _ctx()
    )

    # ... and the step packs exactly what the pure planner does with them.
    expected = plan_batches(paths, sizes, links, max_bytes=600, max_chunks=8)
    assert out.review_batches == expected.batches
    assert all(len(b) <= 2 for b in out.review_batches)


async def test_an_import_link_outranks_a_call_link_in_the_step() -> None:
    contents = {
        **_py("a/s.py", "from z.m1 import thing_one\n\ndef s_main():\n    return thing_two()\n"),
        **_py("b/m2.py", "def thing_two():\n    return 2\n"),
        **_py("z/m1.py", "def thing_one():\n    return 1\n"),
    }
    paths = ["a/s.py", "b/m2.py", "c1/f.py", "c2/f.py", "c3/f.py", "z/m1.py"]

    out = await _gen(_LLM(_empty), _Fetcher(contents), max_bytes=600).run(
        _In(_diff(*paths)), _ctx()
    )

    batch = next(b for b in out.review_batches if "a/s.py" in b)
    assert "z/m1.py" in batch and "b/m2.py" not in batch


# --- US-X9: what commit mode did not review in detail is listed ----------------


class _Touch:
    """Linear history; the job head is ``abc``. ``history`` maps each commit
    sha to the ``{path: patch}`` it changes."""

    def __init__(self, history: dict[str, dict[str, str]]) -> None:
        self.history = history

    async def list_commits(self, **_: Any) -> tuple[CommitInfo, ...]:
        shas = list(self.history)
        parents = ["base", *shas[:-1]]
        return tuple(
            CommitInfo(sha=s, parent_shas=(p,)) for s, p in zip(shas, parents, strict=True)
        )

    async def fetch_range_diff(self, *, base_sha: str, head_sha: str, **_: Any) -> Diff:
        order = ["base", *self.history]
        picked: dict[str, str] = {}
        for sha in order[order.index(base_sha) + 1 : order.index(head_sha) + 1]:
            picked.update(self.history[sha])
        return Diff(
            files=tuple(DiffFile(path=p, status="modified", patch=v) for p, v in picked.items())
        )


def _body(tag: str, size: int = 200) -> str:
    return f"@@ -1 +1 @@\n-old\n+{tag} " + "x" * size


def _touch_step(
    llm: _LLM,
    source: _Touch,
    pr_files: dict[str, str],
    *,
    chunks: int,
    max_bytes: int = 300,
) -> tuple[ReviewGenerationStep, Diff]:
    step = _gen(
        llm,
        _Fetcher(),
        context=_config(commits=source, shrink=ShrinkConfig()),
        max_bytes=max_bytes,
        max_chunks=chunks,
    )
    diff = Diff(
        files=tuple(DiffFile(path=p, status="modified", patch=v) for p, v in pr_files.items())
    )
    return step, diff


async def test_us_x9_files_of_commits_dropped_by_the_cap_are_listed_even_if_partly_reviewed() -> (
    None
):
    history = {f"k{i}": {f"{n}.py": _body(n)} for i, n in enumerate("abcde", 1)}
    history["abc"] = {"a.py": _body("a2")}  # a.py changes again in the last commit
    step, diff = _touch_step(
        _LLM(_empty), _Touch(history), {f"{n}.py": _body(n) for n in "abcde"}, chunks=2
    )

    out = await step.run(_In(diff), _ctx())

    assert set(out.unreviewed_files) == {"a.py", "c.py", "d.py", "e.py"}  # b.py is fully reviewed


async def test_us_x9_a_file_of_a_failed_slice_is_listed_although_another_slice_touched_it() -> None:
    history = {
        "k1": {"a.py": _body("BOOM")},
        "k2": {"b.py": _body("b")},
        "abc": {"a.py": _body("a2")},
    }

    def respond(user: str) -> str:
        if "BOOM" in user:
            raise RuntimeError("llm failed")
        return _empty(user)

    pr_files = {f"{n}.py": _body(n) for n in "abyz"}
    step, diff = _touch_step(_LLM(respond), _Touch(history), pr_files, chunks=3)

    out = await step.run(_In(diff), _ctx())

    assert "a.py" in out.unreviewed_files and "b.py" not in out.unreviewed_files


async def test_us_x9_a_file_shrunk_inside_a_slice_is_listed() -> None:
    big = _body("S", 3000)
    history = {
        "k1": {"testdata/x.json": "@@ -0,0 +1 @@\n+" + "y" * 12_000},
        "k2": {"s1.py": big},
        "abc": {"s2.py": big},
    }
    pr_files = {"testdata/x.json": "@@ -0,0 +1 @@\n+{}", "s1.py": big, "s2.py": big, "s3.py": big}
    step, diff = _touch_step(_LLM(_empty), _Touch(history), pr_files, chunks=3, max_bytes=2500)

    out = await step.run(_In(diff), _ctx())

    assert any(u.startswith("testdata/x.json (data file stubbed") for u in out.unreviewed_files)


async def test_us_x9_a_commit_list_not_ending_at_the_job_head_falls_back_to_batching() -> None:
    history = {f"k{i}": {f"{n}.py": _body(n)} for i, n in enumerate("abcd", 1)}  # ends at k4
    pr_files = {f"{n}.py": _body(n) for n in "abcd"}
    step, diff = _touch_step(_LLM(_empty), _Touch(history), pr_files, chunks=2)

    out = await step.run(_In(diff), _ctx())

    assert len(out.review_batches) == 2 and out.unreviewed_files  # the batch plan, capped
