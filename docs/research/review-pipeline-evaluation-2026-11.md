# Review pipeline evaluation, November 2026

Status: exploratory. Small samples. Written 2026-11-25.
Scope: how well the review pipeline finds real problems in pull requests, and how the answer changes with batching, model choice, and the Jev classifier.

Note: "chaptered" is a private repository. Its pull request comments are quoted as examples only, and readers cannot open it.

This document has two jobs. It reports what we found. It also records exactly how we measured, so that a later run with a different pipeline, model, or protocol can be compared with these results. Read sections 2 to 5 before you compare anything.

Related documents:

- [`pr-review-bot-comments-chaptered.md`](pr-review-bot-comments-chaptered.md) studies what Codacy and CodeRabbit said on the benchmark repository. It is the source of the idea to use human-triaged bot comments as ground truth.
- [`../specs/spec-diff-batching.md`](../specs/spec-diff-batching.md) and [`../specs/spec-review-context.md`](../specs/spec-review-context.md) define the pipeline changes that this evaluation exercised.
- [`../adr/0014-classifier-call-type-swappable-backend-typesafe-first.md`](../adr/0014-classifier-call-type-swappable-backend-typesafe-first.md) defines the classifier (Jev) call type.

## 1. Summary of findings

1. The pipeline now covers large pull requests. Before batching, it cut every diff at 200 KB and ignored the rest. After batching, it reviewed most of a 22,000-line branch and listed the files it skipped. Batching applied only to that branch (D0). The two benchmark pull requests (D1 and D2) were small enough, after input shrinking, to review in one call each. See section 3.3.
2. Real bugs are rare in the findings. On our own branch, none of the findings that we graded was a confirmed bug. On a pull request that Codacy reviewed, none of four runs found a confirmed bug. On a pull request that CodeRabbit reviewed, six distinct findings were confirmed by two independent graders, and none was a false positive.
3. All three models that we tried on the CodeRabbit pull request found the one Critical bug that the author had accepted as real. Together the three runs found 4 of the 9 issues that the author accepted.
4. The models differ little in quality. They differ a lot in time and cost. On 2026-11-25, DeepSeek spend was $0.63 and Claude spend was $4.31 (section 6.9). Sonnet 5.5 took about 55 seconds. DeepSeek flash took 190 seconds. DeepSeek flash with pro verification took 309 seconds. DeepSeek pro as the generator took 1,009 seconds.
5. Jev decided about half of the verification checks without help from the second model. Its rejections were harmless. It passed known false positives, because it sees only the finding and one diff hunk.
6. Graders are noisy on borderline findings. Two graders disagreed on 4 of 17 findings. One finding changed from "confirmed bug" to "minor issue" after a second grader read the plan documents.
7. Output size varies a lot from run to run, even with the same model and the same input. The generator produced between 6 and 69 candidate findings.
8. On D2, graph grouping found more bot-accepted issues per run than no-link grouping (3 to 4 against 1 to 2, three runs each). It did not find more confirmed bugs, and on D1 it made no difference. See section 6.6.
9. Decision of 2026-11-25: keep graph grouping as the default. The evidence is suggestive (section 6.6). Building the links is cheap (section 6.7). The name graph finds 93 to 100% of real imports (section 6.8).

Confidence: high for items 1 and 4. Items 2, 3 and 5 are about single-call reviews of D1 and D2, so they say nothing about batching. Medium for items 2, 3 and 5. Low for any ranking of models, because each setup ran once or twice.

## 2. System under test

### 2.1 Code version

All runs used the working tree of branch `feat/smell-and-spec-review`. The base commit is `5828a6fece8919416ab982bc7fd1f57acdafa6c8`. The changes were not committed when we measured. A fingerprint of the uncommitted state is below.

| Item | Value |
| --- | --- |
| Base commit | `5828a6fece8919416ab982bc7fd1f57acdafa6c8` |
| Tracked files changed | 21 (1,395 insertions, 97 deletions), excluding `.env.local` |
| Untracked files | 31, excluding `.env*` files |
| Fingerprint | first 16 hex characters of SHA-256 over `git diff HEAD` followed by the untracked files, each preceded by `== <path>`: `43ebc7f66f58e22a` |

Two earlier states also appear in the results. Run R1 used the pipeline before batching existed. Run R2 used batching with only the first improvements. Section 6.1 lists which state each run used.

The measured code was committed afterwards as `8e85d2c` (`Add graph-grouped diff batching, input shaping and a local runner`). Compared with the working tree that runs R3 to R12 used, that commit adds only the `link_grouping` switch (default on, so the same behavior), the `diff_batch_link_grouping` runner setting, the `--log-level` runner option, and comment and log wording that says "no-link" and not "directory". Runs R13 to R24 used the switch. To reproduce any run, check out `8e85d2c` and set the settings that section 2.3 and the run log name.

### 2.2 Pipeline stages

The local runner (`python -m apps.local_review`) wires the same steps that the worker runs. It swaps the GitHub and Postgres adapters for local ones.

