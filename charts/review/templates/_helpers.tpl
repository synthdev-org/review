{{/*
Chart/release naming, shared labels, and the credential-field helpers that
back every existingSecret-capable field in values.yaml. Kept in one file
per Helm convention (`helm create`'s own _helpers.tpl layout).
*/}}

{{- define "review.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "review.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "review.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "review.labels" -}}
helm.sh/chart: {{ include "review.chart" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: review
{{- end -}}

{{/*
Per-component selector labels. `component` is "webhook-api" or "worker".
Usage: include "review.selectorLabels" (dict "root" . "component" "webhook-api")
*/}}
{{- define "review.selectorLabels" -}}
app.kubernetes.io/name: {{ include "review.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
The fullname of this chart's own generated credentials Secret (as opposed
to any operator-supplied existingSecret referenced per-field).
*/}}
{{- define "review.secretName" -}}
{{- printf "%s-credentials" (include "review.fullname" .) -}}
{{- end -}}

{{/*
Renders one container `env:` entry sourcing a credential field from either
the operator's existingSecret reference or this chart's own generated
Secret — the one shared shape every credential field in values.yaml
follows (see values.yaml's header comment).

Usage:
  {{ include "review.credentialEnvVar" (dict "envName" "FOO" "field" .Values.some.field "secretName" (include "review.secretName" $) "secretKey" "some-field") | nindent 12 }}
*/}}
{{- define "review.credentialEnvVar" -}}
- name: {{ .envName }}
  valueFrom:
    secretKeyRef:
      {{- if .field.existingSecret.name }}
      name: {{ .field.existingSecret.name }}
      key: {{ required (printf "%s.existingSecret.key is required when %s.existingSecret.name is set" .envName .envName) .field.existingSecret.key }}
      {{- else }}
      name: {{ .secretName }}
      key: {{ .secretKey }}
      {{- end }}
{{- end -}}

{{/*
The REVIEW_DATABASE_URL DSN this chart's own Secret embeds when
database.url has no existingSecret reference. An explicit
database.url.value always wins; otherwise, when postgresql.enabled is
true, it's derived from the bundled subchart's own auth values (host is
the subchart's Service name, per charts/postgresql/templates/_helpers.tpl
"<release>-postgresql"); otherwise there is no safe default and rendering
fails closed rather than silently pointing at a database that doesn't
exist.
*/}}
{{- define "review.databaseUrl" -}}
{{- if .Values.database.url.value -}}
{{- .Values.database.url.value -}}
{{- else if .Values.postgresql.enabled -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password .Release.Name .Values.postgresql.auth.database -}}
{{- else -}}
{{- fail "database.url.value (or database.url.existingSecret) must be set when postgresql.enabled is false — no external database was configured" -}}
{{- end -}}
{{- end -}}
