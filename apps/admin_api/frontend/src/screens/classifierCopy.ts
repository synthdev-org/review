// The classifier egress-disclosure statement (spec-classifier US-C2, plan D4/D5).
// The panel renders it next to the acknowledgement box; the vitest assertion and
// tests/e2e/test_admin_ui.py (which greps the served bundle) match it. Plain ASCII so
// a bundler never rewrites it; edit it only together with docs/reference/operations.md.
// It must never claim zero retention or that no secret leaves: only that secrets
// detectable by the vendored ruleset are redacted.
export const CLASSIFIER_EGRESS_DISCLOSURE =
  "Sent to the configured endpoint on each review, for every finding: the finding text and the diff hunk it refers to, after secrets detectable by the vendored ruleset are redacted. Secrets the ruleset cannot detect may still be sent, and the provider's retention terms are not verified by this product. The endpoint uses one operator-provided key shared by every organization on this installation, so its quota is shared.";
