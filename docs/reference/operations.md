# Operations runbook

Created by the plan's Phase 3 section
([`docs/plans/plan-phase-1-walking-skeleton.md`](../plans/plan-phase-1-walking-skeleton.md))
as a concrete deliverable for a specific interaction that phase introduces
(below), not just a sentence in the plan — extended by later phases as they
each introduce their own operator-facing gotcha. Linked from
[`README.md`](../../README.md)'s "Helm chart" section.

## Worker restart-looping after upgrade? Check startup logs for a fail-fast check first

Before Phase 3, `docker-compose.yml`'s `worker` service used `restart:
"no"` — a genuine startup failure (e.g. the LLM endpoint unreachable)
exited the container once, non-zero, and stayed exited. Phase 3 changes
this to `restart: unless-stopped` so the Worker stays up across its own
long-running dispatch loop's lifetime. The trade-off: the exact same
fail-fast `SystemExit(1)` checks that used to produce one clean failed
container now restart-loop the container indefinitely instead, since
Compose/Kubernetes restarts it after every exit.

If a Worker pod/container is restart-looping after an upgrade, this is
almost always one of `main()`'s own startup checks failing on every boot —
**check the container's startup logs first**, not the orchestrator's
restart count. Every one of these checks logs a clear `error`-level message
naming what's wrong (e.g. "LLM endpoint unreachable at startup; exiting",
"step configuration cannot produce a review; exiting") before exiting
non-zero — `docker compose logs worker` (or `kubectl logs`) shows it
immediately.

## Per-repo review-enqueue rate limiting is disabled by default

`review_enqueue_rate_limit_per_minute=0` — a known, accepted-for-now
trade-off pending PRD-owner review, not an oversight. See the plan's Phase
1 "Assumptions this plan makes" section for the full cost-exhaustion
reasoning (`synchronize` is a trigger, so an attacker-controlled PR can push
repeatedly to drive unbounded LLM spend with no per-repo bound at this
setting's shipped default). Set
`REVIEW_WEBHOOK_API_REVIEW_ENQUEUE_RATE_LIMIT_PER_MINUTE` above `0` (`10` is
this plan's own recommended starting point) to activate it — a single
config value, no code change. A startup `warning` log states plainly
whether this is active in a given deployment.

## Upgrading via Helm when this release includes new migrations

`charts/review/templates/` has no migration `Job`/hook yet (unlike
`docker-compose.yml`'s `migrate` service, which gates `webhook_api`/
`worker` startup on `alembic upgrade head` succeeding first) — a real gap,
first made load-bearing by Phase 1's own migration (the first since Phase
0's baseline). Until Phase 8 adds a proper migration Job/hook: run `alembic
upgrade head` manually — via `kubectl exec` into a migration-capable pod,
or a one-off `Job` using the same image — **before** `helm upgrade` rolls
out the new `webhook_api`/`worker` pods, whenever a release includes new
migrations. Skipping this risks the new pods starting against a schema that
doesn't yet have the columns/tables they expect.

## Worker restart-looping after upgrading to a release with Phase 4? Check `github_app_id`/`github_app_private_key`

Phase 4 adds two new required `WorkerSettings` fields — `github_app_id`
and `github_app_private_key` — with no default, since
`adapter/github/auth.py`'s JWT-signing/token-exchange flow (which
diff-fetch depends on) cannot function without them. An operator upgrading
from a pre-Phase-4 deployment must populate both
(`REVIEW_WORKER_GITHUB_APP_ID`/`REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY` in
Compose, or `github.appId`/`github.appPrivateKey` in the Helm chart's
`values.yaml`) before the Worker will start. Per this runbook's first
entry above, an unset or malformed value now presents as a restart loop,
not a single failed container — check the startup logs for "GitHub App
private key is not usable at startup; exiting" (a malformed/truncated key,
caught by this phase's own fail-fast RS256 signing validation) or a
`pydantic` field-required error naming `github_app_id`/`github_app_private_key`
(unset entirely) before assuming a deeper problem.

## Upgrading to a release with Memory (`plan-deliver-memory.md`)? Existing GitHub App installations need to re-consent

`plan-deliver-memory.md`'s Phase 7 section adds two new webhook event
subscriptions to the GitHub App manifest —
`"pull_request_review_comment"` and `"pull_request_review_thread"` — on
top of the already-subscribed `"pull_request"`/`"push"`. These activate
Phase 3's reply/thread-resolution capture and Phase 4's reaction polling,
both of which have been safe to run without these subscriptions since they
shipped (their handlers simply never receive the new event types until
this happens).

**This is a manifest-only change — it does not, by itself, change what any
already-installed GitHub App instance receives.** GitHub does not push
updated event subscriptions to an existing installation automatically: an
operator must re-approve the App's updated permissions on GitHub's own
"Install App"/"App settings" UI (Settings → GitHub Apps → the App →
"Permissions & events", or the org-level equivalent for an
organization-owned installation) before replies, thread-resolution
capture, or reaction polling start actually receiving data. Until an
operator does this:

- Phases 1–6's code keeps running completely unaffected — provenance
  recording, the admin API, and similarity-based suppression all continue
  to work exactly as before.
- Reaction polling (Phase 4) still partially works, since it reuses the
  already-subscribed `pull_request` event rather than a new subscription.
- Replies and thread-resolution capture (Phase 3, US-M1/US-M2) simply
  receive no events — not an error state, just no new data.

Re-consenting is itself a reversible, operator-initiated action: declining
the updated permissions (or re-consenting and later reverting) leaves
Phases 1–6 running unaffected either way. `"issue_comment"` is
deliberately **not** part of this subscription change — an `issue_comment`
event can never correlate back to a specific posted finding comment (only
`pull_request_review_comment`'s `in_reply_to_id` can), so subscribing to
it would only add unused webhook volume with no corresponding capture
capability.

## Product-knowledge provider configured but not contributing? Read the `outcome` of `review.product_knowledge.call_total`

Added by Phase 7 of
[`plan-deliver-product-knowledge.md`](../plans/plan-deliver-product-knowledge.md)
(the optional external product-knowledge augmentation,
[ADR-0013](../adr/0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md)).
The per-outcome entries below (one `##` heading per operator-visible
`outcome`) complete it.

**What is sent, and where (the data-egress statement).** The Admin UI shows
exactly this sentence next to the endpoint field:

> Sent to this endpoint on each review: the secret-redacted pull request title and linked issue keys. Never sent: code, the diff, or the pull request description.

With a provider configured and enabled for a repo, each review job sends the
configured endpoint **the redacted PR title and the linked issue keys** (at
most 10), and nothing else: never source code, the diff, file paths or the PR
description. The provider's results then travel to your configured LLM
endpoint as part of the review prompt, the same path the diff already takes.
review never contacts any endpoint on its own initiative: only the endpoint
an operator configured, and only from the Worker (the Admin API contacts it
only for an explicit test-connection).

