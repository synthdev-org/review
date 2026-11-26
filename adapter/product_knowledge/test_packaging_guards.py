"""
Packaging guards for test-tooling fakes: the fake MCP provider and the in-memory fake Classifier.

They are test tooling, so it must never be mutated and never ship in the runtime
image. A regression in either file would otherwise be silent. Pure: reads
``pyproject.toml`` and ``Dockerfile`` from the repo root.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTING_DIR = "adapter/product_knowledge/testing"
SAMPLE = f"{TESTING_DIR}/fake_provider.py"
# (testing dir, a production module beside it that must stay mutable)
FAKES = [
    (TESTING_DIR, "adapter/product_knowledge/client.py"),
    ("llmgateway/testing", "llmgateway/classifier.py"),
]


def test_mutmut_does_not_mutate_the_fake_provider_package() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = config["tool"]["mutmut"]["do_not_mutate"]

    assert isinstance(patterns, list)
    assert any(fnmatch.fnmatch(SAMPLE, p) for p in patterns), patterns
    assert any(fnmatch.fnmatch(f"{TESTING_DIR}/loopback.py", p) for p in patterns), patterns
    assert any(fnmatch.fnmatch("llmgateway/testing/fake_classifier.py", p) for p in patterns)
    # the glob must not swallow production modules
    for _, production in FAKES:
        assert not any(fnmatch.fnmatch(production, p) for p in patterns), production


def _runtime_stage_lines() -> list[str]:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"FROM\s+.*\s+AS\s+runtime\s*$", ln))
    stage: list[str] = []
    for ln in lines[start + 1 :]:
        if re.match(r"FROM\s", ln):
            break
        stage.append(ln.strip())
    return stage


@pytest.mark.parametrize("testing_dir", [d for d, _ in FAKES])
def test_runtime_stage_removes_the_fake_after_copying_its_package(testing_dir: str) -> None:
    stage = _runtime_stage_lines()
    package = testing_dir.split("/")[0]
    copy = next(
        i for i, ln in enumerate(stage) if re.match(rf"COPY\s+{package}\s+\./{package}\b", ln)
    )
    removals = [
        i
        for i, ln in enumerate(stage)
        if ln.startswith("RUN ") and "rm " in ln and f"./{testing_dir}" in ln
    ]

    assert removals, f"runtime stage must `rm -rf` {testing_dir}"
    assert min(removals) > copy, f"the removal must come after `COPY {package}`"
    # nothing later re-adds the directory (a second COPY of the package would)
    assert not any(re.match(rf"COPY\s+{package}\b", ln) for ln in stage[removals[0] + 1 :])
