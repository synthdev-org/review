"""memory.fingerprint — the deterministic text a finding's Memory-side
identity is embedded from, per
``docs/plans/plan-deliver-memory.md``'s Phase 5 section (Unit E, US-M4) and
[ADR-0009](../docs/adr/0009-memory-entry-schema-and-dismissal-similarity-signal.md)
decision 3 ("a candidate LLM finding's fingerprint (``file`` + ``rule_id``
+ ``message``) is embedded ... compared via pgvector cosine similarity
against approved, repo-scoped ``dismissed_finding`` entries").

Two independent call sites need to build this *exact same* text from two
different shapes carrying the same three fields — Phase 5's own admin
``approve`` route (over a ``memory.models.PostedComment``, itself built
from a persisted ``posted_finding_comment`` row) and Phase 6's
``VerificationStep`` suppression pre-check (over a candidate
``findings.models.Finding``) — so this one pure function is the single
place that composition is defined, per ADR-0005's "pure domain logic is
Unit-tested directly" precedent. If the two call sites built this text
differently, the cosine-similarity comparison between an approved entry's
stored embedding and a fresh candidate's embedding would silently compare
apples to oranges — this module exists specifically to make that
impossible.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule — a single string-formatting function, no I/O.
"""

from __future__ import annotations


def build_finding_fingerprint(*, file: str, rule_id: str | None, message: str) -> str:
    """The exact text embedded for a finding's Memory-side identity —
    ``file``, ``rule_id`` (``""`` when absent, e.g. an LLM-sourced finding
    with no static-analysis rule id), and ``message``, newline-separated.
    Deterministic and order-preserving: the same three inputs always
    produce the same text, which is what makes two independently-computed
    embeddings of "the same finding" comparable at all.
    """
    return f"{file}\n{rule_id or ''}\n{message}"
