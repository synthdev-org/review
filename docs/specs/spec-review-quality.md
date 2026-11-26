# Review-Quality Improvements Spec

Status: Signed off · Owner: Andy Beak · Last updated: 2026-11-24

Release 1 of the prompt-improvement work (Phases 1-4 below). Derived from
[`docs/research/pr-review-bot-comments-chaptered.md`](../research/pr-review-bot-comments-chaptered.md).
Built against [`docs/rules/architecture.md`](../rules/architecture.md) (tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (tag `RQPTVE`);
stories use `As a / I want / so that` with `Given/When/Then`, each mapped to an
integration test, per `testing.md`.

**Sign-off.** The owner confirmed the Release 1 content below: mandatory
`scenario`, `tier`, reachability severity rubric, style-advice suppression,
opt-in nit tier per org, within-job root-cause dedupe, fence-safe agent-ready fix prompt. `effort` is
dropped. Research items 9 and 11 stay out of scope.

## Problem and goal

**End user: the PR author.** The research shows what makes automated review
costly to read: findings without a failing scenario, severity labels that do not
track real impact (a lint nit marked HIGH), style and complexity advice that
dominates volume, the same root cause repeated per file, and 28-53 inline
comments on large PRs.

**Goal (capability-based, no fabricated precision number):** a review leads with
findings that carry a concrete scenario and an impact-calibrated severity, keeps
style advice out unless the org asks for it, never posts more than a fixed number
of inline comments, merges one root cause into one finding, and renders any
agent-ready fix text so a hostile finding cannot break out of it. Every change is
measured offline against a labelled corpus before it ships.

**Secondary end user: the platform operator**, who can switch the whole bundle
off with one worker setting (`review_quality_v2_enabled`) and sees a
dropped finding counted, not silently lost.

## Scope

**In scope:** `Finding.scenario` and `Finding.tier` (additive); the prompt
rewrite and one extra verification criterion (`not_style_only`); tiered, capped,
deduped, fence-safe output; the `findings_show_nits` org setting; optionally two
deterministic analyzers (RQ8); the offline eval harness (Phase 1).

**Out of scope:** `effort` field; cross-review-round dedupe (needs prior-review
state the pipeline does not hold); plan and spec claim review and learning signals;
the Classifier call type (Release 2, `spec-classifier.md`).

## User stories

### RQ1. Every medium-or-higher finding names a failing scenario

As a PR author, I want each serious finding to say what input or interleaving
breaks, so that I can judge it without reproducing the reviewer's reasoning.

- Given an LLM finding at severity `medium` or above with no `scenario`, when the
  review runs under v2, then the finding is dropped at parse time, a `warning` is
  logged, and `review.generation.dropped_total{reason="missing_scenario"}` is
  incremented.
- Given a finding at `low` or `info` with no `scenario`, when the review runs
  under v2, then it is kept.
- Given the v1 path (`review_quality_v2_enabled` false), when the review runs,
  then no scenario rule applies and output is byte-identical to before.

### RQ2. Severity follows reachability

As a PR author, I want severity to reflect a reachable failure, so that `high`
means act now and a lint-class hit never looks urgent.

- Given a lint-class issue (a non-null assertion, a file-length limit), when it is
  reviewed under v2, then it is never reported above `low`.
- Given a defect with a reachable failing path (a dropped fallback that makes a
  restore throw), when it is reviewed under v2, then it is reported `high`.

### RQ3. No style advice unless the baseline is on

As a PR author, I want style and complexity advice left out by default, so that
the review is about defects.

- Given the smell baseline is disabled, when the diff contains only a style or
  complexity issue ("too many parameters", "extract a component"), then no style
  finding is posted (the generation prompt forbids it and the verification
  criterion `not_style_only` rejects any that slip through).
- Given the smell baseline is enabled, when a smell is found, then it is posted as
  today (`category="smell"`, `low`/`info` only).

### RQ4. Nits are opt-in per organisation

As an org admin, I want nit-tier findings collapsed unless my org opts in, so that
trivial notes do not bury real ones.

- Given an org with `findings_show_nits` false (the default), when nit-tier
  findings exist, then none is posted inline and none appears in the summary
  comment (an all-nit PR posts no inline comments and a correct check-run status).
- Given an org with `findings_show_nits` true, when nit-tier findings exist, then
  they appear in one collapsed summary comment, never inline.
- Given two orgs with different settings, when each is reviewed, then each sees
  only its own setting's behaviour.

### RQ5. At most the cap of inline comments

As a PR author, I want a bounded number of inline comments, so that a large PR
does not produce dozens of them.

- Given more findings than `POSTED_FINDINGS_CEILING`, when the review is posted,
  then at most the ceiling is posted inline (blocker and should_fix first, `nit`
  cut first) and the rest is summarised in one collapsed comment.
- Given a finding with `tier=None`, when ordering, then it is treated as
  `should_fix`.

### RQ6. One root cause, one finding

As a PR author, I want the same defect found in several files reported once, so
that I fix it once.

- Given the same defect reported in two files (same `category`/`rule_id` or both
  `None`, message similarity above the cross-location threshold), when findings
  are merged, then one finding remains with the highest severity and tier, and the
  other locations are recorded in it.
- Given two distinct defects that share boilerplate wording, when merged, then
  they stay separate.

### RQ7. A hostile finding cannot break out of the fix prompt

As a developer pasting a finding's agent-ready prompt into a coding agent, I want
the prompt to treat finding text as untrusted data, so that PR-author-controlled
text cannot inject instructions.

- Given a `scenario`, `message` or file path containing backtick runs, newlines,
  markdown or mentions, when the agent prompt and suggestion block are rendered,
  then the fence is longer than any backtick run in the content, paths failing a
  strict charset are omitted, and an untrusted-data statement is present.
- Given a `scenario` containing a secret-shaped string, when the finding is
  generated, then it is redacted before it is posted (`message` and `scenario`
  both pass `redact_findings`).

### RQ8 (optional). Docs and migrations are checked without an LLM

As a PR author, I want broken doc anchors and unsafe SQL migrations caught
deterministically, so that they are found at zero LLM cost.

- Given a changed markdown file with a broken same-file anchor or a link to a path
  absent from the diff and tree listing, or a migration with a non-concurrent index
  build, a dropped RLS policy or a missing `IF EXISTS`, when the review runs with
  the analyzer enabled, then a finding is produced with no LLM call.
- Given clean fixtures, when the analyzers run, then no finding is produced.

## Non-functional requirements

- **Offline measurement, no live calls in CI.** Prompt and parse changes are
  measured by replaying committed cassettes through the real `LiteLLMClient`
  (respx) and the real `ReviewGenerationStep` and `VerificationStep`. A
  cassette-integrity test fails when a prompt change leaves a cassette stale, so a
  re-record is a deliberate, owner-run step.
- **Secret-free fixtures.** Gitleaks runs in CI over `review/testdata/eval/` and
  every cassette and fixture directory (`scripts/scan-fixtures.sh`); recording
  stores request and response bodies only, never headers. Corpus rows are
  paraphrased, minimal excerpts.
- **Compatibility.** New `Finding` fields are optional (additive). The switch is
  per worker process; rollback is setting it false and restarting workers.
- **Observability.** New instruments are added to `spec-observability.md`'s
  allow-list in the phase that introduces them; label values are closed sets.

## Measurement: the eval harness (Phase 1)

- **Corpus:** `review/testdata/eval/corpus.jsonl`, 40 true-positive and 25 noise
  rows, `{id, source_pr, file, line, severity, finding_text, label, category,
  diff_excerpt|null}`. `diff_excerpt: null` rows feed the verification judge only.
- **Metrics (`review/eval/metrics.py`):** precision of kept findings, recall on
  true positives, findings per PR, per-category keep rate, and the kept row ids.
- **Cassettes:** `review/testdata/eval/cassettes/<version>.json`; `baseline.json`
  holds the v1 metrics. `python -m review.eval.record` records a cassette set
  against a real endpoint (or `--synthetic` without one).
- **Gate:** with about 65 rows a tolerance on a ratio is noise, so the gate is
  "no labelled true positive lost and precision not lower than baseline", or an
  explicit, recorded owner tradeoff.
- **The committed v1 cassette is synthetic** (no live endpoint was available when
  it was written) and says so in its `synthetic` and `note` fields. The owner
  re-records it before any number from it is trusted.

## Traceability

| Story | Given / When / Then (summary) | Phase | Integration test (planned path) |
| --- | --- | --- | --- |
| RQ1 | A medium+ LLM finding has no scenario / review runs under v2 / it is dropped and counted | 2 | `review/test_review_quality_prompt.py` (real `LiteLLMClient`, respx cassette) |
| RQ2 | A lint-class issue / reviewed / never above `low`; a reachable defect is `high` | 2 | same file |
| RQ3 | Smell baseline off / style-only issue in diff / no style finding posted | 2 | same file |
| RQ4 | An org has `findings_show_nits` off / nits exist / none posted, or one collapsed summary when on | 3 | `adapter/github/test_posting_tiers_integration.py` (cassette), plus E2E |
| RQ5 | More findings than the cap / posted / at most the cap inline, rest summarised | 3 | same file, plus `tests/e2e/test_review_pipeline.py` |
| RQ6 | Same defect in two files / merged / one finding, highest severity, locations noted | 3 | `adapter/github/test_posting_rootcause_integration.py` (output step into posting cassette) |
| RQ7 | Hostile `scenario`/path / rendered / fence not closable, secrets redacted | 2, 3 | `adapter/github/test_posting_fence_integration.py` |
| RQ8 | Broken anchor or unsafe migration / reviewed / finding with no LLM call | 4 | `adapter/analyzers/test_markdown_links.py`, `test_sql_migrations.py` via `StaticAnalysisStep` |

Phase 1 delivers the harness the Phase 2-4 tests above run on, not those tests.
Its own integration tests are `review/eval/test_eval_cassettes.py` (real
`LiteLLMClient` over respx cassettes, through the real `ReviewGenerationStep` and
`VerificationStep`: report shape, cassette integrity, no credentials written) and
`review/eval/test_scan_fixtures.py` (real Gitleaks binary: a planted secret fails
the CI step).