**Reading the signal.** Every job that reaches the provider branch records
one `review.product_knowledge.call_total` with `operation="job"` and a closed
`outcome`; the shipped dashboard's "Product knowledge" row splits the non-`ok`
rate by outcome. Config-class outcomes are also logged at `warning`
(`product_knowledge degraded`, with `provider_ref` and `endpoint_host`):
`credential_unavailable` (credential ref missing or not bound to the endpoint
host), `blocked_egress` (the egress guard refused the address),
`unsupported_version` (no contract major in common), `auth_failed`,
`redaction_disabled` (see below). Transient outcomes (`timeout`,
`unreachable`, `provider_error`, `malformed`, `oversized`) and the benign ones
(`empty`, `no_query`, `skipped_public_repo`, `skipped_fork`, `disabled`) are
logged at `info`. **A provider failure never fails or delays a review beyond
`REVIEW_WORKER_PRODUCT_KNOWLEDGE_DEADLINE_SECONDS`** and never changes the
code-context outcome (`review.context_builder.outcome_total`).

`not_configured` (no provider setting for the repo; the steady state for most
repos) is deliberately logged at `debug` only, and the dashboard panel
excludes it (`outcome!~"ok|not_configured"`), so it neither floods the logs
nor buries the actionable outcomes; it is still counted on
`call_total{operation="job"}` if you need the adoption ratio.

**Every job does one settings read.** The provider resolver is wired
unconditionally whenever the feature is enabled, so each review job performs
one keyed `product_knowledge_provider` settings read (bounded by its own 2 s
cap and the branch deadline; a failed or slow read never affects the review's
code path). This is intentional: a provider may be defined only in the
database with `credential_ref` null, which is valid and must not be skipped by
checking the credential map first.

No alert rules ship (this repo ships none by convention). A starting point to
watch for config-class breakage:

```promql
sum by (outcome) (rate(review_product_knowledge_call_total{operation="job",origin="worker",outcome=~"credential_unavailable|blocked_egress|unsupported_version|auth_failed|redaction_disabled"}[15m])) > 0
```

**Why is it skipped?** `skipped_public_repo` / `skipped_fork` is the
exposure gate working: provider content is only used on private repos and
non-fork PRs unless the provider setting opts in with `allow_public_repos`
(a public PR's title is attacker-controlled, and findings are posted as
world-readable comments). `redaction_disabled` means
`REVIEW_WORKER_SECRET_REDACTION_ENABLED=false`: the PR title cannot be
redacted, so the provider is never called. The provider is only queried from
inside the Context Builder step; a startup warning is logged if credentials
are configured while that step is disabled.

**Turning it off.**

- Whole fleet, needs a restart: `REVIEW_WORKER_PRODUCT_KNOWLEDGE_ENABLED=false`
  (a startup `warning` states it; no client is built and nothing is called
  even if provider settings exist; jobs with a configured provider record
  `outcome=disabled`).
- No restart: set the `product_knowledge_provider` setting with
  `enabled: false` at platform scope (fleet) or at the org/repo scope (that
  scope only; the narrowest configured scope wins). Restores exactly the
  behavior without the feature.

### Rotating a credential

Tokens live only in the operator-provisioned
credential map (`REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS` /
`REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_CREDENTIALS`, or Helm
`productKnowledge.credentials`), never in Postgres. Update the Secret, then
**roll both the Worker and the Admin API** (a new ref visible to only one of
them yields `unknown_credential_ref` on config writes or
`credential_unavailable` at job time during the rollout). Each token is bound
to its `allowed_hosts`; the credential ref is global across orgs (a single
trust domain, ADR-0004).

**Pin ports.** An `allowed_hosts` entry and an endpoint allow-list entry may
carry a port (`wiki.internal:8443`, `[::1]:9000`, `*.corp.example:443`); a
URL without a port uses the scheme default (443 for `https`, 80 for `http`).
An entry **with** a port matches that port exactly. An entry **without** one
matches the host on *any* port: this is weaker (another service on the same
host can receive the token), kept for backward compatibility, and
**`host:port` is recommended for every entry**. Entries are validated at
startup (port 1-65535, no scheme, path or userinfo; `allowed_hosts` also no
wildcard).

**Unauthenticated providers need an allow-list entry.** A provider with no
`credential_ref` is not unconstrained: its endpoint must match
`REVIEW_WORKER_PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST` (or some credential's
`allowed_hosts`). Otherwise the config write is a 422
`unauthenticated_host_not_allowed`, test-connection answers
`blocked_by_policy`, and at job time the call fails closed
(`credential_unavailable`) with zero bytes sent. An in-cluster
network-policy-protected provider therefore needs an entry such as
`knowhow.svc.cluster.local:8080` in the allow-list (set it on the Worker and
the Admin API).

**Transport notes.** (a) Cleartext `http` is allowed to *private* addresses,
and a bearer token is sent in clear there; only a public address refuses the
token over `http`. Use `https`, or accept that anyone on that network path can
read the token. (b) There is no option to trust an internal CA: TLS
verification uses the system trust store only and `SSL_CERT_FILE` /
`SSL_CERT_DIR` are deliberately ignored, so an `https` provider behind a
private CA is unreachable until the CA is in the image's system store (build a
derived image). (c) The `endpoint_url` **path must not carry a secret**:
the stored setting is readable by Viewers through `GET /api/v1/config` (the
audit log records only the host), and userinfo, query and fragment are
rejected at write time. Put secrets in the credential map, never in the URL.

### Accepted limitations (stated, not bugs)

- `credential_ref` is global: an Admin can point org A's repo at org B's
  credential. review has a single trust domain (ADR-0004); scope each token to
  the least data its provider account can read.
- Viewers can read the stored `endpoint_url` (full path) through
  `GET /api/v1/config`; it is not redacted. Do not encode anything secret in it.
- `GET /api/v1/product-knowledge/credentials` lists credential **names** to
  Viewers (bound hosts to Admins only); tokens are never returned.
- The whole credential map is in both the Worker's and the Admin API's pod
  environment (the Admin API holds provider tokens so it can validate bindings
  and run test-connection).
- The PR title is stored un-redacted in `job_queue.payload` (the customer's own
  Postgres); only the outbound query is redacted.
- Provider content can reach the PR comment if an attacker-controlled PR
  prompts the model to quote it. The controls are the exposure gate (below),
  the optional `issue_key_prefixes` allow-list and per-repo opt-out, not the
  prompt's "do not quote verbatim" request.

**First-line diagnostic: test-connection.** In the Admin UI's Product
knowledge panel (or `POST /api/v1/product-knowledge/test-connection`, Admin
only, 6/min per session, 6/min per user across sessions and 30/min per Admin API pod), run it against the *saved*
values. It uses the same adapter path as the Worker and returns one of `ok`,
`blocked_by_policy`, `disabled`, `unreachable`, `no_contract`, `malformed`,
`auth_failed`, `unsupported_version`, `deprecated_version`, `timeout`. A target no credential's
`allowed_hosts` or the operator allow-list covers is always the collapsed
`blocked_by_policy`, with no timing, by design. A provider with no credential
(`credential_ref` null) is bound to no `allowed_hosts`, so it is testable only
when its host (and port, if the entry has one) matches the operator allow-list
(`..._ENDPOINT_ALLOWLIST`) or some credential's `allowed_hosts`; otherwise it
is `blocked_by_policy`. The 30/min
global limit is counted per Admin API pod (in memory), not cluster-wide, so N
replicas allow up to N x 30/min. A malformed `REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST`
does not stop the Admin API: it logs the problem at startup (see the
`endpoint allow-list is invalid` error) and reports `disabled` until fixed;
the Worker, by contrast, refuses to start.

