# ADR: A "Classifier" call type behind a swappable Protocol, opt-in per org, with TypeSafe (Jev) as the first backend

## Status

ACCEPTED 2026-11-24 (drafted alongside
[`docs/specs/spec-classifier.md`](../specs/spec-classifier.md); owner signed off, including plan decision D1 (a)-(c): v1 implements `noul` only, and a classifier "no" on a high or critical finding goes to the LLM judge). Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (tag `WKPYNQ`) and
[`docs/rules/testing.md`](../rules/testing.md) (tag `RQPTVE`).

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Some of the platform's LLM calls are classification written as generation:
the verification step asks a model to emit four booleans per finding as JSON.
"System One" classifiers (TypeSafe's Jev is the first) answer typed questions
(yes/no probability, choice, score) with probabilities and no generated text.
They are very fast (a live 5-question call returned in 0.24 s) and very cheap
(about $0.042 per million input tokens, output free).

**Decisions:** (1) add a second call type, `Classifier`, as a consumer-defined
`typing.Protocol` in `llmgateway`, beside `LLMClient`; (2) the first and only
concrete backend is `adapter/classifier/typesafe.py`; the Protocol, a shared
conformance suite and an in-memory fake make a second backend a drop-in;
(3) it is **opt-in per org, off by default**, with the endpoint and a credential
*reference* in Postgres settings and the credential itself operator-provisioned
bootstrap config, reusing ADR-0013's credential map, host binding and egress
guard; (4) the first consumer is verification, as a **cascade**: confident
classifier answers decide, uncertain or failed ones fall back to the existing
LLM judge, and a **shadow mode** runs the classifier without acting.

## Context

- TypeSafe's service is hosted only (`POST https://api.typesafe.ai/v1/systemone`).
  No self-hosted option is documented. Every call carries a `state`, so
  diff-derived text leaves the customer's infrastructure. Stated terms: not
  trained on customer data; zero data retention for enterprise on request. The
  DPA was not reviewed.
- The product promise is that customer code never leaves their control, and the
  platform never holds provider credentials of its own (AGENTS.md). ADR-0013
  already solved the same shape of problem for product-knowledge providers:
  endpoint and credential *reference* in Postgres, token in bootstrap config
  bound to allowed hosts, connection-time egress guard, admin-UI disclosure.
- Live wire facts recorded on 2026-11-24 with synthetic data
  ([`docs/research/jev-live-wire-examples.json`](../research/jev-live-wire-examples.json)):
  model `jev-1.14.0`; one request carries many typed questions evaluated
  independently; `noul` returns `{"type":"noul","noul":0.93}`; `choice` returns
  `choice`, `confidence`, `probabilities`; `score` returns `score`,
  `confidence`, `legend`, `probabilities`; `usage` carries `input_tokens` and
  `output_tokens`; empty `questions` is a 422 with a `detail` list; a bad key is
  401. A 5-question request of 489 input tokens took 0.24 s.
- Verification today is one batched LLM call whose verdicts are correlated back
  by array index, failing closed on unmatched ones.

## Decisions

### 1. `Classifier` Protocol in `llmgateway`

```python
class Classifier(Protocol):
    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse: ...
```

Models are pydantic, with zero infra imports. **v1 implements `noul`
questions only** (plan decision D1b): the wire format also has `choice` and
`score`, which are deferred until a consumer needs them. `ClassificationRequest`
holds `model`, `state: str`, and `questions: dict[str, Question]` where
`Question` is a type alias for `NoulQuestion(instructions)`.
`ClassificationResponse` holds `answers: dict[str, Answer]` (`Answer` is an alias
for `NoulAnswer(p)`) and `usage`. The aliases become discriminated unions
(adding `ChoiceQuestion`/`ChoiceAnswer`, `ScoreQuestion`/`ScoreAnswer`) when
those types land, so no one-member union class has to be rewritten. A backend
that receives an answer type other than the one asked for fails the whole
response. Failures raise the existing `LLMUnreachableError` /
`LLMMalformedResponseError` family (a new `ClassifierError` base is not added
until a caller needs to tell them apart). Every call takes a timeout.

This is a deliberate owner decision to define the seam with one real backend,
which departs from "no interface without a second implementation in view". It
is justified by the explicit swap requirement (other System One vendors, or a
customer-hosted classifier) and bounded by a **conformance suite**
(`llmgateway/classifier_conformance.py`) that every backend, including the
in-memory fake, must pass. If the seam proves wrong the cost is one Protocol
and one adapter.

### 2. Answers are probabilities; the platform owns the thresholds

The platform never takes argmax blindly. A `noul` answer is **yes** at `>= 0.7`,
**no** at `<= 0.3`, otherwise **uncertain** (TypeSafe's documented starting
points, held as constants in `llmgateway/classifier.py`, not settings, until
shadow-mode data shows a need). When `choice` is added it is thresholded on the
selected option's probability, not on `confidence` (the docs' recommendation).
A wrong answer *type* for a question fails the whole response (never treated as
"no"), a lesson from `andybeak/chaptered`'s Jev backend, which also ignored
probabilities entirely.

