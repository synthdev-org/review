# ADR: Fix generation — a separate step behind a validate-before-display gate, off by default, never auto-applied

## Status

PROPOSED (drafted 2026-10-18 alongside
[`docs/specs/spec-auto-fix.md`](../specs/spec-auto-fix.md) and
[`docs/plans/plan-phase-11.md`](../plans/plan-phase-11.md); becomes ACCEPTED
when the owner signs off on that spec).

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Phase 11 lets a review comment carry a GitHub suggested change a person can
commit with one click. Four decisions shape it and are recorded here (spec
D1, D4, D6, D8): the capability is its own pipeline step, not a sub-step of
the findings merger; every fix passes deterministic checks before any model
judges it, and nothing is shown that did not pass; the feature ships off; and
the platform has no code path that applies a fix.

## Context

The roadmap sketched a "propose fix" sub-step inside the findings merger.
`findings/merge.py` is pure, no-I/O domain logic (ADR-0005), while a fix needs
an LLM call, a content fetch and analyzer subprocesses, all of which
[`architecture.md`](../rules/architecture.md) keeps out of pure logic and
behind each stage's own narrow interface. The research
([`research-current-art.md`](../research/research-current-art.md) section 3)
shows commercial reviewers ship one-click fixes, and that validating a
candidate before showing it, not the generating model, is what makes them
trustworthy: unvalidated generated fixes can be syntactically valid and wrong,
or reference things that do not exist. The platform's own earlier finding
applies too: a model re-rating its own output is close to random, so a fix
must not be judged by the call that wrote it.

## Decision

1. **A separate `fix_generation` macro step between `verification` and
   `output` (D1).** It owns its LLM call, its content fetch and its analyzer
   re-runs behind consumer-defined Protocols, and it sees only findings that
   survived verification and Memory suppression, so no spend goes to a finding
   that will not post. It fails open: any problem leaves the finding exactly as
   it was. The roadmap's "merger sub-step" wording is corrected when the phase
   ships.
2. **Validate before display, deterministic first (D4).** A candidate fix is
   checked in order: range and text rules (one contiguous range inside one
   changed hunk, size caps, no no-op, no secret-shaped content), a syntax gate
   (the patched file has no more parse errors than the original), then a final
   arbiter chosen by source. For an analyzer finding the arbiter is the same
   analyzer re-run on the patched file, because the tool that raised the
   finding is the better and cheaper judge. For a verified LLM finding, where
   no deterministic arbiter exists, it is a separate fixed-rubric call that is
   never the generating call. A fix that fails any gate is dropped silently and
   the finding posts without it.
3. **Off by default (D8).** Unlike the free, deterministic Phases 9 and 10,
   this step spends the customer's own LLM budget on calls nobody asked for, so
   the operator opts in, and the runbook states the per-job cost bound.
4. **Never auto-applied (D6).** The only artifact is a suggestion block inside
   an inline comment. No write path to a branch, commit or PR exists and no
   new GitHub permission is requested, so the guarantee holds by absence of
   code rather than by configuration.

## Alternatives considered

- **A sub-step inside `findings/merge.py`** (the roadmap wording). Rejected:
  it would put I/O and LLM calls into the pure merger, or make the merger
  depend on a gateway, against the package rules.
- **Generate fixes in the same call that produces the finding.** Rejected: it
  spends tokens on findings verification later drops, bloats the main prompt,
  and removes the structural separation between author and judge.
- **Let the generating model grade its own fix.** Rejected on the platform's
  own research finding that self-critique is close to random.
- **Default on.** Rejected: unrequested inference spend is the stance
  ADR-0007 already takes for enrichment; the pilot data to justify a default
  does not exist.
- **Commit the fix, or open a fix PR.** Rejected: a larger permission, a
  larger blast radius and a reviewer who never saw it. A patch-bundle
  mechanism is a separate, later decision.

## Consequences

- One new step, six settings, one routing stage (`fix_generation`; the rubric
  reuses `verification`), one additive migration (a defaulted boolean on the
  posted-comment row) and one optional field on `Finding`.
- The stuck-job warning threshold default rises from 600 s to 700 s, because
  its validator sums every step timeout (635 s with the new 90 s step).
- Rejection is cheap and silent to the author, so the operator needs the
  outcome metrics to see the rejection mix; the dashboard and runbook carry
  them. The applied rate is not measurable (GitHub sends no webhook for a
  committed suggestion), so the pilot relies on human-classified Memory entries
  and manual review.
- An accepted fix is a plausible edit, not a proven one: it is neither run nor
  tested. That limit is stated in the runbook, and the author's click remains
  the control.

**Related:** [`ADR-0005`](0005-pure-no-io-logic-unit-tested.md) (pure logic
stays pure), [`ADR-0007`](0007-llm-free-indexing-deferred-rag-enrichment.md)
(unrequested inference spend), [`ADR-0009`](0009-memory-entry-schema-and-dismissal-similarity-signal.md)
(`accepted_fix` classification),
[`docs/specs/spec-auto-fix.md`](../specs/spec-auto-fix.md),
[`docs/plans/plan-phase-11.md`](../plans/plan-phase-11.md).
