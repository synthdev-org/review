"""Packaging guards for the fake MCP provider (plan Phase 4, Assumption 12):
it is test tooling, so it must never be mutated and never ship in the runtime
image. A regression in either file would otherwise be silent. Pure: reads
``pyproject.toml`` and ``Dockerfile`` from the repo root."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTING_DIR = "adapter/product_knowledge/testing"
SAMPLE = f"{TESTING_DIR}/fake_provider.py"


def test_mutmut_does_not_mutate_the_fake_provider_package() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = config["tool"]["mutmut"]["do_not_mutate"]

    assert isinstance(patterns, list)
    assert any(fnmatch.fnmatch(SAMPLE, p) for p in patterns), patterns
    assert any(fnmatch.fnmatch(f"{TESTING_DIR}/loopback.py", p) for p in patterns), patterns
    # the glob must not swallow production modules
    assert not any(fnmatch.fnmatch("adapter/product_knowledge/client.py", p) for p in patterns)


def _runtime_stage_lines() -> list[str]:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"FROM\s+.*\s+AS\s+runtime\s*$", ln))
    stage: list[str] = []
    for ln in lines[start + 1 :]:
        if re.match(r"FROM\s", ln):
            break
        stage.append(ln.strip())
    return stage


def test_runtime_stage_removes_the_fake_provider_after_copying_adapter() -> None:
    stage = _runtime_stage_lines()
    copy = next(i for i, ln in enumerate(stage) if re.match(r"COPY\s+adapter\s+\./adapter\b", ln))
    removals = [
        i
        for i, ln in enumerate(stage)
        if ln.startswith("RUN ") and "rm " in ln and f"./{TESTING_DIR}" in ln
    ]

    assert removals, "runtime stage must `rm -rf` adapter/product_knowledge/testing"
    assert min(removals) > copy, "the removal must come after `COPY adapter`"
    # nothing later re-adds the directory (a second COPY of adapter/ would)
    assert not any(re.match(r"COPY\s+adapter\b", ln) for ln in stage[removals[0] + 1 :])
