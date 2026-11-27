# Operations runbook

Operator-facing gotchas, one entry per interaction an operator can trip over,
extended as new ones appear. Linked from
[`README.md`](../../README.md)'s "Helm chart" section.

## Worker restart-looping after upgrade? Check startup logs for a fail-fast check first

`docker-compose.yml`'s `worker` service uses `restart: unless-stopped` so the
Worker stays up across its own long-running dispatch loop's lifetime. (It
used to be `restart: "no"`, where a genuine startup failure, e.g. the LLM
endpoint unreachable, exited the container once, non-zero, and stayed
exited.) The trade-off: the exact same
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
trade-off, not an oversight. The cost-exhaustion reasoning:
`synchronize` is a trigger, so an attacker-controlled PR can push repeatedly
to drive unbounded LLM spend with no per-repo bound at this setting's shipped
default. Set
`REVIEW_WEBHOOK_API_REVIEW_ENQUEUE_RATE_LIMIT_PER_MINUTE` above `0` (`10` is
a reasonable starting point) to activate it — a single
config value, no code change. A startup `warning` log states plainly
whether this is active in a given deployment.

## Upgrading via Helm when this release includes new migrations

`charts/review/templates/` has no migration `Job`/hook yet (unlike
`docker-compose.yml`'s `migrate` service, which gates `webhook_api`/
`worker` startup on `alembic upgrade head` succeeding first) — a real gap,
first made load-bearing by the first migration after the baseline. Until a
proper migration Job/hook exists: run `alembic
upgrade head` manually — via `kubectl exec` into a migration-capable pod,
or a one-off `Job` using the same image — **before** `helm upgrade` rolls
out the new `webhook_api`/`worker` pods, whenever a release includes new
migrations. Skipping this risks the new pods starting against a schema that
doesn't yet have the columns/tables they expect.

## Worker restart-looping after upgrading to a release with GitHub App credentials? Check `github_app_id`/`github_app_private_key`

The Worker has two required `WorkerSettings` fields — `github_app_id`
and `github_app_private_key` — with no default, since
`adapter/github/auth.py`'s JWT-signing/token-exchange flow (which
diff-fetch depends on) cannot function without them. An operator upgrading
from a deployment that predates them must populate both
(`REVIEW_WORKER_GITHUB_APP_ID`/`REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY` in
Compose, or `github.appId`/`github.appPrivateKey` in the Helm chart's
`values.yaml`) before the Worker will start. Per this runbook's first
entry above, an unset or malformed value now presents as a restart loop,
not a single failed container — check the startup logs for "GitHub App
private key is not usable at startup; exiting" (a malformed/truncated key,
caught by the fail-fast RS256 signing validation) or a
`pydantic` field-required error naming `github_app_id`/`github_app_private_key`
(unset entirely) before assuming a deeper problem.

## Upgrading to a release with Memory? Existing GitHub App installations need to re-consent

Memory adds two new webhook event
subscriptions to the GitHub App manifest —
`"pull_request_review_comment"` and `"pull_request_review_thread"` — on
top of the already-subscribed `"pull_request"`/`"push"`. These activate
reply/thread-resolution capture and reaction polling,
both of which are safe to run without these subscriptions since they
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

- The rest of Memory keeps running completely unaffected — provenance
  recording, the admin API, and similarity-based suppression all continue
  to work exactly as before.
- Reaction polling still partially works, since it reuses the
  already-subscribed `pull_request` event rather than a new subscription.
- Replies and thread-resolution capture (US-M1/US-M2) simply
  receive no events — not an error state, just no new data.

