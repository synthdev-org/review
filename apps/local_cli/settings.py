"""Configuration for the local CLI composition root, parsed once here via pydantic-settings.

Its own ``REVIEW_LOCAL_`` prefix, deliberately not ``REVIEW_WORKER_``: local mode adds nothing to
``WorkerSettings`` (so ``.env.example`` and the Helm chart are untouched) and a developer's shell
never needs worker variables. No ``.env`` file is read: in CI the working tree is attacker-
influenced, and configuration must come only from the environment the user or workflow set.

``LocalSettings`` maps into the shared, domain-owned ``StepEnablement``/``StepTimeouts``/
``ReviewPipelineConfig`` (plan A2/A3). The flags that are off are the steps that need a database or
a code index (clone detection, context builder, memory suppression) or are out of scope for the
first version (fix generation, walkthrough, plan A6).
"""

from __future__ import annotations

import math
from collections.abc import Collection
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from adapter.local.mcp_config import DEFAULT_NOMINATION_FILE
from adapter.local.memory_file import DEFAULT_MEMORY_FILE
from findings.models import Severity
from review.config import (
    ReviewPipelineConfig,
    StepEnablement,
    StepTimeouts,
)

# The values below belong to steps local mode keeps disabled. The registry builder still needs
# every field of ``ReviewPipelineConfig`` stated, so they are the Worker's own defaults.
_STEP_TIMEOUT_HEADROOM_SECONDS = 15


class ModelNotConfiguredError(Exception):
    """No model was configured. The message lists every way to configure one."""

    def __init__(self) -> None:
        super().__init__(
            "no model is configured. Set REVIEW_LOCAL_LLM_MODEL and REVIEW_LOCAL_LLM_ENDPOINT "
            "to use any OpenAI-compatible endpoint (for example a local LM Studio or Ollama "
            "server, or a hosted provider; REVIEW_LOCAL_LLM_API_KEY if it needs a key)."
        )


class LocalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REVIEW_LOCAL_", extra="ignore", populate_by_name=True
    )

    # GitHub Actions (Phase 5). These are the runner's own variable names, read unprefixed. Inside
    # Actions (GITHUB_ACTIONS=true) the run posts its findings to the pull request; the token is
    # held as a secret and never printed.
    github_actions: bool = Field(default=False, validation_alias="GITHUB_ACTIONS")
    github_event_name: str | None = Field(default=None, validation_alias="GITHUB_EVENT_NAME")
    # A generic CI signal (most CI systems set CI=true). Outside a recognised pull-request context
    # it makes the memory file refuse to be read from the (attacker-controlled) working tree.
    ci: bool = Field(default=False, validation_alias="CI")
    github_token: SecretStr | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    github_repository: str | None = Field(default=None, validation_alias="GITHUB_REPOSITORY")
    github_api_url: str | None = Field(default=None, validation_alias="GITHUB_API_URL")
    github_event_path: Path | None = Field(default=None, validation_alias="GITHUB_EVENT_PATH")
    github_timeout_seconds: float = Field(default=10.0, gt=0)
    github_max_retry_attempts: int = Field(default=3, ge=0)
    # The author login the posted review comments carry (a GITHUB_TOKEN posts as this bot): only
    # comments by it that also carry the hidden marker count as ours.
    ci_bot_login: str = "github-actions[bot]"
    # US-C2: exit with a distinct code when a finding is at or above this severity. Off by default.
    fail_on_severity: Severity | None = None

    # Which model backend to use. The default stays the OpenAI-compatible endpoint; "claude_cli"
    # (the developer's own logged-in Claude CLI) is an explicit opt-in. Whether Anthropic's terms
    # permit driving a subscription this way is UNVERIFIED (plan B4).
    backend: Literal["openai_compatible", "claude_cli"] = "openai_compatible"
    claude_cli_executable: str = "claude"

    # No defaults for the endpoint or model: a silent default would send the developer's code to
    # an endpoint they never chose.
    llm_endpoint: AnyHttpUrl | None = None
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_diff_bytes: int = Field(default=200_000, gt=0)

    static_analysis_enabled: bool = True

    # US-L12: the repository memory file. Read from the environment only, never from repository
    # content, so a pull request cannot point review at a file of its choosing.
    memory_file_enabled: bool = True
    memory_file: str = DEFAULT_MEMORY_FILE
    # Explicit opt-in to read the memory file from the working tree on a CI system review cannot
    # recognise (anything but GitHub Actions); otherwise that run refuses, because the working tree
    # is the pull-request head there. Environment only, so a pull request cannot set it.
    memory_from_working_tree: bool = False

    # MCP knowledge (US-L9 to US-L11, ADR-0017). Nothing is started or contacted unless a server is
    # nominated by name (--mcp, or the committed names-only file). The paths and caps are read from
    # the environment only, so a repository cannot change them.
    #
    # In CI this is the only MCP configuration review reads: a file the workflow provides, outside
    # the checkout. Locally it overrides the developer's own client configuration.
    mcp_config: Path | None = None
    mcp_nominations_file: str = DEFAULT_NOMINATION_FILE
    mcp_deadline_seconds: float = Field(default=10.0, gt=0)  # all nominated servers together
    mcp_max_calls: int = Field(default=3, ge=1)  # tool calls per run (one per server)
    mcp_max_processes: int = Field(default=3, ge=1)
    mcp_max_stdio_bytes: int = Field(default=262_144, gt=0)  # also the HTTP response cap
    mcp_max_list_tools_bytes: int = Field(default=65_536, gt=0)
    mcp_max_tools: int = Field(default=100, ge=1)
    mcp_scan_timeout_seconds: float = Field(default=5.0, gt=0)

    # Input-side redaction (US-L15). These bound the work a crafted diff can cause; there is
    # deliberately no setting that turns redaction off.
    redaction_max_files: int = Field(default=2_000, gt=0)
    redaction_max_bytes: int = Field(default=20_000_000, gt=0)
    redaction_timeout_seconds: float = Field(default=20.0, gt=0)

    @field_validator(
        "llm_endpoint",
        "llm_model",
        "fail_on_severity",
        "github_event_path",
        "mcp_config",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("ci", mode="before")
    @classmethod
    def _any_ci_value_is_true(cls, value: object) -> object:
        # Some CI systems set CI to a product name (CI=woodpecker), not a boolean.
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return value

    def require_model(self) -> tuple[str, str]:
        """``(model, endpoint)``, or ``ModelNotConfiguredError`` if either is missing."""
        if self.llm_model is None or self.llm_endpoint is None:
            raise ModelNotConfiguredError
        return self.llm_model, str(self.llm_endpoint)

    def step_enablement(
        self, *, skipped_analyzers: Collection[str] = (), mcp: bool = False
    ) -> StepEnablement:
        """``skipped_analyzers`` are analyzers whose binary is not installed: their per-analyzer
        flag is turned off so the step skips them instead of failing on a missing executable.
        ``mcp`` (a server is nominated) turns the context builder on, for its provider branch only:
        local mode has no code index, so its code branch finds nothing."""
        return StepEnablement(
            static_analysis_enabled=self.static_analysis_enabled,
            clone_detection_enabled=False,
            context_builder_enabled=mcp,
            fix_generation_enabled=False,
            walkthrough_enabled=False,
            **{f"static_analysis_{name}_enabled": False for name in skipped_analyzers},
        )

    def step_timeouts(self) -> StepTimeouts:
        """The Worker's step timeouts, raised where needed so a step never times out before the
        model call inside it does (the Worker does not guard this; a slow local model makes it
        matter here)."""
        defaults = StepTimeouts()
        floor = math.ceil(self.llm_timeout_seconds) + _STEP_TIMEOUT_HEADROOM_SECONDS
        timeouts = defaults.model_copy(
            update={
                # The redaction scan runs inside the prefilter step's fetch.
                "prefilter_step_timeout_seconds": max(
                    defaults.prefilter_step_timeout_seconds,
                    math.ceil(self.redaction_timeout_seconds) + _STEP_TIMEOUT_HEADROOM_SECONDS,
                ),
                # The MCP consultation (servers plus the redaction scan) runs inside this step.
                "context_builder_step_timeout_seconds": max(
                    defaults.context_builder_step_timeout_seconds, self._mcp_window() + 10
                ),
                "intent_summary_step_timeout_seconds": max(
                    defaults.intent_summary_step_timeout_seconds, floor
                ),
                "review_generation_step_timeout_seconds": max(
                    defaults.review_generation_step_timeout_seconds, floor
                ),
                "verification_step_timeout_seconds": max(
                    defaults.verification_step_timeout_seconds, floor
                ),
            }
        )
        return timeouts

    def _mcp_window(self) -> int:
        """Seconds the provider branch may need: query scan, servers, then the reply scan."""
        return math.ceil(self.mcp_deadline_seconds + 2 * self.mcp_scan_timeout_seconds) + 1

    def pipeline_config(
        self, model: str, *, memory_suppression: bool = False, mcp: bool = False
    ) -> ReviewPipelineConfig:
        return ReviewPipelineConfig(
            llm_model=model,
            llm_timeout_seconds=self.llm_timeout_seconds,
            llm_max_diff_bytes=self.llm_max_diff_bytes,
            static_analysis_max_concurrent_analyzers=3,
            clone_detection_step_timeout_seconds=30,
            clone_detection_min_tokens=100,
            clone_detection_min_lines=10,
            clone_detection_max_findings=5,
            # The branch's deadline is capped at this, so it must not be below the MCP window.
            context_builder_internal_timeout_seconds=max(25, self._mcp_window() + 5) if mcp else 25,
            # The branch's backstop: the provider's own deadline (mcp_deadline_seconds) fires first
            # and records which servers timed out; this one only bounds redaction after it.
            product_knowledge_deadline_seconds=self._mcp_window() if mcp else 10,
            memory_suppression_enabled=memory_suppression,
            memory_similarity_threshold=0.90,
            memory_decay_half_life_days=180.0,
            fix_generation_step_timeout_seconds=90,
            fix_generation_max_fixes_per_job=5,
            fix_generation_max_range_lines=20,
            fix_generation_max_replacement_lines=40,
            fix_generation_window_lines=60,
            walkthrough_step_timeout_seconds=45,
            walkthrough_min_files=2,
            walkthrough_max_files=60,
            walkthrough_max_chars=6000,
        )