| Stage | State in our runs |
| --- | --- |
| Prefilter | on |
| Static analysis (gitleaks, semgrep) | off |
| Context builder (needs Postgres) | off |
| Intent summary | off |
| Review generation | on |
| Spec review | off |
| Verification | on |
| Output | on, prints to the terminal |

Because static analysis, the context builder and spec review were off, the reviewer saw only the diff, a PR map and its own prompt. It did not see callers, similar code, or specification documents.

### 2.3 Configuration

These are the defaults of `LocalSettings` in `apps/local_review/__main__.py`. Runs used these values unless section 6 says otherwise.

| Setting | Value | What it does |
| --- | --- | --- |
| `llm_max_diff_bytes` | 200,000 | Size of one prompt's diff. A larger diff is split into batches. |
| `max_diff_chunks` | 8 | The most batches per review. Files in later batches are not reviewed and are listed. |
| `diff_batch_concurrency` | 4 | Batches in flight at once. |
| `diff_batch_link_timeout_seconds` | 15 | Time allowed to fetch and parse files for grouping. |
| `review_shrink_enabled` | true | Stub data files, collapse deletion-only hunks, trim prose. |
| `review_stub_data_min_bytes` | 8,000 | A data patch over this size is stubbed. |
| `collapse_deletion_min_lines` | 20 | A deletion-only hunk of at least this many lines is collapsed. |
| `review_prose_max_bytes` / `prose_keep_bytes` | 12,000 / 4,000 | A prose patch over the first value is cut to the second. |
| `review_risk_ordering_enabled` | true | Order batches so that the cap drops the least valuable files first. |
| `review_pr_map_enabled` / `review_pr_map_max_bytes` | true / 8,000 | Give every batch a per-file summary of the whole pull request. |
| `review_hunk_expansion_enabled` | false | Not used. Would show each hunk with its enclosing function. |
| `verification_file_context_enabled` | false | Not used. Would give the verifier the head version of cited files. |
| `review_commit_mode_enabled` | false | Not used. Would review commit by commit. |
| Finding ceiling | 20 per batch | `REDACTION_FINDING_CEILING` in `review/review_generation.py`. |
| `review_quality_v2` | false | The v1 prompt and the four-criterion rubric were used. |

The generation prompt includes a smell baseline (`smell_baseline_enabled`, default true). The verification prompt asks four questions per finding: valid, actionable, specific, valuable.

### 2.4 Batching algorithm

This applies only when the rendered diff is larger than `llm_max_diff_bytes`. The pipeline first shrinks the diff (section 2.3) and then compares the shrunk size with the limit. A smaller diff uses one call, as before. D1 and D2 were smaller than the limit, so they used one call and no grouping.

1. Fetch the head version of every parseable changed file. Tree-sitter parses TypeScript, PHP, Go and Python.
2. Build a link graph over the changed files only. An import link has weight 3. A call link has weight 1. A test file linked to its source file has weight 100. Common generic names are ignored. Hub files (degree of at least 8 and at least 25% of the files) lose their non-test links.
3. Take connected components of the graph. Pack them into batches of at most `llm_max_diff_bytes` minus the PR-map reserve. Split an oversize component along its weakest links.
4. Order batches by a risk score. Source ranks above tests, then docs, then data. Moved code ranks lower than changed logic.
5. Keep at most `max_diff_chunks` batches. List the files of the rest as not reviewed.
6. Send one generation call per batch, up to the concurrency limit. A failed batch is logged and its files are listed as not reviewed.
7. Merge the findings of all batches with the existing dedupe in `findings/merge.py`.

### 2.5 What each model receives

| Call | Receives | Does not receive |
| --- | --- | --- |
| Generation, one per batch | System prompt with review rules and smell baseline. The batch diff. The PR map. Static-analysis findings for the batch files. | Other batches' diffs. Repository code outside the diff. Callers. |
| LLM verification, one per group of findings | The candidate findings with an index. The diff of the files that those findings cite. | Other files. The generation call's reasoning. |
| Jev, one call per finding | The finding text, its severity, its scenario if any, and the diff hunk that contains the cited line, framed as data. Four yes/no questions. | Repository context. Other findings. |

### 2.6 Jev configuration

Jev is the TypeSafe classifier (ADR-0014). It was on in `active` mode for the runs that say so.

| Item | Value |
| --- | --- |
| Endpoint and model | `https://api.typesafe.ai`, `jev-1.14.0` |
| Questions | valid, actionable, specific, valuable (yes/no probability each) |
| Decision | all four at least 0.7 means pass. Any at most 0.3 means reject. Anything else means defer to the LLM judge. A "no" on a high-severity finding defers instead of rejecting. |
| Timeout | 10 seconds per call. At most 2 retries on a throttle response. |
| Redaction | off in the local runner |

In `shadow` mode, Jev runs next to the LLM judge and only records agreement. We did not use shadow mode.

### 2.7 Models and providers

