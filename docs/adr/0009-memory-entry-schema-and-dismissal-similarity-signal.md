# ADR: Memory entry schema and dismissal-derived similarity signal

## Status

PROPOSED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Phase 4 (Memory) needs two things `c4-architecture.md` left open: a concrete column-level schema for Memory entries, and a concrete mechanism for design goal 6's "similarity check against past-dismissed findings" half of verification (the fixed-rubric half already exists in `review/verification.py`; the similarity-check half has never been built). This ADR decides both: Memory entries are captured deterministically (raw signal only, no auto-inferred meaning) and classified by a human at approval time; the similarity check itself is a deterministic, LLM-free, repo-scoped embedding comparison that reuses Phase 2's existing bi-encoder; and reaction capture — which GitHub does not support via webhook at all — is handled by opportunistic polling rather than a new scheduler.

## Context

Two items are marked "Open" in `c4-architecture.md` for Phase 4:

1. Exact column-level schema for Memory entries (status/provenance/approver/timestamps — design goal 9).
2. How a dismissal becomes the similarity-check signal design goal 6 already commits the platform to using: *"Verification always runs as a distinct call against a fixed rubric **or** a similarity check against past-dismissed findings — never a 'please double-check yourself' follow-up."*

`review/verification.py` today implements only the fixed-rubric half of design goal 6 (a four-criterion LLM judgment call). The similarity-check half has never been built — this ADR is what finishes design goal 6, not just Phase 4's own design goal 9.

Relevant existing state, confirmed by reading the code:

