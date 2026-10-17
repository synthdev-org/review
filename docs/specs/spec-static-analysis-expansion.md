# Phase 9 — Static-Analysis Engine Expansion PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-10-16

Sequences [`docs/roadmap.md`](../roadmap.md)'s Phase 9 ("Expanded
static-analysis engine coverage") into a concrete spec. Extends the
static-analysis macro step specified by
[`spec-review-pipeline.md`](spec-review-pipeline.md) (US4, US9) and built on
[`spec-pipeline-step-framework.md`](spec-pipeline-step-framework.md)'s
inner-registry/`ISOLATE` model; this document specifies *which* new engines
join that step and the behaviors that make them safe and quiet enough to
ship, not a re-architecture of it. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`); requirements are stated as user stories with
`Given/When/Then` acceptance criteria per `testing.md`'s mandatory rule.

**Conventions found / fallbacks.** Spec format: `docs/specs/spec-*.md`
(structure mimicked from `spec-review-pipeline.md` and `spec-memory.md`).
Testing pyramid: `docs/rules/testing.md`. Design system: N/A (no UI
surface — see "UX & Design").

## Problem & Goal

Phase 1 shipped the static-analysis step with two engines — Gitleaks
(secrets) and Semgrep (a small vendored security ruleset,
`adapter/analyzers/semgrep_ruleset.yaml`) — to prove the pipeline end to end.
Neither looks for the everyday correctness bugs a language's own linter
finds (an unused/undefined name in Python, a `no-undef` in TypeScript, an
unchecked error in Go, a public S3 bucket in Terraform, a Dockerfile running
as root). The research this product derives from
([`research-current-art.md`](../research/research-current-art.md) §2.2)
shows commercial tools bundle 15–50+ deterministic engines for exactly this
reason, run them *before* any LLM, and treat them as the free,
zero-variance trust anchor LLM findings get checked against (design goal 4).

**End user: the PR author** (and their reviewers). Today, a PR that
introduces a Python `NameError`, a Go ignored error, or a Terraform
misconfiguration gets that caught only if the LLM happens to notice it —
non-deterministically, at inference cost. The author wants the same
deterministic checks their language ecosystem already trusts, posted on the
changed lines, honoring the rules *their repo* already configured, without
buying or configuring anything.

**Secondary end user: the platform operator**, who needs every new engine
to be pinned, offline, individually switchable, time-boxed and
resource-bounded — a self-hosted Worker running third-party tools over
attacker-influenced content must not become a new way to exfiltrate
credentials, exhaust the node or surprise the customer with a flood of
comments.

**Goal / success metric:** capability-based, matching Phase 1's style. A PR
touching Python, JS/TS, Go, PHP or Terraform/Dockerfile/Kubernetes-manifest
files gets deterministic findings from the matching engine(s) on its
changed lines, using the repo's own tool configuration where it supplies a
safe one; no engine runs for a language the PR didn't touch; one failing,
slow or noisy engine never degrades the review; and the Worker never runs
more analyzer subprocesses at once than its configured bound. Noise is an
explicit goal, not a side concern: the research's most consistent finding is
that false-positive volume is the dominant reason developers mute review
tools, so the default behavior is *fewer, correctness-class findings on
lines the author changed*, not maximum coverage.

## Scope

### In scope

- Five new `StaticAnalyzer` adapters behind the existing static-analysis step
  (`adapter/analyzers/`), each normalizing into the existing
  `findings.models.Finding` schema:
  - **Ruff** — Python lint.
  - **ESLint** — JavaScript/TypeScript lint.
  - **golangci-lint** — Go lint.
  - **PHPStan** — PHP static analysis, run at a low level with no autoloading
    of project dependencies.
  - **Trivy (config scanning only)** — IaC misconfiguration: Terraform,
    Dockerfile, plain Kubernetes YAML manifests.
- Language/file-type detection that scopes which engines run, and which
  files each receives (US-A1).
- Changed-line scoping of the new lint-class engines' findings (US-A3).
- Repo-native tool configuration honored **as data** (US-A4), with a
  pinned, correctness-only default profile when none is usable (US-A5).
- Per-analyzer `*_enabled` and timeout settings, extending the convention
  `apps/worker/settings.py` already uses (US-A7).
- A Worker-process-wide bound on simultaneous analyzer subprocesses (US-A8).
- Deployment-pinned, checksum-verified, offline-capable tool packaging
  (US-A9).
- Helm/Compose wiring, resource defaults, operator runbook entry and
  observability for the above.

### Out of scope

- **Customer-configurable tool versions, custom images or custom rule
  packs.** Decided: tools are deployment-pinned. A customer-selectable
  binary is an arbitrary-code-execution surface and would need its own ADR.
- **Executing repo-supplied code as part of analysis** — JavaScript ESLint
  configs/plugins, PHPStan bootstrap/extension files, Psalm plugins,
  custom Trivy Rego checks, golangci-lint plugins. These are never run
  (US-A4); the pinned default is used instead.
- **Dependency resolution / network access during a scan** (`npm ci`,
  `go mod download`, `composer install`, Terraform module download, Trivy
  database/check updates). Engines run on the changed files plus
  allow-listed config files only.
- **Additional languages/tools** (Java/PMD, Ruby/RuboCop, shell/ShellCheck,
  CloudFormation, Helm chart rendering, Checkov, Bandit). Each is a further
  additive adapter once this phase's framework exists.
- **Whole-repo scans, duplication/clone detection** — Phase 10.
- **Auto-fix of lint findings** — Phase 11.
- **Re-scoping Gitleaks/Semgrep** to changed-line filtering or language
  scoping — their behavior is unchanged by this phase.
- **Admin UI controls** for per-analyzer toggles (bootstrap/Helm config
  only, same as every existing `*_enabled` flag).
- **Persisting or learning from lint findings differently** — Memory
  (Phase 4) treats them like any other dismissed finding, unmodified.

### Priority

Ordered by end-user impact and by dependency, then by the order the product
owner listed the engines (no customer language-mix data exists in this repo
to rank them otherwise — an assumption, see "Resolved decisions").

| Priority | Unit | Why |
| --- | --- | --- |
| Must | A — Framework generalization | Every engine depends on it; zero end-user value alone. |
| Must | B — Repo-config discovery | Without it, "honor the repo's rules" (US-A4) is untrue and noise is uncontrolled. |
| Must | C — Ruff | Smallest engine; first proof of A + B. |
| Should | D — ESLint | Highest-reach language; highest packaging risk. |
| Should | E — golangci-lint | |
| Should | F — Trivy config | |
| Should | G — PHPStan | |
| Must | H — Packaging, ops, observability | Ships with each engine; nothing is releasable without it. |

If time forces a cut, cut from the bottom of the Should rows: each engine
is independently shippable and absent engines cost the end user nothing
they have today.

## Requirements

**US-A1 — Only engines relevant to the PR's languages run**
As a PR author, I want only the analyzers for the languages I actually
changed to run, so that my review isn't slowed or cluttered by irrelevant
tools.
- Given a PR whose changed files are all `.py`, When static analysis runs,
  Then Ruff runs and ESLint, golangci-lint, PHPStan and Trivy are not
  invoked.
- Given a PR changing both `.go` and `.tf` files, When static analysis runs,
  Then golangci-lint receives only the Go files and Trivy only the IaC
  files, and no other new analyzer is invoked.
- Given a PR whose changed files match no new engine, When static analysis
  runs, Then Gitleaks and Semgrep still run exactly as before.
- Detection is by file name/extension (`.py`; `.js .jsx .ts .tsx .mjs .cjs`;
  `.go`; `.php`; `.tf .tf.json .tfvars`; `Dockerfile`/`Dockerfile.*`/
  `*.dockerfile`; `.yaml/.yml` files containing top-level `apiVersion` and
  `kind` keys). Unknown extensions never trigger an engine.

**US-A2 — New engines' findings need no downstream special-casing**
As a PR author, I want lint and IaC findings to look and behave like every
other finding, so that dedupe, severity ordering, redaction, posting and
Memory suppression all work on them.
- Given any new engine reports a finding, When it is normalized, Then it is
  a `Finding` with `file`, `line`, `severity` from the existing vocabulary,
  a `message`, `source` naming the engine and `rule_id` set to the engine's
  own rule identifier.
- Given findings from the new engines, When the findings merger and output
  steps run, Then they require no per-engine code (no branch on `source`).
- Given an engine reports a severity vocabulary of its own, When normalized,
  Then it maps onto the existing five-level vocabulary by a documented
  per-engine table, with an unmapped value defaulting to `medium` — never
  raising.

**US-A3 — Lint findings are limited to lines I changed**
As a PR author, I want lint-class findings only on lines my PR added or
modified, so that I'm not blamed for the file's pre-existing issues.
- Given a changed Python file with a pre-existing unused import on an
  untouched line and a new undefined name on an added line, When Ruff runs,
  Then only the undefined-name finding is posted.
- Given a finding whose reported line falls in no added/modified hunk of
  the file's `patch`, When results are normalized, Then it is dropped and
  counted in the analyzer's `filtered_count` log field.
- Given a file the diff reports without a `patch` (GitHub omits it for
  large/binary changes), When a lint-class engine reports findings for it,
  Then those findings are dropped, not posted file-wide.
- Applies to Ruff, ESLint, golangci-lint and PHPStan. It does not apply to
  Trivy (a misconfiguration is a property of the resource/file, not a line)
  — a Trivy finding is kept when the line range Trivy reports for the
  misconfigured resource (its start and end line) overlaps at least one
  added/modified line in the file's `patch`, otherwise it is dropped by the
  same rule and counted in `filtered_count`. Gitleaks/Semgrep are unchanged.

**US-A4 — My repo's own tool configuration is honored — as data only**
As a PR author, I want the analyzers to follow the lint rules my team
already configured, so that review feedback matches what our own CI and
editor tell us.
- Given a repo with `pyproject.toml` containing `[tool.ruff]` settings,
  When Ruff runs, Then it uses those settings.
- Given a repo with `.golangci.yml`, `.eslintrc.json` (or `.eslintrc.yaml`/
  `package.json` `eslintConfig`), `ruff.toml`, `phpstan.neon` or `trivy.yaml`,
  When the matching engine runs, Then that file — fetched from the PR head
  commit — governs which rules apply.
- Given a repo whose config is executable code (`eslint.config.js`,
  `.eslintrc.js/.cjs`, a `phpstan.neon` that sets `bootstrapFiles`,
  `scanFiles`/`scanDirectories` or loads extensions, golangci-lint custom
  plugins, Trivy custom Rego check paths), When the engine would run, Then
  the config is **not** honored, the pinned default profile is used, and the
  skip is logged with the reason and the config path.
- Given a config that references a plugin, shareable config or `extends`
  target not present in the pinned toolset or not in the fetched allow-list,
  When the engine would run, Then it falls back to the pinned default
  profile and logs the unresolved reference — it never fails the review.
- Given a config the tool itself rejects as invalid, When the engine runs,
  Then it is re-run once with the pinned default profile; only if that also
  fails is the engine treated as failed (US-A6).
- Config files are fetched from a fixed allow-list of file names (per
  engine), never from a path the PR or the config itself names, and are
  size-capped; they are written to the scan directory as inert files.

**US-A5 — With no usable config, defaults are correctness-only and quiet**
As a PR author, I want a repo with no lint config to get only
likely-real-bug findings, not a wall of style nits.
- Given a repo with no config for an engine, When the engine runs, Then it
  uses the pinned default profile, which enables only correctness/security
  rule classes (e.g. undefined/unused-name and syntax errors for Ruff,
  `eslint:recommended`-class rules for ESLint, `govet`/`staticcheck`/
  `errcheck`-class linters for Go, PHPStan level 0–1, Trivy HIGH/CRITICAL
  misconfigurations) and no formatting, naming or documentation rules.
- Given PHP files and no `vendor/` directory, When PHPStan runs, Then no
  finding whose cause is an unresolved class, function or constant (i.e. a
  missing dependency rather than a bug in the changed code) is posted.
- Given Go or TypeScript files that import third-party modules which are
  not materialized, When the engine runs, Then no finding whose cause is an
  unresolved import is posted.
- The exact default profile per engine is a committed, checksummed asset
  (like `semgrep_ruleset.yaml.sha256`), changed only by a reviewed commit.
  It is enumerated in the adapter's plan phase and verified by a
  per-engine fixture: a file seeded with one style-only violation and one
  correctness violation yields exactly the correctness finding and zero
  style findings.
- Noise bound: on each engine's committed "clean-code" fixture (idiomatic
  code with no seeded defect) the default profile yields zero findings.

**US-A6 — "Findings found" is success; a broken engine is isolated**
As a PR author, I want a crashing or hanging analyzer to never block my
review, and a run that merely *found problems* to never be mistaken for a
crash.
- Given an engine that exits non-zero because it reported findings (Ruff,
  ESLint, golangci-lint, PHPStan and Trivy all do by default), When it runs,
  Then its findings are returned as a success; each adapter invokes its
  tool in a mode (or maps its exit codes) so that only genuine failures
  surface as `AnalyzerInvocationError`.
- Given a new engine that crashes, times out or emits unparseable output,
  When static analysis runs, Then it is excluded, logged at `error` with its
  `analyzer_name`, and the review completes with the other engines'
  findings (US9 of `spec-review-pipeline.md`, unchanged).
- Given all enabled engines for a PR fail, When static analysis finishes,
  Then the step still succeeds with zero static findings (existing
  behavior) and the failures are visible in logs and metrics.

**US-A7 — Operators can switch each engine off and time-box it**
As a platform operator, I want per-engine enable flags and timeouts, so that
I can disable a misbehaving or unwanted engine without a redeploy of code.
- Given `static_analysis_<engine>_enabled=false` for any new engine
  (`ruff`, `eslint`, `golangci_lint`, `phpstan`, `trivy`), When a PR would
  trigger that engine, Then it is never invoked.
- Given `static_analysis_<engine>_timeout_seconds` is set, When that engine
  runs, Then its subprocess is killed and reaped at that limit; when unset
  it inherits `static_analysis_analyzer_timeout_seconds`.
- Given a configuration where `static_analysis_enabled` is true but every
  analyzer is disabled, When the Worker starts, Then the existing
  fail-fast "can produce value" startup check (`review/config.py`) treats
  the step as valueless exactly as it does today, extended to cover the new
  engines.
- Given a per-analyzer timeout that cannot fit within
  `static_analysis_step_timeout_seconds` given the concurrency bound
  (US-A8), When the Worker starts, Then it fails fast with a clear message,
  mirroring the existing step-timeout consistency check.
- Settings mirror one-for-one into the Helm `worker` block, as every
  existing Worker setting does.

**US-A8 — The Worker never runs more analyzer subprocesses than configured**
As a platform operator, I want a hard cap on simultaneous analyzer
subprocesses per Worker process, so that a PR touching five languages
cannot exhaust the node.
- Given `static_analysis_max_concurrent_analyzers=N` (default 3), When more
  than N enabled analyzers are due for one job — or across concurrently
  processed jobs in the same Worker process — Then at most N analyzer
  subprocesses are alive at any instant; the rest wait.
- Given an analyzer waiting for a slot, When its turn comes, Then its own
  timeout starts at acquisition (waiting does not consume it), while the
  step-level timeout still bounds the whole step.
- Given the cap is exceeded by demand, When analyzers wait, Then the wait
  duration is recorded per analyzer (see Observability).
- Gitleaks and Semgrep participate in the same bound; the cap is a Worker
  process property, not per-engine.
- Containers: the Helm `worker.resources` memory limit must exceed the sum
  of the N largest measured per-engine peak RSS values (recorded in the
  plan from fixture runs) plus the Worker's own baseline, and the
  operations runbook documents that arithmetic so operators can re-derive
  it if they raise N.

**US-A9 — Every tool is pinned, verified and offline**
As a platform operator, I want each analyzer binary and its default assets
pinned by version and checksum and unable to reach the network at scan
time, so that a scan can neither pull unreviewed code nor leak data.
- Given the image build, When any analyzer binary or bundled asset's
  checksum does not match its committed pin, Then the build fails (a
  `sha256sum -c` step in the `Dockerfile`, as the embedding model already
  does, exercised by the CI image-build job — not a pytest case).
- Given an analyzer running, Then it has no network need: update checks,
  telemetry, rule/DB/check-bundle downloads, plugin installs and
  remote-module resolution are disabled by flag or configuration, and a
  scan succeeds in a container with no outbound network.
- Given a repo-supplied file name beginning with `-`, When any new
  analyzer is invoked, Then it is treated as a positional path (end-of-
  options separator or equivalent), as Semgrep already does.
- Given a new analyzer subprocess, Then it runs as the existing non-root
  `worker` user with the existing credential-stripped environment
  (`analyzer_env`) and in a per-invocation temp directory removed in a
  `finally`.

**US-A10 — Security findings aren't crowded out by lint findings**
As a PR author, I want a secret or SAST finding to survive the combined
finding ceiling even when lint produces dozens of findings.
- Given more combined findings than `REDACTION_FINDING_CEILING`, When the
  ceiling is applied, Then findings are kept highest-severity first, and at
  equal severity those from Gitleaks, Semgrep and Trivy precede lint-class
  findings; dropped counts keep being logged.

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Latency:** each analyzer is bounded by its timeout; with the default
  concurrency of 3, a PR touching all five new languages plus Gitleaks and
  Semgrep must finish static analysis inside
  `static_analysis_step_timeout_seconds` (240 s today) on the fixture set
  of the E2E suite, and a single-language PR adds no more than 30 s to
  today's static-analysis duration (budgets to be re-confirmed by
  measurement in the plan; a measured miss changes the budget by a
  recorded decision, not silently).
- **Image size:** the combined growth of all new engines is recorded per
  engine and capped at 860 MB total, measured as `docker image inspect`
  size growth of the Worker image over the pre-Phase-9 `runtime` baseline
  (1,432.1 MB). Owner decision 2026-10-17: the cap is the measured growth
  (760 MB) plus 100 MB headroom, replacing the original 600 MB placeholder.
  Layer-sum is reported alongside but is not the gated figure.
- **Output cap:** the existing 10 MB per-invocation output cap
  (`run_analyzer_subprocess`) applies to every new engine unchanged.
- **Security — executing repo content.** The dominant new risk: linters can
  load and run repo-supplied code (JS configs/plugins, PHP bootstrap files,
  Go/PHP plugins). US-A4 forbids it by construction; the plan must include a
  test per engine that a config of each executable form is *not* executed
  (canary file not created). The Worker holds database and GitHub App
  credentials, so this is the highest-severity item in the phase.
- **Security — resource exhaustion:** PR-controlled file counts/sizes are
  already bounded by the pre-filter and blob-size ceiling; the concurrency
  bound (US-A8), per-analyzer timeouts and output cap bound the rest.
- **Reliability:** US-A6's isolation; a flaky engine can be disabled with one
  setting (US-A7).
- **Scale:** worst realistic load is five new engines on one PR; concurrency
  bound keeps peak at N subprocesses per Worker process. Horizontal scaling
  is by `worker.replicaCount`, unchanged.

### Privacy / Compliance

No new data leaves the deployment: engines are offline (US-A9) and findings
flow through the existing redaction (`TextRedactor`) before they reach the
LLM or GitHub. Lint messages can quote source lines; the existing
all-findings redaction pass covers them uniformly. No new PII stored.

### Observability

Extends the existing analyzer logging and `spec-observability.md` catalog
rather than inventing parallel names:
- Logs: existing per-analyzer `analyzer_name`, `status`, `finding_count`;
  new `filtered_count` (US-A3), `config_source`
  (`repo|default`) and `config_skip_reason` (US-A4).
- Metrics: per-analyzer run count/outcome/duration, analyzer slot wait
  duration, in-flight analyzer gauge, config-fallback counter.
- Dashboard: Grafana panels for the above. Note for the plan: the Phase 7
  `test_observability` E2E check failed because six new panels got no data
  from the test's traffic — any panel added here must be exercised by E2E
  traffic or the check must be updated in the same change.

### Rollout & Release Safety

- No schema migration is expected: `Finding.source` is a `Literal` widened in
  code. The plan must confirm no persisted column or consumer constrains the
  set of `source` values (none found in `adapter/storage/models.py` during
  drafting).
- Each engine ships independently behind its own `*_enabled` flag, so an
  engine can be released, disabled or rolled back with configuration only.
- Default for new engines: **enabled** (assumption pending validation — see
  below); operators wanting a quiet upgrade set the flags false first.
- Additive: Gitleaks/Semgrep output and behavior are byte-for-byte unchanged.
- Image growth: each engine adds to the single shared image (Webhook API,
  Worker and Admin API share one `Dockerfile`); sizes are recorded per
  engine in the plan so a regression is visible. ESLint additionally adds a
  Node runtime to the runtime image (risk below).

## UX & Design

N/A — no new UI surface. Findings render through the existing
inline-comment/check-run output; the comment shows the engine in `source`
and the engine's `rule_id`. Per-analyzer settings are bootstrap
configuration (Helm values/env), not Admin UI screens (out of scope).

## Stakeholders & Sign-off / Acceptance Criteria

- Owner/sign-off: Andy Beak (sole author/approver per the ADRs' RACI).
- Phase acceptance (each unit has its own, below): a PR containing a
  deliberate defect in each supported language, run through the full Compose
  stack with a stubbed GitHub, posts exactly the expected inline findings
  from the matching engine(s), none from the others; with all new engines
  disabled the posted output equals today's.
- Not verifiable in this repo (carried as in Phase 1's status): behavior on a
  real GitHub repo with real-world configs. The plan lists a manual
  verification step against a real repo.

## Phases / Units

Each unit lists dependency, risk and acceptance. Units C–G are independent
of each other once A and B exist.

- **Unit A — Framework generalization.** Depends on Phase 1/3. Widen
  `Source`; replace `StaticAnalysisStep`'s two named analyzer constructor
  arguments and the hard-coded `gitleaks`/`semgrep` lists in
  `review/config.py` with a registry-driven list of analyzers; language
  detection (US-A1); changed-line filter (US-A3); concurrency semaphore
  (US-A8); ceiling ordering (US-A10); extended can-produce-value check and
  settings plumbing (US-A7). *Risk:* touches the Phase 1 step's constructor
  and its tests (moderate). *Decision:* all behavior above is decided.
  *Acceptance:* US-A1, US-A3 (via a fake analyzer), US-A7, US-A8, US-A10
  pass with no real new engine installed; Gitleaks/Semgrep suites unchanged
  and green.
- **Unit B — Repo-config discovery.** Depends on A. `fetch_content` only
  fetches paths that appear in the diff, so config files (which usually are
  not in the diff) need a bounded fetch of allow-listed names from the PR
  head commit's tree via the existing tree/blob fetching in
  `adapter/github/`. Data-only guard (US-A4). *Risk:* new GitHub fetch path
  (extra API calls per job; rate-limit and size bounds required);
  *assumption pending validation:* configs are read at the PR **head**
  commit. *Acceptance:* US-A4 pass with a fake engine.
- **Unit C — Ruff.** Depends on A, B. *Risk:* low (single binary, JSON
  output, TOML data config). *Acceptance:* US-A1–A6, A9 for Python.
- **Unit D — ESLint.** Depends on A, B. *Risk:* **high** — needs a Node
  runtime and a pinned plugin set in the runtime image (image size and
  attack surface), and real-world configs frequently depend on plugins the
  pinned set lacks (US-A4 fallback makes that safe but reduces fidelity).
  *If the Node runtime is judged unacceptable at plan time, the fallback is
  a single-binary JS/TS linter behind the same unit — a product call to
  make then, not silently.* *Acceptance:* US-A1–A6, A9 for JS/TS.
- **Unit E — golangci-lint.** Depends on A, B. *Risk:* medium — type-aware
  linters need module dependencies; the default profile must exclude
  linters that depend on them and US-A5's unresolved-import suppression
  must hold. *Acceptance:* US-A1–A6, A9 for Go.
- **Unit F — Trivy config.** Depends on A, B. *Risk:* medium — must run with
  its check bundle embedded and every database/update/remote-module
  fetch disabled; Helm charts and CloudFormation out of scope.
  *Acceptance:* US-A1–A6, A9 for IaC.
- **Unit G — PHPStan.** Depends on A, B. *Risk:* medium — PHPStan is a
  PHP program, so the runtime image needs a pinned PHP CLI (like ESLint's
  Node runtime: image size and attack surface; a product call at plan time
  if unacceptable); low-level, no autoload, so noise control (US-A5) is the
  acceptance crux.
  *Acceptance:* US-A1–A6, A9 for PHP.
- **Unit H — Packaging, ops, observability.** Delivered incrementally with
  each engine: Dockerfile stages/pins/checksums, Compose and Helm settings
  mirror, `worker.resources` defaults, `docs/reference/operations.md`
  entry, metrics and dashboard panels, E2E.

## Testing / Key Scenarios

Per `testing.md`: every story maps to at least one integration test; pure
no-I/O logic (path classification, hunk-line parsing, severity mapping,
config allow-list/executable-config detection) is Unit-tested per
[ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md). Adapter
integration tests run the **real pinned binaries** on fixtures
(`adapter/analyzers/testdata`), as Gitleaks/Semgrep do.

| Story | Given/When/Then (summary) | Test path | Level |
| --- | --- | --- | --- |
| US-A1 | Only-`.py` PR runs Ruff only; mixed Go+IaC PR routes files correctly; no match leaves Gitleaks/Semgrep unchanged | `review/test_static_analysis_step.py` (extended) | Integration (real analyzers on fixtures) |
| US-A1 | Extension/name/`apiVersion`+`kind` classification | `review/test_file_matchers.py` (new) | Unit |
| US-A2 | Each engine's fixture output normalizes to `Finding` with mapped severity; unmapped severity → `medium` | `adapter/analyzers/test_<engine>.py` (new) | Integration |
| US-A2 | Merger/output need no per-source branch | `findings/test_merge.py`, `findings/test_models.py` (extended) | Unit |
| US-A3 | Pre-existing issue dropped, added-line issue kept; no-`patch` file drops findings; hunk parsing | `review/test_changed_lines.py` (new, pure) + `review/test_static_analysis_step.py` | Unit + Integration |
| US-A4 | Data configs honored per engine; executable configs ignored with logged reason **and canary not executed**; unknown plugin → default; invalid config → one default re-run | `adapter/analyzers/test_<engine>.py`, `adapter/github/test_repo_config.py` (new, `respx` cassette) | Integration |
| US-A4 | Allow-list, size cap, executable-form detection | `adapter/github/test_repo_config_unit.py` (new, fake tree fetcher; executable-config detection is unit-tested per engine in `adapter/analyzers/test_<engine>.py`) | Unit |
| US-A5 | No-config repo yields only correctness-class findings; PHP/Go/TS unresolved-symbol findings suppressed | `adapter/analyzers/test_<engine>.py` | Integration |
| US-A6 | Exit-with-findings is success; crash/timeout/garbage isolated; all-fail still completes | `adapter/analyzers/test_<engine>.py`, `review/test_review_job.py` (extended) | Integration |
| US-A7 | Disabled engine never invoked; timeout inheritance/override; startup checks | `apps/worker/test_settings.py`, `review/test_config.py` (extended) | Unit + Integration (Worker boot) |
| US-A8 | With N=2 and 5 due analyzers, never >2 live subprocesses; wait excluded from analyzer timeout | `adapter/analyzers/test_analyzer_concurrency.py` (new) | Integration (real subprocesses) |
| US-A9 | Scan succeeds with no network; `-`-prefixed filename safe | `adapter/analyzers/test_<engine>.py`, `tests/e2e/test_static_analysis_expansion.py` (new) | Integration + E2E |
| US-A9 | Checksum mismatch fails the image build | CI image-build job (`Dockerfile` `sha256sum -c` stage) | CI build check |
| US-A10 | 60 findings mixed severity/source: security and high severity survive the ceiling | `review/test_static_analysis_step.py` (ceiling tests), `findings/test_merge.py` | Unit |
| All (happy path) | Compose stack, stubbed GitHub, PR with one defect per language posts the expected comments and no others; all engines disabled reproduces today's output | `tests/e2e/test_static_analysis_expansion.py` | E2E |

## Resolved decisions (formerly open assumptions)

**Decided (by the product owner, 2026-10-16 or by existing docs):**
- Engine set: Ruff, ESLint, golangci-lint, PHPStan, Trivy (IaC only);
  Checkov excluded (Trivy only, to avoid a second locked Python environment
  and duplicate findings).
- Tools are deployment-pinned and checksum-verified; repo-native configs are
  honored as data; per-analyzer `*_enabled` and timeout settings follow the
  `apps/worker/settings.py` convention; selection is language-scoped.
- PHP engine is PHPStan at a low level without autoloading.
- Concurrency: a per-Worker-process semaphore (default 3) plus container
  resource limits; no new deployable component
  ([`architecture.md`](../rules/architecture.md)'s simplicity rule).

**Assumptions pending validation (called out so they are easy to
challenge):**
1. **Ship order** follows the order the product owner listed the engines;
   there is no customer language-mix data here to rank them otherwise.
2. **New engines default to enabled**, relying on minimal default profiles
   and per-engine kill switches; the alternative is default-off with
   opt-in, safer for noise but invisible to the end user.
3. **Config is read at the PR head commit**, not the base: it reflects what
   the repo will look like after merge, at the cost that a PR can loosen its
   own lint rules in the same PR (the loosening is visible in the diff;
   Gitleaks/Semgrep are unaffected).
4. **Changed-line filtering is applied to lint engines only**, leaving
   Gitleaks/Semgrep unchanged, to keep this phase additive.
5. **IaC scope** is Terraform, Dockerfile and plain Kubernetes YAML detected
   by `apiVersion`+`kind`; Helm templates and CloudFormation are deferred.
6. **Per-engine timeouts** inherit the shared value unless overridden; the
   concrete default for slower engines (golangci-lint, ESLint) is set from
   measurement during build.
