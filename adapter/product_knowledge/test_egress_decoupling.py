"""Guards the Phase 5 split: adapter/egress is neutral and still agrees with product knowledge."""

from __future__ import annotations

import ast
from pathlib import Path

from adapter.egress.transport import DEFAULT_MAX_RESPONSE_BYTES, EgressOutcome
from review.product_knowledge import MAX_RESPONSE_BYTES, ProviderOutcome

EGRESS_DIR = Path(__file__).resolve().parents[1] / "egress"


def test_egress_package_imports_no_domain_or_sibling_adapter() -> None:
    for path in EGRESS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not name.startswith(("review", "adapter.product_knowledge")), (path, name)


def test_neutral_default_cap_equals_the_product_knowledge_cap() -> None:
    assert DEFAULT_MAX_RESPONSE_BYTES == MAX_RESPONSE_BYTES


def test_every_egress_outcome_value_is_a_provider_outcome() -> None:
    for outcome in EgressOutcome:
        assert ProviderOutcome(outcome.value).value == outcome.value
