# ADR: Clone detection — a custom Tree-sitter token-winnowing engine, not an adopted jscpd/PMD CPD

## Status

PROPOSED (drafted 2026-10-17 alongside
[`docs/specs/spec-clone-detection.md`](../specs/spec-clone-detection.md);
becomes ACCEPTED when the owner signs off on that spec).

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Phase 10 adds a deterministic `clone_detection` review step that flags code a
PR adds when it duplicates code elsewhere in the repo's default-branch index.
The engine choice is the load-bearing decision: adopt an existing clone
detector (jscpd, PMD CPD) as a subprocess, or build fingerprinting on the
Tree-sitter parse indexing already holds. This ADR records the second: AST-leaf
normalized tokens plus winnowing (Schleimer, Wilkerson and Aiken), sampled
fingerprints persisted at index time and matched at review time. Decision D1 of
the spec; this document is the architecture-direction record
[`architecture.md`](../rules/architecture.md) asks for when a capability
deviates from adopting an off-the-shelf tool.

## Context

What the field does (vendor docs retrieved 2026-10-17; Codacy per
[`research-current-art.md`](../research/research-current-art.md)):

| Tool | Approach | Default minimum | Takeaway |
| --- | --- | --- | --- |
| SonarQube | Token-based; gates on new-code duplication | 100 tokens, 10 lines | Adopt 100/10; judge new code only |
| PMD CPD | Token-based, normalizable identifiers/literals | 100 tokens | Same default; token granularity |
| jscpd | Token-based, many languages | 50 tokens, 5 lines | The noisy end; rejected as default |
| Code Climate | Structural/AST similarity above a "mass" | Per-language mass | Needs per-language tuning |
| Codacy | CPD-style; counts clones introduced by the PR | Configurable | Confirms PR-introduced scoping |

All of them are deterministic, non-LLM engines. CPD and jscpd are batch tools:
each run tokenizes every input again. Review needs the opposite shape, a
persisted index compared against only the changed files.

## Decision

Build the engine in `indexing/clone.py`:

1. Tokenize the Tree-sitter parse indexing already produces (TypeScript, PHP,
   Go), normalizing identifiers, literals and comments away (Type-1/Type-2
   clones).
2. Winnow K-gram hashes (K = 25, window 76) so any duplicated run of at least
   100 normalized tokens shares at least one stored fingerprint, at about
   2.6 % sampled density.
3. Store sampled fingerprints plus the per-token hashes and line map per file,
   so a hash match is verified to token equality and a collision can never
   produce a finding.
4. Run matching as a fail-open macro step that never calls an LLM.

## Alternatives considered

- **Wrap jscpd or PMD CPD.** Rejected. They re-tokenize the whole repo per run
  (or need a second cache beside our index), and add a Node or JVM runtime to
  the image and attack surface at a time Phase 9 already limited tool runtimes.
- **Embedding similarity (Phase 2 retrieval).** Rejected for findings: it is
  approximate, only covers changed chunks, and is a context signal, not a
  verifiable claim of equality.
- **Code Climate style structural mass.** Rejected: needs a tuned number per
  language, where normalized tokens from the AST give one global threshold.

## Consequences

- No new runtime dependency or infrastructure; a Postgres migration adds
  fingerprint and per-file token tables (about 4 MB per million tokens).
- Owning the engine means owning its normalization tables per grammar; they
  are pinned by a completeness test over the fixture corpus.
- Lowering the minimum below 100 tokens needs a `CLONE_FINGERPRINT_VERSION`
  bump with new K/W and a backfill; raising it is a settings change.
- Supporting a fourth language means a new normalization table, not a new tool.

**Related:** [`ADR-0007`](0007-llm-free-indexing-deferred-rag-enrichment.md)
(LLM-free indexing), [`ADR-0009`](0009-memory-entry-schema-and-dismissal-similarity-signal.md)
(dismissal suppression, amended for `clone_detection` findings),
[`docs/plans/plan-phase-10.md`](../plans/plan-phase-10.md).
