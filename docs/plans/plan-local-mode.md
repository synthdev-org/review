# Plan: Local mode — developer-machine and CI review

**Status: Implemented in the working tree, pending owner acceptance.** All phases are built and
every story has a test path (see Traceability). Not yet true: ADR-0016 and ADR-0017 are PROPOSED
(not accepted), B4 (Anthropic's terms for the Claude CLI backend) is UNVERIFIED, Q2 (MCP context
improves a review) is UNPROVEN, and the N = 3 repeated-dismissal threshold for US-L13 proposals is
an unconfirmed proposal. Nothing is committed. Sequences [`docs/specs/spec-local-mode.md`](../specs/spec-local-mode.md)
into ten phases (0 to 9), following the phase / tests / observability & rollback /
acceptance shape of [`plan-phase-12.md`](plan-phase-12.md). Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification tag
`RQPTVE`), including the Protocol-at-the-consumer and
composition-root-only-wiring rules.

This plan decides nothing the PRD, an ADR or the owner has not decided. Where
the PRD leaves an architecture-significant gap, the plan makes the gap a
**blocker with an owning phase** (see "Blockers") instead of an implementer's
guess. Phases that depend on an unaccepted ADR say so in their first line.

**What drafting found in the code (each checked in the file, 2026-10-25).**
The pipeline is more reusable than expected and two PRD premises were wrong.
- Every `review/*.py` step depends only on consumer-defined Protocols; none
  imports `sqlalchemy` or `asyncpg`. `StepRegistry.run(seed, StepContext)`
  (`review/pipeline.py:233,279`) runs the chain in-process. `StepContext` is
  `{job_id, dry_run}` (`review/step.py:118`).
- The Postgres-only adapters are `PostgresCloneIndexQuery`,
  `PostgresGraphQuery`/`PostgresSimilarityQuery`, `PostgresMemoryRepository` and
  `PostgresSettingRepository`. Clone detection and the context builder can be
  disabled or given empty fakes.
- `apps/worker/main.py::_build_review_registry` (L872-1190) builds the registry
  but is typed with concrete Postgres/GitHub classes, and `_run` requires a
  Postgres DSN and GitHub App key (`apps/worker/settings.py:145,295`).
- **The LLM receives the unredacted diff.** Redaction runs only on LLM-sourced
  output text (`review/review_generation.py:40-50`). The PRD's US-L15 and its
  Privacy bullet called input redaction "existing"; it is net-new (Phase 4).
- **The MCP client is Streamable HTTP only** (`adapter/product_knowledge/client.py:55`
  imports `streamable_http_client`), is bound to the 4-tool v1 contract, and
  resolves its provider from Postgres (`FailOpenProviderResolver`). Reading a
  developer's own configured servers, including stdio ones, is new work.
- `StepContext.dry_run` only suppresses publishing (`review/output.py:321-335`);
  the LLM is still called. The PRD's `--dry-run` ("sends nothing") is a
  different thing and is new (Phase 4).
- No `[project.scripts]` entry point exists; there is no CLI.

## Scope

**In scope** — the PRD's 21 stories (US-L1 to US-L15, US-C1 to US-C3, US-P1,
US-P2), in the order below.

