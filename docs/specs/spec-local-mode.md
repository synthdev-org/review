# Local Mode — Developer-Machine and CI Review PRD

Status: Implemented, pending owner acceptance (ADR-0016 and ADR-0017 are PROPOSED) · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-10-25

**Open questions at a glance.** Q1 (subscription terms): UNVERIFIED, backend is opt-in. Q2 (MCP
relevance): UNPROVEN, live run not done. Q3 (where nominations live): answered in ADR-0017 and
implemented as both command options and a names-only committed file, but that ADR is not accepted.
The N = 3 repeated-dismissal threshold for US-L13 proposals (ADR-0016) is also unconfirmed.

**Implementation status.** US-L9, US-L10, US-L11 (MCP knowledge) are **built and tested against
real MCP servers**, but not accepted: ADR-0017 is PROPOSED and open question Q2 (does MCP context
improve a review?) is **UNPROVEN**, because only a live run against a real model and server can show
it (`apps/local_cli/test_mcp_live.py`, not run). The product makes no claim that it helps. US-L7 and US-L8 (subscription backend and its CI
guard) are **implemented as an explicit opt-in**, but open question Q1 (whether Anthropic's terms
permit driving the Claude CLI this way) is **still unverified**: the product does not claim
compliance. Every other story is implemented; see the Traceability table in
[`plan-local-mode.md`](../plans/plan-local-mode.md). Decision D5 is a local-mode-only departure
and applies only to MCP servers the developer nominates (shown in full by `--dry-run`).