**Rotating a credential map does not roll pods.** The chart adds no checksum
annotation for the product-knowledge Secret, whether chart-managed or an
`existingSecret`; restart the Worker and the Admin API after any rotation.

**Why the Worker's `call < deadline < internal` timeout check is conditional.**
It is enforced only when the kill switch is on and the credential map or the
endpoint allow-list is non-empty (`in_use`): the provider setting itself lives
in Postgres, which settings construction cannot read, so those two are the
bootstrap signals (an unauthenticated provider is reachable only through the
allow-list). Enforcing it unconditionally would make default-tuned installs
that never use product knowledge fail to start.

## Provider outcome `unsupported_version` or `deprecated_version`? The provider and review share no contract major

review supports the current and previous contract major and picks the
highest both sides declare via the provider's `product_knowledge_contract`
tool. `unsupported_version` (config-class, `warning`) means there is no
overlap: upgrade the provider (or review), then re-run test-connection.
`deprecated_version` means a shared major exists but is scheduled for removal
(at least 12 months after its successor ships); the review is unaffected.
The negotiation result is cached for about 10 minutes per endpoint, so a
fixed provider is picked up without a restart.

## Provider outcome `blocked_egress`? The egress guard refused the address

The connection-time guard refused the resolved address before any byte was
sent: loopback, link-local, unspecified, multicast and cloud-metadata
(`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`, `168.63.129.16`,
`192.0.0.192`) addresses, NAT64/6to4/IPv4-compatible forms, or a non-`http(s)`
scheme are always denied. Private ranges are allowed unless
`REVIEW_WORKER_PRODUCT_KNOWLEDGE_ALLOW_PRIVATE_NETWORKS=false`; when an
endpoint allow-list (`REVIEW_WORKER_PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST`) is
set, the host (and port, when the entry carries one) must match it, and an
unauthenticated provider must match it (or a credential's `allowed_hosts`)
even when it is empty. Fix the endpoint hostname or the operator policy.
Redirects are never followed, so an endpoint that answers `3xx` is also
reported here or as `provider_error`: point the setting at the final URL. A
`NetworkPolicy` in the cluster remains the outer control; this guard is
defense in depth.

## Provider outcome `credential_unavailable`? The credential ref is missing or not bound to the endpoint host

At job time the Worker re-checks that `credential_ref` exists in its
credential map **and** that the endpoint host (and port, if the entry has
one) is in that credential's `allowed_hosts`. For a null `credential_ref` it
re-checks that the endpoint matches the operator allow-list or some
credential's `allowed_hosts`; failing either, no request is made (the token is never
sent). Typical causes: the Worker's map lacks a ref the Admin API has (a
rollout that rolled only one of them), or the endpoint host was changed
without updating `allowed_hosts`. Update the Secret and roll **both** the
Worker and the Admin API.

## Provider outcome `auth_failed`? The provider rejected the token

The provider answered 401/403. The token in the map is wrong, expired or
lacks access: rotate it (see "Rotating a credential" above), then run
test-connection. If `credential_ref` is null the provider received no token.

## Provider outcome `timeout` or `unreachable`? The provider is slow or down

Transient and `info`-level: the review ran without provider context and was
not failed or delayed beyond
`REVIEW_WORKER_PRODUCT_KNOWLEDGE_DEADLINE_SECONDS` (default 10 s; per-call
limit `..._CALL_TIMEOUT_SECONDS`, default 5 s). There is no retry or circuit
breaker in v1. Persistent `timeout` means the provider's search is slower than
the per-call limit; persistent `unreachable` means DNS/TLS/connect failure
from the Worker pod (check from the Worker's network, not your laptop).
`rate_limited` answers from the provider put that endpoint in a short
in-process cool-down.

## Provider outcome `provider_error`? The provider answered with an error, or an internal step failed

Either the provider returned a tool error (`internal`, `unavailable`, an
unexpected status, a redirect) or a local step inside the branch (for example
the secret redactor) failed. The review is unaffected and the provider's error
text is deliberately never logged. Run test-connection; if it is `ok`, the
failure is intermittent on the provider side. Worker logs name only the
credential ref and endpoint host.

## Provider outcome `malformed` or `oversized`? The provider's response broke the contract

`malformed`: the response did not parse or validate against the v1 schemas
(wrong field types, no `title`, nesting too deep). `oversized`: more than
256 KiB. review drops invalid items and truncates over-long fields, so
`partial` is the more common symptom; these two mean nothing usable came back.
Run the conformance suite against the provider (see the
[provider-implementer guide](product-knowledge-contract/v1/README.md)) to find
the violating check.

## Provider outcome `skipped_public_repo` or `skipped_fork`? The exposure gate is working

By default provider content is only used on **private repos** and on PRs from
the same repo (not forks): on a public repo or a fork PR the title and head ref
are attacker-controlled, and findings are world-readable comments. To allow a
public repo, set `allow_public_repos: true` on that scope's
`product_knowledge_provider` setting (an explicit, audited opt-in); the same
flag lifts the fork skip. A repo that is itself a fork (GitHub's
`head.repo.fork=true`) is skipped the same way unless `allow_public_repos` is
set, even when the PR's head and base are the same repo. Consider `issue_key_prefixes` (e.g. `["HR"]`) so only
keys of your tracker are ever queried. When it is set, the allow-list is a
content restriction: only matching keys are sent as the query and the PR title
text is not; a PR with no matching key makes no provider request.

## Provider outcome `redaction_disabled`? Secret redaction is off, so the provider is never called

`REVIEW_WORKER_SECRET_REDACTION_ENABLED=false`: the PR title cannot be
redacted, and redaction of the outbound query is a precondition of the
feature, not best effort. Re-enable redaction, or leave the provider off. A
redactor *failure* inside the branch is reported as `provider_error` and the
review continues.

## Rolling back, or cleaning up orphaned `product_knowledge_provider` rows after a code revert

The rollback is the setting, not a code revert: `REVIEW_WORKER_PRODUCT_KNOWLEDGE_ENABLED=false`
(restart) or an `enabled: false` row at platform/org/repo scope (no restart).
If the code *was* reverted, rows for the now-unknown key remain and the config
API can no longer edit or delete them (writes validate against the registry).
Remove them directly:

```sql
DELETE FROM org_repo_team_setting WHERE key = 'product_knowledge_provider';
```

Take a backup first; this removes every scope's provider configuration.

## Worker will not start after setting the product-knowledge credential map?

`REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS` must be a JSON object
`{"<ref>": {"token": "...", "allowed_hosts": ["host", ...]}}`: refs match
`[a-z0-9_-]{1,64}`, every entry needs a non-empty token and a non-empty
`allowed_hosts` of exact hostnames, optionally with `:port` (no `*`, no
scheme), and no ref repeats.
The Worker fails fast naming the problem (never the value); the Admin API
instead logs it and disables the product-knowledge routes so the config UI
stays up. An empty value means "not configured". With the feature in use,
`REVIEW_WORKER_PRODUCT_KNOWLEDGE_CALL_TIMEOUT_SECONDS` <
`..._DEADLINE_SECONDS` < `REVIEW_WORKER_CONTEXT_BUILDER_INTERNAL_TIMEOUT_SECONDS`
must hold.

## Static-analysis engines: switches, concurrency arithmetic, config skips, fallback storms

Context: [`docs/specs/spec-static-analysis-expansion.md`](../specs/spec-static-analysis-expansion.md)
and [`docs/plans/plan-phase-9.md`](../plans/plan-phase-9.md). Besides Gitleaks and
Semgrep the Worker runs five correctness-only engines on the files a PR changes:
Ruff (Python), ESLint (JS/TS), golangci-lint (Go), Trivy config (Terraform,
Dockerfile, Kubernetes YAML) and PHPStan (PHP). All scans are offline, run as the
non-root `worker` user, and honor a repo's own config only as inert data (an
executable config such as `eslint.config.js` is skipped, never run).