| Name we used | Provider endpoint | What answered |
| --- | --- | --- |
| `deepseek-chat` | `https://api.deepseek.com/v1` | An alias. The API reports `deepseek-flash`. |
| `deepseek-flash` | same | `deepseek-flash` |
| `deepseek-v4-pro` | same | `deepseek-v4-pro` |
| `claude-sonnet-5-5` | `https://api.anthropic.com/v1` (OpenAI-compatible) | `claude-sonnet-5-5` |

Both DeepSeek models spend reasoning tokens. The request sets no `temperature`, no `max_tokens` and no seed. Each provider used its own defaults, so runs are not repeatable. Treat any single run as one sample.

The same model served both generation and verification unless a run says otherwise. The runner reads `REVIEW_LOCAL_LLM_VERIFY_MODEL` for a separate verification model.

## 3. Datasets

Every dataset is a git range. We reviewed each range as if it were a pull request.

| ID | What | Range | Size | Ground truth |
| --- | --- | --- | --- | --- |
| D0 | This repository: `main` to the branch head | `main...HEAD` plus the uncommitted work | 176 files, 22,852 added lines, 3,678 deleted | None. We graded findings by reading the code. |
| D1 | `andybeak/chaptered` PR 555, OIDC staff login (TypeScript, Next.js) | base `98235f3732b700c60401511f23e08b0c9e7e8c83` to head `c02dba58b039534b358193b3ebb1747f444ed3e1` | 80 files, 3,364 added lines | 8 Codacy inline comments, each answered by the author: 4 fixed, 4 "won't fix" with a reason |
| D2 | `andybeak/chaptered` PR 463, canvas card connections (TypeScript, React) | base `dd255150c3d0b66ca6a6ae664ca94c4e4e83e9ad` to head `65fa5605` (the commit that CodeRabbit first reviewed) | 49 files, 3,914 added lines | 12 CodeRabbit inline comments on that commit, each answered by the author |

D1 and D2 need the pull request refs. Section 3.3 shows which datasets were batched. Run `git fetch origin refs/pull/<number>/head` in a scratch clone. Do not fetch into your working repository.

### 3.1 Ground truth for D2

We counted a CodeRabbit comment as accepted when the author's reply says "Fixed" or "Already addressed". We counted it as rejected when the reply says "Won't fix". Of the 12 comments, 9 were accepted (7 fixed, 2 already addressed) and 3 were rejected.

The accepted ones include real functional bugs. The Critical one is that a blur handler unmounts the label editor before the click on the delete button, so the button cannot be clicked. The rejected ones are two comments that became moot when the author dropped the offline mutation queue, and one that the author would not change in isolation.

### 3.2 Ground truth for D1

The four fixed Codacy comments are: the GCM authentication-tag length, a request body size limit before parsing, an operator migration runbook, and splitting the login page into components. The four rejected ones are a deliberate configuration fallback, two layering comments, and a provider-key source-of-truth claim.

### 3.3 Did batching apply?

Batching applies only when the shrunk diff is larger than `llm_max_diff_bytes` (200,000 bytes).

| Dataset | Rendered size | After shrinking | Batched in runs R1 to R12? |
| --- | --- | --- | --- |
| D0 | far over the limit | far over the limit | yes (R2 onward; R1 predates batching) |
| D1 | 193,249 bytes | 166,189 bytes | no, one call |
| D2 | 226,717 bytes | 170,356 bytes | no, one call |

We found this on 2026-11-25, after runs R6 to R12. We had assumed that the benchmark diffs were batched. Runs R6 to R12 therefore compare models on single-call reviews. They do not exercise grouping. Section 6.6 reports an experiment that forces batching on D1 and D2.

### 3.4 Limits of the ground truth

- The labels come from one author who answered bots. A bug that the bots missed and the author never learned about has no label.
- "Accepted" does not mean "important". Several accepted comments are accessibility and style fixes.
- The author's "moot" replies depend on later commits that changed the design.

## 4. Run protocol

### 4.1 How to run

```
python -m apps.local_review --repo <git dir> --base <base sha> --head <head sha>
```

Settings come from `.env.local` (ignored by git) and from `REVIEW_LOCAL_*` environment variables. An environment variable overrides the file. The runner prints findings to stdout. With a Jev key set, it also prints one `jev-answer:` line per classifier call on stderr, with the finding text and the four probabilities.

### 4.2 Rules we followed

1. Run one full review at a time per provider. Two full reviews on the same provider at once made every generation call slow enough to hit the step timeout. See section 7.
2. Set the per-call timeout to suit the model: 120 seconds for flash, 300 seconds for Sonnet, 600 seconds for pro. The step timeout scales with it.
3. Read the repository at the pinned commit. Never check out a commit in the repository under test.
4. Record the elapsed time, the number of candidate findings (Jev calls), and the number of posted findings.

### 4.3 What a run returns

A run returns posted findings. It also logs candidates, which are the findings that reached verification. The difference between candidates and posted findings is the number that verification rejected. The Jev log lists every candidate, so we can join a posted finding to its Jev answers by file and message text.

