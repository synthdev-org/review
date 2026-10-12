"""
Shared helpers for the MCP client integration tests (never in the runtime image).

The guard is the production code: the endpoint host resolves to a *private*
address (``10.9.8.7``) and ``LoopbackTransport`` performs only the final
connect to the fake's loopback socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from adapter.product_knowledge.client import ClientTimeouts, McpProductKnowledgeClient
from adapter.product_knowledge.testing.fake_provider import FakeProvider
from adapter.product_knowledge.testing.loopback import LoopbackTransport, fixed_resolver
from adapter.product_knowledge.transport import EgressPolicy, ProviderCredential
from review.product_knowledge import ProductKnowledgeError, ProviderOutcome

SCHEMAS = (
    Path(__file__).resolve().parents[3] / "docs" / "reference" / "product-knowledge-contract" / "v1"
)
PRIVATE_ADDRESS = "10.9.8.7"
HOST = "wiki.test"
TOKEN = "tok-SECRET-0123456789"
FAST = ClientTimeouts(connect_seconds=1.0, call_seconds=1.5)
# An unauthenticated provider must be operator-listed (M1); tests list HOST.
LISTED = EgressPolicy(endpoint_allowlist=(HOST,))


def schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / f"{name}.json").read_text())
    return loaded


def make_client(
    provider: FakeProvider,
    *,
    credential: ProviderCredential | None = None,
    timeouts: ClientTimeouts = FAST,
    **kwargs: Any,
) -> tuple[McpProductKnowledgeClient, LoopbackTransport]:
    loopback = LoopbackTransport(provider.port)
    kwargs.setdefault("policy", LISTED)
    client = McpProductKnowledgeClient(
        f"http://{HOST}:{provider.port}/mcp",
        credential=credential,
        timeouts=timeouts,
        resolver=fixed_resolver(PRIVATE_ADDRESS),
        inner_transport=loopback,
        **kwargs,
    )
    return client, loopback


def credential(allowed: tuple[str, ...] = (HOST,)) -> ProviderCredential:
    return ProviderCredential(ref="wiki", token=SecretStr(TOKEN), allowed_hosts=allowed)


async def outcome_of(awaitable: Any) -> ProviderOutcome:
    with pytest.raises(ProductKnowledgeError) as excinfo:
        await awaitable
    return excinfo.value.outcome