### Upgrading: the new engines are on by default

An upgrade changes review output (more inline comments on the languages above). For a
quiet rollout set the flags to `false` first, then enable engines one at a time:

| Engine | Env var (Compose `.env` / Helm `worker.staticAnalysis.*`) | Timeout override |
| --- | --- | --- |
| Ruff | `REVIEW_WORKER_STATIC_ANALYSIS_RUFF_ENABLED` / `ruff.enabled` | `..._RUFF_TIMEOUT_SECONDS` / `ruff.timeoutSeconds` |
| ESLint | `..._ESLINT_ENABLED` / `eslint.enabled` | `..._ESLINT_TIMEOUT_SECONDS` |
| golangci-lint | `..._GOLANGCI_LINT_ENABLED` / `golangciLint.enabled` | `..._GOLANGCI_LINT_TIMEOUT_SECONDS` |
| Trivy | `..._TRIVY_ENABLED` / `trivy.enabled` | `..._TRIVY_TIMEOUT_SECONDS` |
| PHPStan | `..._PHPSTAN_ENABLED` / `phpstan.enabled` | `..._PHPSTAN_TIMEOUT_SECONDS` |

(All env vars carry the `REVIEW_WORKER_STATIC_ANALYSIS_` prefix.) A disabled engine is
not started, costs nothing, and cannot affect a job. An engine that crashes or times
out is isolated: the job still completes with every other engine's findings, the
failure is logged at `error` by `review.static_analysis`, and it shows as a
`failed`/`timeout` series on the dashboard's "Analyzer runs by engine and outcome"
panel. Turn that one engine off with its switch; no rebuild is needed.

The interpreters (Node, PHP, Go) exist only in the `worker-runtime` image target
(`review-worker`); the Webhook and Admin API images contain none of them. Run the
Worker from the `review-worker` image, not `review`.

### Concurrency arithmetic (US-A8) and Worker memory

At most `REVIEW_WORKER_STATIC_ANALYSIS_MAX_CONCURRENT_ANALYZERS` (default `3`) analyzer
subprocesses run at once **per Worker process**, across all of that process's jobs; the
rest wait for a slot. Waiting does not consume an analyzer's own timeout, but the step
timeout still bounds the whole step. Startup fails fast unless

    ceil(enabled_analyzers / N) x max(per-analyzer timeout) <= static_analysis_step_timeout_seconds

(defaults: 7 analyzers, N = 3, 60 s each, 240 s step: `ceil(7/3) x 60 = 180 <= 240`).
Raising N, enabling more analyzers or raising a timeout can break that inequality; the
Worker then refuses to start and names the offending setting.

Total in-flight analyzers across a deployment is `worker replicas x N`
(dashboard: "Analyzers in flight"). Memory limit rule: the Worker limit must exceed the
Worker baseline plus the sum of the N largest per-engine peak RSS values. Measured
(linux/amd64, `worker` user, `--network none`, real fixtures):

| Item | Peak RSS |
| --- | --- |
| Worker process, embedding model loaded (idle / between jobs) | ~390-415 MiB |
| golangci-lint (largest single process; container cgroup peak 369-537 MiB) | 277-303 MiB |
| ESLint (node) | 182-234 MiB |
| Trivy config | 171-233 MiB |
| PHPStan | ~106 MiB |
| Semgrep | ~108 MiB |
| Ruff / Gitleaks | 17-29 MiB / ~16 MiB |
| Whole container, one PR with all five engines + Gitleaks/Semgrep (E2E, N = 3) | 734 MiB peak (`memory.peak`) |

For N = 3: 415 + (303 + 234 + 233) = ~1.2 GiB worst case, so the chart ships
`worker.resources` of request `1Gi` (the observed scan peak, rounded up) and limit `2Gi`
(Compose: `mem_limit: 2g`). If you raise N, recompute: add the next-largest engine peak
for each extra slot and keep the limit above the total with headroom; larger real
repositories use more memory than these small fixtures. An OOM-killed Worker shows up
as restarts plus jobs re-claimed after the stuck-job threshold.

### Config-skip log reasons

Each engine logs `config_source` (`repo` or `default`) when it picks a profile. When a
repo's config was not honored, the same log line carries `config_skip_reason`, and the
default correctness-only profile ran instead. The engine's config is only ever read from
the **repository root** at the PR head commit (nested configs are logged as
`nested analyzer config ignored (repo root only)` and ignored).

| `config_skip_reason` | Meaning |
| --- | --- |
| `executable_config` | The config is code (JS/TS ESLint config, PHP bootstrap, ...); never executed. |
| `config_invalid` | Unparseable, wrong shape, or an unsupported version. |
| `unsupported_syntax`, `unsupported_option`, `unsupported_config_version` | Valid, but uses something the data-only classifier does not allow. |
| `path_option_not_allowed`, `cache_dir_not_allowed`, `output_path_not_allowed` | The config names a path outside the scan directory (cache, output, include). |
| `extend_unresolved`, `extends_unresolved`, `plugin_unresolved`, `parser_unresolved` | References a file, preset, plugin or parser that was not fetched / is not in the pinned set. |
| `env_reference` | Reads an environment variable. |
| `custom_checks_not_allowed`, `remote_checks_not_allowed`, `type_aware_not_supported` | Custom rules, remote rule bundles or type-aware linting. |
| `ignorefile_invalid` | Trivy ignore file unusable. |
| `rejected_by_tool` | The engine itself rejected the config at run time; it was re-run once with the default profile. |
| `not_a_regular_file`, `file_too_large`, `too_many_files` | Config-fetch caps (symlink/submodule, > 256 KiB, > 16 files). |

A handful of these per day is normal (customers use plugins we do not ship). It is not an
incident; the review still ran, on defaults.

### Counting config skips, silent engines and dropped findings

These counters make the failure modes above visible without log searching (dashboard row
"Static analysis engines"):

- `review.static_analysis.config_skipped_total{analyzer_name, reason}` is the metric form of
  the `config_skip_reason` log above (every reason in the table, including `rejected_by_tool`).
  A rise in `rejected_by_tool` for one engine right after an image bump means the pinned tool
  version rejects configs it used to accept; check the engine's release notes and its pinned
  default profile.
- `review.static_analysis.findings_total{analyzer_name}`: an engine at zero while its
  `runs_total{outcome="success"}` keeps climbing is silently broken (routing, output parsing
  or default profile). A sudden jump is a rule-set regression flooding PRs.
- `review.static_analysis.analyzer.skipped_total{analyzer_name}`: runs skipped because no
  changed file matched the engine. A `success` run with no skip is a real execution, so
  "100% success" is only meaningful against this.
- `review.static_analysis.findings_dropped_total`: `reason="redaction_ceiling"` means the
  hard cap discarded findings (consider whether it is too low now that lint engines run);
  `reason="not_changed_line"` (with `analyzer_name`) counts findings hidden by the
  changed-lines filter.

