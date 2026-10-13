{{/*
The release's base name: fullnameOverride, else the release name.
*/}}
{{- define "corpus.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "corpus.configFileName" -}}
{{- default (printf "%s-config-file" (include "corpus.fullname" .)) .Values.configFile.configMapName -}}
{{- end -}}

{{- define "corpus.commonEnvName" -}}
{{- default (printf "%s-config" (include "corpus.fullname" .)) .Values.commonEnv.configMapName -}}
{{- end -}}

{{- define "corpus.image" -}}
{{- printf "%s:%s" .Values.image.repository (default (printf "v%s" .Chart.AppVersion) .Values.image.tag) -}}
{{- end -}}

{{/*
Metadata labels for every object. Args: dict "root" $ "component" <string>.
commonLabels are applied last and win.
*/}}
{{- define "corpus.labels" -}}
{{- $root := .root -}}
{{- $l := dict
  "helm.sh/chart" (printf "%s-%s" $root.Chart.Name $root.Chart.Version | replace "+" "_")
  "app.kubernetes.io/name" $root.Chart.Name
  "app.kubernetes.io/instance" $root.Release.Name
  "app.kubernetes.io/version" $root.Chart.AppVersion
  "app.kubernetes.io/managed-by" $root.Release.Service -}}
{{- with .component }}{{ $_ := set $l "app.kubernetes.io/component" . }}{{ end -}}
{{- range $k, $v := $root.Values.commonLabels }}{{ $_ := set $l $k $v }}{{ end -}}
{{- toYaml $l -}}
{{- end -}}

{{/*
Layer dicts lowest to highest precedence: a later dict replaces a key outright,
except the map keys below, which merge per entry. Returns JSON (wrapped, so
fromJson always gets an object).
*/}}
{{- define "corpus.layer" -}}
{{- $out := dict -}}
{{- $mergeKeys := list "env" "secretEnv" "podLabels" "podAnnotations" -}}
{{- range . -}}
{{- if . -}}
{{- range $k, $v := . -}}
{{- if and (has $k $mergeKeys) (kindIs "map" $v) -}}
{{- $m := dict -}}
{{- with (get $out $k) }}{{ range $ek, $ev := . }}{{ $_ := set $m $ek $ev }}{{ end }}{{ end -}}
{{- range $ek, $ev := $v }}{{ $_ := set $m $ek $ev }}{{ end -}}
{{- $_ := set $out $k $m -}}
{{- else -}}
{{- $_ := set $out $k $v -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson (dict "v" $out) -}}
{{- end -}}

{{/*
The Deployment name of a served tier: deploy.name, else serve.server_name,
else the built-in server name of a built-in tier, else the tier name.
*/}}
{{- define "corpus.tierDeployName" -}}
{{- $t := . -}}
{{- $builtin := dict "sensitive" "corpus" "sanitized" "corpus-index" -}}
{{- $name := "" -}}
{{- with $t.deploy }}{{ $name = default "" .name }}{{ end -}}
{{- if not $name }}{{ with $t.serve }}{{ $name = default "" .server_name }}{{ end }}{{ end -}}
{{- if not $name }}{{ $name = default $t.name (get $builtin $t.name) }}{{ end -}}
{{- $name -}}
{{- end -}}

{{- define "corpus.sourceJobName" -}}
{{- $src := .source -}}
{{- $name := "" -}}
{{- with $src.runtime }}{{ $name = default "" .name }}{{ end -}}
{{- default (printf "%s%s" .prefix $src.name) $name -}}
{{- end -}}

{{/*
The container's env: plain values, then secret references, each sorted by name.
Arg: the layered workload dict.
*/}}
{{- define "corpus.env" -}}
{{- $w := . -}}
{{- range $k := keys (default dict $w.env) | sortAlpha }}
- name: {{ $k }}
  value: {{ index $w.env $k | quote }}
{{- end }}
{{- range $k := keys (default dict $w.secretEnv) | sortAlpha }}
{{- $ref := index $w.secretEnv $k }}
- name: {{ $k }}
  valueFrom:
    secretKeyRef:
      name: {{ required (printf "secretEnv.%s.name is required" $k) $ref.name }}
      key: {{ required (printf "secretEnv.%s.key is required" $k) $ref.key }}
{{- end }}
{{- end -}}

{{/*
envFrom: the shared environment, then each named Secret.
Args: dict "root" $ "w" <layered workload>.
*/}}
{{- define "corpus.envFrom" -}}
- configMapRef:
    name: {{ include "corpus.commonEnvName" .root }}
{{- range .w.envFromSecrets }}
- secretRef:
    name: {{ . }}
{{- end }}
{{- end -}}

{{/*
The container's command, always explicit: an adopting upgrade patches the live
object against the render, so a command the render leaves out survives from
the live object. Args: dict "name" <workload> "w" <layered workload>.
*/}}
{{- define "corpus.command" -}}
{{- if not .w.command }}{{ fail (printf "%s: command must not be empty" .name) }}{{ end -}}
{{- toYaml .w.command -}}
{{- end -}}

{{- define "corpus.configMount" -}}
- name: corpus-config-file
  mountPath: {{ .Values.configFile.mountPath }}
  readOnly: true
{{- end -}}

{{- define "corpus.configVolume" -}}
- name: corpus-config-file
  configMap:
    name: {{ include "corpus.configFileName" . }}
{{- end -}}

{{/*
Pod-level scheduling and security fields shared by every pod spec.
Arg: dict "root" $ "w" <layered workload>.
*/}}
{{- define "corpus.podCommon" -}}
{{- $w := .w -}}
{{- with $w.serviceAccountName }}
serviceAccountName: {{ . }}
{{- end }}
{{- if hasKey $w "automountServiceAccountToken" }}{{ if not (kindIs "invalid" $w.automountServiceAccountToken) }}
automountServiceAccountToken: {{ $w.automountServiceAccountToken }}
{{- end }}{{ end }}
{{- with .root.Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $w.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $w.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $w.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $w.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
The config file: the values' tiers and sources without the chart-only keys.
*/}}
{{- define "corpus.configFile" -}}
{{- $cfg := dict -}}
{{- if .Values.tiers -}}
{{- $tiers := list -}}
{{- range .Values.tiers -}}
{{- $t := omit . "deploy" -}}
{{- if kindIs "map" $t.serve -}}
{{- $s := omit $t.serve "enabled" -}}
{{- if $s }}{{ $_ := set $t "serve" $s }}{{ else }}{{ $t = omit $t "serve" }}{{ end -}}
{{- end -}}
{{- $tiers = append $tiers $t -}}
{{- end -}}
{{- $_ := set $cfg "tiers" $tiers -}}
{{- end -}}
{{- if .Values.sources -}}
{{- $sources := list -}}
{{- range .Values.sources -}}
{{- $sources = append $sources (omit . "runtime") -}}
{{- end -}}
{{- $_ := set $cfg "sources" $sources -}}
{{- end -}}
{{- if $cfg }}{{ toYaml $cfg }}{{ else }}{}{{ end -}}
{{- end -}}