Re-consenting is itself a reversible, operator-initiated action: declining
the updated permissions (or re-consenting and later reverting) leaves
the rest of Memory running unaffected either way. `"issue_comment"` is
deliberately **not** part of this subscription change — an `issue_comment`
event can never correlate back to a specific posted finding comment (only
`pull_request_review_comment`'s `in_reply_to_id` can), so subscribing to
it would only add unused webhook volume with no corresponding capture
capability.

## Product-knowledge provider configured but not contributing? Read the `outcome` of `review.product_knowledge.call_total`

Part of the optional external product-knowledge augmentation
([ADR-0013](../adr/0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md)).
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

## Spec-compliance review or smell findings misbehaving? Read the `spec_review` outcome, or switch them off

(Spec: [`spec-smell-and-spec-review.md`](../specs/spec-smell-and-spec-review.md)).

**What it does.** `spec_review` runs between `review_generation` and
`verification`. It picks one spec document for the PR (a spec-like `.md` the PR
changes, else one in the repo at the PR head, matched on the branch name) and,
if exactly one matches, makes one extra LLM call. Its findings (`category =
"spec"`) go through `verification` like any other. No match or an ambiguous
match means the step is skipped with no LLM call.

**What leaves the deployment.** The spec text (capped at 20,000 characters) and
the diff go only to the LLM endpoint you already configured, as the diff does
today. Nothing else is contacted besides the GitHub API calls the Worker
already makes (one tree listing and one file read at most).

**Outcomes.** Read the `outcome` attribute of
`review.spec_review.resolution_total`: `found_in_diff`, `found_in_repo`,
`none`, `ambiguous`, `fetch_failed`, `degraded`. `fetch_failed` (GitHub tree or
file error, including a repo too large to list) and `degraded` (LLM timeout,
LLM error or unparseable response) log a `warning` with the error type only and
leave the ordinary review untouched. A review is never failed by this step.

**Diagnosing a failing check.** The `reason` attribute on the same counter says
which: `tree_truncated` (the repo is too large to list; the check can never run
there, so switch it off for that deployment or accept it), `fetch_error`
(GitHub unreachable, transient), `timeout` (raise
`REVIEW_WORKER_SPEC_REVIEW_INTERNAL_TIMEOUT_SECONDS`, keeping it below the step
timeout), `parse_error` (the model's output drifted from the expected JSON),
`llm_error` (the endpoint failed). `review.spec_review.resolution_duration_ms`
shows what the lookup costs per outcome, and
`review.spec_review.findings_dropped_total{reason}` separates "the spec was met"
from "every quote was rejected" (`quote_rejected`) or cut by the cap
(`over_cap`). To judge whether smell or spec findings are noisy, compare
`review.verification.rejected_total` to `judged_total` by `category`; to see
what the posted-findings ceiling cuts, read
`review.findings.dropped_by_ceiling_total` by `category`.

**Kill switches (restart the Worker).**
`REVIEW_WORKER_SPEC_REVIEW_ENABLED=false` removes the step;
`REVIEW_WORKER_SMELL_BASELINE_ENABLED=false` removes the smell section from the
review prompt. Spec review is also skipped when `verification` is disabled,
because spec findings are never posted unverified.

**Timeouts.** `REVIEW_WORKER_SPEC_REVIEW_INTERNAL_TIMEOUT_SECONDS` (default 45)
must stay below `REVIEW_WORKER_SPEC_REVIEW_STEP_TIMEOUT_SECONDS` (default 60),
or the Worker refuses to start. The step timeout is part of the stuck-job
threshold sum, now 575 seconds, so
`REVIEW_WORKER_STUCK_JOB_WARNING_THRESHOLD_SECONDS` defaults to 660 (it was
600); a custom value below the sum also fails startup.

## Switching the review-quality v2 bundle on or off, and watching for empty reviews

(Spec: [`spec-review-quality.md`](../specs/spec-review-quality.md)).

**What it is.** One Worker setting, `REVIEW_WORKER_REVIEW_QUALITY_V2_ENABLED`
(default `false`), switches the whole bundle together: the v2 review prompt, the
rule that a `medium`-or-higher finding needs a `scenario`, the extra
`not_style_only` verification criterion, and the new posted layout (tiers, one
collapsed summary, root-cause dedupe, the fence-safe agent prompt). The value is
read once at Worker start. Each job copies it at the start of the job, so a job
never changes version halfway.

**Flip it.** Set the variable to `true` (or `false` to roll back) and restart
the Workers. Jobs claimed before the restart that did not finish are never
resumed (nothing reclaims a `CLAIMED` job), so no job runs under mixed versions.

**Verify it.** The Worker logs `review job context` with
`review_quality_version` = `v2` (or `v1`) for every review job. In v2, read
`review.findings.posted_by_tier_total{tier}` (inline comments),
`review.findings.collapsed_total{reason}` (`nit`: listed in the collapsed summary
for an org that opted in; `nit_hidden`: dropped because the org did not opt in;
`overflow`: over the inline cap, listed in the collapsed summary),
`review.findings.deduped_total{kind}` and
`review.findings.dropped_by_ceiling_by_tier_total{tier}`. Every root-cause merge
also logs `findings-merge: root-cause duplicate folded` with the two locations.

**Nits.** The org setting `findings_show_nits` (`{"enabled": true}`, org scope,
set through the config API) makes nit-tier findings appear in the collapsed
summary, never inline. Absent or invalid means hidden. A settings-store error
also means hidden, logged as `findings_show_nits lookup failed`.

**Alert (starting value).** An empty review otherwise looks like a clean
PR. Alert when, over one hour,
`sum(rate(review_generation_dropped_total{reason="missing_scenario"}[1h]))`
is more than 30% of the `medium`-or-higher findings the model produced
(`review.findings.produced_total` with `severity` is not labelled, so use the
ratio to `sum(rate(review_findings_produced_total[1h]))` as a proxy). Check the
metric names your exporter produces; dots become underscores in Prometheus. A
sustained breach means the model is not following the v2 prompt: roll back to v1
and re-record the eval cassettes (`python -m review.eval.record`).

## Classifier provider (`classifier_provider`): one shared operator key

The classifier credential is one operator-provisioned entry in the same credential
map as product knowledge (`ref` bound to the provider host, for example `typesafe` bound
to `api.typesafe.ai`). It is **a single operator key with a shared quota**: any org
admin can select the ref for their own org, and every org's classifier calls, and
every Admin test-connection probe, spend that one quota. One org's large pull requests
can therefore crowd out others. The Admin API probe limiter (6 per minute per session,
user and org, 30 per minute per pod) is in memory and per pod.

The setting is org scope only, off by default, and `enabled` saves only after the
admin accepts the egress disclosure in the panel; the Admin API refuses
`enabled: true` without `disclosure_acknowledged`. The disclosure says only that
secrets detectable by the vendored ruleset are redacted before the finding text and
diff hunk are sent, and that the provider's retention is not verified by this product.
Roll back by deleting the org's `classifier_provider` setting (absent means no
classifier). The Worker runbook follows.

### Classifier runbook (Worker)

Metric names below are the Prometheus forms (dots become underscores); check what
your exporter produces. Every metric has closed-set labels and never carries
finding text, org ids or endpoints.

**How a job picks its judge.** `VerificationStep` asks the resolver once per job.
No enabled `classifier_provider` row for the org means the LLM judge, exactly as
before. A setting whose credential ref no longer resolves keeps its mode but falls
back to the LLM judge (in `active` mode, counted per finding as
`reason="credential_unresolved"`; in `shadow` mode the classifier never decides, so
it logs one warning per job and counts nothing). A setting
read that fails or takes over 2 s is treated as no setting. Mode and credential
are read per job, so a change takes effect on the next job without a restart. The
Worker has four pinned limits (env names `REVIEW_WORKER_CLASSIFIER_*`): call
timeout 10 s, 8 concurrent calls and 4 concurrent redaction scans per Worker
process, and an 8000-byte ceiling on the text sent per finding. The Worker reaches
public hosts only (`REVIEW_WORKER_CLASSIFIER_ALLOW_PRIVATE_NETWORKS` is for the
Compose E2E).

**Rotating the credential.** Same steps as product knowledge, "Rotating a
credential" above: update the Secret that holds the credential map, then roll the
Worker and the Admin API. Keep the same `ref` and `allowed_hosts` so no org
setting needs a change. Until a Worker restarts it uses the old token; if the old
token is already revoked, expect the 401 storm below until the roll finishes.

**A TypeSafe outage** (`outcome="unreachable"` or `"rate_limited"` rising). Nothing
needs doing for correctness: each affected finding goes to the LLM judge and the
job succeeds. Five consecutive 429 or 529 answers open a per-process breaker for
30 s (`outcome="rate_limited"`, no network call). To stop spending time on a long
outage, set the org's mode to `shadow` or delete its setting (effective next job).
Latency for affected jobs rises by at most the call timeout.

**A 401 or 403 storm** (`outcome="unauthorized"`). The provider rejects the shared
key: it was rotated at the provider, revoked, or the Secret holds a wrong value.
Each rejected call logs one `classifier provider rejected the credential` error
line (no token). Findings still go to the LLM judge. Fix the key per "Rotating the
credential". Check one Worker first: all Workers share the Secret, so a storm on one
Worker only means a bad rollout on that pod.

**Flipping `review_quality_v2_enabled`.** It is independent of the classifier: see
"Switching the review-quality v2 bundle on or off" above (restart the Workers).
Under v2 the classifier asks 5 questions per finding instead of 4 (the extra one is
`not_style_only`). Flip it only when no org is mid-way through a shadow review, or
note the flip date in the shadow report, because agreement is then measured on two
different rubrics.

**Queries.**

- Fallback rate in active mode (all orgs on this Prometheus):
  `sum(rate(review_verification_llm_fallback_total[1h]))
  / (sum(rate(review_classifier_verdict_total{criterion="valid"}[1h]))
  - sum(rate(review_verification_llm_fallback_total{reason=~"error|redaction_failure|credential_unresolved"}[1h])))`.
  The denominator counts findings the classifier answered (every finding gets a
  `valid` verdict) plus findings that never got one. Shadow-mode orgs also add
  verdicts, so with many shadow orgs this understates the rate; filter by the
  `service`/`pod` labels your exporter adds if you need an active-only view.
  Break down by `reason`(`uncertain`,`error`,`high_severity_no`,
  `spec_category`,`redaction_failure`,`credential_unresolved`) to see why.
- Per-criterion agreement in shadow mode:
  `sum by (criterion) (increase(review_classifier_shadow_agreement_total{outcome="agree"}[24h]))
  / sum by (criterion) (increase(review_classifier_shadow_agreement_total{outcome=~"agree|disagree"}[24h]))`.
  Also read `outcome="uncertain"` and `outcome="error"` per criterion: a high
  agreement with many `uncertain` verdicts means the classifier decides little.
- Call health: `sum by (outcome) (rate(review_classifier_calls_total[5m]))` and the
  `review_classifier_call_duration_ms` histogram.
- Slot pressure: `review_classifier_in_flight{resource}` near its limit (8 and 4)
  for long periods means jobs queue for the shared semaphores.

**Alert rules (starting values; tune after shadow data).**

| Alert | Condition | Action |
| --- | --- | --- |
| Classifier fallback rate high | The fallback rate above is over 20% for one hour | Read `reason`; if `error`, see outage or 401; if `uncertain` or `high_severity_no`, the classifier is not deciding enough to pay off: set the org to `shadow` |
| Classifier credential rejected | `increase(review_classifier_calls_total{outcome="unauthorized"}[10m]) > 0` | 401 storm above |
| Missing-scenario drops high | See "Switching the review-quality v2 bundle" above (30% of medium and higher findings over one hour) | Roll back v2 |

### Shadow rollout procedure

The classifier is never switched straight to `active`. Follow these steps for the
first org and again for any later org the owner chooses.

1. Provision the credential (rotation steps above), then in the Admin UI save the
   org's `classifier_provider` with `mode: shadow` after accepting the egress
   disclosure. Press Test connection and expect `ok`.
2. Leave it in `shadow` for at least one full review cycle of that org. The owner
   chooses the volume (number of pull requests) and records it in the shadow
   report. In `shadow` the LLM judge decides every finding; the classifier result
   is only compared.
3. Write the shadow report: per-criterion agreement (query above), `uncertain`
   and `error` shares, call latency and token use against the LLM judge, and the
   adversarial corpus result (`review/testdata/classifier-adversarial-corpus.json`
   structure test plus any live re-run). Real-model verdict stability under
   injected author text is measured only by this live data; the committed corpus
   proves the structure, not the model.
4. The owner reads the report and decides whether to switch the org to `active`.
   **This decision is open; it has not been made.** Nothing in the code or this
   document switches any org to `active`. Record the decision, the date and the
   report link in your own change records.
5. Back out at any time by setting the mode back to `shadow` or deleting the
   setting; it takes effect on the next job.
