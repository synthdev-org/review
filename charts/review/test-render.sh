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
#   4. (Phase 9) helm template (default values.yaml) — asserts the Admin
#      UI's Deployment/Service render with liveness/readiness probes and
#      resources, and that adminApi.enabled: false omits both.
#   5. (Phase 9) helm template — asserts the Ingress renders routing to
#      both admin-api and webhook-api backend Services, under both TLS
#      modes (existingSecret-style literal secretName, and a cert-manager
#      issuer annotation), and that ingress.enabled: false omits it.
#   6. (Phase 9) `docker compose config` — validates the admin_api service
#      block (US-A20's second Given/When/Then) with the required env vars
#      set, the same way an operator's real .env would.
#
# Usage: run from anywhere with `helm` on PATH (and `docker compose` on
# PATH for step 6):
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
    # Phase 9 — Admin API's own credential fields (Assumptions 11 and 13).
    --set adminApi.bootstrapUsername.existingSecret.name=review-admin-credentials
    --set adminApi.bootstrapUsername.existingSecret.key=bootstrap-username
    --set adminApi.bootstrapPassword.existingSecret.name=review-admin-credentials
    --set adminApi.bootstrapPassword.existingSecret.key=bootstrap-password
    --set adminApi.csrfSecret.existingSecret.name=review-admin-credentials
    --set adminApi.csrfSecret.existingSecret.key=csrf-secret
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
assert_not_contains "$EXISTING_OUT" 'admin-bootstrap-username:' \
    "admin-bootstrap-username key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'admin-bootstrap-password:' \
    "admin-bootstrap-password key is absent from this chart's own Secret when existingSecret is used"
assert_not_contains "$EXISTING_OUT" 'admin-csrf-secret:' \
    "admin-csrf-secret key is absent from this chart's own Secret when existingSecret is used"
assert_contains "$EXISTING_OUT" $'name: review-admin-credentials\n                  key: bootstrap-username' \
    "REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME env references the given existingSecret name/key"
assert_contains "$EXISTING_OUT" $'name: review-admin-credentials\n                  key: bootstrap-password' \
    "REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD env references the given existingSecret name/key"
assert_contains "$EXISTING_OUT" $'name: review-admin-credentials\n                  key: csrf-secret' \
    "REVIEW_ADMIN_API_ADMIN_CSRF_SECRET env references the given existingSecret name/key"

echo
echo "=== 4. helm template (default values.yaml): Admin UI Deployment/Service (Phase 9, US-A20/US-A22) ==="
assert_contains "$DEFAULT_OUT" 'name: review-test-admin-api' \
    "default render includes the Admin UI Deployment/Service, named distinctly from webhook-api/worker"
assert_contains "$DEFAULT_OUT" $'app.kubernetes.io/component: admin-api' \
    "Admin UI resources carry their own component label"
assert_contains "$DEFAULT_OUT" $'livenessProbe:\n            httpGet:\n              path: /healthz\n              port: http' \
    "Admin UI Deployment has a livenessProbe against /healthz (Assumption 10)"
assert_contains "$DEFAULT_OUT" $'readinessProbe:\n            httpGet:\n              path: /healthz\n              port: http' \
    "Admin UI Deployment has a readinessProbe against /healthz (Assumption 10)"
# Assumption 10 — probes are new to the Admin UI Deployment only; the
# other two Deployments must not gain one as a drive-by of this phase.
# Isolate each Deployment's own rendered block (by its Source comment)
# rather than grepping $DEFAULT_OUT as a whole, since the bundled
# postgresql subchart's StatefulSet also has its own (pre-existing,
# unrelated) probes.
WORKER_BLOCK=$(awk '/# Source: review\/templates\/deployment-worker.yaml/,/^---$/' <<<"$DEFAULT_OUT")
WEBHOOK_API_BLOCK=$(awk '/# Source: review\/templates\/deployment-webhook-api.yaml/,/^---$/' <<<"$DEFAULT_OUT")
assert_not_contains "$WORKER_BLOCK" 'livenessProbe' \
    "the Worker Deployment is not retroactively given a probe by this phase"
assert_not_contains "$WEBHOOK_API_BLOCK" 'livenessProbe' \
    "the Webhook API Deployment is not retroactively given a probe by this phase"
