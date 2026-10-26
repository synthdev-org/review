# ADR: Local mode — a fourth composition root, shipped as a pip/pipx distribution

## Status

PROPOSED (drafted 2026-10-25 alongside
[`docs/specs/spec-local-mode.md`](../specs/spec-local-mode.md) and
[`docs/plans/plan-local-mode.md`](../plans/plan-local-mode.md); becomes ACCEPTED
when the owner signs off, including the E2E shape and its CI gating in
Decision 3).

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Local mode runs the review pipeline in one process on a developer machine or in
one CI job, with no queue, database or server (spec D1). That needs a fourth
composition root, which `architecture.md` forbids without an ADR. This ADR
records that root, how it is packaged, how it is end-to-end tested, how its
memory matching crosses the existing Memory boundary (plan blocker B3), and two
CI security constraints (B5a, B5d), and where CI detection lives.

## Context

[`architecture.md`](../rules/architecture.md) names three deployable entrypoints
and says no fourth component exists "until a real requirement forces one", with
wiring only at the composition roots. [`testing.md`](../rules/testing.md) defines
E2E as the Docker Compose stack. The pipeline itself is reusable in-process:
every `review/*.py` step depends only on consumer-defined Protocols, and
`StepRegistry.run` needs no queue. The blockers are the rules, not the code.
The `DismissedFindingMatcher` Protocol is embedding-keyed
(`memory/store.py`), so an exact-fingerprint repo file does not fit it.

## Decision

1. **A fourth composition root, `apps/local_cli`, with its adapters in
   `adapter/local` (B1).** It parses config once with `pydantic-settings`,
   fabricates the job seed, and wires `adapter/local` concretes plus the shared
   registry builder. It never imports `apps/worker`; the shared builder moves to
   a neutral module (`review/registry.py`) per the plan (A2). Local mode is a
   separate stateless mode for one developer or one CI run; server mode stays
   the product for teams.
2. **A distribution, not a deployable, so no precedent.** The rule's "deployable
   components" are container images the customer operates and patches. The CLI is
   a Python distribution installed by the user and operates nothing. It is
   shipped through **one channel only: a pip/pipx console script**. No container
   image and no GitHub Action wrapper. Being a distribution sets no precedent:
   a future CLI is not exempt from the "no further component" rule and needs its
   own ADR.
3. **Local E2E = the built distribution installed and run in a clean container**
   with no services and a recorded model, in `tests/e2e/` beside the Compose
   E2E. It is gated like the Compose E2E: on merge to main or nightly. The
   Compose E2E stays the only E2E for server mode and also proves US-P1 (server
   behaviour unchanged).
4. **Memory matching: fix the boundary (B3 option i).** Introduce a
   fingerprint-keyed matcher Protocol (file path, rule id, normalised message
   hash) with the embedder optional, rather than a fake fixed-vector embedder in
   production code (option ii). It touches the server path, so US-P1 must cover
   it: the unchanged worker suite plus the server-mode E2E. The file format is
   recorded in an amendment at Phase 8 start; it does not block Phases 1-7.
5. **CI security constraints (B5a, B5d).** (a) The example workflow uses
   `pull_request`, never `pull_request_target`; fork PRs get no comment and
   findings go to the job log with a non-zero exit. (d) In CI the memory file
   (and MCP nominations) are read from the **base** branch, never the PR head,
   so a PR cannot suppress its own findings. B5(b) and (c) are owned by
   ADR-0017.
6. **CI detection lives in the composition root only (US-L8).** `apps/local_cli`
   reads the conventional `CI` environment variable once, in its
   `pydantic-settings` config, as a typed `in_ci` boolean. The wiring refuses to
   build the subscription (Claude CLI) backend when `in_ci` is true unless the
   explicit override flag is set, and the run summary records the override. No
   adapter or domain package reads the environment. It is a best-effort guard,
   not a security control: a self-hosted runner can unset `CI`.

## Alternatives considered

- **A container image or GitHub Action as a second channel.** Rejected: two
  channels double the install, test and support surface; the spec asks for one.
- **Treat the CLI as a deployable and extend the "three" to four.** Rejected:
  it would make every future CLI a precedent for a new component.
- **B3 option ii, a fixed-vector embedding pair where 1.0 means equal.**
  Rejected: no server-path change, but a fake embedder in production code;
  `architecture.md` says to fix the boundary, not special-case the adapter.
- **Local E2E through the Compose stack.** Rejected: local mode has no services
  to compose, so it would not test the real install path.

## Consequences

- `architecture.md`, `testing.md` and `c4-architecture.md` are amended to
  reference this ADR (done with it).
- Phases 1-9 of the plan are unblocked once this is ACCEPTED; Phase 8 waits on
  the B3 amendment.
- The registry extraction and the matcher-Protocol change are the only edits to
  server-path code; both carry the US-P1 gate.

## Amendment: the repository memory file (Phase 8, B3 option i)

Status: PROPOSED with the rest of this ADR. Records the format and matching rule that Decision 4
deferred to Phase 8.

- **Boundary.** `review/memory_suppression.py` gains a consumer-defined `FingerprintMatcher`
  Protocol (`is_dismissed(repo_id, fingerprint) -> bool`) and a `FingerprintSuppressionConfig`
  beside the existing `MemorySuppressionConfig`; `suppress_via_memory` accepts either. No embedder
  is involved in the exact-fingerprint case. `MemorySuppressionConfig`, its positional field order
  and `DismissedFindingMatcher` are unchanged, and `VerificationStep` and `CloneDetectionStep` only
  widen the annotation of the config they pass through. The Worker never passes a fingerprint
  matcher, so server behaviour is the unchanged existing suite (US-P1).
- **Matching rule.** Key = `memory.fingerprint.build_finding_fingerprint` (`file\nrule_id\nmessage`,
  the text server mode embeds), hashed with SHA-256. This is exact: Decision 4's "normalised
  message" is not applied, because normalising would make two different findings collide silently;
  a reworded finding is a new finding. The fingerprint is computed from the finding after secret
  redaction, so an entry copied from review's output never holds a secret. `memory.decay` is not
  used (a repository file has no age signal).
- **Format.** `.review/memory.jsonl` (the path is a setting, never repository content), JSON Lines,
  one object per line with `file` (or `*`), `rule_id`, `message` and optional `note`; unknown fields
  are rejected. JSON Lines rather than YAML so the file, which a pull request author controls,
  has no aliases, anchors or tags and every error has an exact line. Limits: 256 KiB, 2,000 lines,
  5,000 characters per line, a 2 s parse deadline; symbolic links and non-regular files are refused.
  A malformed file is exit 2 naming the line (never its content), before any model call.
- **CI (B5d).** In CI the file is read from the base commit with `git ls-tree`/`cat-file`; the
  working tree is never consulted, so a pull request cannot dismiss its own findings. Suppressed
  findings are listed (count and the first 20) in the pull-request review body as well as the log.
- **Proposals (US-L13).** The stateless mode cannot count dismissals across runs, so "the same
  fingerprint dismissed repeatedly" is read from the file itself: the same rule id and message
  dismissed for at least `PROPOSAL_MIN_DISMISSALS` = 3 distinct files, with no `*` entry yet. The
  proposal is one appended `*` entry, printed as a unified diff and never written. **N = 3 and this
  reading of "repeated dismissals" are proposals for the owner to confirm; US-L13 remains first to
  cut.**