## 5. Grading protocol

Grading turns a posted finding into a verdict. We used independent AI agents, one agent per finding.

### 5.1 Grader setup

- Each grader is a sub-agent that inherits the session model (Claude Sonnet 5.5). It has read-only access.
- It receives the finding text, the file and line, the severity, and the git range. It does not receive the run it came from, whether Jev passed it, or Jev's scores.
- It must read the code at the pinned head commit, using `git show <sha>:<path>`. For D2 it must not look at later commits, because later commits contain the fixes.
- It must try to prove the claim with a scenario or script, and also try to refute it.
- It returns one verdict, a severity if the claim is real, and evidence.

### 5.2 Verdicts

| Verdict | Meaning |
| --- | --- |
| `confirmed_bug` | A real defect that can occur in the code as written. |
| `true_but_minor` | The claim is correct, but the effect is trivial, unreachable today, or only style. |
| `design_choice` | The code does this on purpose, and the choice is defensible. |
| `false_positive` | The claim is wrong. |
| `unclear` | The grader could not decide after real effort. |

### 5.3 Number of graders

| Dataset and run | Graders per finding |
| --- | --- |
| D0 run 1 | 1, by hand (the author of this document) |
| D0 run 2 | 1, by hand, on a sample of 4 |
| D0 run 3 and later | 1 agent |
| D1 | 1 agent |
| D2 | 2 agents, to measure agreement |

For D2 we counted a finding as confirmed only when both graders said `confirmed_bug`. The graders agreed on 13 of 17 findings. Every disagreement was between neighboring labels.

### 5.4 Metrics

- Candidates: findings that reached verification (equal to the number of Jev calls when Jev is on).
- Posted: findings that the output step printed.
- Confirmed: findings with a `confirmed_bug` verdict. For D2, both graders.
- False-positive rate: `false_positive` verdicts divided by findings graded.
- Recall of accepted comments: the accepted bot comments that the run found, divided by all accepted comments. We matched them by hand, by file and by the problem described. A match does not need the same wording.

### 5.5 Known weaknesses of this grading

- The graders reason about code. They did not run a browser or the test suite. Event-order bugs are judged from the specification of browser behavior.
- One grader's verdict on a borderline finding is unreliable. Use two.
- Graders sometimes rate a real accessibility gap as `true_but_minor`, while the author fixed it.
- A grader may know a library only from memory. Several verdicts say so.

## 6. Results

### 6.1 Run log

All runs happened on 2026-11-25. "Pipeline state" names the code version. Times are wall clock.

| Run | Dataset | Pipeline state | Generation / verification | Jev | Time | Candidates | Posted |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | D0 | before batching (first 200 KB only) | flash / flash | off | not recorded | not recorded | 5 |
| R2 | D0 | batching, first version | flash / flash | off | not recorded | not recorded | 17 |
| R3 | D0 | batching, shrink, risk order, PR map | flash / flash | off | not recorded | not recorded | 20 |
| R4 | D0 | same as R3 | flash / flash | active | not recorded | 69 | 35 |
| R5 | D0 | same as R3 | flash / flash | active, answers linked to findings | not recorded | 43 | 19 |
| R6 | D1 | same as R3 | flash / flash | active | not recorded | 20 | 15 |
| R7 | D1 | same as R3 | flash / pro | active | 156 s | 6 | 6 |
| R8 | D1 | same as R3 | pro / flash | active | 1,009 s | 2 | 2 |
| R9 | D1 | same as R3 | Sonnet 5.5 / Sonnet 5.5 | active | 50 s | 6 | 3 |
| R10 | D2 | same as R3 | Sonnet 5.5 / Sonnet 5.5 | active | 57 s | 8 | 7 |
| R11 | D2 | same as R3 | flash / flash | active | 190 s | 7 | 7 |
| R12 | D2 | same as R3 | flash / pro | active | 309 s | 8 | 8 |
| R13 to R24 | D1 and D2 | same as R3, forced 60 KB ceiling, graph or no-link grouping | Sonnet 5.5 / Sonnet 5.5 | active | 20 to 35 s each | 12 to 17 each | see section 6.6 |

R3 to R5 used the same pipeline, so the spread among them (20, 35 and 19 posted findings) is a direct measure of run-to-run variation. Two earlier attempts at R7 and R8 failed, because we ran them at the same time. They hit the step timeout and produced no findings.

R8 used a per-call timeout of 600 seconds. R9 and R10 ran while R8 was still running. They used a different provider, so they did not compete for the same API.

### 6.2 D0: this repository

| Run | Findings graded | Confirmed | Minor | Design choice | False positive |
| --- | --- | --- | --- | --- | --- |
| R1 | 5 | 0 | 1 | 0 | 4 |
| R2 | 4 (sample of 17) | 0 | 1 | 0 | 3 |
| R3 | 20 | 0 | 4 | 11 | 5 |
| R5 (posted) | 19 | 0 | 5 | 2 | 12 |
| R5 (rejected by Jev) | 5 | 0 | 2 | 0 | 3 |

