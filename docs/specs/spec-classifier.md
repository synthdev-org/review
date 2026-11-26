# Classifier ("System One") Call Type PRD

Status: Accepted 2026-11-24 · Owner: Andy Beak · Last updated: 2026-11-24

Adds an opt-in `Classifier` call type (typed yes/no, choice and score answers
with probabilities, no generated text) behind a swappable Protocol, with
TypeSafe's Jev as the first backend, and uses it as a fast, cheap first judge in
the verification step. Decisions and rationale:
[ADR-0014](../adr/0014-classifier-call-type-swappable-backend-typesafe-first.md).
Built against [`docs/rules/architecture.md`](../rules/architecture.md) (tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (tag `RQPTVE`).
Wire facts: [`docs/research/jev-live-wire-examples.json`](../research/jev-live-wire-examples.json).

## Problem & Goal

**End user, primary: the org admin** who wants cheaper, faster reviews and
decides whether any diff-derived text may go to a third party.
**Secondary: the PR author**, who gets findings verified sooner with no loss of
quality, and the platform operator who provisions the credential.

Verification is a generative call doing classification. A classifier answers it
in a fraction of a second at a fraction of the cost, but the only available
backend is hosted, so it must be explicit opt-in.

**Goal:** (a) an admin can opt an org into a classifier backend, see exactly
what leaves the deployment, test the connection, and switch between `shadow` and
`active`; (b) in `active` mode confident classifier verdicts replace the LLM
judge for those findings, and anything uncertain or failing falls back to it;
(c) with no setting present, nothing changes and nothing leaves the deployment;
(d) a second backend needs zero changes to any consumer.

## Scope

### In scope

- `llmgateway.classifier` Protocol, pydantic request/response models (`noul`
  yes/no questions only in v1), and a conformance suite (`llmgateway/classifier_conformance.py`).
- `adapter/classifier/typesafe.py` backend (httpx, bearer auth, retry on 429
  and 529 only, per-call timeout).
- Shared egress and credential machinery extracted from
  `adapter/product_knowledge/` into `adapter/egress/` (behaviour-preserving).
- Registry setting `classifier_provider` (org scope), its validation, audit
  trail, Admin API test-connection route, and Admin GUI panel with disclosure.
- `FindingJudge` Protocol; existing logic becomes `LLMFindingJudge`; new
  `ClassifierFindingJudge`; `VerificationStep` depends on the Protocol.
- Shadow mode and cascade, with telemetry.

### Out of scope (deferred)

- Finding tiering/severity calibration, hunk routing, spec-per-requirement and
  dismissal-similarity classifiers (candidate future uses).
- `choice` and `score` questions (the wire format has them; v1 is `noul` only), `box` questions and images.
- A second concrete backend (the conformance suite and fake prove the seam).
- Per-repo override of the org setting.
- Classifier calls from indexing (ADR-0007 keeps indexing LLM-free).

## Requirements (user stories)

### US-C1: Operator provisions the credential

As a platform operator, I want to supply the classifier API key as bootstrap
config, so that it never lives in the database.

- Given the credential map holds ref `typesafe` bound to `api.typesafe.ai`,
  When the Worker and Admin API start, Then both parse it and no error or log
  line contains the token.
- Given a ref bound to a different host, When an admin saves a setting pointing
  `endpoint_url` at `api.typesafe.ai`, Then the write is rejected with
  `credential_host_not_bound`.

### US-C2: Admin opts in and configures

As an org admin, I want to enable the classifier for my org and choose
`shadow` or `active`, so that I control cost, speed and egress.

- Given no `classifier_provider` setting, When a review runs, Then no classifier
  request is made and the posted review is identical to today's.
- Given the Admin GUI panel, When the admin has not acknowledged the egress
  disclosure, Then `enabled` cannot be saved.
- Given a valid setting `{enabled, backend: "typesafe", endpoint_url,
  credential_ref, model, mode}`, When saved, Then it is stored without any token
  and the audit log records host, credential reference, enabled and mode only.
- Given an unknown `backend`, a non-`https` endpoint, a denied host
  (loopback/link-local/metadata) or an unknown `credential_ref`, When saved,
  Then it is rejected with a specific validation error.

### US-C3: Admin tests the connection

As an org admin, I want a test button, so that I find a bad key or endpoint
before a review depends on it.

- Given a saved setting, When the admin tests it, Then the Admin API sends one
  fixed synthetic `noul` question (no customer data) and reports ok, latency and
  model, or a closed-set failure (`unauthorized`, `unreachable`, `malformed`,
  `rate_limited`).
- Given 401 from the provider, Then the failure is `unauthorized` and the
  response body never echoes the key.

### US-C4: Active mode, cascade verification

As a PR author, I want findings verified quickly, so that I get the review sooner.

- Given `mode: "active"` and a finding whose four rubric answers are all
  `>= 0.7`, When verification runs, Then the finding is kept with no LLM judge
  call for it.
- Given any rubric answer `<= 0.3` and a finding of `medium` or lower severity,
  Then the finding is rejected with no LLM judge call for it.
- Given any rubric answer `<= 0.3` and a finding of `high` or `critical`
  severity, Then that finding alone goes to the LLM judge and its verdict
  decides (a diff-borne injection must not be able to suppress a
  serious finding).
- Given any answer between 0.3 and 0.7, When verification runs, Then that
  finding alone goes to the LLM judge and its verdict decides.
- Given every finding is decided by the classifier, Then no LLM verification
  call is made for the job.

### US-C5: Fail safe

As an org admin, I want a classifier outage to never fail or weaken a review.

- Given a timeout, 429/529 after retries, 5xx, malformed body, or an answer of
  the wrong type for any question (v1: any answer that is not `noul`), When verification runs, Then every affected
  finding goes to the LLM judge, the job succeeds, and a closed-set
  `outcome` metric records the cause.
- Given the setting is `enabled` but the credential ref no longer resolves at
  job time, Then the classifier is skipped for the job with a warning and the
  LLM judge handles everything.

### US-C6: Shadow mode

As an org admin, I want to see how often the classifier agrees with the LLM
judge before it can act.

- Given `mode: "shadow"`, When verification runs, Then the LLM judge decides
  every finding, the classifier also runs, and the agreement outcome per
  criterion (`agree`, `disagree`, `uncertain`, `error`) is recorded as a metric.
- Given shadow mode, Then no finding is kept or dropped because of a classifier
  verdict.

### US-C7: Swappable backend

As the maintainer, I want a second backend to be a drop-in.

- Given the in-memory fake classifier and the TypeSafe adapter, When the
  conformance suite runs against each, Then both pass.
- Given a fake registered in place of TypeSafe at the composition root, When a
  review runs, Then `VerificationStep` and every other consumer need no change.

### US-C8: No findings content in telemetry

As a security reviewer, I want telemetry that carries no code.

- Given any classifier call, Then metrics carry only closed-set labels
  (`backend`, `outcome`, `criterion`), and logs carry job id, counts and
  latency, never state, questions' content or answers' free text.

## Non-Functional Requirements

- **Privacy/egress:** disclosure text states that the diff hunk and the finding
  text are sent to the configured host. Only hunks relevant to a finding (not
  the whole diff) go in `state`, capped at a configurable byte ceiling well
  inside the provider's 32k-token per-question context.
- **Security:** token bound to host; SSRF guard; no redirects; TLS verified;
  no token in logs, errors, audit rows or test output; injection: classifier
  output is typed, so it cannot emit instructions, and `state` is framed as
  inert data.
- **Reliability:** every call has a timeout (default 10 s, setting-overridable);
  retries only for 429/529 with backoff and `retry-after`; a per-job
  fan-out semaphore (default 8) bounds concurrency.
- **Performance/cost:** target is lower end-to-end verification latency and
  token cost than the LLM judge on clear cases; measured in shadow mode, not
  assumed. Observed: 0.24 s for a 5-question request, 489 input tokens.
- **Observability:** counters `review.classifier.calls_total{backend,outcome}`,
  `review.classifier.verdict_total{criterion,decision}`,
  `review.classifier.shadow_agreement_total{criterion,outcome}`,
  `review.verification.llm_fallback_total{reason}`; token usage recorded like
  `record_llm_token_usage` with a `backend` label.
- **Rollout safety:** default absent; `shadow` before `active`; flipping a
  setting back to absent restores today's behaviour with no deploy.

## Phases / Units

1. **Extract egress/credentials** to `adapter/egress/` (no behaviour change;
   existing product-knowledge tests stay green).
2. **Protocol + models + conformance suite + in-memory fake** (`llmgateway`).
3. **TypeSafe adapter** with recorded cassettes from
   `jev-live-wire-examples.json` and a live-mode nightly test.
4. **Settings + Admin API + Admin GUI** (`classifier_provider`, test route,
   disclosure, audit).
5. **`FindingJudge` refactor + `ClassifierFindingJudge`** in `review/`, shadow
   and active modes, telemetry.
6. **Worker wiring** at the composition root.

## Testing / Key Scenarios

Per `testing.md`: every story maps to at least one integration test; unit tests
cover the pure logic (thresholding, cascade routing, validation).

| Story | Given/When/Then summary | Integration test (planned path) |
| --- | --- | --- |
| US-C1 | credential parse and host binding | `adapter/egress/test_credentials_integration.py` |
| US-C2 | absent setting is a no-op; invalid writes rejected; no token stored | `apps/admin_api/test_classifier_settings_routes.py`, `review/test_classifier_optin_e2e.py` |
| US-C3 | test connection ok / 401 / unreachable, key never echoed | `apps/admin_api/test_classifier_probe_routes.py` (respx cassettes) |
| US-C4 | cascade: yes/no/uncertain routing, no LLM call when fully decided | `review/test_verification_classifier_cascade.py` |
| US-C5 | each failure mode falls back; job succeeds | `review/test_verification_classifier_faults.py` |
| US-C6 | shadow never acts, agreement metric recorded | `review/test_verification_classifier_shadow.py` |
| US-C7 | conformance suite on fake and TypeSafe | `adapter/classifier/test_conformance.py` |
| US-C8 | telemetry has closed-set labels, no content | `review/test_classifier_telemetry.py` |
| E2E | Compose stack, stubbed classifier host, synthetic webhook; opt-in off and active | `tests/e2e/test_classifier.py` |

Live mode (real key, synthetic data) is a manual/nightly flag only, never the
default CI gate.

## Open items (assumptions pending validation)

1. Default thresholds 0.7/0.3 are TypeSafe's documented starting points; tune
   from shadow-mode agreement data before `active` is recommended.
2. `jev-latest` alias moves; default `model` to a pinned version
   (`jev-1.14.0`) and expose it as a setting.
3. Whether one request per finding (4 questions) or one per job is cheaper is
   unmeasured; the docs bill input tokens per request, so per-finding keeps
   `state` small. Measure in shadow mode.
4. Retention terms for non-enterprise accounts are unverified (DPA not read);
   the disclosure must not claim zero retention unless the operator confirms it.
5. TypeSafe rate limits are per organisation and only visible in their console.
6. Accuracy of a small classifier on "valid" judgements is unknown; the
   shadow-mode agreement metric is the gate.
