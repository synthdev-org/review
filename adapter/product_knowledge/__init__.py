"""
adapter.product_knowledge — MCP client adapter, egress guard and fake provider.

Serves the optional product-knowledge augmentation (see
docs/plans/plan-deliver-product-knowledge.md and ADR-0013).

``egress`` (pure host/address logic) and ``transport`` (the httpx2 guard) are
the security boundary; ``client`` is the only module importing ``mcp``;
``testing/`` is test tooling excluded from the runtime image.
"""