The false positives are of one kind. The reviewer states a code defect with confidence and has not checked it. Two examples are a regex that would not match the intended case, and a claim that a file was missing when it was tracked in git. Running the regex in Python showed that both claims were wrong.

R4 was not graded. Its Jev answers could not be joined to findings.

### 6.3 D1: Codacy pull request

| Run | Posted | Confirmed | Minor | Design choice | False positive | Codacy-fixed issues found (of 4) |
| --- | --- | --- | --- | --- | --- | --- |
| R6 flash / flash | 15 | 0 | 11 | 0 | 4 | 1 |
| R7 flash / pro | 6 | 0 (1 disputed) | 6 | 0 | 0 | 0 |
| R8 pro / flash | 2 | 0 | 1 | 1 | 0 | 0 |
| R9 Sonnet / Sonnet | 3 | 0 | 1 | 1 | 1 | 0 |

The disputed finding in R7 and R8 says that an invalid OIDC configuration leaves a dead SSO button. One grader called it a confirmed medium bug. Another read the plan documents and found that the fallback is deliberate, so it called the finding minor. We counted it as minor.

The one Codacy-fixed issue that a run found is the request body size limit, which R6 found at low severity. Nothing found the GCM tag length. The earlier research document lists it as a real security issue.

### 6.4 D2: CodeRabbit pull request

17 distinct findings came from the three runs. Three findings were found by more than one run.

| Run | Posted | Confirmed by both graders | Split | Minor or design | False positive | Accepted comments found (of 9) |
| --- | --- | --- | --- | --- | --- | --- |
| R10 Sonnet / Sonnet | 7 | 3 | 2 | 2 | 0 | 2 |
| R11 flash / flash | 7 | 4 | 0 | 3 | 0 | 2 |
| R12 flash / pro | 8 | 4 | 2 | 2 | 0 | 3 |
| Union of the three | 22 posted, 17 distinct | 6 | 2 | 9 | 0 | 4 |

Findings confirmed by both graders:

| Finding | Found by | Severity | Raised by CodeRabbit |
| --- | --- | --- | --- |
| A blur handler unmounts the editor before the delete click | all three | high | yes, Critical |
| The connector handle overlaps the sticky card resize handle | Sonnet, flash/pro | medium | yes, Major |
| Connection summaries are not exposed to assistive technology | flash | medium | yes, Major |
| A drag-suppression flag stays set, so the next click is lost | all three | low | no |
| The connector test id shares a prefix with the card wrapper test id | flash | medium | no |
| The offline-queued create payload omits the client-generated id | flash/pro | medium | no |

The last finding became moot when the author dropped the offline queue. The other two findings that CodeRabbit did not raise are real and small.

The graders agreed on 13 of 17 findings (76%). Across 34 verdicts, 14 were `confirmed_bug`, 18 `true_but_minor`, 2 `design_choice` and none `false_positive`.

### 6.5 Jev

| Dataset and run | Jev calls | Pass | Reject | Defer | Errors |
| --- | --- | --- | --- | --- | --- |
| D0 R4 | 69 | 26 | 6 | 37 | 0 |
| D0 R5 | 43 | 16 | 5 | 22 | 0 |
| D1 R6 | 20 | 12 | 0 | 8 | 0 |
| D1 R9 | 6 | 3 | 1 | 2 | 0 |
| D2 R10 | 8 | n/r | 0 | n/r | 0 |
| D2 R11 | 7 | n/r | 0 | n/r | 0 |
| D2 R12 | 8 | n/r | 0 | n/r | 4 |

"n/r" means that we did not record the pass and defer split. On D0 R4, Jev decided 32 of 69 findings (46%) without the second model.

What Jev did well and badly, from D0 R5 and D1:

- Jev rejected 6 findings that we graded (5 in D0 R5, 1 in D1 R9). None was a confirmed bug. Three were false positives and three were minor.
- Jev passed 10 of 12 false positives in D0 R5. The `valid` score for those ran from 0.73 to 0.91. For the true-but-minor findings it ran from 0.72 to 0.83. The ranges overlap, so no score separated good from bad.
- `specific` was 0.91 or higher for almost every finding. `actionable` was almost never a "no". Only `valid` discriminated, and only at the low end.
- Four of the eight Jev calls in D2 R12 failed and fell back to the LLM judge. The log line does not show the cause. We have not reproduced it.

D2 had no false positives, so it says nothing about whether Jev filters them.

### 6.6 Grouping experiment: graph grouping against no-link grouping

Question: does grouping files by import and call links (section 2.4) find more than grouping them without links? Date: 2026-11-25. This experiment forced batching on D1 and D2, because neither was batched in runs R1 to R12 (section 3.3).

