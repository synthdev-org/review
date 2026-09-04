"""findings.models — the ``Finding`` domain model every producer normalizes
its own output into, per the plan's Phase 5 section
(docs/plans/plan-phase-1-walking-skeleton.md) and
docs/specs/phase-1-review-pipeline.md's US4/US5 structured-findings schema
("file, line, severity, message").

Lives in ``findings`` (a dedicated bounded-context package,
architecture.md's "organize by bounded context" rule), not inside any one
step module: "what does it mean for a static-analysis or LLM pass to have
found something" is a concept every producer (Gitleaks/Semgrep,
``adapter/analyzers``, this phase; the LLM, Phase 6) and every consumer
(Phase 7's merge/dedupe/posting step) shares — no single step module should
own it on the others' behalf.

Zero infra imports, per architecture.md's domain-package rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["critical", "high", "medium", "low", "info"]
"""A small, fixed vocabulary every ``Finding`` producer normalizes its own
tool-specific severity language into (Gitleaks has no severity concept of
its own and maps to a fixed value; Semgrep's ERROR/WARNING/INFO map onto
this same set; Phase 6's LLM output is forced into this same schema at the
LLM Gateway boundary) — so no downstream consumer (Phase 7's merge/output
step) ever has to reconcile three different vocabularies of its own.
"""

Source = Literal["gitleaks", "semgrep", "llm"]
"""Which producer generated a given ``Finding`` — lets a downstream consumer
(redaction, dedupe, an operator reading logs) distinguish "the free,
deterministic static-analysis pass found this" from "the LLM's own,
unverified single inference pass found this," per the PRD's US4 design
rationale (design goal 4's "deterministic first pass as the free trust
anchor").
"""


class Finding(BaseModel):
    """One normalized finding from any producer, per the PRD's US4/US5
    structured schema ("file, line, severity, message").

    ``line`` is nullable: not every finding is attributable to a single
    line (a file-level match, or a producer that doesn't report line
    numbers) — a producer that always has one still sets it. ``rule_id`` is
    the producing tool's own rule/check identifier where it has one (both
    Gitleaks and Semgrep do; the LLM path, Phase 6, leaves it ``None``) —
    metadata useful to an operator and to Phase 7's dedupe logic, not part
    of the PRD's own minimal four-field schema.

    ``message`` is the one field ``findings.redact.TextRedactor`` operates
    on (per the plan's Phase 5 "Static-analysis-wide secret redaction"
    section) — every producer is expected to put any free-text content that
    could quote source/secret material (a Semgrep match's own quoted source
    line, an LLM's own prose) into this field, not a field this model
    doesn't define, so the redaction pass's "every ``Finding``'s free-text
    fields" guarantee has exactly one field to cover.
    """

    model_config = ConfigDict(frozen=True)

    file: str
    line: int | None = None
    severity: Severity
    message: str
    source: Source
    rule_id: str | None = None