### 3. Opt-in, off by default, org-scoped

New registry setting `classifier_provider`, org scope, value
`{enabled, backend: "typesafe", endpoint_url, credential_ref, model, mode:
"shadow" | "active"}`. The default is absent, which means no classifier calls,
no egress, and a review byte-for-byte what it is today. The token never enters
Postgres. Setting changes are audit-logged with host and credential *reference*
only, as `product_knowledge_provider` already does.

### 4. Credentials and egress reuse ADR-0013's machinery

The operator provisions the key in the existing credential map (a Kubernetes
`Secret`), bound to `allowed_hosts` (here `api.typesafe.ai`). The same single
canonicaliser, host binding, deny-list, no-redirects and resolved-address
pinning from `adapter/egress/egress.py` and `transport.py` apply. A
first implementation step extracts those from `adapter/product_knowledge/` into
a neutral shared package (`adapter/egress/`) so the classifier adapter does not
import a sibling adapter. The admin UI shows an egress disclosure before
`enabled` can be set, in the style of `PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE`.

### 5. Cascade and shadow mode in verification

A consumer-defined `FindingJudge` Protocol (two real implementations from day
one: the existing batched `LLMFindingJudge` and the new
`ClassifierFindingJudge`) is what `VerificationStep` depends on.
`ClassifierFindingJudge` asks one request per finding with one `noul` question
per rubric criterion, and a finding passes only if every criterion is **yes**.
Any **uncertain** criterion, and any classifier error or timeout, sends that
finding (not the whole job) to the LLM judge. In `active` mode a classifier
**no on a high or critical finding also goes to the LLM judge** (plan decision
D1c) instead of dropping it: a diff-borne injection could otherwise suppress a
serious true finding. Medium and low "no" verdicts decide, an accepted risk
covered by the adversarial corpus. In `shadow` mode the classifier
runs and its verdict is recorded next to the LLM verdict, but only the LLM
verdict acts. `active` mode lets confident classifier verdicts decide.

### 6. What it does not do

A classifier cannot write or explain findings, so generation, intent summaries
and fix generation stay on `LLMClient`. v1 covers verification only and `noul`
questions only (D1b); tiering,
hunk routing and spec-per-requirement checks are deferred (see the spec).

## Consequences

- Positive: faster and cheaper verification on clear cases; removes
  index-correlation fragility for those findings; a documented seam for other
  System One vendors; no change for orgs that do not opt in.
- Negative: a new egress path for diff-derived text (mitigated by opt-in,
  disclosure, host binding, shadow mode and the customer's own key); one more
  adapter and an extraction refactor of the egress code; thresholds need tuning
  against our own labelled data because calibration is population-level.
- Risks carried: small classifiers may judge "valid" worse than a large LLM,
  which the shadow-mode agreement metric exists to expose before `active`
  can drop a finding.

## Alternatives considered

- **Hard-code Jev, no Protocol.** Simplest, but couples the pipeline to one
  hosted vendor; rejected by the owner's swap requirement.
- **LLM-backed classifier as the first backend.** Keeps all data in the
  customer's endpoint but gives none of the speed or cost benefit and no
  calibrated probabilities. Kept as a possible second backend, not v1.
- **Replace the LLM judge outright.** Rejected: worst-case quality must not
  fall below today's, hence the cascade.
