"""Value test (b) for MCP knowledge: does context from a real server improve a real review?

A recorded model cannot show that (plan Phase 7; ADR-0017 Q2), so this is the live-mode check:
manual or nightly only, never the default CI gate (docs/rules/testing.md). It is skipped unless
``REVIEW_LIVE_MCP=1``. It makes real model calls with your own endpoint and starts or contacts the
real MCP server you name, from your own MCP configuration.

Procedure (ADR-0017, "Manual live-run procedure to settle Q2"):

1. Pick a real server that holds a fact absent from the repository (an ADR, runbook or policy
   page), and a fixture repository whose feature branch violates that fact in a way a review
   without the context does not flag. Note a short phrase that appears in a finding only if the
   fact was used (``REVIEW_LIVE_MCP_FACT``).
2. Read the query first: ``review run --base main --mcp NAME --dry-run`` prints the exact query and
   the tool, and starts nothing.
3. Run, from the repository's parent (all variables are read here, none from the repository)::

       REVIEW_LIVE_MCP=1 \\
       REVIEW_LIVE_MCP_REPO=/path/to/fixture-repo REVIEW_LIVE_MCP_BASE=main \\
       REVIEW_LIVE_MCP_SERVER=docs REVIEW_LIVE_MCP_FACT="idempotency key" \\
       REVIEW_LOCAL_LLM_ENDPOINT=... REVIEW_LOCAL_LLM_MODEL=... \\
       pytest -m integration apps/local_cli/test_mcp_live.py -s

   It runs the review ``REVIEW_LIVE_MCP_RUNS`` times (default 5) without the nomination and the
   same number with it, and prints one row per run: the exit status, whether a finding carries the
   fact phrase, the finding count and the summary's MCP line. ``REVIEW_LIVE_MCP_OUT`` also writes
   the table to a file.
4. Go if the phrase appears in at least 3 of 5 runs with context and 0 of 5 without, and the
   with-context runs add no false positives (read the findings: the finding count column is the
   first hint). Otherwise no-go: ADR-0017 Decision 11 withdraws US-L9 to US-L11.

The assertions below are only about the plumbing (every run completed; the nominated server was
consulted in the runs that name it). Whether it helped is the table's, and the reader's, to judge.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pytest

from apps.local_cli.main import EXIT_OK, run_review
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("REVIEW_LIVE_MCP") != "1",
        reason=(
            "live mode: set REVIEW_LIVE_MCP=1 with REVIEW_LIVE_MCP_REPO, REVIEW_LIVE_MCP_SERVER, "
            "REVIEW_LIVE_MCP_FACT and a model endpoint (see this file's docstring)"
        ),
    ),
]


async def _run(repo: Path, base: str, mcp: tuple[str, ...]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(LocalSettings(), base=base, repo_dir=repo, out=out, err=err, mcp=mcp)
    return status, out.getvalue(), err.getvalue()


@pytest.mark.asyncio
async def test_live_runs_with_and_without_the_nomination_are_recorded() -> None:
    repo = Path(os.environ["REVIEW_LIVE_MCP_REPO"])
    base = os.environ.get("REVIEW_LIVE_MCP_BASE", "main")
    server = os.environ["REVIEW_LIVE_MCP_SERVER"]
    fact = os.environ["REVIEW_LIVE_MCP_FACT"].lower()
    runs = int(os.environ.get("REVIEW_LIVE_MCP_RUNS", "5"))
    rows = ["nominated | run | exit | fact in a finding | findings | mcp servers"]
    consulted_when_nominated = True

    for nominated in (False, True):
        for number in range(1, runs + 1):
            status, out, err = await _run(repo, base, (server,) if nominated else ())
            findings = [line for line in out.splitlines() if re.match(r"\S+: \w+: ", line)]
            mcp_line = next(
                (x.strip() for x in err.splitlines() if x.strip().startswith("mcp servers:")), ""
            )
            rows.append(
                f"{'yes' if nominated else 'no'} | {number} | {status} | "
                f"{'yes' if any(fact in f.lower() for f in findings) else 'no'} | "
                f"{len(findings)} | {mcp_line}"
            )
            assert status == EXIT_OK
            if nominated and f"used {server}" not in mcp_line:
                consulted_when_nominated = False

    table = "\n".join(rows)
    print("\n" + table)
    target = os.environ.get("REVIEW_LIVE_MCP_OUT")
    if target:
        Path(target).write_text(table + "\n")
    assert consulted_when_nominated, "the nominated server did not supply context in every run"
