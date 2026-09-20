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