What the baseline arm is. We first called it "directory grouping", and that name is wrong. In `plan_batches` (`review/diff_batching.py`), the baseline arm has no import or call links. Each file is its own group, except that a test file stays with the source file that it tests (the filename rule, weight 100). With risk ordering on, which it was in every run, the groups are sorted by risk score, riskiest first, and packed into batches first-fit. Directory order only decides ties between equal risk scores and applies by itself when risk ordering is off. So the baseline arm is "risk-ordered packing with test pairing", called no-link grouping in this document. The same rule applies when link building fails in production: the review falls back to no-link grouping, not to directory grouping. The earlier version of this section, and some of our messages, said "directory" by mistake.

Protocol, which differs from sections 2.3 and 4 in the points listed:

| Item | Value |
| --- | --- |
| Datasets | D1 and D2 |
| Forced setting | `llm_max_diff_bytes` = 60,000 (about 4 batches per dataset; the default is 200,000) |
| Switch | `diff_batch_link_grouping` true (graph) or false (no links). A new setting. With false, the pipeline still fetches file contents for the PR map, but ignores the links. |
| Models | `claude-sonnet-5-5` for generation and verification |
| Jev | active, as in section 2.6 |
| Repeats | 3 per cell, 12 runs, one at a time, interleaved: graph then no-link for each dataset and repeat |
| Per-call timeout | 300 seconds |
| Run time | 20 to 35 seconds each |

Before the runs, we computed the two plans without any model calls. The graph plan keeps more linked file pairs in one batch.

| Dataset | Linked file pairs | Kept together, graph | Kept together, no-link |
| --- | --- | --- | --- |
| D2 at 60 KB | 98 | 54 | 45 |
| D1 at 60 KB | 167 | 99 | 76 |
| D2 at 100 KB | 98 | 74 | 54 |
| D1 at 100 KB | 167 | 139 | 104 |

Failures: 3 of the 12 runs ended with "could not parse verification response against the verdicts schema" (graph on D1 repeat 2, no-link on D2 repeats 1 and 3). Sonnet returned an empty or non-JSON reply to a verification call, and the pipeline treats that as fatal. Because of this, we compared the findings that reached verification (candidates), which exist in every run, and not the posted findings. Grouping affects generation, and verification is the same in both arms.

Analysis method:

1. For each dataset, one agent clustered the candidates of all six runs into distinct findings. Two findings are in one cluster only when they describe the same issue in the same code. The agent also matched each cluster to the bot comment (CodeRabbit for D2, Codacy for D1) that raises the same issue, or to none.
2. Each unmatched cluster was graded by one agent as in section 5. If the verdict was `confirmed_bug`, a second agent graded it again. A cluster counts as confirmed only when both agree.
3. A cluster matched to a bot comment takes the author's label for that comment (accepted or rejected). It was not graded.

The clustering agent's work was not checked by a second agent. We spot-checked two matches by hand, and both were correct. The analysis used 86 agent calls.

Results, per run. "Accepted" counts distinct clusters that match an accepted bot comment. "Confirmed" counts clusters confirmed by both graders.

| Run | Candidates | Accepted | Confirmed | False positive | Minor |
| --- | --- | --- | --- | --- | --- |
| D2 graph 1 | 12 | 4 | 0 | 2 | 6 |
| D2 graph 2 | 15 | 4 | 2 | 3 | 6 |
| D2 graph 3 | 16 | 3 | 1 | 2 | 10 |
| D2 no-link 1 | 13 | 1 | 1 | 2 | 8 |
| D2 no-link 2 | 13 | 2 | 2 | 2 | 6 |
| D2 no-link 3 | 17 | 2 | 2 | 2 | 9 |
| D1 graph 1 | 15 | 0 | 1 | 1 | 8 |
| D1 graph 2 | 13 | 0 | 1 | 1 | 6 |
| D1 graph 3 | 13 | 0 | 0 | 0 | 7 |
| D1 no-link 1 | 17 | 0 | 0 | 2 | 12 |
| D1 no-link 2 | 13 | 0 | 1 | 0 | 8 |
| D1 no-link 3 | 17 | 0 | 1 | 2 | 9 |

Each column counts the distinct clusters that a run contains. The columns do not add up to the candidate counts, for two reasons. Several candidates in one run can fall into one cluster. The table also leaves out design choices, clusters that only one grader confirmed, and clusters matched to rejected bot comments.

Results, per arm (union of the three repeats):

| Dataset | Arm | Candidates | Distinct findings | Accepted bot issues found | Confirmed | False positive | Rejected bot issues raised |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D2 | graph | 43 | 24 | 4 of 9 | 2 | 4 | 0 |
| D2 | no-link | 43 | 21 | 3 of 9 | 2 | 2 | 0 |
| D1 | graph | 41 | 27 | 0 of 4 | 1 | 1 | 2 of 4 |
| D1 | no-link | 47 | 30 | 0 of 4 | 1 | 2 | 1 of 4 |

On D2, the two arms together found 5 of the 9 accepted CodeRabbit issues. Both found the blur-handler bug (D2-R9) and the keyboard-unreachable control (D2-R10). Only graph runs found the concurrent duplicate create (D2-R2) and the blank option row for an untitled card (D2-R11). Only a no-link run found the handle overlap (D2-R6).

