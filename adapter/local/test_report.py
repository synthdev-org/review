"""Unit tests for the findings file: the JSON schema snapshot (a published contract) and the
markdown writer's escaping."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from adapter.local.report import SCHEMA_VERSION, Report, render_json, render_markdown
from adapter.local.summary import BackendSummary, RunSummary
from findings.models import Finding

_SCHEMA_FILE = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "review-report.schema.json"
)


def _report(*findings: Finding) -> Report:
    return Report(findings=list(findings), summary=RunSummary(backend=BackendSummary(model="m")))


def test_the_committed_schema_matches_the_generated_one() -> None:
    """Fails when the report model changes without the published schema (and `schema_version`)
    being updated on purpose. To regenerate, in the review-dev image:
    python -c "import json; from adapter.local.report import Report;
    print(json.dumps(Report.model_json_schema(), indent=2))" \
    > docs/reference/review-report.schema.json
    A breaking change (a removed or retyped field) also needs a new major `SCHEMA_VERSION`."""
    committed = json.loads(_SCHEMA_FILE.read_text())

    assert committed == Report.model_json_schema()


def test_the_schema_is_built_from_the_finding_model_and_carries_a_version() -> None:
    schema = json.loads(_SCHEMA_FILE.read_text())

    assert schema["$defs"]["Finding"]["properties"] == Finding.model_json_schema()["properties"]
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION == "1.0"


def test_rendered_json_validates_against_the_published_schema() -> None:
    finding = Finding(file="a.py", line=3, severity="high", message="m", source="llm", rule_id="R1")
    document = json.loads(render_json(_report(finding)))

    jsonschema.validate(document, json.loads(_SCHEMA_FILE.read_text()))
    assert document["schema_version"] == "1.0"
    assert document["findings"][0]["rule_id"] == "R1"


def test_a_document_of_another_major_version_does_not_validate() -> None:
    document = json.loads(render_json(_report()))
    document["schema_version"] = "2.0"

    try:
        jsonschema.validate(document, json.loads(_SCHEMA_FILE.read_text()))
    except jsonschema.ValidationError:
        return
    raise AssertionError("schema_version 2.0 must not validate against the 1.x schema")


def test_markdown_lists_findings_and_escapes_untrusted_text() -> None:
    finding = Finding(
        file="a_b.py",
        line=None,
        severity="low",
        message="[x](http://e.example)\n# heading @user",
        source="llm",
        rule_id="r*1",
    )

    text = render_markdown(_report(finding))

    assert text.startswith("# Code review\n\n1 finding(s).\n")
    assert "a\\_b\\.py" in text
    # Escaped, and the scheme separator is broken so a bare URL cannot auto-link either.
    assert "\\[x\\]\\(http:\u200b//e\\.example\\)" in text
    assert "\n# heading" not in text
    assert "@user" not in text
    assert "(r\\*1)" in text


def test_markdown_with_no_findings_says_so() -> None:
    assert render_markdown(_report()) == "# Code review\n\nNo findings.\n"