### Fallback storm: `review.static_analysis.config_fallback_total` climbing

The dashboard panel "Repo-config fallbacks (default profile used)" counts jobs whose whole
repo-config fetch failed, by `reason`: `tree_fetch_failed` (GitHub tree API error or
timeout after retries), `content_fetch_failed` (blob fetch failed) or `tree_truncated`
(repository tree too large for one API response; permanent for that repo). Reviews keep
working; every engine just runs its default profile, so repo-specific rules silently stop
applying.

1. A sudden rise across many repos with `tree_fetch_failed` / `content_fetch_failed` is
   a GitHub-side or network problem: check `adapter.github.http` retry/budget-exhausted
   warnings in the Worker logs, GitHub status, and the App's rate limit. It clears when
   GitHub recovers; no action on the platform.
2. `tree_truncated` for one or two very large repos is expected and permanent (a
   per-path contents lookup is the known follow-up); do not chase it.
3. Nothing needs disabling: a fallback never fails a job. Only disable an engine if its
   own `failed`/`timeout` series is the problem.

## Clone detection: switches, backfill load, rollback, alerts

Context: [`docs/specs/spec-clone-detection.md`](../specs/spec-clone-detection.md) and
[`docs/plans/plan-phase-10.md`](../plans/plan-phase-10.md). The `clone_detection` review step flags
code a PR copies from the default branch (or from another file in the same PR) as a `low`-severity
inline comment naming both locations. It compares normalized token fingerprints stored by indexing,
so it makes no LLM call and needs no extra service.

### Settings

All five are Worker settings, mirrored one-for-one in Compose (`.env`) and Helm
(`worker.cloneDetection.*`):

| Env var (`REVIEW_WORKER_CLONE_DETECTION_`) | Helm value | Default | Notes |
| --- | --- | --- | --- |
| `ENABLED` | `enabled` | `true` | Kill switch for the review step only. Fingerprint writing during indexing is not gated by it. |
| `STEP_TIMEOUT_SECONDS` | `stepTimeoutSeconds` | `30` | Must exceed 5: the step fails open after `value - 5` seconds, inside the registry timeout. |
| `MIN_TOKENS` | `minTokens` | `100` | **Must be >= 100**, else the Worker fails startup. See below. |
| `MIN_LINES` | `minLines` | `10` | A duplicated region shorter than this is not reported. |
| `MAX_FINDINGS` | `maxFindings` | `5` | Per-job cap on clone comments. |

The default is **on in code**, so reverting only the Helm values file does nothing once the Worker
image ships; set `enabled: false` explicitly. For a quiet rollout set it to `false` before upgrading.

**Why `MIN_TOKENS >= 100`.** Fingerprints are winnowed with gram size 25 and window 76, which
guarantees that any duplicated run of at least 100 normalized tokens shares a stored fingerprint. That
guarantee is baked into stored data, so `MIN_TOKENS` can be raised (a post-filter) but not lowered below
100 without bumping `CLONE_FINGERPRINT_VERSION` in `indexing/clone.py` with new `K`/`W` and
re-fingerprinting every repo.

The Memory dismissal check (an approved dismissal suppresses a recurring clone finding) shares the one
`REVIEW_WORKER_MEMORY_SUPPRESSION_ENABLED` flag with LLM-finding suppression: turning it off disables
both.

### Upgrade order: migrate first

The chart has no migration Job or hook. Run `alembic upgrade head` (migration `0011` adds
`index_clone_file`, `index_clone_fingerprint` and `repo.clone_fingerprint_version`) **before**
`helm upgrade`, exactly as in "Upgrading via Helm when this release includes new migrations" above.
Skipping it is now louder than before: the new Worker reads `repo.clone_fingerprint_version`, so its
index jobs crash against the old schema.

### Backfill: the first push per repo

Existing repos have no fingerprints. The first default-branch push after the upgrade finds
`repo.clone_fingerprint_version` below the current version (`1`) and runs the index job as a **full
re-index of every in-scope file** (full-run ceilings: 20,000 files / 2 GB), re-chunking and re-embedding
as well as fingerprinting. Expect one full-run-sized job per repo. Until it completes the review step
finds no default-branch matches for that repo (in-PR duplicates are still found); that is a normal
state, not an error. To defer the load, run with `REVIEW_WEBHOOK_API_INDEXING_ENABLED=false` (the
code and Compose default). The Helm chart does not render this setting, so on Helm indexing is off,
no index job is enqueued and the backfill does not run at all until an operator enables indexing by
some other means (the chart has no extra-environment mechanism today). A backfill with a transient failure (GitHub fetch or database write)
leaves the version unstamped and the next default-branch push retries it. Files dropped by the ceilings
and tokenizer (`clone` stage) failures are deterministic: they are counted and the version is stamped,
so a repo larger than the ceilings is permanently partly covered (visible as a nonzero
`clone_backfill_dropped_total`), the same limit the initial index has.

**Storage.** Measured on the fixture corpus: sampled fingerprints are about 1.9 % of tokens (the design
figure is 2.6 %), at roughly 12 B plus index entry per row, and the per-file token-hash and line-map
arrays cost about 4 B per token, roughly the source size, or about 4 MB per million tokens. Plan
for a few MB of Postgres per 100 KLOC of default-branch code. Tokenize-and-fingerprint costs about
0.45 ms of CPU per file, roughly doubling the Tree-sitter-side CPU of indexing; embedding still dominates
a backfill's wall time.

### Release note: stuck-job threshold

`stuck_job_warning_threshold_seconds` must be at least the sum of the macro-step timeouts, which grows
from 515 s to **545 s** with the 30 s `clone_detection` step. The 600 s default still holds, but an
operator override between 515 and 545 now fails Worker startup (check the startup log); raise it, or
lower `REVIEW_WORKER_CLONE_DETECTION_STEP_TIMEOUT_SECONDS`.

### Rolling back

1. Set `REVIEW_WORKER_CLONE_DETECTION_ENABLED=false` (`worker.cloneDetection.enabled: false`) and
   restart the Workers. This alone stops all clone comments and needs no data change.
2. If you also revert to a Worker image that predates this feature, note that old code never touches
   the clone tables: files changed or deleted while it runs leave stale fingerprints, and a later
   roll-forward would not repair them (the version is already stamped). Before the next roll-forward,
   once **every old Worker has drained** (scale old Workers to zero and wait for
   `SELECT count(*) FROM job_queue WHERE status = 'claimed';` to reach 0), force a re-backfill:

   ```sql
   UPDATE repo SET clone_fingerprint_version = 0 WHERE clone_fingerprint_version <> 0;
   ```

   Running it earlier is useless: a still-running old Worker keeps leaving stale rows behind the reset.
   The next default-branch push per repo then re-fingerprints it in full.

No down-migration is needed for safety: old code ignores the new tables and column.

### Alerts and a stale-repo query

Metric names are the OpenTelemetry names with dots as underscores, as on the dashboard (for example
`review_indexing_files_processed_total`).

- Stuck backfill (jobs ending `incomplete` repeatedly):
  `increase(review_indexing_clone_backfill_total{outcome="incomplete"}[6h]) > 3`
- Files a backfill dropped at the full-run ceilings:
  `increase(review_indexing_clone_backfill_dropped_total[1d]) > 0`