Reading of the result:

- On D2, graph runs found 3 to 4 accepted bot issues per run. No-link runs found 1 to 2. The ranges do not overlap, but each arm has only three runs.
- On confirmed findings that the bots did not raise, no-link runs did slightly better or equal (graph 0, 2 and 1 per run; no-link 1, 2 and 2).
- False positives and minor findings were about the same in both arms.
- On D1, neither arm found any of the four issues that the author fixed. The grouping made no visible difference.

This result suggests that graph grouping helps recall of bot-accepted issues on D2. It does not show it. Three runs per arm, one dataset with an effect, one model, a forced ceiling, and a single clustering agent all limit the claim. The experiment does not show that graph grouping finds more confirmed bugs.

### 6.7 Cost of building the links

We timed the link step on the real data on 2026-11-25. The timings read file contents from a local git directory. In production, the worker fetches each file from the GitHub API, which we did not measure. The pipeline allows 15 seconds for that fetch and falls back to no-link grouping on a timeout.

| Dataset | Files parsed | Source size | Parse, all files | Link building, end to end |
| --- | --- | --- | --- | --- |
| D0 | 145 | 1,635 KB | 667 ms | 921 ms |
| D1 | 70 | 381 KB | 100 ms | 177 ms |
| D2 | 43 | 510 KB | 106 ms | 150 ms |

The median file parses in about 1 ms. The slowest file was 79 KB and took 131 ms. The parse step is `chunk_file`, which parses and then chunks, so pure tree-sitter parsing is a little faster. A model call takes 20 to 300 seconds, so the link step adds almost nothing to the wait. Link building runs in a worker thread and does not block the model calls.

### 6.8 Quality of the name-based links

The link graph matches names (section 2.4). We measured it against a simple real import resolution on 2026-11-25. The resolution handles relative imports and the `@/` alias for TypeScript (D1 and D2), and relative and absolute module paths for Python (D0, with the standard `ast` module). It counts only imports between files that are both in the diff. The "truth" is a set of file pairs. It does not include calls through injected objects, shared types, CSS modules or JSON files.

| Dataset | Real import pairs | Found by the name graph | Name-graph pairs that are real imports | Import-edge pairs that are real |
| --- | --- | --- | --- | --- |
| D1 (TypeScript) | 147 | 137 (93%) | 137 of 167 (82%) | 133 of 140 (95%) |
| D2 (TypeScript) | 92 | 87 (95%) | 87 of 98 (89%) | 86 of 91 (95%) |
| D0 (Python) | 382 | 382 (100%) | 382 of 803 (48%) | 381 of 659 (58%) |

The name graph finds nearly every real import. Its weakness is precision on Python, where common names link unrelated files. The TypeScript misses that we saw are CSS modules, JSON message files and a re-export. Some pairs that the table counts as wrong are real call relationships that need no import, so the precision figures are lower bounds on usefulness. We did not test whether more precise links change what the reviewer finds.

### 6.9 Cost

The only cost data is the provider spend for 2026-11-25, as the project owner reported it. We did not record tokens or cost per run.

| Provider | Spend for the day | What it covers |
| --- | --- | --- |
| DeepSeek | $0.63 | Runs R1 to R8, R11 and R12 (10 recorded runs), three runs that failed on the step timeout, and a few small test calls |
| Anthropic (Claude Sonnet 5.5) | $4.31 | API calls only: runs R9, R10 and R13 to R24 (14 runs), and one small test call. It does not include the coding session. |
| TypeSafe (Jev) | not reported | Jev was called in R4 onward |

Read these figures with care:

- The totals are for one day and one API key per provider. The Anthropic figure covers API calls only. It does not include the coding session, so it does not include the agents that graded findings (section 5), which ran inside that session and used millions of tokens. The cost of the evaluation work as a whole is therefore higher than the figures here. The cost of the review pipeline itself is what these figures measure.
- The two workloads differ. The DeepSeek runs include the largest reviews, five runs on the 22,000-line branch (D0) with up to 69 candidate findings each. The Sonnet runs are all on the smaller D1 and D2 pull requests, as single calls or about four batches. A cost per run therefore understates how much more Sonnet costs per token.
- As a rough guide only, $0.63 over about 13 DeepSeek runs is about $0.05 a run. $4.31 over 14 Sonnet runs is about $0.31 a run. That is about 6 times more per run, on smaller inputs. Both averages include failed runs and test calls.
- The Sonnet runs were 3 to 20 times faster than the DeepSeek runs (section 1, item 4). A fair comparison needs cost per run on the same dataset, with tokens recorded.

## 7. What we learned about running the evaluation

