"""Unit tests for review.citation.render_citation (US6), per the plan's
Phase 6 Tests section and ADR-0005's pure-no-I/O exemption — no real
Postgres, no real bundle-assembly pipeline, just already-constructed
``ContextBundle``/``ContextBundleItem`` values.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from review.citation import render_citation
from review.context_bundle import ContextBundle, ContextBundleItem, ContextItemKind
from review.product_knowledge import (
    ProductKnowledgeItem,
    ProductKnowledgeSection,
)

_JOB_ID = "job-1"


def _item(
    *,
    id: int = 0,
    file_path: str = "src/caller.ts",
    symbol_name: str | None = "caller",
    content: str = "function caller() { return helper(); }",
    line_range: tuple[int, int] | None = (10, 12),
) -> ContextBundleItem:
    return ContextBundleItem(
        id=id,
        kind=ContextItemKind.CALLER,
        file_path=file_path,
        symbol_name=symbol_name,
        content=content,
        line_range=line_range,
    )


def _bundle(*items: ContextBundleItem) -> ContextBundle:
    return ContextBundle(items=tuple(items))


def test_valid_id_renders_the_right_text() -> None:
    bundle = _bundle(
        _item(id=0, file_path="src/caller.ts", symbol_name="caller", line_range=(10, 12))
    )

    citation = render_citation([0], bundle, job_id=_JOB_ID)

    assert citation is not None
    # `.` is one of `escape_markdown`'s escaped characters -- the embedded
    # file path is individually escaped, but the surrounding template
    # (backticks, the bold header) is not -- see
    # `test_template_markdown_survives_while_embedded_values_are_escaped`
    # below for the regression this pins.
    assert "src/caller\\.ts" in citation
    assert "caller" in citation
    assert "10" in citation
    assert "12" in citation


def test_template_markdown_survives_while_embedded_values_are_escaped() -> None:
    """Regression test for the citation-escaping bug: `render_citation`'s
    own Markdown template (the bold `**Context:**` header, the backtick
    code-span delimiters) must survive unescaped in the returned string --
    only the *embedded* file path/symbol name are individually escaped.
    Under the old, buggy design (citation text appended into `Finding.message`
    and escaped a second time by `adapter/github/posting.py`), the template
    itself would have been mangled into literal `\\*\\*Context:\\*\\*` /
    `\\`...\\`` -- this test operates at `render_citation`'s own boundary,
    so it can't accidentally pass by relying on a later pipeline stage
    skipping the second escape (that pipeline-level guarantee is covered
    separately in `review/test_findings_output_step.py`)."""
    bundle = _bundle(
        _item(id=0, file_path="src/caller.ts", symbol_name="do_something_now", line_range=(10, 12))
    )

    citation = render_citation([0], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert citation.startswith("**Context:**\n")
    assert "\\*\\*" not in citation
    # The embedded symbol name has an escapable `_` -- it must be escaped
    # *inside* the still-literal backticks, not the backticks themselves.
    assert "`src/caller\\.ts:10-12` (`do\\_something\\_now`)" in citation


def test_unknown_id_renders_nothing_and_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    bundle = _bundle(_item(id=0))

    with caplog.at_level(logging.WARNING, logger="review.citation"):
        citation = render_citation([999], bundle, job_id=_JOB_ID)

    assert citation is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "unknown context_ref_id" in warnings[0].message
    assert warnings[0].__dict__["ref_id"] == 999
    assert warnings[0].__dict__["job_id"] == _JOB_ID


def test_empty_ref_ids_renders_nothing() -> None:
    bundle = _bundle(_item(id=0))

    citation = render_citation([], bundle, job_id=_JOB_ID)

    assert citation is None


def test_a_real_id_never_leaks_the_underlying_chunk_content() -> None:
    """Regression test for the metadata-only scope boundary (per this
    module's own docstring and the plan's Security review) — a rendered
    citation must never echo the bundle item's `content` field, since
    citation text posts *after* `TextRedactor` has already run and would
    otherwise bypass redaction entirely."""
    secret_marker = "sk_live_TOTALLY_REAL_SECRET_TOKEN_VALUE"
    bundle = _bundle(_item(id=0, content=f"const token = '{secret_marker}';"))

    citation = render_citation([0], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert secret_marker not in citation


def test_item_with_no_line_range_still_renders_omitting_only_that_portion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle = _bundle(
        _item(id=0, file_path="src/legacy.ts", symbol_name="legacyFn", line_range=None)
    )

    with caplog.at_level(logging.INFO, logger="review.citation"):
        citation = render_citation([0], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert "src/legacy\\.ts" in citation
    assert "legacyFn" in citation
    info_logs = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_logs) == 1
    assert "without a line-range portion" in info_logs[0].message


def test_multiple_valid_ids_render_all_of_them() -> None:
    bundle = _bundle(
        _item(id=0, file_path="src/a.ts", symbol_name="a"),
        _item(id=1, file_path="src/b.ts", symbol_name="b"),
    )

    citation = render_citation([0, 1], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert "src/a\\.ts" in citation
    assert "src/b\\.ts" in citation


def test_duplicate_valid_ids_render_the_item_only_once() -> None:
    """`context_ref_ids` is LLM-controlled and only length-capped, not
    deduplicated, at the schema level (per the plan's Phase 6 section) — a
    response like `[0, 0, 0]` must not render the same citation line three
    times.
    """
    bundle = _bundle(_item(id=0, file_path="src/a.ts", symbol_name="a"))

    citation = render_citation([0, 0, 0], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert citation.count("src/a\\.ts") == 1


def test_mixed_valid_and_unknown_ids_renders_only_the_valid_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle = _bundle(_item(id=0, file_path="src/a.ts", symbol_name="a"))

    with caplog.at_level(logging.WARNING, logger="review.citation"):
        citation = render_citation([0, 999], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert "src/a\\.ts" in citation
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


# --- Product-knowledge block (Phase 3, US-PK3 / US-PK6) ------------------------


def _pk_item(
    id: int,
    *,
    title: str = "Refund policy",
    url: str | None = "https://wiki.example.com/refunds",
    source_type: str = "confluence",
    content: str = "SECRET-SNIPPET-TEXT",
) -> ProductKnowledgeItem:
    return ProductKnowledgeItem(
        id=id,
        provider_item_id=f"doc-{id}",
        title=title,
        source_type=source_type,
        url=url,
        content=content,
    )


def _pk_bundle(
    *pk_items: ProductKnowledgeItem, code: int = 1, label: str = "Acme Wiki"
) -> ContextBundle:
    return ContextBundle(
        items=tuple(_item(id=i) for i in range(code)),
        product_knowledge=ProductKnowledgeSection(label=label, items=tuple(pk_items)),
    )


def test_product_knowledge_block_rendered_with_link_type_and_host_no_content() -> None:
    citation = render_citation([1], _pk_bundle(_pk_item(1)), job_id=_JOB_ID)

    assert citation == (
        "**Product knowledge (Acme Wiki):**\n"
        "- [Refund policy](https://wiki.example.com/refunds) \u2014 confluence \u00b7 "
        "wiki\\.example\\.com"
    )
    assert "SECRET-SNIPPET-TEXT" not in citation


def test_code_list_comes_first_then_product_knowledge_in_id_order() -> None:
    bundle = _pk_bundle(_pk_item(1, title="First"), _pk_item(2, title="Second"))

    citation = render_citation([2, 0, 1], bundle, job_id=_JOB_ID)

    assert citation is not None
    context_pos = citation.index("**Context:**")
    pk_pos = citation.index("**Product knowledge (Acme Wiki):**")
    assert context_pos < pk_pos
    assert citation.index("First") < citation.index("Second")


def test_item_without_a_usable_url_renders_plain_escaped_title() -> None:
    bundle = _pk_bundle(
        _pk_item(1, title="Plain", url="javascript:alert(1)"),
        _pk_item(2, title="None", url=None),
    )

    citation = render_citation([1, 2], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert "](" not in citation
    assert "- Plain \u2014 confluence" in citation
    assert "javascript" not in citation


@pytest.mark.parametrize("char", ["(", ")", "*", "_", "[", "]", "`", "@", "<", "|"])
def test_markdown_in_title_and_display_name_is_escaped(char: str) -> None:
    bundle = _pk_bundle(_pk_item(1, title=f"a{char}b"), label=f"Wiki{char}X")

    citation = render_citation([1], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert f"a{char}b" not in citation
    assert f"Wiki{char}X" not in citation


def test_cleartext_and_ip_hosts_are_flagged() -> None:
    bundle = _pk_bundle(
        _pk_item(1, url="http://wiki.example.com/x"),
        _pk_item(2, url="https://10.1.2.3/x"),
    )

    citation = render_citation([1, 2], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert "(cleartext http)" in citation
    assert "(IP address)" in citation


def test_hostile_url_cannot_break_out_of_the_link_destination() -> None:
    bundle = _pk_bundle(_pk_item(1, url="https://wiki.example.com/a)(javascript:x) b"))

    citation = render_citation([1], bundle, job_id=_JOB_ID)

    assert citation is not None
    assert "https://wiki.example.com/a%29%28javascript:x%29%20b" in citation


def test_unknown_id_is_dropped_with_warning_and_all_unknown_is_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle = _pk_bundle(_pk_item(1))

    with caplog.at_level(logging.WARNING, logger="review.citation"):
        citation = render_citation([1, 99], bundle, job_id=_JOB_ID)
        none = render_citation([42], bundle, job_id=_JOB_ID)

    assert citation is not None and "Product knowledge" in citation
    assert none is None
    assert sum("unknown context_ref_id" in r.message for r in caplog.records) == 2


def test_product_knowledge_block_is_capped_at_five_lines() -> None:
    items = [_pk_item(i) for i in range(1, 8)]
    citation = render_citation([i.id for i in items], _pk_bundle(*items), job_id=_JOB_ID)

    assert citation is not None
    assert citation.count("\n- ") == 5


def test_ref_total_counts_rendered_and_unresolved_refs_by_kind(
    reset_otel_global_providers: None,
) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    try:
        render_citation([0, 0, 99], _bundle(_item(id=0)), job_id=_JOB_ID)
        data = reader.get_metrics_data()
    finally:
        provider.shutdown()

    assert data is not None
    points = {
        (a["kind"], a["result"]): p.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.citation.ref_total"
        for p in m.data.data_points
        for a in [dict(p.attributes or {})]
    }
    # duplicate id 0 is counted once; 99 is unresolved with unknown kind
    assert points == {("code", "rendered"): 1, ("unknown", "unresolved"): 1}
