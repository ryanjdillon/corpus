# corpus Helm chart

Deploys one corpus **security domain** per release: the config file (tiers and
sources), a pipeline CronJob per source, an MCP server per served tier, and the
NetworkPolicies around them. The model, the contracts the chart keeps and the
migration from plain manifests are in
[docs/deployment/helm.md](../../../docs/deployment/helm.md).

```sh
helm template corpus deploy/helm/corpus -n corpus \
  -f deploy/helm/examples/values-personal.yaml
helm lint --strict deploy/helm/corpus -f deploy/helm/examples/values-shared-tier.yaml
just helm-check     # lint and render both examples
```

## One release, one domain

A release holds one domain's config, workloads and network policy, in the
release namespace. A second domain is a second release in a second namespace,
with its own databases, credentials and gateway key. It is never a second set of
tiers in the first release. Growing to more domains or more sources is a matter
of values, not of forking the chart.

**Within a release there is no per-user isolation.** Each store connects as one
database role and sets no per-request principal, so Row-Level Security cannot
tell principals apart (`GOVERNANCE.md`, *Isolation model*). Everything a
domain's servers can read, every caller the gateway admits to them can read.
Data that must not mix belongs in another release, with its own database.

## What it renders

| Object | From | Name |
| --- | --- | --- |
| ConfigMap (config file) | `tiers`, `sources` | `configFile.configMapName`, default `<release>-config-file` |
| ConfigMap (shared env) | `commonEnv.vars`, `server`, `otel`, `configFile` | `commonEnv.configMapName`, default `<release>-config` |
| Deployment + Service | each tier with `serve.enabled: true` | see [Names](#names) |
| CronJob | each source (`runtime.mode: cronjob`) | `runtime.name`, default `<defaults.pipeline.namePrefix><source name>` |
| CronJob | each `jobs` entry | its key |
| NetworkPolicy | each `networkPolicies` entry | its key |

It renders no Secret and no Namespace. Secrets are referenced by name and key
only, and are created next to the release (SOPS, External Secrets, ...). The
namespace is the release's.

## Names

- **Deployment**: `tiers[].deploy.name`, else `serve.server_name`, else the
  built-in server name (`sensitive` → `corpus`, `sanitized` → `corpus-index`),
  else the tier name. Its selector is `app.kubernetes.io/name: <name>` alone.
  A selector cannot change, so the chart never adds to it.
- **Service**: `tiers[].deploy.serviceName`, else the Deployment's name.
- **Pipeline CronJob**: `sources[].runtime.name`, else
  `defaults.pipeline.namePrefix` + the source name (`corpus-pipeline-` by
  default).

Use the overrides to keep the names existing alerts, dashboards and gateway
backends refer to.

## Values

The config file is written from `tiers` and `sources`, entry for entry.
Each entry is exactly a corpus config entry ([Tiers](../../../docs/tiers.md),
[Source configuration](../../../docs/fetchers/config.md)), plus chart-only keys
that are stripped before the file is written:

| Chart-only key | Meaning |
| --- | --- |
| `tiers[].serve.enabled` | Run `corpus serve <tier>` as a Deployment and Service (default `false`). |
| `tiers[].deploy` | That Deployment's settings (below). |
| `sources[].runtime` | The source's CronJob settings (below). `mode: worker` is not implemented yet and fails the render. |

An empty `tiers` writes no `tiers:` (corpus's built-in tiers apply), and an empty
`sources` writes no `sources:` and renders no CronJob. Since corpus 0.6.0 a tier
named `sensitive` or `sanitized` inherits every key it leaves out from the
built-in tier, so a tier entry need carry only what differs.

### Workload settings and layering

Each workload's settings are layered. `defaults.pod` applies to every pod.
`defaults.serve` or `defaults.pipeline` applies next, then the workload's own
block (`tiers[].deploy` or `sources[].runtime`). A higher layer replaces a key,
except `env`, `secretEnv`, `podLabels` and `podAnnotations`, which merge entry
by entry. Each entry under `jobs` layers on `defaults.pod` only. To drop a
chart default, set it to `null` in a values file, e.g.
`defaults.pod.podSecurityContext: null`.

| Key | Where | Meaning |
| --- | --- | --- |
| `nodeSelector`, `tolerations`, `affinity` | all | Scheduling. |
| `podSecurityContext`, `securityContext` | all | Default: non-root uid 1000, RuntimeDefault seccomp, no privilege escalation, all capabilities dropped. See [Root init containers](#root-init-containers). |
| `serviceAccountName`, `automountServiceAccountToken` | all | Default: the namespace's default account, token not mounted. |
| `containerName` | all | `server`, `pipeline`, `job` by default. |
| `command` | all | The container command, `[corpus]` (the image's `ENTRYPOINT`) by default. Always rendered and never empty; see [Adopting existing objects](#adopting-existing-objects). |
| `resources` | all | Container resources. |
| `env` | all | Plain variables, `NAME: "value"`. Strings only: quote numbers (see below). |
| `secretEnv` | all | `NAME: {name: <secret>, key: <key>}`, a `secretKeyRef` each. |
| `envFromSecrets` | all | Secrets loaded whole (`envFrom`), after the shared ConfigMap. |
| `podLabels`, `podAnnotations` | all | On the pod template. |
| `terminationGracePeriodSeconds` | all | Pod field. |
| `replicas`, `strategy`, `readinessProbe`, `livenessProbe`, `servicePortName` | serve | The default readiness probe is an exec probe on localhost, so an ingress policy that admits only the gateway cannot block it. |
| `name`, `serviceName` | `tiers[].deploy` | Name overrides. |
| `schedule`, `timeZone`, `suspend`, `concurrencyPolicy`, `successfulJobsHistoryLimit`, `failedJobsHistoryLimit`, `backoffLimit`, `activeDeadlineSeconds`, `restartPolicy` | pipeline, jobs | CronJob and Job fields. `concurrencyPolicy` defaults to `Forbid`. |
| `namePrefix` | `defaults.pipeline` | Default CronJob name prefix. |
| `name`, `mode`, `extraArgs` | `sources[].runtime` | Name override, `cronjob` (`worker` is not implemented yet), arguments after `pipeline <kind>:<name>`. |
| `args`, `initContainers`, `extraVolumes`, `extraVolumeMounts` | jobs | The job's arguments. `args` is required, and `suspend` defaults to `true`. An init container is a container spec and must set `command`. One without `image` gets the chart's image, and runs under the pod's `podSecurityContext`. |

### Release-wide

| Key | Meaning |
| --- | --- |
| `image.repository`, `image.tag`, `image.pullPolicy`, `imagePullSecrets` | The tag defaults to `v<appVersion>`. |
| `server.host`, `server.port` | `CORPUS_HOST`, `CORPUS_MCP_PORT` (set on each server container; the schema refuses it in `env` and `commonEnv.vars`) and the Service port. |
| `otel.endpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| `otel.serviceName` | `OTEL_SERVICE_NAME` for every workload. Unset by default, so each command reports its own name. |
| `configFile.mountPath`, `configFile.fileName` | Where the file is mounted. `CORPUS_CONFIG_FILE` is set to it. |
| `commonEnv.vars` | Shared non-secret settings ([Configuration](../../../docs/configuration.md)). Strings only, like `env`. |
| `commonLabels` | Added to every object's metadata, never to selectors. |
| `networkPolicies.<name>` | A NetworkPolicy `spec`, rendered as written. |
| `gateway.enabled` | Gateway resources are not implemented yet. `true` fails the render. |

`values.schema.json` refuses unknown keys at every level the chart owns, and in
the corpus config entries too, so a misspelt key fails `helm lint`/`template`
rather than reaching corpus.

`env` and `commonEnv.vars` values must be strings. Helm reads an unquoted
number as a float and would write `2000000` as `2e+06`, so the schema refuses
it: write `"2000000"`. Integer fields (`replicas`, `activeDeadlineSeconds`,
ports, ...) take plain numbers and render exactly.

Every served pod carries a `checksum/config` annotation: the sha256 of both
rendered ConfigMaps, the config file and the shared environment. A change to
either therefore rolls the servers. A CronJob reads both fresh on each run.

### Root init containers

The default `podSecurityContext` (`runAsNonRoot`, uid 1000) binds every
container in the pod, init containers included. An init container that must
run as root, such as a backfill job's setup step, is refused under it, so the
hardening defaults cannot simply be enabled while such a job exists. Set the
pod fields to `null` on that job, or, for a like-for-like migration of
manifests that set none of them, drop all three defaults:

```yaml
defaults:
  pod:
    podSecurityContext: null
    securityContext: null
    automountServiceAccountToken: null
```

## Adopting existing objects

When the release takes over objects that already exist, Helm patches them
against the render rather than replacing them, so a field set only on the live
object survives. A live `command: [sh, -c]` would wrap the chart's arguments
and break the container. The chart therefore always renders `command`, and
requires it on init containers. ConfigMap data keys behave the same way: a
key the values no longer set stays in the adopted ConfigMap. Compare each live
object (`kubectl get -o yaml`) with the render before adopting; see
[Live-only fields survive adoption](../../../docs/deployment/helm.md#live-only-fields-survive-adoption).

## Examples

- [`examples/values-personal.yaml`](../examples/values-personal.yaml): one
  Gmail source, both tiers served, an on-demand backfill job, and gateway-only
  ingress.
- [`examples/values-shared-tier.yaml`](../examples/values-shared-tier.yaml): a shared-mailbox
  domain. Three IMAP sources, each with its own raw schema, login and DSN,
  feed one shared sanitized tier. There is a domain-wide `OTEL_SERVICE_NAME`,
  a default-deny policy, and egress to the mail server by pinned IP.

Both are synthetic. Real deployments keep their values next to their other
manifests.

## Checking a migration

[`scripts/manifest_diff.py`](../scripts/manifest_diff.py) compares two
rendered manifest streams by kind and name. It ignores tool bookkeeping and
Secret payloads, and compares ConfigMap YAML documents by content:

```sh
kustomize build path/to/app > before.yaml
helm template corpus deploy/helm/corpus -n <ns> -f values.yaml > after.yaml
python deploy/helm/scripts/manifest_diff.py before.yaml after.yaml \
  --add-labels-a app.kubernetes.io/name=corpus
```
