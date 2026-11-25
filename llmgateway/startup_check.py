"""llmgateway.startup_check — startup connectivity check for the
configured LLM endpoint.

Per the plan's Phase 3 "Observability & rollback" section: "A startup
connectivity check (same fail-fast pattern as Phase 2's pgvector check)
makes one low-cost call through the configured LLM endpoint at Worker
startup ... and exits the Worker process non-zero if unreachable." This
module is that check; `apps/worker/main.py` calls it at startup and is
responsible for the actual `sys.exit(1)` (matching
`adapter/storage/pgvector_check.py`'s split: the check module only raises a
typed error, the composition root decides what "fail fast" means for the
process).

This module depends only on the `LLMClient` Protocol and
`llmgateway.models`/`exceptions` — no `litellm`/`httpx` import — so it
stays inside `llmgateway`'s zero-infra-import boundary even though it
exercises real I/O through whatever concrete client the caller wires in.
"""

from __future__ import annotations

from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, Message

# A minimal, low-cost prompt — this call exists purely to prove the
# configured endpoint is reachable and speaks the expected response shape,
# not to produce a useful completion. `max_tokens=1` keeps it cheap on a
# real hosted provider too, not just free-as-in-local-LM-Studio.
_PROBE_PROMPT = "ping"


async def check_llm_reachable(client: LLMClient, *, model: str, timeout: float) -> None:
    """Make one low-cost completion call through `client` to verify the
    configured LLM endpoint is reachable and returns a well-formed
    response.

    Raises `llmgateway.exceptions.LLMUnreachableError` or
    `llmgateway.exceptions.LLMMalformedResponseError` (via the `client`
    implementation) on failure — this function does not itself catch or
    translate those; the caller (`apps/worker/main.py`) decides how to
    react (log + exit non-zero, per this phase's fail-fast requirement).
    """
    request = CompletionRequest(
        model=model,
        messages=[Message(role="user", content=_PROBE_PROMPT)],
        max_tokens=1,
    )
    await client.complete(request, timeout=timeout)
