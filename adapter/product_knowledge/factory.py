"""adapter.product_knowledge.factory — builds a per-endpoint provider client
from already-parsed config (plan Assumption 4): the ``ProviderOpener`` the
Worker injects and the probe factory the Admin API's test-connection uses.

The credential is resolved from the operator map by *name* here; the job-time
binding re-check (credential exists and is bound to the endpoint's host)
happens before any DNS or network, and fails closed with
``credential_unavailable``. The guarded transport re-checks the same binding
per request after DNS - defense in depth, same canonicaliser.
"""

from __future__ import annotations

from dataclasses import replace

import httpx2

from adapter.product_knowledge.client import (
    ClientTimeouts,
    EndpointGate,
    McpProductKnowledgeClient,
    NegotiationCache,
)
from adapter.product_knowledge.credentials import CredentialMap
from adapter.product_knowledge.transport import EgressPolicy, Resolver, system_resolver
from review.product_knowledge import TOP_K, ProductKnowledgeError, ProviderOutcome

__all__ = ["ProviderClientFactory"]


class ProviderClientFactory:
    """Implements ``review.product_knowledge.ProviderOpener`` (``__call__``)."""

    def __init__(
        self,
        *,
        credentials: CredentialMap,
        policy: EgressPolicy,
        timeouts: ClientTimeouts | None = None,
        cache: NegotiationCache | None = None,
        gate: EndpointGate | None = None,
        bypass_cache: bool = False,
        top_k: int = TOP_K,
        resolver: Resolver = system_resolver,
        inner_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._policy = policy
        self._timeouts = timeouts or ClientTimeouts()
        # Shared across every client this factory builds: one negotiation per
        # endpoint per TTL and one concurrency bound per endpoint.
        self._cache = cache or NegotiationCache()
        self._gate = gate or EndpointGate()
        self._bypass_cache = bypass_cache
        self._top_k = top_k
        self._resolver = resolver
        self._inner = inner_transport

    def __call__(self, endpoint_url: str, credential_ref: str | None) -> McpProductKnowledgeClient:
        problem = self._credentials.binding_problem(
            credential_ref, endpoint_url, self._policy.endpoint_allowlist
        )
        if problem is not None:
            raise ProductKnowledgeError(ProviderOutcome.CREDENTIAL_UNAVAILABLE)
        credential = self._credentials.get(credential_ref) if credential_ref is not None else None
        return McpProductKnowledgeClient(
            endpoint_url,
            credential=credential,
            policy=replace(self._policy, credential_hosts=self._credentials.all_allowed_hosts()),
            timeouts=self._timeouts,
            cache=self._cache,
            gate=self._gate,
            bypass_cache=self._bypass_cache,
            top_k=self._top_k,
            resolver=self._resolver,
            inner_transport=self._inner,
        )