- Do not run two full reviews against one provider at the same time. In the first attempt at R7 and R8, both runs hit the 288-second step timeout. Single calls were fast at the time. Run them one after the other.
- The pro model needs a longer per-call limit. R8 failed at 120 seconds per call and succeeded at 600.
- The shell matters. Our default shell did not split a quoted variable into arguments, so two runs failed at start. Use a script with bash.
- Output size varies. R3 to R5 had 20, 35 and 19 posted findings from the same pipeline and the same model.
- The verification step is fatal when a model returns an empty or non-JSON reply. 3 of the 12 runs in section 6.6 failed this way with Sonnet 5.5. A batched review makes more verification calls, so it has more chances to hit this. A fix is not part of this evaluation.
- Analysis cost grows with the number of runs. Clustering and grading the 12 runs of section 6.6 took 86 agent calls. Plan a smaller analysis, or a cheaper matcher, before adding more repeats.

## 8. Interpretation

1. Batching works as intended. R2 onward reviewed far more of D0 than R1. The review lists the skipped files, so the gap is visible.
2. The reviewer finds real bugs when the code is a UI event flow with a clear failure scenario. D2 shows this. It finds fewer when the issue is hardening or architecture, as in D1. All runs missed the GCM tag length, the migration runbook and the login page split.
3. Model choice changes time more than quality. We cannot rank the models from this data.
4. Verification by a second model and by Jev removes few false positives that matter, because the verifier sees the same diff slice as the reviewer. Its mistakes are the reviewer's mistakes.
5. The model comparisons on D1 and D2 are comparisons of single-call reviews. We have not shown that batching or grouping helps. Section 6.6 reports the first test of the grouping.
6. Recall against bot comments is low (4 of 9 for the best union). This is partly a limit of the ground truth. Bots flag style and accessibility items that a bug-focused reviewer ignores.

## 9. Threats to validity

- Small samples. Most setups ran once. Two datasets have a ground truth.
- One author and one repository supply the ground truth. The language is TypeScript for D1 and D2 and Python for D0.
- Graders are AI agents of one model family. They may share blind spots with the reviewer models.
- The code state was uncommitted and changed during the evaluation. R1 to R5 used earlier states. Only R3 onward used the state in section 2.1.
- Costs were measured only as one daily total per provider (section 6.9). We did not record tokens or cost per run, and the daily totals mix different workloads.
- The runs sent code to third parties (DeepSeek, Anthropic, TypeSafe). That limits which repositories can serve as datasets.
- The reviewer prompts and the pipeline default settings may have changed since this was written.

## 10. How to compare a future run with these results

Record all of the following for each future run. A comparison is only fair when the first five items match.

1. Dataset and git range (section 3).
2. Code version: commit hash, or the fingerprint method in section 2.1 if the tree is dirty.
3. Pipeline stages and settings (sections 2.2 and 2.3), including every value that differs from the defaults here.
4. Models and providers, with the model name that the API reports (section 2.7).
5. Jev state and mode (section 2.6).
6. Per-call timeouts and concurrency.
7. Time, candidates, posted findings, any Jev errors, and the tokens and cost per provider for the run.
8. Number of graders, and the grader prompt version (section 5).

Changes that make old and new results not comparable:

- A change to a prompt, the rubric criteria, or the Jev thresholds.
- A change to the batching algorithm or the shrink rules.
- Turning on a stage that was off: static analysis, the context builder, hunk expansion, verification file context, commit mode, or spec review.
- A change to the grader model, the grader prompt, or the number of graders.
- A new ground truth, or a change to how we match bot comments.

For a fair model comparison, run each setup at least three times and report the range, not one number.

### 10.1 Experiments that we have not run

0b. Compare the name graph with a resolved-import graph (section 6.8): resolve imports per language, and keep a call link only when the caller imports the callee. Measure link quality first, then findings.
0. Repeat the grouping experiment of section 6.6 with more repeats (at least 5 per arm), at a 100 KB ceiling (where the plans differ more), with a second model, and with a second matcher agent to check the clustering. Fix the verification parse failure first, so that posted findings can be compared too.

1. A seeded-bug test. Plant a known set of realistic bugs in a throwaway copy of a repository and measure recall directly. This removes the dependency on bot comments.
2. Repeated runs of each setup, to measure variance (at least 3 each), with token counts recorded per run, so that cost per run and per finding can be compared. The runner currently logs token usage only as telemetry, which the local runner does not export.
3. Jev in shadow mode, to compare its verdict with the LLM judge on every finding.
4. Jev severity classification. Add severity questions to the same request, which adds no extra calls.
5. Hunk expansion and verification file context turned on.
6. The context builder with Postgres, so that the reviewer sees callers.
7. A different ground truth: a repository without bot reviews, graded with executed tests.
8. Fix the classifier error log so that the failure type is visible, then find the cause of the four failures in R12.

## Appendix A. Glossary

- Batch: a group of changed files that one generation call reviews.
- Candidate: a finding that reached the verification stage.
- Posted finding: a candidate that verification accepted and the output step printed.
- Jev: the TypeSafe classifier. It answers yes/no questions with a probability.
- Defer: Jev's answer is uncertain, so the LLM judge decides.
- Accepted comment: a bot comment that the author fixed or marked as already addressed.
- Ground truth: the human decisions that we compare the pipeline against.
