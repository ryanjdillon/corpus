{{/*
One CronJob running a corpus container. Args: dict "root" $ "name" <name>
"component" <label> "w" <layered workload>. The workload supplies schedule,
suspend, timeZone, the job and pod fields, and command/args.
*/}}
{{- define "corpus.cronjob" -}}
{{- $root := .root -}}
{{- $w := .w -}}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ .name }}
  labels:
    {{- include "corpus.labels" (dict "root" $root "component" .component) | nindent 4 }}
spec:
  schedule: {{ required (printf "%s: schedule is required" .name) $w.schedule | quote }}
  {{- with $w.timeZone }}
  timeZone: {{ . }}
  {{- end }}
  {{- if $w.suspend }}
  suspend: true
  {{- end }}
  concurrencyPolicy: {{ default "Forbid" $w.concurrencyPolicy }}
  successfulJobsHistoryLimit: {{ $w.successfulJobsHistoryLimit | int64 }}
  failedJobsHistoryLimit: {{ $w.failedJobsHistoryLimit | int64 }}
  jobTemplate:
    spec:
      {{- if not (kindIs "invalid" $w.backoffLimit) }}
      backoffLimit: {{ $w.backoffLimit | int64 }}
      {{- end }}
      {{- if not (kindIs "invalid" $w.activeDeadlineSeconds) }}
      activeDeadlineSeconds: {{ $w.activeDeadlineSeconds | int64 }}
      {{- end }}
      template:
        metadata:
          labels:
            {{- $pl := merge (dict "app.kubernetes.io/instance" $root.Release.Name "app.kubernetes.io/component" .component) (default dict $w.podLabels) }}
            {{- toYaml $pl | nindent 12 }}
          {{- with $w.podAnnotations }}
          annotations:
            {{- toYaml . | nindent 12 }}
          {{- end }}
        spec:
          restartPolicy: {{ $w.restartPolicy }}
          {{- if not (kindIs "invalid" $w.terminationGracePeriodSeconds) }}
          terminationGracePeriodSeconds: {{ $w.terminationGracePeriodSeconds | int64 }}
          {{- end }}
          {{- with (include "corpus.podCommon" (dict "root" $root "w" $w) | trim) }}{{ . | nindent 10 }}{{ end }}
          {{- with $w.initContainers }}
          initContainers:
            {{- range . }}
            {{- $c := . }}
            {{- if not $c.image }}{{ $c = set (deepCopy $c) "image" (include "corpus.image" $root) }}{{ end }}
            - {{- toYaml $c | nindent 14 }}
            {{- end }}
          {{- end }}
          containers:
            - name: {{ $w.containerName }}
              image: {{ include "corpus.image" $root }}
              {{- with $root.Values.image.pullPolicy }}
              imagePullPolicy: {{ . }}
              {{- end }}
              {{- with $w.command }}
              command:
                {{- toYaml . | nindent 16 }}
              {{- end }}
              args:
                {{- toYaml $w.args | nindent 16 }}
              envFrom:
                {{- include "corpus.envFrom" (dict "root" $root "w" $w) | nindent 16 }}
              {{- with (include "corpus.env" $w | trim) }}
              env:
                {{- . | nindent 16 }}
              {{- end }}
              {{- with $w.resources }}
              resources:
                {{- toYaml . | nindent 16 }}
              {{- end }}
              {{- with $w.securityContext }}
              securityContext:
                {{- toYaml . | nindent 16 }}
              {{- end }}
              volumeMounts:
                {{- with $w.extraVolumeMounts }}
                {{- toYaml . | nindent 16 }}
                {{- end }}
                {{- include "corpus.configMount" $root | nindent 16 }}
          volumes:
            {{- with $w.extraVolumes }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
            {{- include "corpus.configVolume" $root | nindent 12 }}
{{- end -}}