**Out of scope** (tracked in the PRD's own out-of-scope list):
- review's own local code index (PRD D3) — local mode gets context from MCP;
- shared or team memory, Admin UI, multi-org tenancy;
- auto-fix and walkthrough in local mode (PRD D6; both stay off, see
  Assumption A6);
- non-GitHub hosts and CI systems;
- any change to server-mode behaviour (US-P1). Refactors are allowed only where
  they leave worker behaviour unchanged and the existing tests passing (a few existing tests and
  fixtures were edited, listed in "What changed in existing tests" below, so "unchanged" is not
  claimed of the suite).

## Blockers (decisions this plan cannot make)

Each is architecture-significant or hard to reverse; per the principle that a
plan sequences, it does not decide, each is owned by a phase that produces the
decision and gates the phases that depend on it.

| ID | Gap | Why a plan cannot settle it | Owner phase | Gates |
| --- | --- | --- | --- | --- |
| B1 | A local CLI is a **fourth composition root**; `architecture.md` allows three "until a real requirement forces one" (L30-31, L67-69) and says wiring happens only in the webhook and worker roots (L41). Also: packaging and distribution (pip, container image or GitHub Action; the PRD does not say, and the ADR must pick **one**), whether a distributed CLI counts as a "deployable" at all so this does not set a precedent for every future CLI, and how E2E is shaped when `testing.md` defines E2E as the Compose stack (L21, L47) | Contradicts mandatory rules; `architecture.md` says deviation needs an ADR | Phase 0 (ADR-0016) | Phases 1-9 |
| B2 | **How review consults an arbitrary MCP server**: how it discovers config (which client config formats), how it connects (stdio and HTTP; the existing client is HTTP-only), which tool it calls and how it forms a query from a diff. Also the PRD's D5 departure from server mode's "issue keys only" rule | Core product and security design; PRD Q2 says relevance is unproven | Phase 6 (spike) → ADR-0017 | Phase 7 |
| B3 | **Memory matching semantics in a repo file.** The `DismissedFindingMatcher` Protocol is embedding-keyed (`memory/store.py:139`: `best_effective_similarity(embedding, half_life_days, now)`), and `MemorySuppressionConfig` requires an `embedding_client` (`review/memory_suppression.py:29,56`). An exact-fingerprint file store does not fit that boundary. Options for ADR-0016: (i) fix the boundary (a fingerprint-keyed Protocol with the embedder optional; **recommended**, per `architecture.md` "fix the boundary, don't special-case the adapter"; touches the server path so US-P1 must cover it), or (ii) a deterministic fixed-vector embedding pair where 1.0 means equal (no server-path change, but a fake embedder in production code) | Behavioural and boundary choice | Phase 8 start (ADR-0016 amendment) | Phase 8 |
| B4 | **Q1 from the PRD: subscription terms** for the Claude CLI backend | Not a code question; **still unverified**. Phase 3b was built anyway as an explicit opt-in (runbook says so); the owner must still verify before anyone relies on it | Owner | Phase 3 (US-L7, US-L8 only) |
| B5 | **Security posture for CI and MCP** that the plan can state as constraints but the owner must accept: (a) the example workflow uses `pull_request`, never `pull_request_target`; fork PRs get no comment and findings go to the job log; (b) nominations choose **names only**, commands/args/env/url come only from the developer's own MCP config and are rejected if present in any repo file; (c) in CI stdio servers are refused unless defined by the workflow itself, and repo-local MCP configs (e.g. `.mcp.json`) are not read in CI; (d) CI reads the memory file and nominations from the **base** branch, not the PR head | Each is a trust-boundary decision with a usability cost | Phase 0 (ADR-0016) for (a), (d); Phase 6 (ADR-0017) for (b), (c) | Phases 5, 7, 8 |

## Assumptions this plan makes (flagged, not decided)

Sequencing-level calls, easy to override.
- **A1.** The entry point is a new package `apps/local_cli` (a composition root
  beside `apps/worker`), exposed as a console script. ADR-0016 may rename it.
- **A2.** The shared registry builder is extracted from `apps/worker/main.py`
  into a neutral module, `review/registry.py`, so neither root imports the other.
  `_build_review_registry` reads 22 `WorkerSettings` fields (the 22 fields of `ReviewPipelineConfig`) (`llm_model`,
  `walkthrough_*`, `clone_detection_*`, `fix_generation_*`, `memory_*`,
  `product_knowledge_*`), so Protocol-typed parameters are not enough: the
  extraction also introduces a `ReviewPipelineConfig` pydantic model next to
  `StepEnablement`/`StepTimeouts` in `review/config.py`, which both roots populate
  (`WorkerSettings` and `LocalSettings` each map into it). The extraction lands as
  its **own PR before any CLI code**, keeps a thin `_build_review_registry` wrapper
  in `worker_main` (`apps/worker/test_main.py:398,438,519` and
  `test_walkthrough_wiring.py:75` patch and call that name), and is proven
  behaviour-neutral by a snapshot of step names, order, enablement and timeouts for
  representative `WorkerSettings`, pinned constructor arguments derived from
  `ReviewPipelineConfig`, plus the worker suite passing as it stood. Fallback if
  that proves too invasive: local mode copies only the wiring it needs.
- **A3.** First version runs with clone detection, the context builder's code
  index, memory suppression via Postgres, fix generation and walkthrough
  disabled, using the flags in `StepEnablement` (`review/config.py:26`).
- **A4.** Local memory matching (B3) starts as exact-fingerprint match
  (file path, rule id, normalised message hash), no embeddings. ADR-0016 may
  replace it.
- **A5.** The CI path uses `GITHUB_TOKEN`, not a GitHub App installation. This
  needs a token-auth mode in `adapter/github` (see Phase 5); I have not read
  `adapter/github/auth.py` and Phase 5 starts by confirming how invasive that is.
- **A6.** Walkthrough and fix generation are left disabled; no code is written
  for them.
- **A7.** Local adapters (`LocalGitDiffFetcher`, `LocalTreeFetcher`,
  `TerminalFindingsPublisher`, the memory-file matcher) live in a new
  `adapter/local` package, never under `apps/`, per the adapter/entrypoint
  layering. Command-line parsing is stdlib `argparse`; no new dependency.
- **A8.** Findings rendering for terminal and markdown reuses
  `findings/markdown.py` (`escape_markdown`, `strip_display_text`,
  `neutralize_prose`, `suggestion_block`) and moves `_finding_body` out of
  `adapter/github/posting.py` into it if needed, so local and PR output stay
  consistent (US-P2). The JSON schema is generated from
  `findings.models.Finding.model_json_schema()` with a top-level `schema_version`.

## Phase 0 — Decisions and document corrections (gates everything)

**Why first:** B1 and B3 contradict or extend mandatory rules; coding before the
ADR accepts is the "deviation without an ADR" the rules forbid.
- ADR-0016 "Local mode composition root": a fourth entry point, in-process
  execution with no queue, no database and no server (statelessness, PRD D1),
  packaging and distribution, the shape of local E2E (built distribution run in a
  clean container, not the Compose stack), the memory-file format and matching
  rule (B3), and where CI detection lives.
- Amend `docs/reference/c4-architecture.md` (the "zero infrastructure abandoned"
  paragraph and a second composition root), `docs/rules/architecture.md` (the
  "three deployable entrypoints" sentence **and** the composition-root list at
  L41), and `docs/rules/testing.md` (a local-distribution E2E in a clean
  container, its location, and its CI gating: on merge to main or nightly like the
  Compose E2E). All owner-approved in principle by PRD D1; each edit is reviewed.
- ADR-0016 also records the B5(a) and (d) security constraints, and says that the
  CLI's packaging class (a distribution, not a deployable) sets no precedent for
  other CLIs.
- Correct the PRD: US-L15 and Privacy now say input redaction is new work; Unit C
  and E risks updated (done alongside this plan).
- Roadmap and documentation-index rows.

**Tests:** none (documents). **Reuse:** follow the existing ADR format in
`docs/adr/0015-*.md`.

**Observability & rollback:** N/A, documents only.

**Acceptance:** ADR-0016 is marked ACCEPTED by the owner (including the E2E shape
and its CI gating); the C4, architecture and testing rules are amended; and the PRD
no longer claims input redaction exists. The B3 matching decision may be recorded in
an ADR-0016 amendment at Phase 8 start without blocking Phases 1-7.

## Phase 1 — Walking skeleton: review a branch end to end (US-L1, US-L4)

**Why first among code:** it proves the central bet (the pipeline runs in-process
with no Postgres) and exposes unknowns early. Everything else hangs off it.
- **1a — registry extraction (its own PR, merged and released first).** Per A2:
  `review/registry.py` plus `ReviewPipelineConfig`, thin worker wrapper kept, a
  step-name/order/enablement/timeouts and constructor-argument snapshot test, and the worker
  suite passing.
  **Extends** `_build_review_registry` (`apps/worker/main.py:872-1190`); no new
  step code. Rollback is a plain revert of this PR alone; nothing depends on it
  until 1b merges. The Compose-based US-P1 E2E is to be run at this merge, not deferred to
  Phase 9.
- **1b — the CLI.** `apps/local_cli`: console entry (argparse); `LocalSettings`
  via `pydantic-settings` parsed once at the entry point with its own prefix, not
  `REVIEW_WORKER_` (so `apps/test_env_example_sync.py` is untouched); `LocalSettings`
  maps into `ReviewPipelineConfig`; logging configured at this entry point (stderr,
  a verbosity flag, no content in logs); fabricates the `JobIdentity` seed fields
  (`review/step.py:42-84`: `org_id`, `repo_id`, `installation_id`,
  `repo_full_name`, `pr_number`, `head_sha`; optional `base_sha`).
- Local adapters in `adapter/local` (A7), each behind the existing
  consumer-defined Protocol: `LocalGitDiffFetcher` (`ingestion.diff.DiffFetcher`:
  it only **splits** `git diff` output per file, since `DiffFile.patch` is a
  per-file patch string and hunk parsing already exists in
  `review/changed_lines.py`; reuses `SYMLINK_MODE`/`SUBMODULE_MODE` from
  `adapter/github/git_data.py:65-66`; raises `DiffContentUnavailableError`/
  `DiffContentMalformedError`); `LocalTreeFetcher` (`indexing.tree.RepoTreeFetcher`,
  reusing `RepoTreeEntry` and its error types; **only built if** A3's disabled code
  index still needs a tree, otherwise a stub); `TerminalFindingsPublisher`
  (`review.output.FindingsPublisher`, three methods, rendering via A8).
- Reuse: `LiteLLMClient` (`adapter/llm/litellm_client.py`), the analyzer
  adapters (`adapter/analyzers`, no clone needed, they write a temp dir),
  `GitleaksTextRedactor`. Any embedding fake needed for a disabled step is copied
  into `adapter/local`, not imported from a test `conftest`.
- **Request-capture test fixture.** Extend `adapter/llm/conftest.py` (whose
  `respond()` takes no arguments, never sees the request and records nothing) so the
  stub records every request it receives. Phases 3, 4 and 7 assert on what was or was
  not sent; without capture those assertions pass vacuously.
- Repository shapes: shallow clones, detached HEAD and CI merge refs, where a
  missing base ref is the likely real-world case of US-L1's bad-ref scenario.
- Resolve the two open points the investigation did not check: whether
  `FindingsOutputStep` and the step input models require a `context_bundle` or a
  non-empty patch when the context builder is off, and whether
  `check_step_configuration_viable` (`review/config.py:404`) accepts the A3 flag
  set. If either rejects it, the fix is a boundary fix in `review/config.py` that
  **loosens nothing the worker can reach**: the existing worker rejections
  (static analysis enabled with prefilter disabled, and the transitive
  review-generation case) stay, and `review/test_config.py` passes unchanged.

**Tests:** *Unit* — settings parsing, seed fabrication, per-file diff splitting,
mode skips. *Integration* (the story tests): `LocalGitDiffFetcher` against a real
temporary git repository, including shallow, detached-HEAD and merge-ref shapes; the
full registry run against a recorded LLM cassette (US-L1: findings printed,
nothing-to-review exits 0 without an LLM call, bad base ref non-zero). **US-L4 has
its own integration test**: the full registry run with no database, queue or
container runtime (first clause) and with no model configured (second clause:
non-zero exit listing the options). *E2E (one):* the built distribution run in a
clean container with no services, recorded model, as the packaging check.

**Observability & rollback:** local logging as above; step outcomes already carry
metrics, which are no-ops with no collector (OpenTelemetry's API returns a no-op
meter), so the Phase 2 run summary is the only local signal. Rollback: 1a is a
revert of one PR; 1b is "do not invoke".

**Acceptance:** `review run --base main` on a fixture repo prints findings from a
recorded model with no database; the snapshot test and unchanged worker suite pass;
the US-P1 E2E must pass when 1a is committed as its own change (see "Delivery status and
splitting 1a" below: in this working tree that has not happened yet).

## Phase 2 — Complete the local run (US-L2, US-L3, US-L5)

**Why here:** these are small variations once Phase 1 exists, and exit codes and
the run summary are used by every later phase.
- `--staged` selection in `LocalGitDiffFetcher`.
- Findings writers for `--output` markdown and JSON via A8; the JSON carries the
  run summary too (PRD Observability) and a top-level `schema_version`. A
  schema-snapshot test fails if the committed schema differs from the generated
  one. Output path rules: `--output` is a command-line flag only (never read from
  repo config), symlinks are refused, tracked files are not silently overwritten,
  writability is checked before any LLM call.
- Analyzer selection from installed binaries via `shutil.which` and the
  per-analyzer flags in `StepEnablement` (`review/config.py:79-85`). The summary
  distinguishes **skipped** (not installed) from **errored** (present but failed).
- Exit-code map (success, gate tripped, usage/config error, runtime failure) with
  one documented reason string per runtime failure.
- **The run summary is an extensible structure defined here**, not a fixed list, so
  later phases add fields without reshaping it. Fields from the start: stages and
  outcomes, analyzers (used / skipped / errored), backend, token usage or "not
  available", suppressed count, redaction (count, or "failed, nothing sent"),
  truncation, **MCP servers (per server: used / empty / timed out / errored)**,
  **memory file (loaded with N entries / not found / disabled)**, **CI comments
  (posted / updated / outdated / skipped, and any dedupe failure)**. The tests in
  this phase assert the structure accepts those fields, so a later phase cannot omit
  one silently.
- Plain-text mode (no colour; also when output is piped), per-stage progress on
  stderr, and no stack traces for expected failures (PRD UX).
- Runbook entries for this phase's exit codes and summary fields are written in
  this phase's PR, not left to Phase 9.

**Tests:** *Unit* — exit-code mapping, summary rendering, schema generation.
*Integration* — staged vs unstaged hunks on a real repo (US-L2); output files
validated against the schema, unwritable and symlinked paths fail first (US-L3); an
analyzer binary on a fixture, a deliberately missing one, and one that crashes
(US-L5).

**Observability & rollback:** the summary is the observability surface. Rollback:
additive flags. Once the JSON schema is published it is a contract: a breaking
change is a new major `schema_version`.

**Acceptance:** US-L2, US-L3 and US-L5 integration tests pass, with their test paths
filled into the Traceability table.

## Phase 3 — Model backends (US-L6, US-L7, US-L8)

**Why here:** the model choice is a PRD Must; the subscription backend is the
riskiest part and is isolated so it can be cut.
- 3a (US-L6): configuration of any OpenAI-compatible endpoint through the
  existing `LiteLLMClient`; unreachable endpoint fails within `llm_timeout_seconds`.
- 3b (US-L7, US-L8) — **built, opt-in, B4 still UNVERIFIED** (placement: `adapter/llm/claude_cli.py`). A `ClaudeCliClient` implementing
  `llmgateway.client.LLMClient` in a new `adapter/llm` module (the Protocol's
  docstring names this seam, `llmgateway/client.py`). It **extends**
  `adapter/analyzers/subprocess_runner.py::run_analyzer_subprocess` (the shared
  kill-and-reap, bounded-read and timeout mechanism) with a `stdin` parameter and a
  `merge_stderr` option, because the runner currently merges stderr into stdout,
  which would corrupt JSON parsing. Output is parsed and validated into
  `CompletionResponse`; usage is "not available" when absent. A hung or logged-out
  CLI is killed, reaped and reported as a runtime failure naming the backend.
  - **The CLI is an agent and the diff, which an attacker may control, is its
    prompt.** Required invocation hardening: all tools disabled (an empty allowed
    tools list or the CLI's no-tools mode), no permission-skipping flags, an empty
    temporary working directory (not the repository), an allow-listed environment
    that excludes `GITHUB_TOKEN`, model keys and anything else not needed, the prompt
    on stdin (never in argv), and the response validated before use.
    The flags are now fixed from `claude --help` (Claude Code 2.1.308) and listed in the module
    docstring of `adapter/llm/claude_cli.py` and in the runbook: `-p --output-format json
    --input-format text --tools "" --permission-mode dontAsk --permission-prompts none --safe-mode
    --strict-mcp-config --no-session-persistence --disable-slash-commands --system-prompt <fixed>`.
    Beyond the `stdin` and `merge_stderr` options the plan named, the runner also gained an
    additive `env` option (a complete allow-listed child environment), because
    `env_overrides` can only add to the credential-stripped `os.environ`. Selector:
    `REVIEW_LOCAL_BACKEND=claude_cli`; override flag `--allow-claude-cli-in-ci`;
    `BackendSummary.kind` gains `claude_cli` and `ci_guard_overridden`.
  - CI detection (the `CI` environment variable) refuses this backend unless an
    explicit override is passed. It is documented as a **best-effort guard, not a
    security control** (self-hosted runners can unset it).

**Tests:** *Integration* — 3a against the local HTTP stub in `adapter/llm/conftest.py`
(success; unreachable endpoint within the timeout with no findings presented as
complete; **no host other than the configured endpoint contacted**, using the Phase 1
request-capture fixture); 3b against a recorded subprocess fixture (success, missing CLI,
logged-out CLI, hung CLI killed and reaped, **no API key read**, tools disabled and
prompt on stdin asserted from the recorded argv/stdin) and the CI refusal and
override. A **live-mode flag** (manual or nightly, per `testing.md`) runs the real
CLI so the recorded fixture cannot drift unnoticed. *Unit* — output parsing.

**Observability & rollback:** logging follows `litellm_client.py`; no content in
logs. Rollback: 3b is removed as a unit if B4 comes back negative; nothing else
depends on it.

**Acceptance:** US-L6 passes; US-L7/US-L8 pass or are cut with the PRD updated.

## Phase 4 — Trust: input redaction and egress preview (US-L15, US-L14)

**Why before CI and MCP:** both would otherwise send content nothing has screened
or listed.
- **Input-side redaction (net-new).** Reuse `findings.redact.TextRedactor`
  (`findings/redact.py:154`) and `GitleaksTextRedactor`
  (`adapter/analyzers/gitleaks.py:232`), but note the existing use is a single short
  finding message, whereas a diff is large and multi-file. Requirements:
  - **One batched gitleaks invocation** over scratch files in a mode-0700 temp
    directory removed in `finally`, never one subprocess per hunk or file. A hard cap
    on **file count and bytes is enforced before the subprocess** (`llm_max_diff_bytes`
    bounds bytes only), plus an overall deadline. A PR author can otherwise craft
    thousands of tiny files and burn CI minutes.
  - Covers added, removed and context lines and file paths; applied to **everything
    sent**: the LLM prompt, the Claude CLI prompt, MCP queries, and MCP responses that
    re-enter the prompt. Redaction runs after truncation or on the full text, never
    between (a secret split by a cut can escape the ruleset).
  - A path deny-list (for example `.env*`, private-key and credential files): matching
    files are skipped with a notice in the summary, not sent.
  - **Fails closed**: any redaction failure aborts the run with a runtime-failure exit
    code and the summary line "redaction: failed, nothing sent". It must not run
    inside `suppress_telemetry_errors` (ADR-0012 exempts telemetry recording only; the
    recording of the count may use it, the redaction call may not).
  - No setting disables it (no `NoOpTextRedactor` path in the local root); there is no
    flag, so "rollback" is a revert. Coverage is the Gitleaks ruleset only; the
    runbook says bespoke secrets can pass through.
- **Egress preview (`--dry-run`).** The real run's request builders with a
  recording, non-sending transport, so the report cannot drift from a real run (no
  parallel reporting path). Masks headers and tokens in URLs. Distinct from
  `StepContext.dry_run`, which only suppresses posting; a one-line doc note says so.
- Size-ceiling report using `llm_max_diff_bytes` (PRD US-L14), in both dry-run and
  real runs.
- **MCP query capture** is added alongside the LLM request capture delivered in
  Phase 1.

**Tests:** *Integration* — a planted secret (in added, removed and context lines, in a
path, and split across a truncation boundary) is absent from every captured LLM and
MCP request (US-L15); redaction failure aborts the run and sends nothing; a diff over
the file-count cap is rejected before any subprocess starts; a deny-listed file is
skipped with a notice; dry-run records zero requests, lists each destination and
query, and **its report equals the requests captured on a real run of the same
fixture** (US-L14); an oversized diff states what was cut (US-L14, second clause).
*Unit* — redaction coverage on representative secret shapes.

**Observability & rollback:** the summary reports the redaction count or failure.
Rollback: revert (no flag by design).

**Acceptance:** US-L14 and US-L15 pass, test paths filled into the Traceability table.

## Phase 5 — CI: one pull-request comment (US-C1, US-C2, US-C3)

**Why here:** the owner's stated main goal and a PRD Must (US-C2 is Could and stays
separable).
- **Trigger model (B5a).** The example workflow uses `pull_request`, never
  `pull_request_target`, and declares workflow-level `permissions:
  pull-requests: write` and `contents: read`. Fork PRs get a read-only token and no
  secrets, so no comment is posted: the run prints findings to the job log and exits
  non-zero with the reason "no permission to comment" (never a green job with
  nothing posted). Fork behaviour is documented and tested.
- **Token authentication.** Read `adapter/github/auth.py` first (A5, now resolved
  from the reuse review): `GitHubInstallationTokenProvider.get_token(*,
  installation_id, repo_full_name)` is a concrete class typed directly in the
  constructors of `diff.py`, `tree.py`, `posting.py`, `reactions.py`, `walkthrough.py`
  and `repo_config.py`. The lowest-risk change is a sibling `StaticTokenProvider`
  with the same `get_token` signature (ignoring `installation_id`), plus widening
  those constructor parameters to a small Protocol with the existing class as one
  implementation. The existing class and its signature are untouched, so worker and
  webhook callers (about 8 test files) keep working; a test per touched module
  confirms it.
- **Publishing.** Reuse `GitHubFindingsPublisher.post_review` for the review body and
  comments, with a no-op implementation of the existing provenance-recorder Protocol
  (`_record_provenance`, `posting.py`), not an optional parameter. It cannot update or
  outdate earlier comments (`post_review` does not retry writes and only posts), so
  update/outdate is new code in `adapter/github`.
- **Dedupe (US-C3).** `is_platform_comment` (`adapter/github/walkthrough.py`)
  identifies comments by `performed_via_github_app.id`, which a `GITHUB_TOKEN` does
  not have. Identity therefore needs a login-based variant (marker **and** the bot's
  author login, since a PR author can copy a hidden marker): either parameterise
  `is_platform_comment` or add a sibling. Line-range matching is new. The Postgres
  `PostedCommentReader` is not reusable in CI.
- **Failure handling.** Marker lookup, listing, update or "mark outdated" calls that
  fail, are rate-limited (429) or return 5xx are logged, counted in the summary
  (posted / updated / outdated / skipped), and exit non-zero with the findings still
  in the job log. Findings posted to GitHub are sanitised for markdown/HTML
  injection (no auto-linked URLs or images, no `@mentions`, length cap), reusing
  `findings/markdown.py` (A8).
- US-C2: a severity threshold producing a distinct exit code; off by default.
- A documented example workflow, shipped as documentation, not a new deployable.

**Tests:** *Integration* with `respx` cassettes for GitHub (`testing.md`): one review
posted (US-C1); missing permission and fork-PR read-only token exit non-zero with
findings logged (US-C1); two sequential runs covering unchanged, resolved and new
findings, plus a spoofed-marker comment by another author that must not be updated
(US-C3); 429/5xx on lookup and on update (US-C3 failure handling); threshold vs no
threshold exit codes (US-C2); a canary test that a planted `GITHUB_TOKEN` is absent
from the log, summary, JSON output and exceptions.

**Observability & rollback:** the job log carries the run summary. Rollback: remove
the workflow file; a comment already posted stays on the PR (documented), and the
next run marks stale findings outdated.

**Acceptance:** US-C1, US-C2 and US-C3 integration tests pass, paths filled in.

## Phase 6 — MCP spike and ADR-0017 (resolves B2, B5b/c; may start in parallel after Phase 1)

**Why early:** Unit C is the PRD's highest-risk unit and Q2 (does it improve
reviews?) is unproven; learning that late would waste Phases 7-9. It stays a separate
phase because a go/no-go and a security ADR are its deliverables, not code.
- Time-boxed spike against two or three real MCP servers the owner uses: read their
  client configuration, connect over stdio and HTTP using the official `mcp` SDK
  already a dependency (`mcp>=2.3,<3`), list tools, and try simple tool/query
  strategies on fixture diffs. **Live evidence for Q2 is produced here** (a
  documented manual run against a real model), because a recorded cassette cannot
  show relevance.
- Output is ADR-0017 plus that evidence, not shippable code; the spike's code is
  deleted or marked throwaway.
- ADR-0017 must decide: which config sources are read (repo-local ones such as
  `.mcp.json` excluded in CI); the names-only nomination schema (B5b) and refusal of
  stdio in CI (B5c); environment scrubbing for stdio children (so `GITHUB_TOKEN`
  and model keys are not inherited); caps on tool calls, list-tools size and stdio
  bytes read; whether the egress policy's loopback/link-local denial is loosened for
  nominated localhost servers only (the CI runner metadata address stays blocked);
  and that tokens are never forwarded across a redirect.

**Tests:** none beyond the spike's recorded observations.

**Observability & rollback:** N/A, no shipped behaviour.

**Acceptance:** ADR-0017 ACCEPTED by the owner, including the security posture (the
PRD's D5 departure) and a go/no-go on shipping Unit C.

**Status (spike done, ADR-0017 PROPOSED).** The spike ran against stub stdio and Streamable HTTP
servers built on the official SDK; no real server was contacted (all configured ones need
credentials or are not safely runnable here) and no model endpoint was used, so **Q2 is
UNPROVEN**. The deviation from the bullet above is deliberate: "live evidence for Q2 is produced
here" was not possible; ADR-0017 records the exact manual live-run procedure and a go/no-go bar
instead. The spike code was kept in `/tmp` and is not in the repo. Acceptance (owner sign-off)
is still outstanding, so Phase 7 stays blocked.

## Phase 7 — MCP knowledge (US-L9, US-L10, US-L11) — built; relevance UNPROVEN (ADR-0017 PROPOSED)

**Status (built, owner decisions outstanding).** Implemented per ADR-0017 and tested against real
MCP servers on the official SDK. **Two things are not settled and the runbook says so:** (1) PRD Q2,
whether the context improves a review, is **unproven**; value test (b) is
`apps/local_cli/test_mcp_live.py`, skipped unless `REVIEW_LIVE_MCP=1` and **not run** (it needs a real
model endpoint and a real server); (2) ADR-0017 is PROPOSED, so the security posture and the go/no-go
are not accepted. **Where it lives:** `adapter/product_knowledge/mcp_tools.py` (stdio opener with a
byte-bounded reader and kill-and-reap, HTTP over the existing `GuardedTransport`, tool choice),
`adapter/local/mcp_config.py` (nominations, definitions, CI rules), `adapter/local/mcp_knowledge.py`
(query, provider, summary records), `apps/local_cli/mcp.py` (wiring), plus additive local edits to
`summary.py`, `egress.py`, `terminal.py`, `report.py`, `redaction.py` (`redact_texts`),
`unavailable.py` (`EmptyIndexServices`), `settings.py`, `main.py`. **No server-mode file was
edited**; the shared product-knowledge seam (`ContextBuilderStep`'s resolver/opener, a
`ProductKnowledgeProvider`) is used with new local implementations. **Deviations from the text
below, each deliberate:** `NegotiationCache` is not used (a nominated server speaks no contract, so
nothing is negotiated; the SDK `Client`, `ClientTimeouts`, `GuardedTransport` and the failure
classifiers are reused); loopback is allowed by a `GuardedTransport` subclass rather than a new
`EgressPolicy` field, so `transport.py` is untouched; the ADR's `~/.claude.json` size cap is 8 MiB
for that one file (it holds the developer's whole history) and 256 KiB for every other file; a
server defined and nominated only by repository files is refused (hardening the ADR does not
state); the pipeline's own title-based query is replaced by a diff-derived one, with the seed
carrying a fixed marker title only so the provider branch runs; the context builder is enabled only
when a server is nominated, with `EmptyIndexServices` so its code branch finds nothing; tool choice
with several search-like tools is `no usable tool` (the ADR's "first by name order" and "several
tie, skipped" contradict; never guessing was chosen). The `should_query` exposure gate is passed by
`allow_public_repos=True`, because the server is the developer's or the workflow author's own choice
and `--dry-run` shows the query.

**Phase 7 hardening (after review).** A tool is called only if the server declares it read-only
(or the developer named it with `--mcp NAME:TOOL`) and the query goes only into a query-named
parameter (ADR-0017 Decision 8); nominations in CI with `--staged` are refused (HEAD is the pull
request); malformed URLs and deeply nested nomination files are exit-2 usage errors; an unexpected
server failure is `errored`; the tool name in an item title is validated and redacted; orphaned
children of an exited stdio server are killed; the inherited environment, SDK logger and teardown
registry belong to the composition root. **One shared file is touched, behaviour-neutral:**
`adapter/product_knowledge/client.py` (server mode) now exports `exception_leaves`,
`network_outcome` and `is_malformed` (previously underscore-private, imported across modules) and
`adapter/local/memory_file.py` exports `safe_relative`, `read_working_tree`, `read_from_commit`
(renamed in place; their only other users are in this feature). The file-read exception for
`mcp_config.py` is recorded at the end of ADR-0017.

### Original Phase 7 text

- Implementation per ADR-0017. Constraints already fixed by the PRD and the code:
  servers run only if nominated; connection details are read from the developer's
  configuration into memory only; per-item limits as server mode (snippet 1,000,
  content 4,000 characters); total deadline 10 s; content framed with
  `INERT_REFERENCE_DATA_FRAMING` (`review/diff_prompt.py`); HTTP servers use the
  existing egress guard (`adapter/product_knowledge/transport.py`).
- **Reuse, not a second client.** Reuse `McpProductKnowledgeClient`'s session,
  `ClientTimeouts` and `NegotiationCache` (`adapter/product_knowledge/client.py`) and
  swap only the transport; the arbitrary-tool selection and query formation per
  ADR-0017 is the genuinely new part. The stdio opener lives in
  `adapter/product_knowledge` (the subprocess rule keeps it out of `review/`), with
  its own byte cap, process-count cap, and kill-and-reap with no zombies.
- **Server path untouched.** Local mode adds new implementations and does not edit
  shared types or settings in `review/product_knowledge.py` or the product-knowledge
  env/contract files (`apps/test_product_knowledge_env_sync.py` stays green). If
  ADR-0017 needs a shared-type change, that change gets its own US-P1-covered PR.
- Replace `FailOpenProviderResolver(PostgresSettingRepository)` on the **local
  composition path only**; review the `should_query` exposure gate for public repos
  and forks against the local case.

**Tests:** *Integration* with a real MCP stub server on the official SDK: nothing
started unless nominated, nominated server consulted, unknown name fails, a repo file
with `command`/`args`/`env`/`url` keys is rejected, a PR-head nomination is ignored
in CI (US-L9); output names which server supplied context for which finding (US-L9);
timeout, protocol error, malformed output, empty result (US-L10); hostile content
framed, truncated with the truncation in the summary, and **no further tool calls or
commands issued** (US-L11); a canary that a planted token in MCP config/env is absent
from the summary, JSON, exceptions and dry-run output; scrubbed child environment.
**The value test is split:** (a) a request-sensitive stub or hash-keyed cassette
asserts the retrieved fact is in the prompt; (b) the live-mode run (manual/nightly)
is the real evidence that the context improves a review. A fixed canned reply cannot
prove (b).

**Observability & rollback:** per-server outcomes in the summary. Rollback: inert
without nominations; removing the option disables it.

**Acceptance:** US-L9 to US-L11 pass; the value test (a) passes and (b) is recorded.

## Phase 8 — Repository memory file (US-L12, US-L13) — per the ADR-0016 B3 amendment

- A matcher implementation behind the boundary B3 settles (recommended: a
  fingerprint-keyed Protocol with the embedder optional, covered by US-P1 because it
  touches `review/memory_suppression.py` on the server path). Key = existing
  `memory/fingerprint.py::build_finding_fingerprint` (raw `file\nrule_id\nmessage`
  text), hashed with `hashlib`; stored fields come from **post-redaction** text so
  the file never echoes a secret. `memory.decay` is skipped.
- Safe parser (no unsafe YAML load, no alias bombs), size and line limits, parse
  timeout, line-numbered errors (non-zero exit on a malformed file).
- **In CI the memory file is read from the base branch** (B5d), so a PR author cannot
  dismiss their own findings; suppressed findings are also listed as a count and
  visible item in the PR comment, not only on stderr.
- Proposals (US-L13): the trigger is defined here as the same fingerprint dismissed
  in N runs or reviews, with N set in ADR-0016's amendment (or US-L13 is cut, being
  first-to-cut). Written only as a suggested change to a tracked file; never applied.

**Tests:** *Integration* — suppression on a second run; malformed file reports the
line; missing/disabled file reports "not found"/"disabled" in the summary, not
"suppressed: 0" (US-L12); in CI a PR-head memory file is ignored; oversized file
rejected; a proposal is emitted and the working tree is unchanged (US-L13). *Unit* —
fingerprint stability.

**Observability & rollback:** memory-file status and suppressed count in the summary.
Rollback: disable the flag.

**Acceptance:** US-L12 and US-L13 pass (or US-L13 is cut with the PRD updated).

## Phase 9 — Parity, end-to-end, packaging and documentation (US-P1, US-P2)

- US-P1: the full existing suite, including the Compose-based E2E, passes against the finished
  tree. It is **not** "unmodified": the edits to existing tests and fixtures are listed in
  "What changed in existing tests" below. No Compose E2E was run at a 1a merge, because 1a was
  never a separate merge in this working tree.
- US-P2: the same fixture diff and recorded model through both composition roots
  yields identical file, line range, severity and message. The server side runs on
  real Postgres via testcontainers; the fixture disables the same steps in both roots
  (A3), or the test would compare different pipelines.
- Packaging per ADR-0016 (one channel), the clean-container E2E as `testing.md` now
  defines it, a published-release rollback note (yank or pin the previous version),
  documentation index and roadmap rows, a note in `spec-product-knowledge.md` that
  PRD D5 is local-only, and the consolidated runbook (per-phase entries already
  written in their own PRs).

**Tests:** *Integration* for parity; *E2E* local-distribution test per ADR-0016.

**Observability & rollback:** N/A beyond the above. Rollback: yank or pin.

**Acceptance:** US-P1 and US-P2 pass; every row of the Traceability table below has a
real test path.

**Status (Phase 9 delivered).** Parity test, `[build-system]` packaging, the clean-container E2E
(`tests/e2e/test_local_distribution.py`, picked up by `.github/workflows/e2e.yml`), the rollback
note and the consolidated runbook (`docs/reference/operations.md`, "Local mode") are done. Phase 7
(US-L9 to US-L11, MCP knowledge) was built afterwards: its tests exist, but its relevance (Q2) is
unproven and ADR-0017 is not accepted (see Phase 7's status). Phase 3b (US-L7, US-L8) was
built afterwards as an explicit opt-in backend: its tests exist and its Traceability row has paths,
but B4 (whether Anthropic's terms permit driving a subscription through the CLI) is **still
unverified** and the runbook says so.

**E2E as actually run (final verification run).** The full Compose E2E (`tests/e2e`) gave 42
passed and 1 failed. The failure is `tests/e2e/test_observability.py::test_dashboard_panels_resolve_real_data_within_two_minutes`
(four panels have no data after 120 s); it fails identically on a pristine `git archive HEAD` copy,
so it is pre-existing and not caused by local mode. It was not fixed (unrelated server behaviour),
and "Compose E2E green" is therefore NOT claimed. `tests/e2e/test_local_distribution.py` (the built
wheel in a clean container) passed, as one of the 42. US-P2 now runs the server side on a real
migrated pgvector Postgres (testcontainers). The default and integration suites were green; the
Compose E2E was still never run at a separate 1a merge.

## Traceability

Every phase's acceptance requires its rows to carry a real test path before the
phase is done (`testing.md`: Story → G/W/T → test path). Paths are assigned in each
phase PR; none is left as a description.

| Story | Phase | Integration test (path filled in by the phase PR) |
| --- | --- | --- |
| US-L1 | 1b | `apps/local_cli/test_local_review.py` (`test_us_l1_*`: full-registry run against the recorded model with request capture, nothing-to-review exits 0 with no model call, bad and shallow-missing base ref exit non-zero, detached HEAD, CI merge ref, unreachable model); `adapter/local/test_diff.py` (real temporary git repos incl. shallow, detached, merge-ref, rename, symlink, submodule, binary) |
| US-L4 | 1b | `apps/local_cli/test_local_review.py` (`test_us_l4_*`: review completes through the real entry point with only a checkout and a model, no database or worker import; no-model error lists the options and sends nothing). Clean-container E2E: `tests/e2e/test_local_distribution.py` (the built wheel installed in a clean container, recorded model, no services) |
| US-L2, US-L3, US-L5 | 2 | `apps/local_cli/test_complete_run.py`: `test_us_l2_*` (staged hunks only and staged line numbers, nothing staged, no commits), `test_us_l3_*` (JSON validates against `docs/reference/review-report.schema.json`, markdown neutralises markup, empty report, unwritable/symlink/tracked/wrong-extension paths fail before any model call, failed run writes no file), `test_us_l5_*` (real `ruff` finding reported and given to the model, missing binary `skipped`, crashing binary `errored`, static analysis off); also the summary, progress, exit-code and no-traceback tests in the same file. Unit: `adapter/local/test_report.py` (schema snapshot), `test_summary.py`, `test_observer.py`, `test_output.py`, `test_usage.py`; `apps/local_cli/test_runbook.py` (runbook lists every exit code, failure reason and summary field) |
| US-L6 | 3a | `apps/local_cli/test_model_endpoint.py` (`test_us_l6_*`: review completes on the configured OpenAI-compatible stub and every connection/lookup goes only to that host:port, using the request-capture fixture; an endpoint that never answers fails with exit 1 within the timeout, naming the endpoint, with no output that reads as a completed review). The connection-refused case is `test_us_l1_an_unreachable_model_*` in `test_local_review.py` |
| US-L7, US-L8 | 3b | `apps/local_cli/test_claude_cli_backend.py` (real git, the real pipeline, the recorded `claude` process): `test_us_l7_*` (a review completes with the planted `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` absent from every recorded child environment and usage `not available`; a missing CLI and a logged-out CLI exit 1 naming the Claude CLI before any completion call; a hung CLI fails within the timeout and its process is gone), `test_us_l8_*` (`CI=true` and `GITHUB_ACTIONS=true` refuse with exit 2 and start no process; `--allow-claude-cli-in-ci` proceeds and the summary states the override), opt-in default and dry run. Client level: `adapter/llm/test_claude_cli_process.py` (hardened argv with `--tools ""` and no permission-skipping flag asserted from the recording, prompt on stdin and never argv, empty temporary working directory removed afterwards, allow-listed environment, missing/logged-out/lapsed/hung (killed and reaped)/crashed/garbage/oversized/error-result), `adapter/analyzers/test_subprocess_runner_stdin.py` (the new `stdin`, `merge_stderr`, `env` options). Unit: `adapter/llm/test_claude_cli.py`. Live mode: `adapter/llm/test_claude_cli_live.py` (flag-vs-`claude --help` drift check whenever the CLI is installed; one real call only with `REVIEW_LIVE_CLAUDE_CLI=1`). **B4 (Anthropic's terms) is still UNVERIFIED**; the backend is opt-in and the runbook says so |
| US-L14, US-L15 | 4 | `apps/local_cli/test_trust.py` (`test_us_l15_*`: planted secret in added/removed/context lines and a path absent from every captured request, secret split by the size ceiling, fail-closed on scanner failure and on a hung scan, file-count cap rejected before the scanner starts, deny-listed file withheld and named, no off switch; `test_us_l14_*`: dry-run records zero requests and its report equals the requests a real run of the same fixture captures, oversized diff states what was cut in real and dry runs, credentials masked). Unit: `adapter/local/test_redaction.py`, `adapter/local/test_egress.py`; `adapter/analyzers/test_gitleaks.py` (batched finder, scratch directory removed). The MCP halves (Phase 7) are in `apps/local_cli/test_mcp.py`: `test_us_l15_*` (a secret in a server's response and in the diff, hence the query, never reaches a model prompt, a server, stdout, stderr, JSON or markdown; a failing scanner starts no server and sends nothing), `test_us_l14_*` (a dry run lists each server, its transport, host, tool and exact query, starts and contacts none, and shows no configured value); unit: `adapter/local/test_mcp_knowledge.py` (query redaction and withholding, response redaction, fail-closed scan), `adapter/local/test_mcp_surfaces.py` (`redact_texts`, the egress text). Audit additions: byte cap rejected before the scanner starts and a mode-0700 scratch directory (`test_trust.py`); `CI=true` without a pull-request context refuses the working-tree memory file, redacted text is what memory keys on and what is printed, no telemetry host contacted (`test_credentials.py`) |
| US-C1, US-C2, US-C3 | 5 | `apps/local_cli/test_ci.py` (respx cassettes for GitHub, the full pipeline on a real repo and the recorded model): `test_us_c1_*` (one review posted anchored to the diff line with the job-log summary; missing permission and fork read-only token exit 1 with findings logged; non-`pull_request` event and missing token exit 2; hostile text sanitised), `test_us_c2_*` (gate code 3 at the threshold, not below it, never without one, never for a tool error), `test_us_c3_*` (unchanged finding not re-posted, resolved finding minimised and new one posted once, no findings still outdates, reworded finding patched, spoofed marker by another author never touched, 429/5xx on lookup, update and outdate), and the token canary test. Unit: `adapter/github/test_ci_review.py` (identity, keys, sanitising, `StaticTokenProvider`, every widened constructor accepts it, walkthrough sends its token), `apps/local_cli/test_ci_unit.py` (event parsing, gate count). Audit additions: `pull_request_target`/`push`/missing event name exit 2, `--staged` refused in Actions, range-comment keying, GitHub read timeout (`test_ci.py`); credential-named variables never reach git, gitleaks or analyzer children and a planted model key is absent from stdout, stderr, JSON, markdown, dry-run and logs (`apps/local_cli/test_credentials.py`) |
| US-L9, US-L10, US-L11 | 7 | `apps/local_cli/test_mcp.py` (real git, the real pipeline and context-builder provider branch, real `gitleaks`, real MCP servers on the official SDK spawned as stdio children or served over loopback HTTP, a request-sensitive recorded model). `test_us_l9_*`: nothing started unless nominated; a nominated server consulted once and named per finding in stdout, summary and JSON; **value test (a)**: the retrieved fact is in the prompt, framed inert, and the same model without the nomination cannot produce the finding; unknown name exits 2 naming it with nothing sent; found in `~/.claude.json`; nominated by the committed file; a nomination file with `command`/`args`/`env`/`url`/`headers` rejected with nothing run; a server only the repo defines and nominates refused; named tool; HTTP with its own headers; **CI**: a nomination added by the PR head ignored, one on the base honoured, stdio refused without a workflow-provided file, a config file inside the checkout refused. `test_us_l10_*`: hang, crash, tool error, garbage, over-cap output and ambiguous tools each leave the review complete, state which server and how, and leave no process; one deadline for three hung servers; empty result implies no context; one failing server does not stop another; HTTP 500. `test_us_l11_*`: hostile content only inside the framed section, cut to 4,000 characters, truncation in the summary, exactly one tool call and nothing run. Also the canary (a planted token in MCP config, env and the process environment absent from stdout, stderr, JSON, markdown, dry run, model requests, logs and error messages) and the scrubbed child environment. Client level: `adapter/product_knowledge/test_mcp_tools.py` (stdio and HTTP against the stub: scrubbed environment and empty working directory, kill-and-reap, byte cap on the stream, list-tools cap, redirect never followed and the configured header never crossing it, loopback only for a literal loopback name, link-local/metadata/unspecified/multicast blocked, cleartext credentials refused). Unit: `adapter/local/test_mcp_config.py`, `adapter/local/test_mcp_knowledge.py`, `adapter/local/test_mcp_surfaces.py`, `apps/local_cli/test_mcp_unit.py`. **Value test (b), the evidence that context improves a real review, is `apps/local_cli/test_mcp_live.py` (skipped unless `REVIEW_LIVE_MCP=1`) and has NOT been run; PRD Q2 stays unproven** |
| US-L12, US-L13 | 8 | `apps/local_cli/test_memory_file.py` (real git repos, the recorded model, `respx` for CI): `test_us_l12_*` (a dismissed finding is suppressed on the next run, listed and counted; a reworded one is not; malformed file exits 2 naming the line and sends nothing; oversized file, escaping path and symlink rejected; missing and disabled reported as `not found`/`disabled`, not `0`; **in CI** the base-branch file suppresses and the pull-request review lists it, a file added by the PR is ignored, deleting the entry in the PR does not unsuppress it, a malformed base file fails), `test_us_l13_*` (a repeated dismissal is printed as a patch that `git apply --check` accepts, the working tree and file bytes are unchanged; nothing below the threshold or once the rule exists). Unit: `adapter/local/test_memory_file.py` (parser limits and line numbers, fingerprint stability, matcher, proposals, loader), `review/test_memory_suppression.py` (fingerprint config), `apps/local_cli/test_registry.py` (matcher wiring) |
| US-P1 | 1a, 9 | 1a: `apps/worker/test_registry_snapshot.py` (step names/order/enablement/timeouts snapshot, plus a test pinning the constructor arguments derived from `ReviewPipelineConfig` with a distinct value per field) and the worker tests `apps/worker/test_main.py` and `apps/worker/test_walkthrough_wiring.py`, which this change does not edit. 9: the existing suite passes against the finished tree, but it is **not unmodified** (see "What changed in existing tests"); `pyproject.toml` gains `[project.scripts]`, `[build-system]`, setuptools package discovery and one schema `package-data` entry, and `[tool.uv] package = false` keeps the build backend out of the server images. The Compose E2E (`tests/e2e/test_review_pipeline.py` and siblings, `.github/workflows/e2e.yml`) was **not** run at a 1a merge (1a was never a separate merge here). In the final run it gave 42 passed and 1 failed (`tests/e2e/test_observability.py`, dashboard panels); the same failure reproduces on a pristine `git archive HEAD` copy, so it is pre-existing, in a file this working tree does not touch, and its cause is not diagnosed. The owner must fix or waive it, and re-run the Compose E2E on the 1a commit |
| US-P2 | 9 | `apps/test_parity_local_server.py` (`test_us_p2_*`: the real Worker `_build_review_registry` and the CLI's `build_registry`, same step enablement, same fixture diff and recorded model, identical file, line, severity and message). The server side runs over a real migrated pgvector Postgres (testcontainers) and the local side reads a real temporary git repo through `LocalGitDiffFetcher`; findings are asserted non-empty. Delivery-specific state (provenance, memory rows) is out of the comparison |

## Security review

New inputs and trust boundaries, once, across the plan.
- **CI trigger model (B5a).** `pull_request` only; `pull_request_target` is forbidden
  because it would give fork PRs secrets while reading attacker-controlled files.
  Fork PRs cannot comment (findings go to the log, exit non-zero). Permissions are
  declared at workflow level, least-privilege.
- **Repository contents, diffs, the repo's review config, memory file and MCP
  nominations are attacker-influenced** (a PR author controls them in CI). They are
  parsed with pydantic at the boundary, never executed. In CI, memory and nominations
  are read from the **base branch** (B5d). `--output` is a flag only, never repo
  config, so a PR cannot choose a write path.
- **Stdio MCP servers run commands.** Nominations carry names only (schema rejects
  `command`, `args`, `env`, `url`); commands come only from the developer's own
  config; CI refuses stdio unless the workflow itself defines it and reads no
  repo-local MCP config; children get a scrubbed environment; caps on calls and bytes;
  kill-and-reap.
- **MCP responses and the diff are untrusted data**: framed as inert, size-capped,
  causing no further tool calls or commands (US-L11). Residual risk, stated plainly:
  framing is a mitigation, not a control, and a hostile response or diff can still
  steer the model's output; posted findings are therefore markdown-sanitised and
  length-capped (no links, images or `@mentions`).
- **The Claude CLI is an agent whose prompt is attacker-influenced.** Tools disabled,
  empty temp working directory, allow-listed environment, prompt on stdin, output
  validated (Phase 3b). The CI-detection check is best-effort, not a control.
- **Secrets.** Input redaction is new (Phase 4): one batched gitleaks run, caps
  enforced before the subprocess, deny-listed paths, covers everything sent including
  MCP responses and the CLI prompt, fails closed, no disable switch. Coverage is the
  Gitleaks ruleset only. Credentials are held in memory and never appear in output,
  logs, JSON, the memory file, `--dry-run` output or exceptions, enforced by canary
  tests (planted token in env and MCP config) in Phases 5 and 7. The stderr summary
  contains no code content or queries, since CI logs of public repositories are
  public.
- **Resource exhaustion.** Diff bytes capped by `llm_max_diff_bytes`, file count
  capped, every external call has a timeout, MCP bytes/calls/processes capped,
  gitleaks invoked once per run.
- **Self-hosted runners and PII.** Code and any PII in a diff go to the model
  endpoint and nominated MCP servers (the developer's choice, visible in dry-run).
  Persistent runners must not retain scratch files: temp directories are mode 0700
  and removed in `finally`, including gitleaks scratch and report files.
- **Subscription login in CI** is refused by default (US-L8) so a personal session
  never lands on a shared runner.

## Dependency failure by outcome

The PRD allows degradation only for analyzers and MCP servers; everything else fails
visibly with a non-zero exit.

| Dependency | Failure or slowness | Outcome |
| --- | --- | --- |
| Model endpoint | unreachable / timeout | runtime-failure exit within `llm_timeout_seconds`, endpoint named, no findings presented as complete |
| Claude CLI | missing / logged out / hung | runtime-failure exit, backend named; hung process killed and reaped |
| Redaction | any failure | runtime-failure exit, "redaction: failed, nothing sent" |
| Analyzer | not installed / crashed | review continues; summary says skipped vs errored |
| MCP server | timeout / error / malformed / empty | review continues without that context; summary names the server and the way it failed |
| GitHub (CI) | no permission / 429 / 5xx / fork read-only | non-zero exit, findings in the job log, failure counted in the summary |
| Memory file | malformed | non-zero exit naming the line |
| Memory file | missing / disabled | review continues; summary says so |

## Compatibility & rollout

- Additive. Server images, settings and database schema are unchanged. Shared-code
  changes are exactly: the registry extraction (Phase 1a, own PR, snapshot-tested,
  worker wrapper kept), the Protocol widening of `adapter/github` token-provider
  parameters (Phase 5, existing class and signature untouched), and, if ADR-0016
  chooses option (i) for B3, the fingerprint-keyed memory boundary (Phase 8, covered
  by US-P1). Each is a separate PR with the worker suite and the Compose E2E green.
- `LocalSettings` uses its own prefix and adds nothing to `WorkerSettings` or
  `StepEnablement`, so `.env.example` and Helm are unchanged.
- The findings JSON schema carries `schema_version`; changes are additive within a
  major, a breaking change is a new major, and a snapshot test guards drift.
- Dependency behaviour is the "Dependency failure by outcome" table above.
- Rollback: Phase 1a by reverting one PR; later phases by not invoking or disabling
  the feature; a published release by yanking or pinning. The only artefact left in
  a repository is a memory file the developer chose to commit and any PR comment
  already posted.

## Delivery status and splitting 1a

**1a was not a separate PR in this working tree.** Everything (1a, 1b and Phases 2 to 9) sits
uncommitted in one tree, so the plan's "1a first, merged and released" and "Compose E2E at the 1a
merge" were not carried out. To honour them at commit time, commit 1a alone first, on a branch
cut from `main`, then run the worker suite and the Compose E2E on that commit before building on it.

**Files that belong to 1a** (the registry extraction and nothing else):
- `review/registry.py` (new: `build_review_registry`)
- `review/config.py` (only the new `ReviewPipelineConfig`; the rest of the diff there, if any,
  belongs to later phases)
- `apps/worker/main.py` (the thin `_build_review_registry` wrapper that maps `WorkerSettings` into
  `ReviewPipelineConfig` and delegates)
- `apps/worker/test_registry_snapshot.py` (new)
- `review/test_step_boundaries.py` (lists `registry` as a pipeline-assembly module)
- `review/clone_detection.py`, `review/verification.py`, `review/memory_suppression.py` carry the
  `SuppressionConfig` type widening (Phase 8), not 1a, and must not ride along. If 1a
  needs them to type-check, split `memory_suppression.py` so 1a keeps only the parts it needs.
- `findings/test_merge.py` and `review/test_static_analysis_step.py` are small typing fixes
  (a parameter type and an `int(...)`); commit them with 1a or on their own, not with a feature.

Everything else is 1b or later: `apps/local_cli/**`, `adapter/local/**`, `adapter/github/*`
(Phase 5 token-provider widening, `ci_review.py`, `token_provider.py`), `adapter/analyzers/gitleaks.py`
(Phase 4 batched finder), `adapter/llm/conftest.py` and `adapter/llm/litellm_client.py` (request
capture), `findings/markdown.py` (A8), `pyproject.toml` (Phase 9), the docs and ADR-0016.
Phase 7 (MCP knowledge) adds only new files outside `adapter/local` and `apps/local_cli`:
`adapter/product_knowledge/mcp_tools.py`, its test `test_mcp_tools.py`, and the test tooling
`adapter/product_knowledge/testing/local_mcp_stub.py` and `local_mcp_harness.py`. It edits no
existing server-mode file, so it does not need to ride with 1a and must not.

## What changed in existing tests

The US-P1 wording "the existing suite is unmodified" is false. Pre-existing test files and
fixtures this work edited (each edit is additive or a type fix; every test in them passes):
- `adapter/analyzers/test_gitleaks.py`: new tests for the batched secret-line finder.
- `adapter/llm/conftest.py`: the stub server now records requests (the request-capture fixture); the
  previous `stub_llm_server` / `start(respond)` signature is kept.
- `review/test_memory_suppression.py`: new tests for the fingerprint suppression config.
- `findings/test_merge.py`: a parameter annotation (`str` to `Source`).
- `review/test_static_analysis_step.py`: `int(point.value)` for the type checker.
- `review/test_step_boundaries.py`: `registry` added to the pipeline-assembly allow-list.

Caveat for the split: `review/registry.py` as it stands also takes a `fingerprint_matcher` and
builds a `FingerprintSuppressionConfig` (Phase 8). To commit 1a truly alone, drop that parameter and
branch from 1a's copy of the file, then add it back with Phase 8's `memory_suppression.py` change.