ADMIN_API_DISABLED_OUT=$(helm template "$RELEASE" "$CHART_DIR" --set adminApi.enabled=false)
# Ingress still legitimately references the admin-api Service *name* as a
# backend (its default ingress.hosts[].paths route there regardless), so
# assert on the component label instead — that only ever renders on the
# Deployment/Service themselves, never the Ingress.
assert_not_contains "$ADMIN_API_DISABLED_OUT" 'app.kubernetes.io/component: admin-api' \
    "adminApi.enabled: false omits the Admin UI Deployment/Service entirely"

echo
echo "=== 5. helm template: Ingress (Phase 9, US-A21) ==="
assert_contains "$DEFAULT_OUT" 'kind: Ingress' \
    "default render includes an Ingress (ingress.enabled: true by default)"
assert_contains "$DEFAULT_OUT" 'name: review-test-admin-api' \
    "Ingress default render references the admin-api backend Service"
assert_contains "$DEFAULT_OUT" 'name: review-test-webhook-api' \
    "Ingress default render references the webhook-api backend Service"
INGRESS_TLS_SECRET_OUT=$(helm template "$RELEASE" "$CHART_DIR" \
    --set 'ingress.tls[0].secretName=review-tls' \
    --set 'ingress.tls[0].hosts[0]=review.example.com')
assert_contains "$INGRESS_TLS_SECRET_OUT" 'secretName: review-tls' \
    "Ingress renders TLS via a literal/existingSecret-style secretName"
INGRESS_CERT_MANAGER_OUT=$(helm template "$RELEASE" "$CHART_DIR" \
    --set 'ingress.tls[0].secretName=review-tls-cm' \
    --set 'ingress.tls[0].hosts[0]=review.example.com' \
    --set 'ingress.annotations.cert-manager\.io/cluster-issuer=letsencrypt-prod')
assert_contains "$INGRESS_CERT_MANAGER_OUT" 'cert-manager.io/cluster-issuer: letsencrypt-prod' \
    "Ingress renders a cert-manager issuer annotation as the second TLS mode"
assert_contains "$INGRESS_CERT_MANAGER_OUT" 'secretName: review-tls-cm' \
    "cert-manager mode still declares the Secret name cert-manager will populate"
INGRESS_DISABLED_OUT=$(helm template "$RELEASE" "$CHART_DIR" --set ingress.enabled=false)
assert_not_contains "$INGRESS_DISABLED_OUT" 'kind: Ingress' \
    "ingress.enabled: false omits the Ingress entirely, independent of adminApi.enabled"

echo
echo "=== 6. docker compose config: admin_api service (Phase 9, US-A20) ==="
COMPOSE_ROOT="$(cd "$CHART_DIR/../.." && pwd)"
COMPOSE_OUT=$(
    cd "$COMPOSE_ROOT" && \
    REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET=test-webhook-secret \
    REVIEW_WEBHOOK_API_GITHUB_APP_ID=1 \
    REVIEW_WEBHOOK_API_GITHUB_APP_PRIVATE_KEY=test-private-key \
    REVIEW_WORKER_GITHUB_APP_ID=1 \
    REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY=test-private-key \
    REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME=admin \
    REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD=test-bootstrap-password \
    REVIEW_ADMIN_API_ADMIN_CSRF_SECRET=test-csrf-secret \
    docker compose config
)
assert_contains "$COMPOSE_OUT" 'admin_api:' \
    "docker compose config validates the admin_api service block"
assert_contains "$COMPOSE_OUT" 'apps.admin_api.main:app' \
    "admin_api service runs its own uvicorn entrypoint, distinct from webhook_api/worker"
assert_contains "$COMPOSE_OUT" 'target: 8001' \
    "admin_api service exposes its own port, distinct from webhook_api's 8000"
assert_contains "$COMPOSE_OUT" "/healthz" \
    "admin_api service's healthcheck targets the real /healthz surface"

echo
if [[ "$FAILED" -eq 0 ]]; then
    echo "All Helm chart render checks passed."
else
    echo "One or more Helm chart render checks FAILED." >&2
    exit 1
fi