- `adapter/github/posting.py` posts PR review comments statelessly today — it persists no comment ID anywhere. Capturing inbound signal on a posted comment requires it to start persisting a comment-to-finding mapping at post time, which it does not do now.
- `adapter/github/triggers.py` only handles `pull_request` and `push` webhook events today. Replies (`issue_comment`, `pull_request_review_comment` "created" actions) and thread-resolution (`pull_request_review_thread`, actions `resolved`/`unresolved`) both have real GitHub webhook events, confirmed via GitHub's own webhook documentation, and need new end-to-end subscriptions/handling.
- **GitHub does not fire any webhook when a reaction is added or removed on a comment** — confirmed via [GitHub community discussion #7168](https://github.com/orgs/community/discussions/7168) and GitHub's webhook-events-and-payloads docs. This is a hard platform limitation, not a gap in our design, and it means reactions cannot be captured through the same webhook path replies and thread-resolution use.
- `adapter/embedding/local_onnx.py` already provides a bi-encoder embedding client (`LocalONNXEmbeddingClient`, 384-dim, pgvector-backed) used today for Phase 2 indexing (AST chunk embeddings) — the natural reuse for a similarity check, keeping Memory LLM-free by default per [ADR-0007](0007-llm-free-indexing-deferred-rag-enrichment.md)'s precedent.
- `adapter/storage/models.py` already establishes the `org_id`/`repo_id`/`team_id` nullable-FK "narrow the scope" convention (`OrgRepoTeamSetting`, `ModelRoutingConfig`) that Memory's scoping should follow rather than inventing a new pattern.
- `findings/models.py`'s `Finding` model (`file`, `line`, `severity`, `message`, `source`, `rule_id`, ...) is the only shape available on both a historical dismissed finding and a new candidate finding at verification time — so a similarity comparison has to be over that fingerprint, not diff/code context.
- [ADR-0004](0004-multi-org-tenancy.md) already leaves org-level RBAC as an explicitly open question (Phase 6 territory). This ADR does not attempt to resolve that; it defaults to the tightest safe scope instead.

## Options considered

**Signal classification (how a captured reaction/reply/thread-resolution becomes a labeled Memory entry):**

| Option | Reliability | Matches design goal 9 ("never silent") | Verdict |
| --- | --- | --- | --- |
| Deterministic capture only; human assigns meaning at approval | High — no inference to get wrong | Yes — a human always decides what a signal means | **Accept** |
| Keyword/sentiment heuristics at capture time | Low — nuanced replies routinely misclassify | No — a misclassified pending entry is a silent mislabel until a human happens to catch it | Reject |

**Similarity search scope:**

| Option | Consistency with ADR-0004 | Blast radius if wrong | Verdict |
| --- | --- | --- | --- |
| Repo-scoped only | Matches ADR-0004's still-open RBAC status — adds no cross-team exposure | Contained to one repo | **Accept** |
| Org-wide by default | Assumes an RBAC story ADR-0004 explicitly hasn't decided yet | A team's dismissal signal could suppress another team's finding with no access control behind it | Reject (for now) |

**Action on a similarity match:**

| Option | Cost | Fits design goal 6's own framing | Verdict |
| --- | --- | --- | --- |
| Deterministic auto-suppress (no LLM call) | Free — vector comparison only | Yes — design goal 6 frames the similarity check as an alternative verification mechanism, not extra context | **Accept** |
| Feed match as context into the existing rubric LLM call | An LLM call plus non-deterministic judgment on what should be a deterministic check | No — reintroduces the LLM into a check design goal 4's "free layer" ethos says shouldn't need one | Reject |

**Entry type taxonomy:**

| Option | Future migration cost | Cost today | Verdict |
| --- | --- | --- | --- |
| Model all three types now (`dismissed_finding`/`accepted_fix`/`correction`), consume only one | None later | An unused enum value | **Accept** |
| Model `dismissed_finding` only | A real migration against live data whenever a later phase needs the others | Slightly simpler now | Reject |

**Reaction capture, given no webhook exists:**

| Option | New infra required | Coverage | Verdict |
| --- | --- | --- | --- |
| Opportunistic poll piggybacked on existing job triggers | None — reuses jobs that already touch the PR | Partial (only PRs that get a later push) | **Accept** |
| Dedicated polling scheduler | A wholly new time-based job paradigm, ongoing API volume | Complete | Reject — disproportionate to a secondary signal |
| Drop reaction capture from Phase 4 | None | None | Reject — unnecessary given free partial coverage is available |

## Decision

1. **Capture is deterministic only, never sentiment/keyword classification.** When a reply, thread-resolution, or reaction (decision 5) is observed on a platform-posted comment, the GitHub Adapter's write path records only the raw signal — which comment, the raw signal type (`reaction`/`reply`/`thread_resolution`), and the raw content (reaction emoji, reply text, resolved boolean) — as a Memory entry with `status=pending` and `entry_type` unset. It never infers dismissal/acceptance/correction from the text or reaction itself. A human assigns `entry_type` (`dismissed_finding` | `accepted_fix` | `correction`) as part of the admin API's approve action. Keyword/sentiment heuristics were rejected as too fragile to trust with design goal 9's "never a silent black box" requirement.

2. **Similarity search is repo-scoped only for Phase 4, not org- or team-wide.** The similarity check (decision 3) only ever compares against approved `dismissed_finding` entries from the same repo. Org/team-wide sharing is deferred, not built, until Phase 6 resolves the RBAC question ADR-0004 left open. This can widen later as a pure query-scope change — no schema change required.

3. **A similarity match above threshold against an approved `dismissed_finding` entry deterministically auto-suppresses the candidate finding — no LLM call.** A candidate LLM finding's fingerprint (`file` + `rule_id` + `message`) is embedded with the same `LocalONNXEmbeddingClient` Phase 2 indexing already uses, compared via pgvector cosine similarity against approved, repo-scoped `dismissed_finding` entries (weighted by the entry's own ranking-time decay per the C4 doc's half-life model), and a match above threshold routes the finding through the exact same rejection path `review/verification.py` already uses for a rubric-rejected finding. Only `approved` entries are ever read — `pending` never influences a real review (design goal 9).

4. **The Memory entry schema models all three entry types now** (`dismissed_finding`, `accepted_fix`, `correction`) via an `entry_type` column, even though Phase 4 only builds consumption logic for `dismissed_finding`. `accepted_fix`/`correction` entries are captured and stored but unread — an explicit, named gap for a later phase, not an oversight. Adding the column later would be a real migration against live data; an unused enum value today costs nothing.

5. **Reactions are captured via opportunistic polling piggybacked on existing job triggers**, not a new webhook subscription (none exists for reactions) and not a new dedicated polling scheduler. A later push triggering a new `REVIEW` job on the same PR fetches current reactions on that PR's previously-posted comments (via the comment-provenance mapping from the `posting.py` change below) as a side effect of work already happening. Replies and thread-resolution use real webhook subscriptions instead (`issue_comment`/`pull_request_review_comment` "created"; `pull_request_review_thread` `resolved`/`unresolved`), since those webhooks genuinely exist. **Named gap:** a PR that never receives a second push after the platform's own comment will never have its reactions captured — accepted, not silent.

## Consequences

**Easier / positive:**
- Finishes design goal 6 in full (rubric + similarity check), not just Phase 4's own design goal 9.
- No new embedding model, no new LLM spend for the similarity check — reuses Phase 2's existing bi-encoder end to end, consistent with ADR-0007.
- No new job paradigm, no new scheduler — reaction capture reuses the existing event-triggered job model.
- Memory's scoping and entry-type extensibility follow patterns (`org_id`/`repo_id`/`team_id` nullable FKs, forward-compatible enums) already established elsewhere in the schema, rather than inventing new ones.
- Human-in-the-loop classification means no fragile text/sentiment inference to maintain or get wrong.

**Harder / trade-offs:**
- `adapter/github/posting.py`, an existing Phase-1-era component, must change from stateless posting to persisting a comment-to-finding provenance mapping (new table, e.g. `posted_finding_comment`: comment id, repo, PR, finding fingerprint, commit sha, created_at) — a necessary consequence of decisions 1/3/5, since nothing can correlate an inbound signal back to the finding it's about without it.
- Reaction capture has structurally incomplete coverage: PRs with no follow-up push never surface their reactions. This is accepted, not solved.
- `accepted_fix`/`correction` entries accumulate in storage with no consumer until a later phase is scoped to read them.
- Repo-scoping the similarity check means a dismissal in one repo never helps a sibling repo in the same org, even where that might be genuinely useful — deliberately deferred pending Phase 6's RBAC design, not a permanent limitation.

## Related

- [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) — design goals 4, 6, and 9; the Level 2 Memory bullets this ADR makes concrete.
- [ADR-0004: Multi-org tenancy](0004-multi-org-tenancy.md) — leaves org-level RBAC open; this decision defers to it rather than resolving it.
- [ADR-0007: LLM-free indexing, deferred RAG enrichment](0007-llm-free-indexing-deferred-rag-enrichment.md) — precedent for reusing the bi-encoder embedding client and staying LLM-free by default.
- [`docs/roadmap.md`](../roadmap.md) — Phase 4 section this ADR unblocks.
