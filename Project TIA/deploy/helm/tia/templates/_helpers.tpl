{{- define "tia.labels" -}}
app.kubernetes.io/name: tia
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "tia.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{- define "tia.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
{{- end -}}

{{- define "tia.secretFile" -}}
- name: {{ .env }}_FILE
  value: /run/secrets/tia/{{ .key }}
{{- end -}}

{{- define "tia.commonEnv" -}}
- name: TIA_WORKSPACE
  value: /var/lib/tia/repos
- name: TIA_LOG_FORMAT
  value: json
- name: TIA_AUDIT_KEY_FILE
  value: /run/secrets/tia/audit-key
- name: TIA_MASTER_KEY_FILE
  value: /run/secrets/tia/master-key
- name: TIA_METRICS_TOKEN_FILE
  value: /run/secrets/tia/metrics-token
{{- range $k, $v := .Values.extraEnv }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}

{{- define "tia.volumes" -}}
- name: secrets
  secret: {secretName: {{ .Values.existingSecret }}, defaultMode: 0400}
- name: repos
  persistentVolumeClaim: {claimName: {{ .Release.Name }}-repos}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "tia.mounts" -}}
- {name: secrets, mountPath: /run/secrets/tia, readOnly: true}
- {name: repos, mountPath: /var/lib/tia/repos}
- {name: tmp, mountPath: /tmp}
{{- end -}}
