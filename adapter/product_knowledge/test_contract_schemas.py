"""US-PK16 — the committed product-knowledge contract schemas equal the ones
generated from ``review.product_knowledge``'s wire models, and every committed
example validates (or fails) as its name says.

Pure static check, no I/O beyond reading committed files (ADR-0005's
pure-logic exemption). The integration evidence for US-PK16 is Phase 4/5's
validation of live fake-provider responses against these same files.

To regenerate after a model change::

    PK_UPDATE_SCHEMAS=1 pytest adapter/product_knowledge/test_contract_schemas.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
from pydantic import BaseModel, ValidationError

from review.product_knowledge import (
    ContractDeclaration,
    ContractRequest,
    FetchRequest,
    FetchResponse,
    ProviderError,
    RelatedRequest,
    SearchRequest,
    SearchResponse,
)

CONTRACT_DIR = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "product-knowledge-contract" / "v1"
)
EXAMPLES_DIR = CONTRACT_DIR / "examples"

# schema file stem -> wire model. ``related.output`` is exactly the search
# output shape (spec: "output is exactly the search ``results`` shape").
_MODELS: dict[str, type[BaseModel]] = {
    "contract.input": ContractRequest,
    "contract.output": ContractDeclaration,
    "search.input": SearchRequest,
    "search.output": SearchResponse,
    "fetch.input": FetchRequest,
    "fetch.output": FetchResponse,
    "related.input": RelatedRequest,
    "related.output": SearchResponse,
    "error": ProviderError,
}


def generate_schemas() -> dict[str, str]:
    """Return ``{filename: json text}`` for every committed schema file."""
    return {
        f"{stem}.json": json.dumps(model.model_json_schema(), sort_keys=True, indent=2) + "\n"
        for stem, model in _MODELS.items()
    }


def _example_files(kind: str) -> list[Path]:
    return sorted((EXAMPLES_DIR / kind).glob("*.json"))


def _stem(example: Path) -> str:
    # "search.output.minimal.json" -> "search.output"; "error.not_found.json" -> "error"
    parts = example.name.removesuffix(".json").split(".")
    return ".".join(parts[:2]) if parts[0] != "error" else "error"


def _schema(stem: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((CONTRACT_DIR / f"{stem}.json").read_text())
    return loaded


if os.environ.get("PK_UPDATE_SCHEMAS") == "1":

    def test_regenerate_schemas() -> None:
        CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in generate_schemas().items():
            (CONTRACT_DIR / name).write_text(text)


def test_committed_schemas_equal_generated() -> None:
    generated = generate_schemas()
    committed = {p.name: p.read_text() for p in CONTRACT_DIR.glob("*.json")}
    assert committed == generated, (
        "committed schemas drifted from review/product_knowledge.py; "
        "regenerate with PK_UPDATE_SCHEMAS=1"
    )


def test_generated_schemas_are_valid_json_schema() -> None:
    for name in generate_schemas():
        jsonschema.Draft202012Validator.check_schema(json.loads((CONTRACT_DIR / name).read_text()))


def test_every_example_is_named_for_a_known_schema() -> None:
    for example in _example_files("valid") + _example_files("invalid"):
        assert _stem(example) in _MODELS, example.name


def test_each_schema_has_a_valid_and_an_invalid_example() -> None:
    # contract.input takes no arguments, so it has no invalid example to give.
    for stem in _MODELS:
        assert any(_stem(p) == stem for p in _example_files("valid")), stem
        if stem != "contract.input":
            assert any(_stem(p) == stem for p in _example_files("invalid")), stem


@pytest.mark.parametrize("example", _example_files("valid"), ids=lambda p: p.name)
def test_valid_example_validates(example: Path) -> None:
    payload = json.loads(example.read_text())
    stem = _stem(example)
    jsonschema.validate(payload, _schema(stem), cls=jsonschema.Draft202012Validator)
    _MODELS[stem].model_validate(payload)


@pytest.mark.parametrize("example", _example_files("invalid"), ids=lambda p: p.name)
def test_invalid_example_fails(example: Path) -> None:
    payload = json.loads(example.read_text())
    stem = _stem(example)
    assert not jsonschema.Draft202012Validator(_schema(stem)).is_valid(payload)
    with pytest.raises(ValidationError):
        _MODELS[stem].model_validate(payload)


def test_title_is_required_on_search_items_and_fetch_responses() -> None:
    item = {"id": "a", "snippet": "s", "source_type": "jira"}
    with pytest.raises(ValidationError):
        SearchResponse.model_validate({"results": [item]})
    with pytest.raises(ValidationError):
        FetchResponse.model_validate({"id": "a", "content": "c", "source_type": "jira"})
    assert "title" in _schema("search.output")["$defs"]["SearchItem"]["required"]  # type: ignore[index]


def test_response_models_ignore_unknown_fields_requests_forbid_them() -> None:
    parsed = SearchResponse.model_validate(
        {
            "results": [
                {"id": "a", "title": "t", "snippet": "", "source_type": "jira", "future": 1}
            ],
            "next_cursor": "x",
        }
    )
    assert parsed.results[0].id == "a"
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"contract_version": "1", "query": "q", "extra": 1})


def test_search_request_defaults_and_bounds() -> None:
    assert SearchRequest(contract_version="1", query="q").top_k == 5
    for bad in (0, 21):
        with pytest.raises(ValidationError):
            SearchRequest(contract_version="1", query="q", top_k=bad)
    with pytest.raises(ValidationError):
        SearchRequest(contract_version="v1", query="q")
