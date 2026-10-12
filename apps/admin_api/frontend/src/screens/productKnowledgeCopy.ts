// The one literal egress-disclosure statement (US-PK15). The Admin UI panel
// renders it, the vitest assertion, tests/e2e/test_admin_ui.py (which reads
// this file and greps the served bundle) and the operator runbook all match
// it — keep it plain ASCII so a bundler never rewrites it, and edit it only
// together with docs/reference/operations.md.
export const PRODUCT_KNOWLEDGE_EGRESS_DISCLOSURE =
  "Sent to this endpoint on each review: the secret-redacted pull request title and linked issue keys. Never sent: code, the diff, or the pull request description.";
