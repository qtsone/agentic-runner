{{- define "agentic-runner.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentic-runner.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "agentic-runner.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "agentic-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: runner
app.kubernetes.io/part-of: agentic-os
app.kubernetes.io/version: {{ include "agentic-runner.tag" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "agentic-runner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentic-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "agentic-runner.tag" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}

{{- define "agentic-runner.agentTokenSecret" -}}
{{- default (printf "%s-agent-token" (include "agentic-runner.fullname" .)) .Values.agentToken.existingSecret -}}
{{- end -}}

{{- define "agentic-runner.recipientKeySecret" -}}
{{- default (printf "%s-recipient-key" (include "agentic-runner.fullname" .)) .Values.recipientKey.secretName -}}
{{- end -}}

{{- define "agentic-runner.tags" -}}
{{- $pairs := list -}}
{{- range $key, $value := .Values.tags -}}
{{- $pairs = append $pairs (printf "%s=%s" $key $value) -}}
{{- end -}}
{{- join "," $pairs -}}
{{- end -}}

{{/* The uid the Runner runs as: root with capabilities under contract_uid (17 A1), the
     image's unprivileged `runner` user under none (17 A2). */}}
{{- define "agentic-runner.contractUid" -}}
{{- eq .Values.isolation "contract_uid" -}}
{{- end -}}

{{- define "agentic-runner.podSecurityContext" -}}
{{- if eq (include "agentic-runner.contractUid" .) "true" }}
runAsNonRoot: false
runAsUser: 0
runAsGroup: 0
seccompProfile:
  type: RuntimeDefault
{{- else }}
runAsNonRoot: true
runAsUser: 65532
runAsGroup: 65532
fsGroup: 65532
# Once, on the empty volume: the default re-applies g+rw to every file on every mount,
# and the Runner refuses an identity file a group can read.
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end }}
{{- end -}}

{{- define "agentic-runner.containerSecurityContext" -}}
{{- if eq (include "agentic-runner.contractUid" .) "true" }}
# ADR-0015 §1: SETUID/SETGID to spawn a Directive, a verifier or a hook as the Contract's
# uid, CHOWN/FOWNER to hand it its Workspace and harness root, DAC_OVERRIDE to keep
# reading the 0700 trees it just handed over -- and nothing else. NoNewPrivs stays off
# because the bounding set below is the real bound and getting it wrong strips
# CAP_SETUID from every Directive (17 A1).
allowPrivilegeEscalation: true
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
  add: ["SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE"]
{{- else }}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
{{- end -}}
