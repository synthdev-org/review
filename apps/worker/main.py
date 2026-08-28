"""Worker entrypoint — composition root.

Parses config once (pydantic-settings) and configures logging here; later
phases wire further adapters (storage, analyzers, job dispatch) in via
constructor injection at this same composition root, per
docs/rules/architecture.md's Decoupling section. Phase 3 of the delivery
plan adds the LLM Gateway's startup connectivity check here — the same
fail-fast pattern as Phase 2's pgvector check
(`adapter/storage/pgvector_check.py`): make one low-cost call through the
configured LLM endpoint, and if it's unreachable or returns a malformed
response, log a clear error and exit the process non-zero rather than
start up in a silently-broken state.

Settings are parsed and logging configured inside `main()`, not at module
import time, so importing this module (mypy, pytest collection) never
requires environment variables or performs process-level side effects.
"""

from __future__ import annotations

import asyncio
import logging

from adapter.llm.litellm_client import LiteLLMClient
from apps.worker.logging_config import configure_logging
from apps.worker.settings import WorkerSettings
from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMError
from llmgateway.startup_check import check_llm_reachable


def _build_llm_client(settings: WorkerSettings) -> LLMClient:
    """Wire the domain-defined `LLMClient` Protocol to its LiteLLM-backed
    implementation. The one place in this composition root that does so,
    per architecture.md's "wire concrete implementations only at the
    composition root" rule — and a seam a unit test below substitutes a
    fake at, so `main()`'s config-parsing/logging behavior stays testable
    without a real LLM endpoint.
    """
    return LiteLLMClient(
        endpoint=str(settings.llm_endpoint),
        api_key=settings.llm_api_key.get_secret_value(),
    )


async def _check_llm_startup(
    client: LLMClient, settings: WorkerSettings, logger: logging.Logger
) -> None:
    """Run the LLM Gateway startup connectivity check and raise
    `SystemExit(1)` if it fails — the same fail-fast pattern as Phase 2's
    pgvector check."""
    try:
        await check_llm_reachable(
            client, model=settings.llm_model, timeout=settings.llm_timeout_seconds
        )
    except LLMError as exc:
        logger.error(
            "LLM endpoint unreachable at startup; exiting",
            extra={"llm_endpoint": str(settings.llm_endpoint), "error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info("llm endpoint reachable", extra={"llm_endpoint": str(settings.llm_endpoint)})


def main() -> None:
    """Process entrypoint: parse config, configure logging, verify the LLM
    endpoint is reachable (exiting non-zero if not), then idle.

    Replaced by a real claim/dispatch loop once a later phase's job-queue
    dispatch logic exists.
    """
    settings = WorkerSettings()
    configure_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info("worker starting", extra={"environment": settings.environment})

    client = _build_llm_client(settings)
    asyncio.run(_check_llm_startup(client, settings, logger))

    logger.info("worker idle: no job dispatch wired yet")


if __name__ == "__main__":
    main()
