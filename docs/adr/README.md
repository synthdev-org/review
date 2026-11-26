# Architecture decision records

Each record explains one major decision and the reasons for it. Read the record before you change the area it covers.

| Number | Title | Status |
| --- | --- | --- |
| 0001 | [Self-hosted deployment via Kubernetes (production) and Docker Compose (development)](0001-self-hosted-kubernetes-deployment.md) | Accepted |
| 0002 | [Implement the platform in Python](0002-implement-in-python.md) | Accepted |
| 0003 | [Review owns its own code indexing; external product-knowledge providers augment via review's own MCP contract, knowhow first](0003-knowhow-product-knowledge-augmentation.md) | Accepted |
| 0004 | [Multi-org tenancy, scoped to one customer's internal structure](0004-multi-org-tenancy.md) | Accepted |
| 0005 | [Pure, no-I/O logic is Unit-tested regardless of story or package](0005-pure-no-io-logic-unit-tested.md) | Accepted |
| 0006 | [A Protocol may be defined alongside the data model it produces or substantively operates on, not only at its first consumer](0006-protocol-defined-at-data-model.md) | Accepted |
| 0007 | [Indexing stays LLM-free; richer RAG enrichment deferred to an optional phase](0007-llm-free-indexing-deferred-rag-enrichment.md) | Accepted |
| 0009 | [Memory entry schema and dismissal-derived similarity signal](0009-memory-entry-schema-and-dismissal-similarity-signal.md) | Proposed |
| 0010 | [Admin sign-in — local login plus OIDC behind a swappable provider Protocol (Authentik first), and a fixed Admin/Viewer role split](0010-oidc-admin-sign-in-and-fixed-role-split.md) | Accepted |
| 0011 | [The OpenTelemetry API package may be imported directly in domain packages, held as module-level handles, with no new `typing.Protocol` telemetry adapter](0011-otel-api-imported-directly-in-domain-packages.md) | Accepted |
| 0012 | [A telemetry-recording failure is logged and the wrapped block continues, never re-raised](0012-telemetry-recording-failures-log-and-continue.md) | Accepted |
| 0013 | [Product-knowledge MCP client — official `mcp` SDK in one adapter, operator-provisioned credentials with host binding, a network egress guard, and integer-major contract versioning](0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md) | Proposed |
| 0014 | [A "Classifier" call type behind a swappable Protocol, opt-in per org, with TypeSafe (Jev) as the first backend](0014-classifier-call-type-swappable-backend-typesafe-first.md) | Accepted |

ADR numbers are permanent identifiers. Number 0008 is intentionally unused because that proposal was withdrawn before it was used.