- Tokenizer failures, as a share of every file that reached the clone stage (a fifth is the threshold):
  `rate(review_indexing_clone_stage_failed_total[1h]) / (rate(review_indexing_files_processed_total[1h]) + rate(review_indexing_clone_stage_failed_total[1h])) > 0.2`
- The step failing open (query, content fetch or timeout):
  `sum(rate(review_clone_detection_outcome_total{outcome=~"failed_open_.*"}[1h])) / sum(rate(review_clone_detection_outcome_total[1h])) > 0.2`

`incomplete` only emits when a job runs, so a repo that never gets another default-branch push is
invisible to the first rule. Count repos still awaiting backfill directly (replace `1` with the current
`CLONE_FINGERPRINT_VERSION`):

```sql
SELECT count(*) FROM repo WHERE clone_fingerprint_version < 1;
```

### Limits and trust assumptions

- **Trust assumption.** A clone comment discloses a default-branch file path and line range to anyone who
  can read the PR, and the step acts as an oracle: any PR author can learn whether near-identical code
  exists on the default branch. This is acceptable only because the PR author is assumed to have read
  access to the repo (one customer's own repos). An author with narrower access (a fork author on a
  private repo, a restricted-path setup) would learn paths they cannot read. Messages contain paths and
  integers only, never source text.
- **Non-default base branches.** A PR into a release or gathering branch is compared against the
  default-branch index only. Duplication of code that exists only on the base branch is not found, and
  code removed on the base branch can still match.
- **Background work after a timeout.** Parsing runs in a worker thread (`asyncio.to_thread`), which
  `wait_for` cannot cancel. After a `failed_open_timeout` the parse may keep running in the background
  until it finishes; it is bounded by the blob-size ceiling and holds no database resources.

### Noisy or wrong findings

Disable the step (`ENABLED=false`, restart the Workers); no rebuild or data change is needed, and
indexing keeps fingerprinting so re-enabling is instant. Before disabling, try raising `MIN_TOKENS` or
`MIN_LINES`, or lowering `MAX_FINDINGS`, and have reviewers dismiss repeats: an approved dismissal
suppresses the same clone pair on later pushes (line shifts do not defeat it), and a dismissal for one
pair never suppresses a different pair. Check the `review_clone_detection_outcome_total` outcomes
(`completed`, `no_matches`, `no_eligible_files`, `failed_open_*`) to tell "no matches" from "not
working". `no_matches` cannot distinguish an un-backfilled index from a PR with no duplicates; read
backfill coverage from `review_indexing_clone_backfill_total{outcome="complete"}` instead. For a slow
or timed-out step, `review_clone_detection_phase_duration_ms` shows which phase (`fetch_content`,
`prepare`, `find_candidates`, `load_files`, `match`, `memory_suppression`) is responsible;
`candidate_cap_dropped_total{reason}` shows which fan-out cap is firing; `files_skipped_total{reason="unparseable"}`
rising points at a parser bug; a non-zero `memory_suppression_failed_total` means dismissed clone
findings are being re-posted because the Memory check is failing.

## Fix generation: switch, cost, outcomes, rollout, rollback

Context: [`docs/specs/spec-auto-fix.md`](../specs/spec-auto-fix.md),
[`docs/plans/plan-phase-11.md`](../plans/plan-phase-11.md) and
[ADR-0015](../adr/0015-fix-generation-step-and-validation-gate.md). The `fix_generation` step runs after
`verification` and attaches a GitHub suggested change (a one-click-committable block) to eligible findings in
their existing inline comment. It never commits anything: a person clicks "Commit suggestion" in GitHub.

### What leaves the deployment, and what it costs

Per attempted fix the Worker sends the configured LLM endpoint a window of the file
(`FIX_GENERATION_WINDOW_LINES`, default 60 numbered lines) plus the finding text; a verified LLM finding's fix
sends a second, smaller call (the finding, the window and the proposed replacement) for the rubric check. That
is the same class of data the review already sends (the diff and context) to the same endpoint; nothing goes to
any new party. Secret-shaped replacements are rejected before display, and Gitleaks findings are never
candidates. At the defaults the cost bound is **at most 10 extra LLM calls per job** (5 fixes, up to 2 calls
each); analyzer findings use one call, since the originating analyzer's re-run is the second check.

### Settings

Worker settings, mirrored in Compose (`.env`) and Helm (`worker.fixGeneration.*`):

| Env var (`REVIEW_WORKER_FIX_GENERATION_`) | Helm value | Default | Notes |
| --- | --- | --- | --- |
| `ENABLED` | `enabled` | `false` | **Off by default**: it spends LLM budget on calls nobody asked for. |
| `STEP_TIMEOUT_SECONDS` | `stepTimeoutSeconds` | `90` | Must exceed 5: the step fails open after `value - 5` seconds, keeping the fixes already accepted. |
| `MAX_FIXES_PER_JOB` | `maxFixesPerJob` | `5` | Candidates over the cap are counted in `skipped_cap_total`; the best-ranked are kept. |
| `MAX_RANGE_LINES` | `maxRangeLines` | `20` | Longest line range a fix may replace. |
| `MAX_REPLACEMENT_LINES` | `maxReplacementLines` | `40` | Longest replacement text. |
| `WINDOW_LINES` | `windowLines` | `60` | Must be at least `MAX_RANGE_LINES + 2`. |

Model choice is the Admin UI's model-routing row `fix_generation`; the rubric call reuses the `verification` row.

**Pair enabling it with the review-enqueue rate limiter.** Every push re-runs the review and no fix is reused
across pushes (each `synchronize` enqueues a fresh review; no cross-push de-duplication exists), so spend is
bounded per job, not per PR. Set `REVIEW_WEBHOOK_API_REVIEW_ENQUEUE_RATE_LIMIT_PER_MINUTE` above `0` (see
"Per-repo review-enqueue rate limiting is disabled by default" above) before enabling fix generation on repos
open to outside contributors.

### Upgrade order and the stuck-job threshold

Run `alembic upgrade head` **before** the Worker rolls (migration `0012` adds
`posted_finding_comment.carried_suggestion`), as in "Upgrading via Helm when this release includes new
migrations". The opposite order is fail-safe: the column is defaulted and an old Worker never writes it.

`stuck_job_warning_threshold_seconds` must be at least the sum of every macro-step timeout, which grows from
545 s to **635 s** with the 90 s `fix_generation` step (counted whether or not the step is enabled). The
default rises from 600 s to **700 s**; an operator override between 545 s and 635 s now fails Worker startup
(the log names the required minimum). Raise it, or lower `STEP_TIMEOUT_SECONDS`.

### Outcome meanings

`review_fix_generation_outcome_total{outcome,source}` counts each attempt once (`source` is `llm` for the rubric path, `analyzer` for the re-run path):

| `outcome` | Meaning |
| --- | --- |
| `accepted` | Passed every gate; posted as a suggestion. |
| `syntax_error` | The patched file has more parse errors than the original (or does not parse, for Python). |
| `reanalysis_failed` | The originating analyzer still reports the finding, reports something new, or could not re-run. |
| `rubric_failed` | A verified LLM finding's fix failed the four-criteria rubric, or the verdict was unparseable. |
| `noop` | The replacement equals the text it replaces. |
| `redaction` | The replacement looked like a secret (or the redactor failed). |
| `oversized` | The replacement is longer than `MAX_REPLACEMENT_LINES`. |
| `out_of_range` | The range misses the finding's line, exceeds `MAX_RANGE_LINES`, leaves its hunk, or leaves the window. |
| `invalid_text` | The replacement contains a carriage return or NUL, which a suggestion cannot carry. |
| `overlap` | Two accepted fixes overlapped; only the better-ranked is kept. |
| `parse_error` | The model's reply was not the fixed JSON shape. |
| `timeout` | The LLM call timed out or the endpoint was unreachable, or the step timeout abandoned the attempt. |
| `fetch_failed` | The file content could not be fetched or decoded. |

