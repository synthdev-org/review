"""Configuration for the Worker entrypoint, parsed once here via pydantic-settings.

Per docs/rules/architecture.md's Decoupling section: config is parsed once at
each entrypoint's composition root and passed down explicitly — domain and
adapter code never read env vars, Kubernetes Secrets, or files directly.

Phase 3 of the delivery plan extends this from Phase 1's near-empty model
with the LLM Gateway's bootstrap config (endpoint + credential fields), per
the C4 doc's "Configuration & Secrets" section: the LLM endpoint is
bootstrap config, read once here, never sourced from Postgres. The Webhook
API has no equivalent fields — per the C4 doc's Level 3 diagram, the LLM
Gateway is "used by Worker only."

Phase 2 of the delivery plan adds the pipeline step framework's own
bootstrap config: one enable/disable boolean per macro step and one
timeout-in-seconds field per macro step, plus one shared timeout field
covering both of the static-analysis step's own analyzer sub-steps (see
``review/config.py``'s ``StepEnablement``/``StepTimeouts`` docstrings for
the full reasoning). ``apps/worker/main.py``, the composition root, reads
these fields once here and constructs one ``StepEnablement`` and one
``StepTimeouts`` instance from them.

Phase 3 of the delivery plan adds the storage DSN (``database_url`` — the
same bare ``REVIEW_DATABASE_URL`` variable
``apps/webhook_api/settings.py``'s own field already reuses, per that
phase's "Composition-root wiring for storage" section) and the dispatch
loop's own two bootstrap fields: ``poll_interval_seconds`` (how often to
poll ``JobQueueRepository.claim_next()`` when the queue is empty) and
``stuck_job_warning_threshold_seconds`` (the plain, summed-worst-case-plus-
headroom threshold past which a still-running job gets a ``warning`` log —
see ``apps/worker/dispatch.py``'s own docstring for the full reasoning
behind both defaults).

Phase 4 of the delivery plan adds the GitHub App server-to-server
auth/diff-fetch bootstrap config — moved here from where an earlier plan
draft placed it (Phase 7), since this phase is the first to make real
GitHub API calls under the App's identity load-bearing (see
``adapter/github/auth.py``'s own docstring for the full "why here"
reasoning): ``github_app_id``/``github_app_private_key`` (required, no
default — diff-fetch cannot function without them, and the Worker's
fail-fast startup check, extended this phase, refuses to boot with them
unset or unusable), ``github_api_base_url``/``github_api_timeout_seconds``
(reusing the exact pattern/default ``apps/webhook_api/settings.py``'s
``WebhookAPISettings`` already established for the same purpose), and
``github_retry_max_attempts`` (the GitHub-side retry/backoff policy's own
configurable lever — see ``adapter/github/http.py``'s module docstring).

Phase 5 of the delivery plan adds the static-analysis step's own two
analyzer sub-step enablement flags (``static_analysis_gitleaks_enabled``,
``static_analysis_semgrep_enabled`` — extending ``StepEnablement`` the same
way the four macro-step flags already do, per US-F4) and the independent
``secret_redaction_enabled`` flag (default ``True`` — see
``findings/redact.py``'s module docstring for the full reasoning: this
gates ``findings.redact.TextRedactor``'s own wiring, decoupled from
``static_analysis_gitleaks_enabled`` on purpose, since disabling the
Gitleaks *scanner* sub-step for noise reasons must never silently disable
redaction of a secret the LLM, Phase 6, echoes back verbatim into its own
finding prose).

Phase 6 of the delivery plan adds ``llm_max_diff_bytes`` (the llm-review
step's own diff-size prompt ceiling — see ``review/llm_review.py``'s module
docstring: diff size is fully PR-author-controlled and directly drives LLM
token cost, so an oversized PR truncates rather than sending an unbounded
prompt). No new LLM-endpoint fields: ``llm_endpoint``/``llm_api_key``/
``llm_model``/``llm_timeout_seconds`` (Phase 3, above) are reused unchanged
— this phase is the first real *caller* of the already-built
``llmgateway.client.LLMClient``/``LiteLLMClient``, not a second consumer
needing its own config surface.

Phase 7 of the delivery plan adds ``dry_run`` (``REVIEW_WORKER_DRY_RUN``,
per ``docs/specs/phase-1-review-pipeline.md``'s Rollout & Release Safety
section's "decided" dry-run design) — a single bootstrap flag, not
per-step granularity (Phase 1 has exactly one step with an external side
effect to suppress). Read once here at Worker startup and set on Phase 2's
``StepContext`` for every claimed job (``apps/worker/dispatch.py``), not
read directly by ``review/output.py`` from settings — see that module's
own docstring for why threading it through ``context`` rather than reading
settings inside the step keeps this a general mechanism a future
side-effecting step can reuse for free, not a one-off escape hatch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Bootstrap settings for the Worker process.

    `environment` has no default on purpose: an entrypoint that silently
    defaulted it could boot as "development" in production by accident.
    The LLM fields *do* default — to the local LM Studio instance Phase 3's
    Docker Compose target points at (per the C4 doc's Level 3 diagram) —
    because, unlike `environment`, an LLM misconfiguration is caught by
    this phase's own fail-fast startup connectivity check
    (`llmgateway.startup_check.check_llm_reachable`, wired in
    `apps/worker/main.py`) rather than silently running in a wrong mode; a
    production deployment overrides these via Helm `Secret`/`existingSecret`
    values same as any other bootstrap config.

    `llm_api_key` is `pydantic.SecretStr` per the plan's Security review
    ("GitHub App private key, webhook secret, and any LLM API key ... must
    never appear in logs") — never logged or `repr()`-able by accident.
    """

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"]
    log_level: str = "INFO"

    # --- Storage (Phase 3, adapter/storage) ---
    # Sourced from the bare REVIEW_DATABASE_URL env var, not this class's own
    # REVIEW_WORKER_ prefix — the same variable name WebhookAPISettings'
    # own `database_url` field already reuses (Phase 1) and that Phase 0's
    # `migrate` Compose service and both Helm Deployments already inject.
    # Required: the dispatch loop (apps/worker/dispatch.py) cannot claim or
    # mark jobs without a database.
    database_url: SecretStr = Field(validation_alias="REVIEW_DATABASE_URL")

    llm_endpoint: AnyHttpUrl = AnyHttpUrl("http://host.docker.internal:1234/v1")
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = "local-model"
    llm_timeout_seconds: float = 30.0

    # Pipeline step framework (Phase 2, docs/plans/plan-phase-1-walking-skeleton.md):
    # one enable/disable boolean per macro step, mirroring `review.config.StepEnablement`'s
    # field set exactly — `apps/worker/main.py` builds that model from these.
    prefilter_enabled: bool = True
    static_analysis_enabled: bool = True
    llm_review_enabled: bool = True
    output_enabled: bool = True
    # Phase 5: the static-analysis step's own two analyzer sub-steps,
    # toggled independently per US-F4 — mirrors review.config.StepEnablement's
    # own field set exactly.
    static_analysis_gitleaks_enabled: bool = True
    static_analysis_semgrep_enabled: bool = True

    # One timeout-in-seconds field per macro step, plus one shared field covering both of
    # the static-analysis step's own analyzer sub-steps — see `review.config.StepTimeouts`'s
    # docstring for why the analyzer sub-steps share a field rather than getting one each,
    # and the plan's Phase 2 section for the reasoning behind each default below.
    prefilter_step_timeout_seconds: int = 30
    static_analysis_step_timeout_seconds: int = 240
    static_analysis_analyzer_timeout_seconds: int = 60
    llm_review_step_timeout_seconds: int = 90
    output_step_timeout_seconds: int = 30

    # --- Job dispatch loop (Phase 3, apps/worker/dispatch.py) ---
    # How often to poll JobQueueRepository.claim_next() when the queue is
    # empty — a concrete default, not left unspecified, per the plan's
    # Phase 3 section.
    poll_interval_seconds: float = 2.0
    # The four macro steps run strictly sequentially (see each phase's own
    # "Why here" note), so their worst-case healthy-run duration is the sum
    # of their own configured timeouts above, not their max: 30
    # (prefilter) + 240 (static_analysis) + 90 (llm_review) + 30 (output) =
    # 390 seconds. 450 gives that sum roughly 15% headroom — comfortably
    # longer than the worst healthy-run total without leaving a genuinely
    # stuck job unwarned for an unreasonable stretch.
    stuck_job_warning_threshold_seconds: float = 450.0

    # --- GitHub App auth / diff fetch (Phase 4, adapter/github/auth.py + diff.py) ---
    # Required, no default: adapter/github/auth.py's JWT signing (the
    # server-to-server App-auth flow) cannot function without the App's own
    # identity and private key. SecretStr per architecture.md's
    # credential-handling rule. Validated at Worker startup
    # (adapter.github.auth.validate_private_key, wired in apps/worker/main.py)
    # by attempting one real RS256 signing call with it — a malformed/
    # truncated key fails loudly at startup, not silently on the first real
    # job's GitHub write.
    github_app_id: int
    github_app_private_key: SecretStr

    # Reuses the exact pattern/default apps/webhook_api/settings.py's
    # WebhookAPISettings already established for the same purpose
    # (consumed there by adapter/github/manifest.py's exchange_temporary_code) —
    # every new Worker-side GitHub-calling site this phase and Phase 6/7
    # add (auth.py, diff.py, posting.py) shares these two fields rather
    # than each hardcoding its own base URL/timeout.
    github_api_base_url: str = "https://api.github.com"
    github_api_timeout_seconds: float = 10.0
    # The GitHub-side retry/backoff policy's own configurable lever (see
    # adapter/github/http.py's module docstring for the full policy this
    # gates): the number of *retries* a GitHub API call is allowed beyond
    # its first attempt. `0` achieves the same effect as disabling the
    # mechanism wholesale — an ordinary validated value, not a magic
    # sentinel needing special handling, since a bounded retry loop
    # naturally no-ops at zero iterations.
    github_retry_max_attempts: int = 3

    # --- Secret redaction (Phase 5, findings/redact.py) ---
    # Default-active per the plan's Phase 5 section — a baseline-engineering-
    # practice mitigation for a live secret-leak vector, not a product/cost-
    # policy tradeoff (see the plan's "Assumptions this plan makes" section
    # for that distinction), so it ships on by default rather than gated
    # behind prior confirmation. Independent of
    # static_analysis_gitleaks_enabled above on purpose — see this file's
    # own module docstring.
    secret_redaction_enabled: bool = True

    # --- LLM-review step (Phase 6, review/llm_review.py) ---
    # A fixed byte ceiling on the diff text included in the LLM prompt —
    # roughly a few hundred changed lines, generous for a genuine
    # review-sized PR while bounding worst-case token cost, matching every
    # other numeric bootstrap-config field's pattern. Diff size is fully
    # PR-author-controlled and directly drives LLM token cost, per the
    # plan's Security review.
    llm_max_diff_bytes: int = 200_000

    # --- Findings-merge-and-output step (Phase 7, review/output.py) ---
    # A single bootstrap-config kill-switch: when set, the full pipeline
    # still runs (including this step's own merge/redaction-consuming
    # logic) but every real GitHub write is suppressed and the would-be
    # payload is logged at `info` instead. Defaults to `False` — Phase 1's
    # own shipped default posts real reviews, matching both PRDs' Must-have
    # scope; an operator opts into dry-run explicitly to validate a fresh
    # deployment before trusting it with real writes.
    dry_run: bool = False
