# Plan: Phase 9 — Static-analysis engine expansion

**Status: Proposed.** Sequences
[`docs/specs/spec-static-analysis-expansion.md`](../specs/spec-static-analysis-expansion.md)
— [`docs/roadmap.md`](../roadmap.md)'s Phase 9 — into buildable,
independently-testable phases, following the phase / tests / observability /
acceptance shape this repo's earlier plans used (recoverable with
`git show HEAD:docs/plans/plan-deliver-memory.md`; shipped plans are removed
from the working tree per the roadmap's "sequencing, not a Kanban board"
convention). Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`).

The spec was drafted and self-reviewed against `prd-review` (one full
review pass; its one Weak dimension, requirement testability, was closed
with real content). This plan does not re-litigate its product calls — the
engine set, deployment-pinned tools, repo configs honored as data only,
Trivy-only IaC, PHPStan without autoload, and the per-process concurrency
bound are decided there. It sequences the work and records the
implementation-shape choices the spec leaves open as **Assumptions** below.

This is additive work on running code, not greenfield. Every phase names the
existing module it extends; each reference below was checked against the
file during drafting: `review/static_analysis.py` (`StaticAnalysisStep`,
`StaticAnalyzer`, `_AnalyzerSubStep`, `REDACTION_FINDING_CEILING`),
`review/config.py` (`StepEnablement`, `StepTimeouts`, the
`static_analysis_*` can-produce-value predicate), `apps/worker/settings.py`,
`apps/worker/main.py` (analyzer wiring), `findings/models.py` (`Source`,
`Severity`, `Finding`), `findings/redact.py` (`apply_redaction_ceiling`),
`ingestion/diff.py` (`DiffFile.patch`, `DiffFetcher`),
`adapter/analyzers/subprocess_runner.py` (`materialize_into_temp_directory`,
`run_analyzer_subprocess`, `analyzer_env`, `original_path_mapping`),
`adapter/analyzers/gitleaks.py` / `semgrep.py` (the adapter pattern to copy),
`adapter/github/tree.py` (`GitHubTreeFetcher.fetch_tree` / `fetch_content`),
`Dockerfile` (the `gitleaks` and `semgrep-tool` stages, the `worker` user),
`charts/review/values.yaml` + `templates/deployment-worker.yaml`,
`docker-compose.yml`, `docs/reference/operations.md`, `observability/grafana`.

## Scope

**In scope** — the spec's Units A–H in full: framework generalization, repo
config discovery, and the five engines (Ruff, ESLint, golangci-lint, Trivy
config, PHPStan), with packaging, settings, runbook, observability and E2E
shipping with each.

**Out of scope** — the spec's out-of-scope list applies unchanged:
customer-configurable tool versions or rule packs, running any repo-supplied
code, dependency resolution or network access during a scan, further
languages/tools (Java, Ruby, shell, CloudFormation, Helm rendering, Checkov,
Bandit), whole-repo scans and clone detection (roadmap Phase 10), auto-fix
(Phase 11), retrofitting changed-line filtering or language scoping onto
Gitleaks/Semgrep, and Admin UI controls for per-analyzer toggles.

## Assumptions this plan makes (flagged, not decided)

Sequencing/implementation-shape calls the spec does not fix. Challenge any of
these before the phase that depends on it starts.

1. **Registry-driven analyzers.** `StaticAnalysisStep.__init__` currently
   takes `gitleaks_analyzer` and `semgrep_analyzer` by name and hard-codes the
   two-entry list; Phase 1 replaces that with a sequence of small
   `AnalyzerRegistration` records (name, analyzer, file matcher,
   `changed_lines_only`, config file names), defined in
   `review/static_analysis.py`. Gitleaks and Semgrep register with an
   all-files matcher and `changed_lines_only=False`, so their behavior is
   unchanged.
2. **Filtering lives in the step, not the adapters.** Language scoping and
   changed-line filtering are pure functions applied around each analyzer in
   `_AnalyzerSubStep`, so the five adapters stay thin and the logic is
   unit-tested once (ADR-0005). Two new pure modules: `review/file_matchers.py`
   and `review/changed_lines.py` (hunk parsing of `DiffFile.patch`).
3. **The slot wait must not consume the analyzer timeout.** The inner
   `StepRegistry` wraps each sub-step in `asyncio.wait_for` using the
   analyzer's timeout, which would include time queued on the semaphore.
   *Decided (owner, after Phase 1 verification found that handing sub-steps
   the step-level budget breaks* `test_analyzer_exceeding_its_timeout_is_isolated_the_other_still_completes`
   *and the "existing suites unchanged" rule):* keep the registry-level
   per-analyzer timeout, and have each sub-step acquire its semaphore slot
   **before** the timed region (outside the registry's `wait_for`), so queue
   time is excluded. The per-subprocess `timeout` remains a second guard. No
   existing test is rewritten.
4. **One signature change to the `StaticAnalyzer` Protocol:** a keyword-only
   `config_files: Mapping[str, bytes]` argument, added in Phase 2. Gitleaks,
   Semgrep and every fake/test double accept and ignore it. Phase 2 greps every
   implementer and fake before changing it.
5. **Config fetch reuses the tree/blob fetchers.** A small
   `RepoConfigFetcher` Protocol (defined at the consumer in
   `review/static_analysis.py`) is implemented in `adapter/github/` by calling
   `GitHubTreeFetcher.fetch_tree` at the PR head commit and fetching only
   allow-listed names, then `fetch_content`. A truncated tree or any fetch error
   degrades to "no repo config" with a logged reason — never a failed job.
   *Cost note:* one extra Git Trees call per job that triggers a new engine;
   bounded and skipped when no new engine is due.
6. **Per-engine data-only classification lives in each adapter module** as a
   pure function (unit-tested), since which config forms execute code is
   tool-specific knowledge; the domain only carries the allow-list names and a
   size cap.
7. **Ceiling ordering** (US-A10) is a stable sort inside
   `StaticAnalysisStep` before it calls the unchanged
   `apply_redaction_ceiling`, rather than changing the shared helper, because
   `review/review_generation.py` also uses it.
8. **Engine ship order** follows the spec's priority table: Ruff, ESLint,
   golangci-lint, Trivy, PHPStan. Each engine phase is independently
   mergeable after Phase 2.

9. **Config location: repository root only** for the first cut (the spec
   says "from the PR head commit" without a location). Tools that cascade
   per directory (Ruff, ESLint) therefore see only the root config in a
   monorepo; nested configs are ignored and a nested-config-present case is
   logged. Also a known fidelity limit: `fetch_tree` raises on a truncated
   tree (very large repos), which degrades to default profiles. Revisit with
   a per-path contents lookup only if pilot repos hit either limit.
10. **Startup budget formula** (US-A7): `ceil(enabled_analyzers / N) ×
    max(per-analyzer timeout) ≤ static_analysis_step_timeout_seconds`, else
    fail fast. The 240 s / 60 s / N=3 defaults pass with up to nine
    analyzers; engines with larger timeouts (Phase 3–7 measurements) are
    checked against the same formula.
11. **Redactor subprocesses are outside the concurrency bound.**
    `GitleaksTextRedactor` calls run after analyzers finish and are bounded
    by `REDACTION_FINDING_CEILING`; the bound covers analyzer scans only,
    as US-A8 says.

## Per-engine checklist (applies to Phases 3–7)

Stated once; each engine phase lists only its deltas.

- Pinned binary/runtime added to the `Dockerfile` in its own stage with a
  version and `sha256sum -c` (pattern: the `gitleaks` and `embedding-model`
  stages), copied into both `dev` and `runtime` stages, runnable by the
  existing non-root `worker` user.
- Adapter `adapter/analyzers/<engine>.py` implementing `StaticAnalyzer`,
  copying the shape of `semgrep.py`: `materialize_into_temp_directory`,
  `original_path_mapping`, `run_analyzer_subprocess`, `-- ` end-of-options
  guard, JSON output parsed into `Finding` with the spec's documented severity
  table (unmapped → `medium`), `AnalyzerOutputParseError` on malformed output.
- Exit-code handling so "findings found" is success (US-A6).
- Offline flags (no update check, telemetry, plugin or module download),
  proven by running the engine's integration test inside a container with
  networking disabled (`--network none`) and in the E2E stack with the
  engine's would-be download hosts unreachable.
- Default correctness-only profile as a committed asset with a `.sha256`
  (pattern: `semgrep_ruleset.yaml` + `.sha256`) and the spec's fixture checks:
  style-only violation → no finding; clean-code fixture → zero findings.
- Config side-effect keys: the classifier also rejects (config skipped →
  default) or the invocation overrides any key that makes the tool write or
  read outside the scan directory — Ruff `cache-dir`/`extend` outside the
  fetched set, golangci-lint `output.formats[].path` and `run.*cache*`,
  ESLint cache/output options, Trivy output/cache paths — and every
  invocation passes its own cache/output location inside the per-invocation
  temp directory. Covered by a canary test (a config naming a path outside
  the scan directory must not create a file there).
- Config-honoring: the pure data-only classifier, honored vs. skipped logging
  (`config_source`, `config_skip_reason`), fallback to default on unresolved
  references, one default re-run on a tool-rejected config.
- Executable-config canary test: a config of each executable form that would
  create a marker file if run — assert the marker is **not** created.
- `Source` literal gains the engine name; settings
  `static_analysis_<engine>_enabled` / `_timeout_seconds`; Compose env and
  Helm `worker` block mirror; composition-root wiring in `apps/worker/main.py`.
- Recorded per-engine measurements for the plan's budgets: peak RSS,
  single-language duration, image-size delta.

## Phase 1 — Framework generalization (spec Unit A)

**Why first:** every engine, the config path and the concurrency bound hang
off this; it has no end-user value alone, so it ships with a fake analyzer to
prove each story end to end without any new tool installed.

- Verify Assumption 3 (registry timeout semantics) before any code change.
- Widen `findings.models.Source`; confirm no persisted column or consumer
  constrains `source` values (grep `adapter/storage/models.py` and consumers;
  none found at drafting).
- `review/file_matchers.py`: path/extension/name classification and the
  `apiVersion`+`kind` YAML check (pure).
- `review/changed_lines.py`: parse `DiffFile.patch` hunks into added/modified
  line sets (pure); no-`patch` file → empty set (US-A3).
- Refactor `StaticAnalysisStep` to the registration list (Assumption 1);
  `_AnalyzerSubStep` applies matcher → file subset (skips the analyzer when
  empty), runs the analyzer under the semaphore, then the changed-line filter
  for registrations that ask for it, logging `filtered_count`.
- Per-process `asyncio.Semaphore` sized by the new
  `static_analysis_max_concurrent_analyzers` (default 3), created in the
  composition root (`apps/worker/main.py`) and injected — no module-level
  singleton (architecture.md). Gitleaks and Semgrep acquire it too.
- `review/config.py` / `apps/worker/settings.py`: generalize the per-analyzer
  enablement and timeout mappings (new engines' flags default `True`, per spec
  assumption; timeout fields optional, inheriting
  `static_analysis_analyzer_timeout_seconds`); extend the can-produce-value
  predicate and add the fail-fast check that the timeouts fit the step budget
  at the configured concurrency.
- US-A10: stable sort (severity, then security-class sources first) before
  `apply_redaction_ceiling`.
- Telemetry via `review/_telemetry.py` (ADR-0011/0012): analyzer run
  outcome/duration, slot wait duration, in-flight gauge, config-fallback
  counter. **No dashboard panels yet** (see Phase 8).

**Tests:** *Unit* — file matchers, hunk parsing, severity-first ordering,
settings/predicate/fail-fast validation, and
`review/test_static_analysis_step.py` extended with fake analyzers (fakes at
the Protocol boundary are unit tests under `testing.md`): language routing
(US-A1), changed-line filtering incl. no-`patch` (US-A3), disabled engine
never invoked and timeout inheritance (US-A7). The same behaviors get their
story-level *integration* proof against real tools in Phase 3 onward (the
traceability table's "real" column). *Integration* —
`adapter/analyzers/test_analyzer_concurrency.py` with real short-lived
subprocesses proving ≤ N alive and that queued time is excluded from the
analyzer timeout (US-A8). Existing Gitleaks/Semgrep suites run unchanged and
must stay green.

**Observability & rollback:** new metrics emit but nothing is registered
beyond the two existing analyzers, so production behavior is unchanged;
rollback is a plain revert (no schema, no config the old code reads).

**Acceptance:** all Phase 1 tests pass with a fake engine; full existing
unit + integration + `test_review_pipeline.py` E2E unchanged and green.

## Phase 2 — Repo-config discovery (spec Unit B)

**Why second:** Ruff (Phase 3) is meant to prove US-A4 on the first real
engine, so config discovery must exist before it. Depends on Phase 1's
registration (`config_filenames`).

- `RepoConfigFetcher` Protocol (Assumption 5) and the adapter in
  `adapter/github/` reusing `GitHubTreeFetcher` and the existing blob fetch
  ceilings; allow-list names only, per-file size cap, max file count, never a
  path named by a config or the PR.
- `StaticAnalyzer.run` gains `config_files` (Assumption 4); update Gitleaks,
  Semgrep, all fakes.
- Fetch happens once per job and only when at least one engine with config
  names is due; result passed to each sub-step; failures degrade to no config.
- Pure shared helper for the common bookkeeping (honored vs. skipped result
  type, logging fields); the per-engine classifiers arrive with each engine.

**Tests:** *Integration* — adapter against a `respx` cassette: config present,
absent, truncated tree, oversize file, fetch error → no config, job succeeds,
reason logged. *Unit* — allow-list/size-cap logic. A fake engine proves the
step passes only its own allow-listed files and writes them as inert files.

**Observability & rollback:** `config_source` log field and the
config-fallback counter exist; GitHub call volume watched via the existing
GitHub-adapter telemetry. Rollback: revert, or no engine yet depends on it.

**Acceptance:** a fake engine receives the head-commit config it names and
nothing else; every fetch failure mode leaves the job green.

## Phase 3 — Ruff (spec Unit C; first real engine)

**Why third:** smallest engine (single binary, JSON output, TOML data
config) — proves Phases 1–2 on real tool output before riskier packaging.

- Per-engine checklist, plus: `ruff.toml` / `.ruff.toml` / `pyproject.toml`
  `[tool.ruff]` honored; `extend` targets outside the fetched allow-list →
  fallback to default; default profile enables undefined-name, unused-import
  and syntax-error classes only.
- Ruff exit code 1 with findings is mapped to success.

**Tests:** per-engine checklist, *Integration* with the real pinned Ruff
binary on `adapter/analyzers/testdata` fixtures; US-A3/A4/A5/A6/A9 for Python.

**Observability & rollback:** `static_analysis_ruff_enabled=false` disables it
with configuration only.

**Acceptance:** spec US-A1–A6, A9 for Python pass; Python-only PR posts only
Ruff-attributable findings on added lines.

**Recorded measurements (Ruff 0.16.5, linux/amd64, Docker `dev` image):** peak
RSS 17 MiB for a single file, 29 MiB for 200 files with findings; wall time
8 ms (one file) and 14 ms (200 files), `--no-cache`; image-size delta is the
binary alone, 22.9 MiB (23,962,624 bytes). These feed the Phase 8
`worker.resources` arithmetic.

## Phase 4 — ESLint (spec Unit D; highest packaging risk)

**Why here:** highest reach but riskiest; after the framework is proven.
**Decided (owner delegated; see Decisions below):** ship ESLint on a pinned
Node runtime, but only in a worker-only image target (`worker-runtime`, built
`FROM runtime`), so the Webhook and Admin API images do not gain Node. This
phase introduces that target and points the Helm `worker` Deployment and
Compose `worker` service at it.

- Pinned Node runtime and ESLint with a fixed plugin set in a dedicated
  `Dockerfile` stage; `eslint.config.*` and `.eslintrc.js/.cjs` classified as
  executable → skipped; `.eslintrc.json/.yaml/.yml` and `package.json`
  `eslintConfig` honored if every `extends`/`plugins` reference resolves in the
  pinned set; default = `eslint:recommended`-class rules.
- Run with `--no-eslintrc`-style isolation so only the selected config is
  read, and no resolution outside the scan directory.

**Tests:** per-engine checklist, incl. the canary for a JS config; fixtures for
unresolvable plugin → default fallback.

**Observability & rollback:** as Phase 3; image-size delta recorded against
the spec's 600 MB total budget.

**Acceptance:** spec US-A1–A6, A9 for JS/TS.

**Recorded measurements (Node 22.23.3, ESLint 9.39.5 in eslintrc mode,
typescript-eslint 8.71.2, TypeScript 6.0.3; linux/amd64):**

- *Image size:* `worker-runtime` adds two layers over `runtime` — the Node
  binary (125 MB) and `/opt/eslint` (49.5 MB; TypeScript 24 MiB, ESLint 5 MiB,
  typescript-eslint 7 MiB) — so **+174.5 MB (166 MiB) by layer sum**, 1,034.8 →
  1,201.2 MiB; `docker image inspect` reports 1.47 → 1.69 GB (+227.5 MB), the
  larger figure, using the other accounting. Both are well inside the 600 MB
  budget (29–38 %), leaving ~370–430 MB for PHP and the Go toolchain. The
  `runtime` image gained nothing (no `node`, no `review-eslint`).
- *Duration (`review-eslint`, default profile, `worker` user):* 0.6 s for one
  JS file, 0.55 s for one TS file, 0.8–1.0 s for 200 files with findings —
  far under the 30 s single-language budget.
- *Peak RSS (node process):* 182 MiB (one file), 183 MiB (one TS file), 234 MiB
  (200 files). Container cgroup peak (page cache of the 120 MB `node` binary
  included) 153–208 MiB. These feed Phase 8's `worker.resources` arithmetic:
  at the default concurrency of 3, ESLint accounts for at most ~0.7 GiB.
- *Offline:* the whole integration module and a scan through the built
  `worker-runtime` image as the non-root `worker` user both pass under
  `docker run --network none`.

## Phase 5 — golangci-lint (spec Unit E)

- **Decided (see Decisions 4):** golangci-lint v2 always loads packages via
  `go list`/`go env`, so a pinned, checksum-verified Go toolchain goes in the
  worker-only `worker-runtime` target (not the shared `runtime` image), run
  with `GOPROXY=off`, `GOFLAGS=-mod=mod -buildvcs=false`, `GOTOOLCHAIN=local`
  and `GOCACHE`/`GOMODCACHE` inside the per-invocation temp directory. This
  phase therefore ships after Phase 4 (which creates the target) and is
  sequenced last among the engines so the image-size delta is measured with
  everything else present.
- Config forms `.golangci.yml/.yaml/.toml/.json` are data; any custom-plugin
  section → skipped. Default profile limited to linters that do not need
  resolved third-party types; findings caused by unresolved imports suppressed
  (US-A5). Runs with module download and network disabled.

**Tests / Observability / Acceptance:** per-engine checklist;
spec US-A1–A6, A9 for Go.

**Recorded measurements (golangci-lint 2.15.0, Go 1.27.2 pruned, linux/amd64):**

- *Image size — budget status first:* the whole-phase growth (HEAD `runtime`
  1,432.1 MB → `worker-runtime` 2,192.2 MB by `docker image inspect`) is
  **760 MB, inside the 860 MB budget (owner decision 2026-10-17, 100 MB
  headroom)**; by layer-sum (`docker history`, apparent bytes) it is 584 MB: Ruff
  24 + Trivy 175 in `runtime`, and 385 MB worker-only (Node 125, ESLint 49.5,
  PHP 10.5 + libonig 1.1, PHPStan phar 31.2, Go 126, golangci-lint 41.7). The
  two figures differ because `inspect` counts disk blocks (the Go tree is
  thousands of small files). Go's share is 126 MB after pruning (283 MB
  unpruned: dropped `test`, `doc`, `api`, `misc`, `src/cmd`, `testdata`,
  `*_test.go`, and every `pkg/tool` binary except `compile`/`asm`/`link`, which
  `go list -export` needs) plus 41.7 MB for golangci-lint. Nothing trimmed
  beyond the above. **Resolved (owner, 2026-10-17):** the budget is re-based
  to the measured 760 MB plus 100 MB, so golangci-lint stays default-on and
  this phase is budget-compliant (100 MB headroom for later engines).
- *Duration (cold per-invocation caches, `worker` user, `--network none`):*
  2.7 s for one file, 6.2 s for one file importing `net/http`, 2.9 s for 200
  files across 20 packages (400 findings) — all far under the 30 s budget.
- *Peak RSS:* 277 MiB (one file), 303 MiB (`net/http`), 282 MiB (200 files)
  for the largest single process (`golangci-lint`); container cgroup peak
  369–537 MiB. At the default concurrency of 3 this is at most ~1.5 GiB (cgroup
  figure) for Phase 8's `worker.resources` arithmetic.
- *Offline:* the measurements above ran through the built `worker-runtime`
  image under `docker run --network none`; the integration module also ran in
  the `dev` image.
- *Known fidelity limit:* golangci-lint skips type-aware linters for a package
  with any unresolved import, so a package importing a non-stdlib module yields
  no lint findings (only compile errors); first-cut accepted, see the adapter
  docstring.

## Phase 6 — Trivy config (spec Unit F)

- Pinned Go binary (pattern: `gitleaks` stage) with its misconfiguration
  check bundle embedded at build; DB/check updates, remote module download
  and telemetry disabled; `config` scanning only.
- `trivy.yaml` and an ignore file honored; custom-check paths (Rego) →
  skipped. Findings mapped by resource line-range overlap with added lines
  (US-A3's Trivy rule). Default = HIGH/CRITICAL.
- Not `changed_lines_only` in the generic sense — it uses its own resource-
  range rule inside the adapter's normalization; documented in the adapter.

**Tests / Acceptance:** per-engine checklist; Terraform, Dockerfile and plain
Kubernetes fixtures; spec US-A1–A6, A9 for IaC. Helm/CloudFormation fixtures
assert *no* engine invocation (out of scope).

**Implementation notes (Phase 6):** `adapter/analyzers/trivy.py` runs
`trivy config` with `--misconfig-scanners terraform,dockerfile,kubernetes`,
`--skip-check-update` (the 0.76.0 binary falls back to its embedded checks),
telemetry/version-check off, cache under the scratch directory, and always an
explicit `--config`/`--ignorefile` so nothing is auto-loaded by name. Trivy has
no switch for Terraform remote-module download, so the child gets a dead HTTP(S)
proxy plus `GIT_ALLOW_PROTOCOL=none` (and the image has no `git`); a loopback
module source would still bypass Go's proxy handling, so the network policy
remains the real boundary. A finding with no line range (e.g. Dockerfile
"no USER") is anchored on the file's first added line rather than dropped.
`match_iac` now rejects YAML containing `{{` so Helm templates are never routed.
`run_analyzer_subprocess` gained an `env_overrides` argument for the proxy pins.

**Recorded measurements (Trivy 0.76.0, linux/amd64, Docker `dev` image,
`--network none`):** peak RSS 171 MiB for a single Terraform file and 233 MiB
for 300 IaC files (100 each of Terraform, Dockerfile, Kubernetes); wall time
0.49 s (one file) and 2.8 s (300 files); image-size delta is the binary alone,
174.6 MB (174,563,488 bytes) uncompressed. Docker-reported sizes of the built
targets: `runtime` 1.69 GB, `worker-runtime` 1.92 GB (vs 1.47 / 1.69 GB before
the Trivy stage) — these on-disk figures exceeded the original 600 MB
placeholder; the budget is now 860 MB `docker image inspect` growth (see the
Phase 5 measurements and Decisions 3). Peak RSS here feeds the Phase 8
`worker.resources` arithmetic: Trivy is by far the heaviest engine.

## Phase 7 — PHPStan (spec Unit G)

**Decided (see Decisions below):** pinned PHP CLI plus a pinned, checksum-
verified PHPStan phar, added to the same worker-only `worker-runtime` target
introduced in Phase 4 (so this phase depends on Phase 4's target, not on
the shared runtime image). PHP runs with `allow_url_fopen=0`, no extensions
beyond the minimum, and no composer.

- `phpstan.neon` honored only when it sets none of `bootstrapFiles`,
  `scanFiles`, `scanDirectories`, extension includes or autoload directives;
  otherwise skipped. Default = level 0–1, no autoload; findings about
  unresolved classes/functions/constants suppressed (US-A5).

**Tests / Acceptance:** per-engine checklist; spec US-A1–A6, A9 for PHP.

**Implementation notes (Phase 7):** PHP is built from the official source tarball
(8.4.26, `sha256sum -c` against php.net's published digest) with `--disable-all`
plus only `phar tokenizer mbstring ctype filter` (no curl/openssl/sockets/XML/
database, no pear/composer), in the `php-runtime` stage on the same base as
`runtime`; the one extra shared library is `libonig5`. PHPStan 2.3.1 is the
single `phpstan.phar` (digest equals GitHub's asset `digest`) wrapped by
`tools/phpstan/review-phpstan` (`php -n`, `allow_url_fopen=0`,
`allow_url_include=0`, errors off the JSON stream; exit 1 with a JSON report is
mapped to success, exit 1 without one stays a failure so a rejected config can be
re-run on the default). Config is classified by an *allow-list*: only a
`parameters:` block of known data keys is honored; `bootstrapFiles`,
`scanFiles/scanDirectories`, `autoload_*`, `includes`, `services`, `extensions`,
`rules`, `tmpDir`, `parallel`, unknown keys and `%env.%` references skip it. The
chosen config is wrapped in a generated file that pins `tmpDir` inside the scan
directory and `parallel.maximumNumberOfProcesses: 1`, and supplies `level: 1` when
the repo config sets none. Any changed `vendor/` file is dropped before
materialization (PHPStan requires `vendor/autoload.php` from its cwd; canary test).
Unresolved class/function/constant/trait/interface findings are dropped (US-A5).
Known limits: PHPStan skips analysis of every other file when one file has a
syntax error (a PR with a parse error gets only that finding); `includes`
(e.g. baseline files) is not honored; level 0 reports definitely-undefined
variables while level 1 adds possibly-undefined ones.

**Recorded measurements (PHP 8.4.26, PHPStan 2.3.1, linux/amd64, `worker`
user, `--network none`):** peak RSS about 106 MiB for both a single file and 200
small classes (200 files with findings: 0.9 s wall; one file: 0.74 s, dominated
by phar start-up); image-size delta over `worker-runtime` is 42.8 MB by layer sum
(phar + wrapper 31.2 MB, `php` 10.5 MB, `libonig5` 1.1 MB), and Docker-reported
`worker-runtime` 1.92 → 1.97 GB, `runtime` unchanged at 1.69 GB with no `php`.
The original 600 MB placeholder was exceeded by the on-disk figures recorded in
Phase 6; the re-based 860 MB budget covers it, and PHPStan adds about 43-50 MB. Larger real-world
files will use more memory than these synthetic fixtures; `memory_limit` is 1G.
The whole `test_phpstan.py` module passes under `docker run --network none`.

## Phase 8 — Observability, runbook, E2E, real-repo check (spec Unit H, closing)

**Why last:** these cross-cut every engine and need real engines producing
traffic; adding dashboard panels earlier would repeat the Phase 7
`test_observability` failure (panels with no data).

- Grafana panels in `observability/grafana` for the Phase 1 metrics.
- `tests/e2e/test_static_analysis_expansion.py` (full Compose stack, stubbed
  GitHub): one defect per language posts exactly the expected comments; all
  new engines disabled reproduces today's output. Extend the E2E traffic so
  every new panel resolves data, or update `test_observability` in the same
  change.
- `docs/reference/operations.md`: per-engine switch, concurrency arithmetic
  (US-A8), config-skip log reasons, what to do on a fallback storm.
- `charts/review/values.yaml`: `worker.resources` sized per the recorded peak
  RSS arithmetic; `charts/review/test-render.sh` and
  `apps/test_helm_render.py` updated and passing.
- `docs/documentation_index.md` already lists the spec; `docs/roadmap.md`
  Phase 9 **Status is updated only after re-running the roadmap's verification
  basis**, never from this plan.
- Manual verification (not automatable here): run against a real repo with
  real configs; record the result honestly in the roadmap, as Phase 1's
  unexercised "real PR" clause was.

**Tests:** *E2E* as above; *Integration* for Helm render.
**Observability & rollback:** this phase *is* the observability closeout.
**Acceptance:** E2E green with all five engines; `test_observability` green;
`ruff`, `ruff format --check`, `mypy --strict`, unit, integration, frontend
and Helm render all green.

## Security review

One pass across everything this plan introduces. Worst case first.

- **Executing repo-supplied code (highest severity).** Linters load configs
  and plugins that are code (JS configs, PHP bootstrap/extension files, Go
  plugins, Rego). The Worker holds DB and GitHub App credentials. Mitigation:
  configs honored **as data only** with per-engine classifiers; executable
  forms skipped; a canary test per engine; scans offline so even a bypass
  cannot exfiltrate over the network; tools run as the non-root `worker` user
  with `analyzer_env` stripping the provider-credential map. Residual risk: a
  classifier bug on a form we did not anticipate — hence the allow-list of
  config *names*, the pinned plugin set, and the per-engine kill switch.
- **Path / argument injection.** PR-controlled filenames go through
  `validate_relative_path` and the `--` separator; config files come from a
  fixed name allow-list, never from a path a config names.
- **Resource exhaustion.** Pre-filter and blob-size ceilings, per-analyzer
  timeout with kill-and-reap, 10 MB output cap, and the per-process
  concurrency bound; config fetch has size and count caps.
- **Supply chain.** Every binary/runtime/asset pinned and checksum-verified at
  image build; no scan-time downloads.
- **Information exposure.** Lint messages can quote source; all findings pass
  the existing `TextRedactor` pass; engines have no network.
- **Authz / cross-tenant.** No new endpoint or permission. Config fetch uses
  the job's own installation token for the job's own repo.
- **Noise as a trust attack.** A flood of low-value findings can bury real
  ones; defaults are correctness-only and US-A10 keeps security findings
  ahead under the ceiling.

## Compatibility & rollout

- No API, schema or migration change expected (confirm `Source` is not a
  persisted constraint in Phase 1). The `StaticAnalyzer` Protocol gains one
  keyword argument (Assumption 4) — internal, with all implementers updated
  in the same change.
- Rollout is per engine behind `static_analysis_<engine>_enabled`, merged one
  phase at a time; each engine ships the same release as its image stage,
  Helm/Compose settings and runbook entry. Operators can turn an engine off
  without a code change.
- **Default-on caveat:** new engines default enabled (spec assumption), so an
  upgrade changes review output. The release note and runbook tell operators
  to set the flags `false` before upgrading for a quiet rollout.
- Dependency failure containment: a missing/slow engine is isolated by the
  existing `ISOLATE` policy; config-fetch failure falls back to defaults;
  nothing here can fail a job that would otherwise succeed.
- Image: the single-binary engines (Ruff, Trivy) stay in the shared
  `runtime` image. Node, PHP and the Go toolchain (with golangci-lint) land
  only in a worker-only `worker-runtime` target, so the internet-facing Webhook and Admin API
  images gain no interpreter. Phases 4 and 7 record the `worker-runtime`
  delta against the 600 MB budget; exceeding it is surfaced to the owner,
  not silently accepted.

## Traceability (story → phase → integration test)

| Story | Phase | Integration test |
| --- | --- | --- |
| US-A1 language scoping | 1 (fake), 3–7 (real) | `review/test_static_analysis_step.py` |
| US-A2 normalized findings | 3–7 | `adapter/analyzers/test_<engine>.py`; `findings/test_merge.py` + `test_models.py` unit |
| US-A3 changed-line scoping | 1, 6 (Trivy rule) | `review/test_static_analysis_step.py`; `adapter/analyzers/test_trivy.py` |
| US-A4 config as data | 2, 3–7 | `adapter/github/test_repo_config.py`; `adapter/analyzers/test_<engine>.py` (canary) |
| US-A5 quiet defaults | 3–7 | `adapter/analyzers/test_<engine>.py` |
| US-A6 findings ≠ failure; isolation | 3–7 | `adapter/analyzers/test_<engine>.py`; `review/test_review_job.py` |
| US-A7 settings | 1, 3–7 | `apps/worker/test_settings.py`; `review/test_config.py` |
| US-A8 concurrency bound | 1 | `adapter/analyzers/test_analyzer_concurrency.py` |
| US-A9 pinned & offline | 3–7 | adapter tests (offline, `-` filename); CI image-build job (checksum) |
| US-A10 ceiling ordering | 1 | `review/test_static_analysis_step.py`; `findings/test_merge.py` unit |
| All, happy path | 8 | `tests/e2e/test_static_analysis_expansion.py` |

A phase is not done until its rows' tests exist and pass.

## Decisions (owner delegated these to the implementer, 2026-10-16)

1. **ESLint on Node, worker-only image.** Competing tools (CodeRabbit,
   Codacy) run the repo's own ESLint/PHPStan; a Biome/oxlint substitute would
   ignore customers' existing configs, defeating "repo-native configs
   honored". Attack surface is contained by confining Node to the Worker's
   image target rather than by dropping the engine.
2. **PHPStan on a PHP CLI, same worker-only target.** PHP is a mainstream
   enterprise language and has no single-binary equivalent; dropping it
   would leave a visible coverage hole.
3. **Image budget re-based (owner, 2026-10-17):** the cap is the measured
   growth (760 MB by `docker image inspect`, `runtime` baseline 1,432.1 MB →
   `worker-runtime` 2,192.2 MB) plus 100 MB = **860 MB**, replacing the 600 MB
   placeholder. `inspect` is the gated figure because it is the larger of the
   two accountings. **Latency and defaults unchanged from the spec:** 30 s
   single-language latency, new engines
   default-on (industry norm; the profiles are correctness-only, and the
   release note tells operators to set the flags `false` before upgrading
   for a quiet rollout).

4. **golangci-lint with a pinned Go toolchain, worker-only.** Phase 5's
   implementer proved (v2.15.0, `--network none`) that golangci-lint cannot
   run without `go`/GOROOT (~250 MB), reversing the earlier "binary only"
   assumption. Alternatives (a syntax-only checker, or deferring Go) drop the
   repo-native-config promise for Go, which competitors honor. Accepted, on
   the condition that it is built last so the `worker-runtime` size is
   measured against the budget. Measured 760 MB against the re-based 860 MB
   cap (Decisions 3): within budget, so golangci-lint stays default-on.

Revisit any of these if the measured image or latency figures miss budget.