A rejection is a safe outcome: the finding still posts, without a suggestion. Many rejections at the start is
normal; read the mix before tuning anything.

`reanalysis_failed` mixes the gate working with the re-run breaking, so read
`review_fix_generation_reanalysis_total{analyzer,result}` beside it: `rejected` is the gate working, `failed` means
the analyzer errored, timed out or is unknown, and `no_rule` means the finding had no `rule_id`.
`review_posting_comments_total{carried_suggestion}` counts posted inline comments with and without a fix, so
dismissal or feedback rates can be compared between the two.

### Alerts

Metric names are the OpenTelemetry names with dots as underscores, as on the dashboard.

- An analyzer whose re-run keeps breaking (every fix for its findings is rejected regardless of quality):
  `sum(rate(review_fix_generation_reanalysis_total{result="failed"}[1h])) by (analyzer) / sum(rate(review_fix_generation_reanalysis_total[1h])) by (analyzer) > 0.5`

- A failing endpoint, prompt or content fetch (the share of attempts ending in `parse_error`, `timeout` or
  `fetch_failed` over an hour; a third is the threshold):
  `sum(rate(review_fix_generation_outcome_total{outcome=~"parse_error|timeout|fetch_failed"}[1h])) / sum(rate(review_fix_generation_outcome_total[1h])) > 0.33`
- A broken multi-line correlation: after enabling, the Worker log line `posting: provenance capture
  correlation summary` should not appear with a sustained non-zero `unmatched_count`. A multi-line suggestion
  comment is anchored at its end line; if GitHub's echoed comments stopped matching, Memory would stop
  capturing fix comments (the review itself is unaffected). Alert on that line's rate where logs are queryable.

### Limits

- **One suggestion at a time.** Each fix is validated alone against the file as the PR has it. GitHub applies
  suggestions one at a time, but two accepted, non-overlapping fixes are never validated *together*; committing
  both can in principle interact. The reviewer sees each in context.
- **Re-pushes.** A new push produces a new review with new fix attempts. Nothing is cached or reused.
- **Languages.** TypeScript, JavaScript, PHP, Go and Python only (the syntax gate's reach). JavaScript is
  parsed with the TSX grammar, which can accept some invalid JavaScript; ESLint's re-run, which parses it as
  JavaScript, catches those.
- **Correctness bar.** An accepted fix passed the syntax gate and the analyzer's (or the rubric's) judgment. It
  is not run, tested or sandboxed; a reader still decides.
- **Applied rate is not measured.** GitHub sends no webhook for a committed suggestion. Memory marks comments
  that carried a suggestion so a person can classify them `accepted_fix` at approval; nothing reads those yet.

### Verifying a rollout

1. Deploy with `enabled: false`; confirm the migration applied (`\d posted_finding_comment` shows
   `carried_suggestion`).
2. Enable on one staging deployment or org. Open a PR with a known Ruff finding (for example an unused import).
3. Check the posted comment carries a `suggestion` block, the Worker log line `fix_generation completed` shows
   `accepted >= 1`, and the "Auto-fix suggestions" dashboard row shows `accepted` outcomes and `fix_generation`
   token usage.
4. Watch the rejection mix and the fix LLM spend for a week before widening.

### Rolling back

Set `REVIEW_WORKER_FIX_GENERATION_ENABLED=false` (`worker.fixGeneration.enabled: false`) and restart the
Workers: instant, no cleanup, and suggestions already posted remain ordinary GitHub comments. Reverting the code
as well (`helm rollback` or an image revert) leaves migration `0012` in place, unused; it need not be reversed.

## Walkthrough: switch, data flow, outcomes, alerts, cleanup, rollout, rollback

