"""review.citation — deterministic, platform-rendered citations for posted
findings (Unit F), per docs/plans/plan-deliver-context-builder.md's Phase 6
section and docs/specs/spec-context-builder.md's US6.

``render_citation`` looks up an LLM-sourced ``Finding``'s own
``context_ref_ids`` against the same job's already-retrieved
``review.context_bundle.ContextBundle`` and renders a citation from that
data alone — never text the LLM itself composed describing what it was
shown, per US6's own "the citation content always matches the platform's
own retrieved data" requirement.

**Renders file path, symbol name, and line range only — never the bundle
item's underlying ``ContextBundleItem.content`` (the chunk's real source
text).** This is a deliberate scope boundary, not an oversight:
``review.output.FindingsOutputStep`` posts citation text *after*
``findings.redact.TextRedactor`` has already run (upstream, inside
``review/review_generation.py``/``review/static_analysis.py``, per that module's
own docstring) — a citation that echoed raw chunk content would bypass
redaction entirely and could post an unrelated file's real source
(potentially containing a secret nobody flagged for removal) verbatim into
a PR comment, a materially worse and irreversible outcome versus the
diff-only content Phase 1 already exposes.

**Graceful, silent-but-logged degradation for both failure shapes this
module can hit:**

- A bundle item with ``line_range = None`` (a chunk indexed before
  Assumption 5's ``index_chunk`` migration backfilled it, and never
  re-indexed since) renders a citation without the line-range portion,
  logged at ``info`` — never an error, never a dropped citation.
- An ID with no matching bundle entry (a fabricated/hallucinated
  ``context_ref_ids`` value, or one this run's own budget truncation
  dropped) is silently excluded from the rendered citation, never a
  ``KeyError`` — logged at ``warning``, a qualitatively different signal
  than "found nothing" (possibly the LLM hallucinating a reference),
  deserving the same visibility tier as a genuine degrade.

Empty ``ref_ids`` (no context was drawn on) or every ID in ``ref_ids``
failing to resolve both render ``None`` — "no citation", not an empty
citation section — the shape ``review.output.FindingsOutputStep`` uses to
decide whether to set a finding's own ``citation_markdown`` field at all.

**Escapes each embedded file path/symbol name individually via
``findings.markdown.escape_markdown``, but never the surrounding Markdown
template this module itself builds (backticks, the bold ``**Context:**``
header).** ``render_citation``'s return value is set on
``findings.models.Finding.citation_markdown`` — a field deliberately kept
separate from ``message`` precisely so ``adapter/github/posting.py`` can
append it *unescaped* after its own ``escape_markdown(finding.message)``
pass, rather than risk a second escaping pass mangling this module's own
template characters into literal backslash-escaped text (see
``Finding.citation_markdown``'s own docstring for the full reasoning).

**Pure, no-I/O, per ADR-0005** (this phase's own deliverable registers this
module in that ADR's "Scope, extended per phase" list) — ``render_citation``
performs no I/O of its own; it only looks up and formats already-retrieved
``ContextBundle`` data. ``findings.markdown.escape_markdown`` is itself a
pure, zero-infra-import function, so importing it here doesn't add an infra
dependency — the same ordinary domain-to-domain import shape Assumption 4
already establishes for this plan's other cross-package imports.

**Product-knowledge items** (docs/plans/plan-deliver-product-knowledge.md,
Phase 3, US-PK3) get their own block after the code list:
``**Product knowledge (<display name>):**`` with one
``[<title>](<url>) — <source_type> · <host>`` line per cited item, in id
order, at most ``TOP_K`` lines. Title, display name, source type and host
pass through ``escape_markdown``; the URL comes from the single sanitizer in
``review.product_knowledge`` (no URL ⇒ plain escaped title); cleartext/IP
hosts are flagged; **no snippet or content is ever echoed**.

Zero infra imports, per architecture.md's domain-package rule.
"""

from __future__ import annotations

import logging
from collections import Counter

from opentelemetry import metrics

from findings.markdown import escape_markdown
from review._telemetry import suppress_telemetry_errors
from review.context_bundle import ContextBundle, ContextBundleItem
from review.product_knowledge import TOP_K, ProductKnowledgeItem, sanitize_url, url_display

logger = logging.getLogger(__name__)

meter = metrics.get_meter("review.citation")

_ref_total = meter.create_counter(
    "review.citation.ref_total",
    description="Context ref ids the LLM cited, by id space and whether they resolved.",
)


