# Future plans

Ideas that are worth pursuing but are **not committed**: no spec, no schedule,
and nothing on [`roadmap.md`](roadmap.md) depends on them. Each entry records
the idea, why it might pay off, why it might not, and the measurement that
decides whether it graduates to a spec and plan. Delete an entry when it is
adopted (moved to a spec and plan) or rejected.

## Classifier-based pre-screening of hunks

Status: idea, captured 2026-11-24. Depends on the Classifier call type
([ADR-0014](adr/0014-classifier-call-type-swappable-backend-typesafe-first.md),
[`plans/plan-deliver-prompt-improvements-and-classifier.md`](plans/plan-deliver-prompt-improvements-and-classifier.md))
being delivered and running in `shadow` mode first.

### The idea

Use a fast, cheap classifier (TypeSafe's Jev is the first backend) as a
**screen in front of the LLM review call**. For each hunk, ask a catalogue of
yes/no questions (defect classes, a subset of smells, "is this hunk low-risk")
in one request. The LLM then reads only hunks the screen flagged, with a short
prompt covering only the flagged categories, instead of the whole diff with the
full checklist.

The classifier cannot find line numbers, write a failing scenario or compose a
comment. It returns probabilities only. So it is a **screen and router**, never
the author of findings: flagged `(hunk, category)` pairs go to the LLM, which
writes the finding, and verification (the classifier-first cascade already
planned) still judges it.

```
hunks --> classifier screen --> flagged (hunk, category) --> LLM writes finding
              | confidently clean                                  |
              v                                                    v
        skipped, no LLM read                          verification (cascade)
```

### Why it might help

- Classifier cost is near zero next to an LLM call (about $0.042 per million
  input tokens; the hunk goes in `state` once with all questions).
- Hunks confidently cleared are tokens the generation call never reads.
- A narrower prompt (only flagged categories' checklists) tends to improve
  precision.

### Why it might not, or only a little

- `prefilter` already drops lockfiles, vendored, generated and binary files by
  path. The screen only adds value on code-shaped hunks that carry little risk
  (renames, formatting, simple additions, tests). In most PRs nearly every code
  hunk is relevant to some check.
- **Recall risk, the main cost.** The highest-value findings in
  [`research/pr-review-bot-comments-chaptered.md`](research/pr-review-bot-comments-chaptered.md)
  needed context beyond one hunk: a fallback dropped by a refactor, a migration
  contradicting the PR description, a helper not reused in a sibling file. A
  false "clean" loses a real finding silently, and nobody sees a comment that
  was never written.
- Most of the 12 smells (duplicated code, feature envy, shotgun surgery,
  divergent change) are cross-file and fit a per-hunk classifier badly. Mysterious
  Name, Speculative Generality and Primitive Obsession might fit.
- **Larger egress.** The verification cascade sends only a finding and its hunk
  to the third party. A screen sends every hunk of every PR, and secret
  redaction (a Gitleaks subprocess per call) scales with hunk count.
- Thresholds need calibrating per category on our own data.

### Gates (what decides whether to build it)

Do these in order; stop at the first failure.

1. **Token split.** Read the existing per-stage token metrics
   (`record_llm_token_usage`, `{stage}` label). If `review_generation` is under
   about half of total LLM spend, the screen cannot save much. Stop.
2. **Cheaper alternative.** Compare against routing first-pass generation to a
   cheaper model through the existing per-stage model routing
   (`llmgateway/routing.py`). If that saves comparable cost with no recall risk,
   prefer it.
3. **Shadow measurement.** Run the screen in `shadow` mode on the eval corpus
   (`review/testdata/eval/`) and real PRs. Record the fraction of hunk tokens it
   would have skipped, and whether any skipped hunk had a finding that was later
   accepted. Proceed only if it skips a meaningful share (initial bar: at least
   20% of hunk tokens) **and** loses no accepted finding.
4. **Per-category check.** Enable only the categories whose shadow precision and
   recall hold. Cross-file smells stay LLM-only.

### If it proceeds

- Needs a spec amendment (a new consumer of the `Classifier` Protocol) and its
  own plan. It uses the same yes/no question type, so ADR-0014 does not change.
- Additive: runs before the full-diff review for cheap categories only; the
  full-diff LLM review remains the default path and the fallback on any
  classifier error.
- Opt-in per org through the same `classifier_provider` setting, with its own
  mode (`shadow` before `active`) and the egress disclosure updated to say all
  hunks, not only findings, are sent.
- Same safeguards as the verification cascade: redact then truncate, bounded
  scanner and request concurrency, circuit breaker, no content in telemetry.

### Open questions

- What is the real split of LLM spend across `intent_summary`,
  `review_generation` and `verification` today?
- Which categories are decidable from a hunk alone? The eval corpus rows with a
  diff excerpt can answer this; rows with `diff: null` cannot.
- Is one request per hunk (about 25 questions, state billed once) cheaper than one
  request per question group, given TypeSafe bills input tokens per request?