Context: [`docs/specs/spec-pr-walkthrough.md`](../specs/spec-pr-walkthrough.md) and
[`docs/plans/plan-phase-12.md`](../plans/plan-phase-12.md). The `walkthrough` step runs after `fix_generation`
(or `verification` when that is off) and before `output`. It asks the LLM to group the PR's changed files in
reading order, sanitizes the answer, and creates or edits **one** PR issue comment (found by a marker and the
GitHub App's identity). It passes its input through unchanged, so a walkthrough problem never costs the author
a review.

### What is sent where, and what it costs

- **Sent:** the list of reviewable file paths, status, change counts and a patch slice per file (capped at 2,000
  characters each, at most `MAX_FILES` files), plus the intent summary the review already computed. It goes to
  the same LLM endpoint and with the same posture as `intent_summary`: no new party receives anything.
- **Cost bound:** at most **one extra LLM call per reviewed push**, only for a PR with at least `MIN_FILES`
  reviewable files. Off by default for that reason. The bound is per job, not per PR: rapid pushes multiply
  calls, so set `REVIEW_WEBHOOK_API_REVIEW_ENQUEUE_RATE_LIMIT_PER_MINUTE` above `0` (see "Per-repo
  review-enqueue rate limiting is disabled by default" above) before enabling it on busy repos.
- **Fork PRs:** a fork PR's diff is sent like any other PR's; the comment is written with the App's installation
  token on the base repository, as the review is. Enable it knowing outside contributors' code reaches your LLM.
- **Latency trade-off (accepted):** the walkthrough runs before `output`, so findings and the check-run can
  arrive up to `STEP_TIMEOUT_SECONDS` later than without it (typically one LLM call). This is accepted so the
  step stays inside the fail-open pipeline; it is the reason the timeout is small.
- **Redaction is best-effort.** Posted text passes the secret redactor (Gitleaks) and a sanitizer that makes
  links, images, HTML, mentions and issue references inert. A secret shape Gitleaks does not know is not caught.

### Settings

Worker settings, mirrored in Compose (`.env`) and Helm (`worker.walkthrough.*`):

| Env var (`REVIEW_WORKER_WALKTHROUGH_`) | Helm value | Default | Notes |
| --- | --- | --- | --- |
| `ENABLED` | `enabled` | `false` | **Off by default**; a bootstrap setting, so changing it needs a Worker restart. |
| `STEP_TIMEOUT_SECONDS` | `stepTimeoutSeconds` | `45` | Must be at least `LLM_TIMEOUT_SECONDS + 15`; the step fails open after `value - 5` seconds. |
| `MIN_FILES` | `minFiles` | `2` | Fewer reviewable files: no call, no write (`skipped_small_pr`). Must be at least 1. |
| `MAX_FILES` | `maxFiles` | `60` | Files sent to the model; 1 to 200. The rest are counted in a "N more files not shown" line. |
| `MAX_CHARS` | `maxChars` | `6000` | Rendered comment ceiling, cut at a group boundary; 500 to 60000. |

Model choice is the Admin UI's model-routing row `walkthrough`. The companion settings are validated only when
`ENABLED` is true.

### Enabled but ineffective: the startup warning

The step is effective only when `ENABLED` is true **and** `review_generation_enabled` **and**
`secret_redaction_enabled` are both on (the redactor is what stops a secret reaching a public comment). If it is
not, the Worker starts normally, builds no walkthrough step, and logs at WARNING:

> `walkthrough_enabled is true but the walkthrough step is ineffective and will not run: it requires review_generation_enabled and secret_redaction_enabled`

Causes and fixes:

1. `REVIEW_WORKER_REVIEW_GENERATION_ENABLED=false`: turn review generation back on.
2. `REVIEW_WORKER_SECRET_REDACTION_ENABLED=false`: turn redaction back on. Never disable it to get a walkthrough.
3. The flag is mis-set so that the Worker does not see `true`: a wrong variable name, a Helm value under the
   wrong key, or a Worker not restarted after the change. **This one logs no warning**, because the Worker
   believes the step is off; confirm the effective value in the `step configuration valid` log line
   (`enabled_steps.walkthrough`).

In the ineffective state **no outcome metric is emitted** (the step does not exist). That is why the rollout
check greps for the warning rather than waiting for a metric.

### Outcome meanings

`review_walkthrough_outcome_total{outcome}` counts each step run once:

| `outcome` | Meaning |
| --- | --- |
| `posted` | The first walkthrough comment was created. |
| `updated` | The PR's existing comment was edited in place. |
| `skipped_small_pr` | Fewer than `MIN_FILES` reviewable files. Expected traffic. |
| `skipped_comment_limit` | The PR has more than 500 comments (five pages of 100) and none was the walkthrough, so none is written. Documented and accepted, **not alerted**; watch its panel during the pilot. |
| `llm_timeout` | The LLM call timed out, the endpoint was unreachable, or the step timeout fired during the call. |
| `parse_failure` | The model's reply was not the fixed JSON shape. |
| `github_error` | A comment list, create or edit call failed, or the publish deadline passed. |
| `dedupe_failed` | Two jobs raced to create the comment and convergence failed: a duplicate may remain (see cleanup). |
| `internal_error` | Anything else; fail-closed paths land here. |

`llm_timeout`, `parse_failure`, `github_error` and `internal_error` on any push after the first can **leave a
stale comment** in place: its footer names the commit it describes ("Describes commit `<sha>`"), so a reader can
tell. The next successful push edits it.

`internal_error` has known causes: a redactor or Gitleaks outage or timeout, and a multi-line scanner rule that
spans the sentinel lines on a hostile PR; both fail closed (nothing is posted). Tell causes apart by the `stage`
field the Worker logs with `walkthrough finished` (`validate`, `prompt`, `llm`, `parse`, `redact`, `sentinel`,
`render`, `publish`) and `exc_type`. For example, with the Worker's log stream in Loki:

```
{app="review-worker"} |= "walkthrough finished" | json | outcome="internal_error" | line_format "{{.stage}} {{.exc_type}}"
```

(Adjust the stream selector to your log labels.) Logs carry counts and identifiers only, never comment text.

`review_walkthrough_outcome_total` also carries a `stage` label, set only on `internal_error`, so a redaction
failure (`sentinel`) reads apart from input plumbing (`validate`) on the dashboard. Every non-ok
`walkthrough finished` log line carries `exc_type` and, when the failure was an HTTP error, `status_code`: a
`github_error` with `status_code=403` on every PR is a GitHub App missing `issues:write`, not a transient outage.

### Tuning metrics

- `review_walkthrough_files_not_shown_total{reason}`: files left out of the comment. `max_files` are files past
  `WALKTHROUGH_MAX_FILES` (never sent to the model); `max_chars` are files dropped to fit `WALKTHROUGH_MAX_CHARS`.
  `rendered_chars` is capped by that setting, so only this counter shows how much was cut.
- `review_walkthrough_reconciled_total{kind}`: `unknown_path` and `duplicate_path` are paths the model invented
  or repeated; `omitted` are files it left out (shown under "Other files"). Compare before and after changing the
  `walkthrough` model-routing row.

### Alerts

Starting values, tuned after a pilot. Metric names are the OpenTelemetry names with dots as underscores.

- **(a) Failing endpoint, redactor or GitHub, by share.** Skipped outcomes stay in the denominator; 6 is the
  minimum-volume guard:
  `sum(increase(review_walkthrough_outcome_total{outcome=~"llm_timeout|parse_failure|github_error|dedupe_failed|internal_error"}[1h])) / sum(increase(review_walkthrough_outcome_total[1h])) > 0.33 and sum(increase(review_walkthrough_outcome_total[1h])) >= 6`
- **(b) Orphan duplicates or an unupdated comment**, sustained for 1 h:
  `sum(increase(review_walkthrough_outcome_total{outcome=~"github_error|dedupe_failed"}[1h])) > 0`

Alert (b) fires for the outcomes that leave a duplicate or an unupdated comment. Alert (a) is what covers the
other stale-comment outcomes and a redactor outage, which can hide below the 0.33 share for a single tenant.

### Cleanup: finding and deleting a walkthrough comment

Every walkthrough comment starts with the marker `<!-- review-walkthrough:v1 -->` and is authored by the GitHub
App. To find them, list a repo's PR issue comments
(`GET /repos/{owner}/{repo}/issues/{pr}/comments`, or the repo-wide
`GET /repos/{owner}/{repo}/issues/comments`) and keep those whose body begins with the marker **and** whose
`performed_via_github_app.id` is the platform's App id. Delete one with
`DELETE /repos/{owner}/{repo}/issues/comments/{id}`. Do this for a bad or leaked comment, for a duplicate after
`dedupe_failed`, and after a sanitizer fix (the next push re-creates a clean one). The platform itself never
edits or deletes a marker comment that another author wrote.

### Stuck-job arithmetic, rollback and rollout

- The macro-step timeouts sum to **680 s** with the flag on (635 s plus the 45 s step), under the unchanged 700 s
  `stuck_job_warning_threshold_seconds`. An operator override below 680 s, or raising `llm_timeout_seconds`
  above 30 with the flag on, fails Worker startup naming the setting. Both validators key on the raw flag, so
  they apply even when the step is ineffective.
- **Rollback is the flag:** set `REVIEW_WORKER_WALKTHROUGH_ENABLED=false` (`worker.walkthrough.enabled: false`)
  and restart the Workers. It is a restart, not a live toggle. Already-posted comments stay in place; delete them
  as above if wanted. No migration is involved.
- **Rollout check:** after enabling and restarting, confirm the "enabled but ineffective" warning is **absent**
  and `enabled_steps.walkthrough` is `true`; open a two-file PR and confirm one comment appears and a second push
  edits it; then watch the "PR walkthrough" dashboard row for a week before widening.

### Manual release gate (REQUIRED, not yet recorded)

Run on a throwaway test-org repo with the real GitHub App. **A recorded result of "passed" for both items is
required before the flag is recommended to any customer, and both must be re-run after any change to
`neutralize`.** Like the other unexercised real-repo checks this is a release gate, not a CI gate.

| # | Check | Result |
| --- | --- | --- |
| 1 | The App's existing permissions allow creating, editing and deleting its own PR comments, and the real `performed_via_github_app` shape matches the recorded Phase 2 cassette. | **Not run. Required.** |
| 2 | The hostile-fixture PR from Phase 1 renders with no active link, image, notification or cross-reference. | **Not run. Required.** |