def _record_ref_counts(counts: Counter[tuple[str, str]]) -> None:
    with suppress_telemetry_errors(
        logger, "citation ref telemetry recording failed (citation unaffected)"
    ):
        for (kind, result), count in counts.items():
            _ref_total.add(count, {"kind": kind, "result": result})


def _render_location(item: ContextBundleItem, *, job_id: str) -> str:
    """Renders one bundle item's file path (plus line range, when known) —
    the "metadata only, never content" half of this module's own scope
    boundary. The file path is ``escape_markdown``-neutralized here, at the
    one place this module builds its own Markdown template characters
    (backticks/bold, in ``_render_item``/``render_citation`` below) — so a
    file path containing a Markdown-significant character can't break out
    of, or be mistaken for, that template, while the template characters
    themselves stay literal, unescaped Markdown (never passed through a
    second, blanket ``escape_markdown`` pass downstream)."""

    if item.line_range is None:
        logger.info(
            "citation: rendering context item without a line-range portion "
            "(chunk indexed before the line_range migration backfilled it)",
            extra={"job_id": job_id, "file_path": item.file_path, "symbol_name": item.symbol_name},
        )
        return escape_markdown(item.file_path)

    start, end = item.line_range
    return f"{escape_markdown(item.file_path)}:{start}-{end}"


def _render_item(item: ContextBundleItem, *, job_id: str) -> str:
    location = _render_location(item, job_id=job_id)
    if item.symbol_name is not None:
        return f"- `{location}` (`{escape_markdown(item.symbol_name)}`)"
    return f"- `{location}`"


def _render_product_knowledge_item(item: ProductKnowledgeItem) -> str:
    title = escape_markdown(item.title)
    source = escape_markdown(item.source_type)
    safe_url = sanitize_url(item.url)
    display = url_display(safe_url)
    if safe_url is None or display is None:
        return f"- {title} — {source}"
    host, flags = display
    line = f"- [{title}]({safe_url}) — {source} · {escape_markdown(host)}"
    if flags:
        line += f" ({', '.join(flags)})"
    return line


def render_citation(ref_ids: list[int], bundle: ContextBundle, *, job_id: str) -> str | None:
    """Renders a citation section from ``bundle``'s own already-retrieved
    data for exactly the items named in ``ref_ids``, per this module's own
    docstring.

    ``job_id`` (already available on ``review.output.FindingsOutputInput``/
    ``review.step.JobIdentity``, so this is a zero-cost addition, not new
    plumbing) is threaded through purely so an unknown-ID ``warning`` log
    (and the no-line-range ``info`` log) can be correlated to the
    originating job.

    Returns ``None`` when ``ref_ids`` is empty or when every ID in it fails
    to resolve against ``bundle`` — "no citation", never an empty citation
    section appended to a finding that drew on nothing real.
    """

    lines: list[str] = []
    product_items: dict[int, ProductKnowledgeItem] = {}
    seen_ids: set[int] = set()
    ref_counts: Counter[tuple[str, str]] = Counter()
    for ref_id in ref_ids:
        if ref_id in seen_ids:
            continue
        seen_ids.add(ref_id)
        item = bundle.item(ref_id)
        if item is None and (pk_item := bundle.product_knowledge_item(ref_id)) is not None:
            product_items[ref_id] = pk_item
            ref_counts[("product_knowledge", "rendered")] += 1
            continue
        if item is None:
            ref_counts[("unknown", "unresolved")] += 1
            logger.warning(
                "citation: unknown context_ref_id; dropping from citation "
                "(possibly a hallucinated or budget-truncated reference)",
                extra={"job_id": job_id, "ref_id": ref_id},
            )
            continue
        lines.append(_render_item(item, job_id=job_id))
        ref_counts[("code", "rendered")] += 1
    _record_ref_counts(ref_counts)

    blocks: list[str] = []
    if lines:
        blocks.append("**Context:**\n" + "\n".join(lines))
    if product_items and bundle.product_knowledge is not None:
        pk_lines = [
            _render_product_knowledge_item(product_items[i]) for i in sorted(product_items)[:TOP_K]
        ]
        label = escape_markdown(bundle.product_knowledge.label)
        blocks.append(f"**Product knowledge ({label}):**\n" + "\n".join(pk_lines))

    if not blocks:
        return None

    return "\n\n".join(blocks)