Adds a second way to run the review pipeline: a single command on a developer's
machine or in a CI job, with no deployed platform. It reuses the pipeline defined
in [`spec-review-pipeline.md`](spec-review-pipeline.md),
[`spec-verification-pipeline.md`](spec-verification-pipeline.md) and the step
framework ([`spec-pipeline-step-framework.md`](spec-pipeline-step-framework.md)).
Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`); requirements are user stories with `Given/When/Then`
acceptance criteria per `testing.md`'s mandatory rule. Technical planning is
deliberately **not** part of this document; it follows as a separate plan.

**Conventions found / fallbacks.** Spec format: `docs/specs/spec-*.md`, structure
mimicked from `spec-pr-walkthrough.md` (indexed in `docs/documentation_index.md`).
Testing pyramid: `docs/rules/testing.md`. Design system: N/A (no UI of review's
own; output is a terminal, a file and a GitHub PR comment). Every decision is
tagged **decided** or **assumption pending validation** under "Resolved decisions".
Two document conflicts and one owner instruction are surfaced there (D1, D2, D5).

## Problem & Goal

Review today exists only as a deployed service: Webhook API, Worker, Postgres with
pgvector, a Helm chart ([`c4-architecture.md`](../reference/c4-architecture.md)).
That is the right shape for a team or enterprise that wants shared memory and a
durable index. It is the wrong shape for three people who want the same quality of
review:

1. **The solo or indie developer.** No cluster, no ops budget, often no paid CI
   minutes. Today they cannot use review at all.
2. **The developer on a team that has not deployed the platform**, or who wants a
   review *before* pushing, so the first reader of their code is not a teammate.
3. **A small team that wants review in CI** without running a server.

**End users.** (1) The **developer** whose change is being reviewed, who wants
specific, trustworthy findings on their own diff before anyone else sees it.
(2) The **small team** whose CI job posts the same review on a pull request.
The **platform maintainer** is secondary: local mode must not change what server
customers get (US-P1) and must not fork the pipeline (US-P2).

**Why it matters to the end user.** A finding caught before pushing costs minutes;
the same finding caught in review costs a round trip and a teammate's attention.
For developers with no CI budget there is currently no alternative to doing
without review, or to sending code to a hosted SaaS they may not be allowed to use.

**Goal / success metric (capability-based, matching the existing specs):** a
developer with only a checkout, a git branch and a model choice runs one command
and receives the pipeline's findings for their diff on the terminal, with nothing
deployed; the same command in CI posts one pull-request comment; no code leaves
the machine except to the model and the MCP servers the developer chose; and with
local mode absent or unused, server-mode behaviour is byte-identical to today.
**No numeric adoption or review-quality target is set:** there is no pilot data to
ground one, and quality depends on the developer's chosen model. A target is set by
recorded decision after a pilot.

## Scope

### In scope

- A command-line entry point that reviews a local diff (branch vs base, staged
  changes) and prints findings, optionally writing them to a file.
- Running the existing **static analysis first, then LLM review, then
  verification** pipeline for that diff, with the same findings model.
- Choosing the model: any OpenAI-compatible endpoint (including a local model),
  or, locally only, the developer's own logged-in Claude CLI.
- Using the developer's **own already-configured MCP servers** as the knowledge
  source for repository and product context, in place of review's own code index.
- A version-controlled memory file in the repository so a dismissed finding stays
  dismissed, with learned rules reviewable as an ordinary diff.
- Running the same command in GitHub Actions, posting one pull-request comment and
  optionally failing the job on a severity threshold.
- A dry-run that states exactly what would leave the machine, and secret redaction
  on everything sent.

### Out of scope

- **Review's own code index in local mode (call/import graph, embeddings,
  incremental refresh).** Decided (D3): context comes from nominated MCP servers.
  Trigger to revive: a pilot showing developers with no suitable MCP server get
  materially worse reviews than server mode.
- **Shared or team memory, the Admin UI, multi-org tenancy, SSO.** These are
  server-mode features. Local mode has no durable shared state by design (D1).
- **A hosted service or vendor-run SaaS.** Deferred in [`roadmap.md`](../roadmap.md).
- **Driving the interactive agent through a terminal multiplexer (herdr) or any
  scraping of a TUI.** Considered and rejected as a model backend: unstructured
  output and no token accounting. Trigger: a harness that exposes a structured
  non-interactive API only through a pane.
- **Auto-fix and the PR walkthrough in local mode** (first version). Both exist
  in server mode (`spec-auto-fix.md`, `spec-pr-walkthrough.md`). Assumption
  pending validation (D6). Trigger: developer demand after the review path ships.
- **Other git hosts and CI systems** beyond GitHub. The host adapter boundary
  already allows them; none is built here.
- **Automatically applying any change to the developer's working tree.** Review
  never commits or applies a fix (ADR-0015); local mode inherits that.
- **Using one person's subscription login to serve a team or a CI runner.**
  Refused by default (US-L8).

### Priority

- **Must have (end-user impact order):** US-L1, US-L4, US-L5, US-L6, US-L14, US-L15
  (review my diff, with no setup, safely, on a model I already have);
  US-C1, US-C3 (the same review as one pull-request comment that does not
  duplicate on re-push: CI is the owner's stated main goal, D9);
  US-P1 (do not break server customers).
- **Should have:** US-L2, US-L3, US-L9, US-L10, US-L11 (staged review, file output,
  my MCP servers as context); US-L12, US-L13 (memory file);
  US-L7, US-L8 (subscription backend and its guard); US-P2.
- **Could have:** US-C2 (CI severity gate).
- **First to cut** if time is short: US-C2, then US-L7/US-L8 as a pair (the guard
  is only needed if the backend ships), then US-L13. None of these is on the path of
  "an indie developer reviews their diff on a model they already have".

## Requirements

Each story maps to at least one integration test (see the traceability table).
"Findings", "severity" and "step outcome" have the meanings in
`spec-review-pipeline.md` and `spec-verification-pipeline.md`.

### A. Review a local diff

**US-L1 — Review my branch before I push.**
As a developer, I want to review the difference between my branch and a base
branch so that I get feedback before anyone else reads my code.
- Given a git repository with a branch that diverges from `main`,
  When I run the review command with `--base main`,
  Then findings for the changed lines are printed to the terminal, each with file,
  line, severity and message, and the process exits 0 unless a configured gate
  trips (US-C2).
- Given a branch with no changes against the base,
  When I run the command,
  Then it prints that there is nothing to review and exits 0 without calling the
  model.
- Given `--base` names a ref that does not exist,
  When I run the command,
  Then it exits non-zero with a message naming the missing ref.

**US-L2 — Review only what I have staged.**
As a developer, I want to review staged changes so that I can fix problems before
I commit.
- Given staged and unstaged edits to the same file,
  When I run the command with `--staged`,
  Then only the staged hunks are reviewed and findings are anchored to staged line
  numbers.

**US-L3 — Keep the findings.**
As a developer, I want findings written to a markdown or JSON file so that I can
read them in my editor or hand them to another tool.
- Given a completed review,
  When I pass `--output review.md` (or `review.json`),
  Then the file contains the same findings as the terminal, and the JSON form
  validates against a published schema.
- Given the output path is not writable,
  When I run the command,
  Then it exits non-zero before calling the model.

**US-L4 — Works without setting anything up.**
As a developer trying the tool for the first time, I want it to run with only a
checkout and a model choice so that evaluation takes minutes, not an afternoon.
- Given a fresh clone, one configured model, and no database, server, container
  runtime or account,
  When I run the command,
  Then a review completes.
- Given no model is configured,
  When I run the command,
  Then it exits non-zero with a message stating the options (US-L6, US-L7).

**US-L5 — Deterministic checks first.**
As a developer, I want static analyzers to run before the model so that the
findings are anchored by checks that do not hallucinate.
- Given a diff introducing an issue an enabled analyzer detects,
  When I run the command,
  Then the analyzer finding appears in the output and is supplied to the model as
  ground truth, as in server mode.
- Given an analyzer binary is not installed,
  When I run the command,
  Then the review continues without it and the run summary names the skipped
  analyzer.

### B. Choose the model

**US-L6 — Use the model I already have.**
As an indie developer, I want to point review at any OpenAI-compatible endpoint,
including a model on my own machine, so that I pay nothing extra and my code stays
local.
- Given a local OpenAI-compatible server and its model name in configuration,
  When I run the command,
  Then the review completes using it and no other network host is contacted for
  inference.
- Given the endpoint is unreachable,
  When I run the command,
  Then it exits non-zero within the configured timeout, naming the endpoint, and
  prints no partial findings as if complete.

**US-L7 — Use my Claude subscription.**
As a Claude subscriber, I want an optional backend that drives my own logged-in
Claude CLI so that reviews count against my subscription, not a separate API bill.
- Given the Claude CLI is installed and logged in,
  When I select that backend,
  Then a review completes and no API key is read.
- Given the CLI is missing or not logged in,
  When I select that backend,
  Then it exits non-zero with a message saying which, before sending any content.
- Given this backend does not report token usage the way API endpoints do,
  When the run completes,
  Then the run summary states that usage is not available rather than showing
  zero.

**US-L8 — Do not smuggle a personal login into CI.**
As the owner of a repository, I want the subscription backend refused in CI by
default so that no one's personal session ends up on a shared runner.
- Given the run is detected as CI (the conventional `CI` environment signal),
  When the subscription backend is selected,
  Then it refuses with an explanation and exits non-zero.
- Given an explicit override flag,
  When the same command runs in CI,
  Then it proceeds and the run summary states that the override was used.

### C. Knowledge from my own MCP servers

**US-L9 — Use the MCP servers I already have.**
As a developer, I want review to consult MCP servers I have already configured
for my other tools so that it uses the knowledge I already maintain, with no
second configuration to keep in sync.
- Given an existing MCP configuration containing several servers,
  When I run the command without nominating any,
  Then no MCP server is started or contacted.
- Given I nominate one configured server by name (in command options or the
  repository's review config),
  When I run the command,
  Then review reads that server's connection details from my existing
  configuration, consults it for context relevant to the diff, and the output
  states which server supplied context for which finding or note.
- Given I nominate a name that is not in my configuration,
  When I run the command,
  Then it exits non-zero naming the missing entry.

**US-L10 — A failing knowledge source never blocks the review.**
As a developer, I want the review to complete when an MCP server is slow, down or
returns nonsense so that a flaky helper does not cost me the review.
- Given a nominated server that exceeds the MCP deadline (default 10 seconds in
  total across all nominated servers, matching server mode's
  `product_knowledge_deadline_seconds`), returns a protocol error, or returns a
  response that is not valid MCP tool output,
  When I run the command,
  Then the review completes without that context and the run summary and output
  state which server failed and which of those three ways it failed.
- Given a nominated server that returns an empty result,
  When I run the command,
  Then the review completes and the output does not imply context was used.

**US-L11 — Content from a server is data, never instructions.**
As a developer, I want text returned by an MCP server treated as untrusted so that
a poisoned wiki page or ticket cannot steer the review or make it run anything.
- Given a server response containing instructions aimed at the model,
  When review builds the prompt,
  Then the content appears only inside the same inert, framed reference section
  server mode uses for retrieved content, and review issues no further tool calls
  or commands because of it.
- Given a result item with a snippet over 1,000 characters or content over 4,000
  characters (the limits server mode applies to product-knowledge items),
  When review builds the prompt,
  Then it is truncated to those limits, not dropped, and the truncation is noted
  in the summary.

### D. Memory that lives in the repository

**US-L12 — Dismiss a finding once.**
As a developer, I want a dismissed finding to stay dismissed so that I am not
shown the same false positive on every run.
- Given I record a dismissal for a finding in the repository's memory file,
  When the same issue is found on a later run,
  Then it is suppressed and the run summary counts suppressed findings.
- Given the memory file is malformed,
  When I run the command,
  Then it exits non-zero identifying the line, rather than silently ignoring the
  file.

**US-L13 — Learned rules arrive as a diff I can review.**
As a team lead, I want anything review proposes to remember to arrive as a change
to a tracked file so that nothing silently reshapes future reviews, matching the
platform's human-approval principle.
- Given review proposes a new rule from repeated dismissals,
  When the run completes,
  Then the proposal is written only as a suggested change for a tracked file and
  is not applied until a human commits it.

### E. Trust

**US-L14 — Know what leaves my machine.**
As a security-conscious developer, I want a dry-run that lists what would be sent
and where so that I can decide before any code leaves.
- Given any model backend and any nominated MCP servers,
  When I run the command with `--dry-run`,
  Then it prints each destination (model endpoint or CLI, each MCP server), the
  files and byte counts that would be sent to each, and the queries that would be
  sent to each MCP server, and sends nothing.
- Given the diff exceeds the size ceiling (the existing `llm_max_diff_bytes`),
  When I run the command, dry-run or not,
  Then the summary states what was cut.

**US-L15 — Secrets stay out of prompts.**
As a developer, I want credentials found in my diff removed before anything is sent
so that a key in a test file is not shared with a model provider.
- Given a diff containing a string the redactor recognises as a secret,
  When the prompt is built for the model or an MCP query,
  Then the secret is replaced and the run summary reports that redaction occurred.

### F. Use it in CI

**US-C1 — One review comment on the pull request.**
As a small team, I want the same command in GitHub Actions to post one review on
the pull request so that we get CI review without a server.
- Given a pull-request workflow with a model and GitHub token configured,
  When the job runs,
  Then one pull-request review is posted with findings anchored to diff lines, and
  the job log contains the run summary.
- Given the token lacks permission to comment,
  When the job runs,
  Then it exits non-zero naming the missing permission and the findings are still
  printed to the log.

**US-C2 — Optionally fail the build.**
As a team, I want to fail the job above a severity threshold so that high-severity
findings gate the merge.
- Given a configured threshold of `high` and a `high` finding,
  When the job runs,
  Then it exits with a code distinct from tool errors.
- Given no threshold is configured,
  Then the job never fails because of findings (informational, matching server
  mode's neutral check-run).

**US-C3 — A new push updates the review, not duplicates it.**
As a reviewer, I want a new push to refresh the earlier review so that the pull
request does not accumulate stale comments.
- Given an earlier review from this tool on the pull request,
  When the job runs again after a push and a finding from the earlier run is
  still present on the same file and line range,
  Then that finding is not posted again.
- Given a finding from the earlier run is no longer present,
  When the job runs again,
  Then its comment is marked outdated.
- Given a new finding,
  When the job runs again,
  Then it is posted once.

### G. Server mode is unaffected

**US-P1 — Server customers see no change.**
As an enterprise operator, I want nothing in my deployment to change so that
local mode is not a risk to what I run.
- Given the existing end-to-end suite and an unchanged deployment configuration,
  When the full suite runs,
  Then it passes, with no server-mode test weakened or removed, and posted output is
  unchanged for the same inputs (a few existing tests and fixtures are edited additively or for
  typing; see "What changed in existing tests" in the plan).

**US-P2 — One pipeline.**
As a maintainer, I want local and server mode to run the same pipeline code so that
a fix lands once.
- Given the same fixture diff and a recorded model response,
  When run in local mode and in server mode,
  Then the findings have identical file, line range, severity and message text;
  only delivery-specific fields (comment ids, check-run ids, timestamps) may
  differ.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Time.** Every external call (model, CLI, each MCP server, GitHub) takes an
  explicit timeout, per `architecture.md`. The model timeout reuses the existing
  `llm_timeout_seconds` (default 30 s); all MCP consultation together is bounded by
  a deadline defaulting to 10 s, matching `product_knowledge_deadline_seconds`, so
  slow servers cost at most that bound in total. Both are overridable. **No wall-clock target for a whole run is set:** it
  depends on diff size and the developer's model, and there is no pilot data.
- **Size.** The diff sent to a model is bounded by the existing `llm_max_diff_bytes`;
  anything cut is reported (US-L14).
- **Exit codes.** Distinct, documented codes for: success, gate tripped (US-C2),
  usage or configuration error, and runtime failure. A failed run never exits 0.
- **No partial success presented as complete.** A run that could not complete a
  required stage fails visibly; degradation is allowed only where this document
  says so (analyzers, MCP servers).
- **Untrusted input.** Diff content, MCP responses and the repository's own config
  and memory files are untrusted: validated at the boundary, framed as inert data
  in prompts, and never executed.
- **MCP servers run only if nominated.** Starting a server process or contacting a
  remote server happens only for names the developer explicitly nominated (US-L9).
  Review never enumerates and uses everything in the developer's configuration.
- **Credentials.** Review reads MCP connection details from the developer's own
  configuration at the entry point and holds them only in memory; it never writes
  them to output, logs, the memory file or the findings file.
- **Scale.** Single developer or single CI job. No concurrency or multi-tenant
  requirement.

### Privacy / Compliance

- Code leaves the machine only to (a) the chosen model endpoint or CLI and
  (b) MCP servers the developer nominated. US-L14 makes this inspectable before any
  send.
- **MCP queries may include code-derived terms** (symbol names, changed
  identifiers) because these servers are code and knowledge sources. This departs
  from server mode's product-knowledge rule of sending only issue keys
  ([`spec-product-knowledge.md`](spec-product-knowledge.md), decision 5) and is
  recorded as D5.
- No telemetry leaves the machine by default (D7). The run summary is local output.
- Secrets are redacted before sending (US-L15). **Correction (2026-10-25):** server
  mode does not redact prompt input today, only LLM-sourced output text
  (`review/review_generation.py:40-50`), so this is new behaviour, built on the
  existing `TextRedactor`, and it fails closed.
- The subscription backend is the developer's own account used on the developer's
  own machine. Whether Anthropic's terms permit other uses is **not** assumed; see
  open question Q1.

### Observability

- Every run ends with a human-readable summary on stderr: stages run and their
  outcomes, analyzers skipped, MCP servers consulted and how each ended
  (used, empty, timed out, errored), suppressed-by-memory count, redaction
  occurred, truncation, backend used, and token usage or "not available".
- `--output` JSON carries the same fields in machine-readable form so CI can assert
  on them.
- Structured log lines follow the existing logging format and carry no content
  beyond identifiers and counts.

### Rollout & Release Safety

- Additive: a new entry point. No change to the server images' behaviour,
  configuration or database schema is required by this document (US-P1).
- Documented as a distinct mode with its own limits (no shared memory, no durable
  index) so no one mistakes it for a replacement for the server deployment.
- Backwards compatibility: the findings JSON schema is versioned; changes are
  additive within a major, following the existing contract-versioning stance.
- Rollback: not installing or not invoking local mode is complete rollback; nothing
  persistent is created in a repository except the memory file the developer
  chooses to commit.

## UX & Design

N/A as a graphical surface. There is no UI of review's own in local mode and no
design system applies. The user-facing surfaces are text, and these requirements
stand in for the usual UX checks:

- **Primary flow:** one command, sensible defaults, findings on the terminal
  (US-L1, US-L4).
- **First use / empty state:** with no model configured, the error lists the ways to
  configure one (US-L4); with nothing to review, it says so (US-L1).
- **Error recovery:** every configuration error names the missing or invalid item
  and the way to fix it; no stack traces for expected failures.
- **Accessibility of output:** no meaning carried by colour alone; a plain-text mode
  for terminals without colour and for piping; the markdown and JSON outputs are
  the machine-readable path.
- **Consistency:** findings use the same fields, severities and wording as server
  mode (US-P2). The CI comment renders in GitHub's native UI like the existing
  comments.
- **Perceived performance:** progress per stage is shown while the run is in
  flight, so a long model call does not look like a hang.

## Stakeholders & Sign-off / Acceptance Criteria

| Who | Role | Sign-off |
| --- | --- | --- |
| Andy Beak | Owner, sole engineer; product and architecture | Approves this PRD and the ADR/C4 amendments in "Doc edits required elsewhere" |
| First external pilot users (to be identified) | Indie developer / small-team representatives | Validate D1, D3, D6 and Q1 before the first public release |

Acceptance for the whole PRD: every story's Given/When/Then passes as an
integration test, the existing end-to-end suite passes (US-P1; some existing tests and fixtures were
edited, see the plan), and the
traceability table below has no unmapped story.

## Phases / Units

Dependencies run left to right. Units B-F can start once A exists.

| Unit | Stories | Depends on | Risk | Decision vs assumption | Unit acceptance |
| --- | --- | --- | --- | --- | --- |
| A. Local review run | US-L1-L5 | none | Medium: the pipeline currently assumes a job queue and Postgres-backed state; whether it runs without them is a planning question | Decided (D1) | A diff reviewed end to end with no deployed service, and US-P2 holds for it |
| B. Model backends | US-L6-L8 | A | **High for US-L7**: structured output and usage reporting from a non-API backend are unproven; terms question Q1 | L6 decided; L7/L8 assumption (D4) | Local-endpoint review works; subscription backend works or is cut cleanly |
| C. MCP knowledge | US-L9-L11 | A | **High**: arbitrary servers with arbitrary tools; relevance of results is unproven; trust boundary; the existing MCP client is Streamable-HTTP-only and bound to the four-tool contract, so stdio and generic tool use are new; in CI a committed nomination file is attacker-influenced | Decided direction (D3); departure from PK privacy rule is assumption (D5) | One real nominated server improves a fixture review; failures degrade cleanly |
| D. Repo memory | US-L12-L13 | A | Low-medium: file format and dismissal workflow are new | Assumption (D6) | Dismissal persists across runs; proposals never self-apply |
| E. Trust | US-L14-L15 | A, and C/B for what to list | Medium: input-side redaction does not exist today and the current redactor spawns one gitleaks process per call | Decided | Dry-run output matches what a real run sends |
| F. CI | US-C1-C3 | A | Low-medium: dedupe on re-push reuses existing posting logic | Decided | One comment, updated not duplicated |
| G. Parity | US-P1-P2 | A | Medium: guards against drift | Decided | Suites pass and equivalence test passes |

## Resolved decisions

| ID | Decision | Tag |
| --- | --- | --- |
| D1 | Local mode is a **separate, stateless mode** for a single developer or a single CI run. It has no shared memory and no durable index. Server mode remains the product for teams. Conflicts with `c4-architecture.md`'s statement that the "zero infrastructure" CLI design was abandoned because it could not deliver real indexing and team-shared memory; resolved by the owner on 2026-10-25: scope the new mode to the cases where that gap does not apply, and amend C4. | **decided** (owner) |
| D2 | Story selection: the story list drafted with the owner on 2026-10-25 is in scope except the local-index stories (then numbered L8 and L9), which are replaced by US-L9-L11 per D3. Renumbering is in the mapping below. | **decided** (owner) |
| D3 | In local mode, repository and product context comes from **the developer's own MCP servers**, read from their existing configuration so there is nothing new to maintain and the knowledge source is easy to change. Review's own code index is out of scope for local mode. | **decided** (owner) |
| D4 | The subscription backend and its CI guard are included as Should-have, locally scoped, and are the first things cut if Q1 comes back negative. | **assumption pending validation** |
| D5 | MCP queries in local mode may carry code-derived terms, departing from the server-mode product-knowledge rule (decision 5 in `spec-product-knowledge.md`). Justified because the nominated servers are the developer's own and the developer sees queries in dry-run first. Applies to local mode only. | **assumption pending validation** |
| D6 | Memory file, its format, and the dismissal workflow are new. Auto-fix and the walkthrough are not in local mode's first version. | **assumption pending validation** |
| D7 | No telemetry leaves the machine by default. | **assumption pending validation** |
| D9 | CI comment (US-C1, US-C3) is Must-have, promoted from Should-have after self-review found it contradicted the owner's stated main goal of running in CI. US-C2 (the gate) stays Could-have. | **decided** (owner's stated goal) |
| D8 | The CI severity gate is opt-in and off by default, consistent with server mode's always-neutral check-run (`spec-review-pipeline.md`, out of scope: severity-based blocking). | **decided** |

**Story mapping from the 2026-10-25 list.** L1→US-L1, L2→US-L2, L3→US-L3, L4→US-L4,
L10→US-L5, L5→US-L6, L6→US-L7, L7→US-L8, L11→US-L12, L12→US-L13, L13→US-L14,
L14→US-L15, C1→US-C1, C2→US-C2, C3→US-C3, P1→US-P1, P2→US-P2. Previous L8 and L9
(local index) are dropped; US-L9, US-L10 and US-L11 are new and implement D3.

## Testing / Key Scenarios

Per `testing.md`, each story is proven by at least one **integration** test, not a
unit test, against a real adapter or high-fidelity recording. LLM calls use recorded
cassettes by default. Unit tests cover new pure logic (diff selection, memory-file
parsing, redaction coverage, exit-code mapping) and are necessary but not
sufficient. A story is not done until its row below has a passing test.
Storage-touching tests, if any are needed, run against real Postgres, not an
in-memory substitute; whether local mode needs storage at all is a planning
question.

| Story | Level | Boundary the test crosses | Key scenario |
| --- | --- | --- | --- |
| US-L1 | Integration + one E2E | git CLI → pipeline → terminal output | Happy path; no-change; bad base ref |
| US-L2 | Integration | git index → diff selection | Staged-only hunks reviewed |
| US-L3 | Integration | pipeline → file writer | JSON validates against schema; unwritable path fails early |
| US-L4 | E2E | built distribution, no services running | Review completes with a recorded model |
| US-L5 | Integration | analyzer binaries on fixtures | Finding supplied as ground truth; missing binary skipped |
| US-L6 | Integration | OpenAI-compatible stub or LM Studio | Success; unreachable endpoint fails in time |
| US-L7 | Integration | recorded Claude CLI process | Success; missing or logged-out CLI; usage "not available" |
| US-L8 | Integration | CI env signal → backend selection | Refused in CI; override accepted |
| US-L9 | Integration | developer MCP config → real MCP stub server | Nothing started unless nominated; nominated server consulted; unknown name fails |
| US-L9 (value) | Integration | recorded model + MCP stub holding a fact outside the diff | The review with the server cites the fact; the same review without it does not. This is the only test that shows Unit C earns its place (open question Q2) |
| US-L10 | Integration | MCP stub (timeout, error, garbage) | Review completes; failure reported |
| US-L11 | Integration | MCP stub returning hostile content | Content framed inert; truncation noted; no further calls |
| US-L12 | Integration | memory file → findings merge | Suppressed on rerun; malformed file fails with line |
| US-L13 | Integration | proposal → suggested change | Never applied automatically |
| US-L14 | Integration | dry-run → network stubs | Lists destinations and queries; stubs record zero requests |
| US-L15 | Integration | prompt and MCP query assembly | Planted secret absent from every outbound request |
| US-C1 | Integration | respx cassette for GitHub | One review posted; missing permission fails with log output |
| US-C2 | Integration | exit code | Gate trips with distinct code; no gate never fails |
| US-C3 | Integration | respx cassette, two sequential runs | Update not duplicate; resolved marked outdated |
| US-P1 | E2E | existing Compose stack | Existing suite passes (edits to it are listed in the plan) |
| US-P2 | Integration | same fixture through both composition roots | Equivalent findings |

## Truly open questions for the owner

- **State (implemented, pending acceptance):** Q1 UNVERIFIED (backend built as opt-in; the owner
  must check Anthropic's terms); Q2 UNPROVEN (`apps/local_cli/test_mcp_live.py`, `REVIEW_LIVE_MCP=1`,
  not run); Q3 implemented as "both, names only" per ADR-0017 (PROPOSED).
- **Q1. Subscription terms.** May a Claude subscription be driven by a third-party
  tool through the logged-in CLI for this use? Unverified. Answer decides D4 and
  with it US-L7 and US-L8. Recommended: verify with Anthropic's current terms
  before any implementation of US-L7; until then keep it a Should-have.
- **Q2. Relevance with arbitrary servers.** The dev's MCP servers expose
  different tools. Whether and how review chooses a tool and query that yields
  useful context is a product risk (Unit C), not a settled design. Recommended: a
  pilot with two or three real servers before committing to a general mechanism.
- **Q3. Where nominations live.** Command options only, or a committed repository
  config file as well. Recommended: both, with the file holding names only, never
  connection details.

## Doc edits required elsewhere

- `docs/reference/c4-architecture.md`: amend the "zero infrastructure abandoned"
  paragraph and add local mode as a second composition root (D1).
- `docs/adr/`: a new ADR for local mode's storage and composition choices once the
  technical plan exists; an ADR is required if any non-Postgres store is proposed
  (`testing.md` requires real Postgres for DB-touching tests).
- `docs/roadmap.md`: add local mode and its sequencing.
- `docs/documentation_index.md`: add a row for this document.
- `docs/specs/spec-product-knowledge.md`: note that D5 applies to local mode only
  and does not change that spec's decision 5.
