{{/*
Fullname for subchart-owned resources. Namespaced under the parent
release name so it doesn't collide with a sibling install, mirroring the
parent chart's own review.fullname helper.
*/}}
{{- define "postgresql.fullname" -}}
{{- printf "%s-postgresql" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "postgresql.labels" -}}
app.kubernetes.io/name: postgresql
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: review
{{- end -}}

{{- define "postgresql.selectorLabels" -}}
app.kubernetes.io/name: postgresql
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
