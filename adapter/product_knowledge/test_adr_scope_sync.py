"""Docs-sync guard: ADR-0005 registers the pure, unit-tested modules by naming
their functions. A public module-level function added to one of the
product-knowledge pure modules without an ADR mention fails here, so the
scope entries cannot silently drift from the code. Pure: reads source text
and the ADR (read-only)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = ROOT / "docs" / "adr"
MODULES = (
    "review/product_knowledge.py",
    "ingestion/pr_metadata.py",
    "adapter/product_knowledge/egress.py",
)


def _public_functions(relative: str) -> list[str]:
    tree = ast.parse((ROOT / relative).read_text())
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


@pytest.mark.parametrize("module", MODULES)
def test_every_public_function_is_named_in_the_adr_scope_entries(module: str) -> None:
    if not ADR_DIR.is_dir():  # the `dev` image excludes docs/ (.dockerignore)
        pytest.skip("docs/adr is not present in this environment")
    (adr,) = ADR_DIR.glob("0005-*.md")
    text = adr.read_text()
    functions = _public_functions(module)

    assert functions, f"no public functions found in {module}: the guard is vacuous"
    missing = [f for f in functions if not re.search(rf"\b{re.escape(f)}\b", text)]
    assert not missing, f"{adr.name} does not name {module}'s public functions: {missing}"
