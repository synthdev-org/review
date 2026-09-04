#!/usr/bin/env bash
# charts/review/test-render.sh — Phase 6 ("Helm chart skeleton") "Tests"
# section: `helm lint` and `helm template` as a CI job, no real cluster
# involved (the Helm-equivalent of the pyramid's unit tier).
#
# Runs, in order:
#   1. helm lint + helm template against the default values.yaml.
#   2. helm lint + helm template with values-production.yaml layered on
#      top (plus the credential overrides a real operator would supply —
#      values-production.yaml deliberately ships without them, per its own
#      header comment) — asserts the merged postgresql.enabled is false by
#      checking the bundled subchart's resources are absent from the
#      rendered output.
#   3. helm template with postgresql.enabled: false plus an existingSecret
#      reference for every credential field (including
#      github.setupStateSecret, exercised with webhookApi.setupMode: true
#      since that field's env var only renders when setup mode is on) —
#      asserts this chart's own Secret manifest contains no literal
#      credential value for any existingSecret-backed field, and that every
#      Deployment env referencing that field points at the given
#      existingSecret name/key.
#
# Usage: run from anywhere with `helm` on PATH:
#   ./charts/review/test-render.sh
# Exits non-zero on the first failed assertion.

set -euo pipefail

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE=review-test
FAILED=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILED=1; }

assert_contains() {
    local haystack="$1" needle="$2" desc="$3"
    if grep -qF -- "$needle" <<<"$haystack"; then pass "$desc"; else fail "$desc"; fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" desc="$3"
    if grep -qF -- "$needle" <<<"$haystack"; then fail "$desc"; else pass "$desc"; fi
}

echo "=== 1. helm lint (default values.yaml) ==="
helm lint "$CHART_DIR"

echo
echo "=== 1b. helm template (default values.yaml) ==="
DEFAULT_OUT=$(helm template "$RELEASE" "$CHART_DIR")
assert_contains "$DEFAULT_OUT" 'kind: Secret' \
    "default render includes a Secret manifest"
assert_contains "$DEFAULT_OUT" 'github-webhook-secret: "replace-with-a-real-webhook-secret"' \
    "default Secret is sourced from values.yaml's literal value"
assert_contains "$DEFAULT_OUT" '# Source: review/charts/postgresql/templates/statefulset.yaml' \
    "postgresql.enabled: true (default) bundles the pgvector subchart"
assert_contains "$DEFAULT_OUT" 'kind: Deployment' \
    "default render includes Deployment manifests"
assert_contains "$DEFAULT_OUT" $'resources:\n            limits:\n              cpu: "2"\n              memory: 2Gi\n            requests:\n              cpu: 500m\n              memory: 512Mi' \
    "default render includes the Worker Deployment's non-empty resources.requests/limits (Phase 5)"

echo
echo "=== 2. helm lint (values-production.yaml layered on top) ==="
PROD_OVERRIDES=(--set 'database.url.value=postgresql+asyncpg://prod:prod@external-db:5432/review')
helm lint "$CHART_DIR" -f "$CHART_DIR/values-production.yaml" "${PROD_OVERRIDES[@]}"

echo
echo "=== 2b. helm template (values-production.yaml layered on top) ==="
PROD_OUT=$(helm template "$RELEASE" "$CHART_DIR" -f "$CHART_DIR/values-production.yaml" "${PROD_OVERRIDES[@]}")
assert_not_contains "$PROD_OUT" '# Source: review/charts/postgresql/' \
    "values-production.yaml's postgresql.enabled: false suppresses the bundled subchart"
assert_contains "$PROD_OUT" 'REVIEW_WEBHOOK_API_ENVIRONMENT: "production"' \
    "values-production.yaml's webhookApi.environment: production takes effect"
assert_contains "$PROD_OUT" 'database-url: "postgresql+asyncpg://prod:prod@external-db:5432/review"' \
    "externally-managed database.url.value is embedded when postgresql.enabled: false"

echo
echo "=== 2c. helm template (values-production.yaml, no DB override) fails closed ==="
if helm template "$RELEASE" "$CHART_DIR" -f "$CHART_DIR/values-production.yaml" >/dev/null 2>/tmp/prod-no-db.err; then
    fail "rendering values-production.yaml without database.url or existingSecret should fail closed, but succeeded"
else
    if grep -q "database.url.value (or database.url.existingSecret) must be set" /tmp/prod-no-db.err; then
        pass "rendering fails closed with a clear error when postgresql.enabled: false and no DB configured"
    else
        fail "rendering failed, but not with the expected fail-closed error: $(cat /tmp/prod-no-db.err)"
    fi
fi

echo
echo "=== 3. helm template: postgresql.enabled: false + existingSecret for every credential field ==="
# webhookApi.setupMode is turned on here specifically so
# github.setupStateSecret's env var (gated behind setupMode, see
# templates/deployment-webhook-api.yaml) actually renders — otherwise its
# existingSecret path would go unexercised, since the other three
# credential fields all render unconditionally.
EXISTING_SECRET_OVERRIDES=(
    --set postgresql.enabled=false
    --set database.url.existingSecret.name=review-db-credentials
    --set database.url.existingSecret.key=database-url
    --set github.webhookSecret.existingSecret.name=review-github-credentials
    --set github.webhookSecret.existingSecret.key=webhook-secret
    --set webhookApi.setupMode=true
    --set github.setupStateSecret.existingSecret.name=review-github-credentials
    --set github.setupStateSecret.existingSecret.key=setup-state-secret
    --set github.appPrivateKey.existingSecret.name=review-github-credentials
    --set github.appPrivateKey.existingSecret.key=app-private-key
    --set llm.apiKey.existingSecret.name=review-llm-credentials
    --set llm.apiKey.existingSecret.key=api-key
)
EXISTING_OUT=$(helm template "$RELEASE" "$CHART_DIR" "${EXISTING_SECRET_OVERRIDES[@]}")

assert_not_contains "$EXISTING_OUT" 'database-url:' \
    "database-url key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'github-webhook-secret:' \
    "github-webhook-secret key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'github-setup-state-secret:' \
    "github-setup-state-secret key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'github-app-private-key:' \
    "github-app-private-key key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'llm-api-key:' \
    "llm-api-key key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'replace-with-a-real-webhook-secret' \
    "no literal default credential leaks into the rendered output"
assert_not_contains "$EXISTING_OUT" '"not-needed"' \
    "no literal default LLM credential leaks into the rendered output"
assert_contains "$EXISTING_OUT" $'name: review-db-credentials\n                  key: database-url' \
    "REVIEW_DATABASE_URL env references the given existingSecret name/key"
assert_contains "$EXISTING_OUT" $'name: review-github-credentials\n                  key: webhook-secret' \
    "REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET env references the given existingSecret name/key"
assert_contains "$EXISTING_OUT" $'name: review-github-credentials\n                  key: setup-state-secret' \
    "REVIEW_WEBHOOK_API_SETUP_STATE_SECRET env references the given existingSecret name/key"
assert_contains "$EXISTING_OUT" $'name: review-github-credentials\n                  key: app-private-key' \
    "REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY env references the given existingSecret name/key"
assert_contains "$EXISTING_OUT" $'name: review-llm-credentials\n                  key: api-key' \
    "REVIEW_WORKER_LLM_API_KEY env references the given existingSecret name/key"

echo
if [[ "$FAILED" -eq 0 ]]; then
    echo "All Helm chart render checks passed."
else
    echo "One or more Helm chart render checks FAILED." >&2
    exit 1
fi
