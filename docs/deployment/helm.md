# Helm chart

`deploy/helm/corpus` deploys corpus on Kubernetes. One release is one
**security domain**. The chart's values map one to one onto corpus's own
config file, so a domain is described once, in the terms corpus already uses.
The values reference is in the chart's
[README](https://github.com/ryanjdillon/corpus/blob/main/deploy/helm/corpus/README.md).

## A release is a domain

A release renders one domain: its config file, a `corpus pipeline` CronJob per
source, a `corpus serve` Deployment and Service per served tier, and the
domain's NetworkPolicies. All of it goes into the release namespace. Two
domains, such as a person's mail and a club's shared mailboxes, are two
releases of the same chart in two namespaces. Each has its own databases,
Secrets, gateway key and network policy. Scaling up means adding sources,
tiers or releases. It never means forking the chart.

This follows the isolation model in `GOVERNANCE.md`: domains that must never
mix are separated physically. Within a release, every store connects as a
single database role and sets no per-request principal. Row-Level Security
therefore cannot separate users inside a domain, and the chart does not
pretend to. What a domain's server can read, every caller the gateway admits
to it can read. Data that must stay apart goes in another release, with its
own database. Per-principal filtering inside a domain waits for roadmap stages
3–4 (OIDC identity and a policy decision point). Nothing in the chart assumes a
shared key or a single principal across releases.

## Values are the config file

The config file (`CORPUS_CONFIG_FILE`) has two lists, `tiers:` and `sources:`.
The chart's `tiers` and `sources` values are those lists, entry for entry. The
chart renders them into a ConfigMap, mounts it at `/etc/corpus/corpus.yaml` in
every corpus container and sets `CORPUS_CONFIG_FILE` to that path. The chart
adds three keys of its own, which it removes before writing the file:

| Key | Becomes |
| --- | --- |
| `tiers[].serve.enabled` | A Deployment and Service running `corpus serve <tier>` |
| `tiers[].deploy` | That Deployment's pod settings and name overrides |
| `sources[].runtime` | The source's CronJob (`corpus pipeline <kind>:<name>`): schedule, secrets, pod settings |

```yaml
tiers:
  - name: sanitized              # → tiers[0] in corpus.yaml
    serve:
      access: [orchestrator]     # → serve.access
      enabled: true              # chart only
    deploy:                      # chart only
      secretEnv:
        CORPUS_INDEX_DATABASE_URL: {name: corpus-index-db, key: DATABASE_URL}
sources:
  - name: inbox-a                # → sources[0] in corpus.yaml
    kind: imap
    enrich: true
    pipeline: {limit: 200}
    runtime:                     # chart only
      schedule: "40 4,16 * * *"
      envFromSecrets: [mail-inbox-a]
```

Since corpus 0.6.0, a tier named like a built-in tier (`sensitive`,
`sanitized`) inherits every key it leaves out
([Tiers](../tiers.md#built-in-tiers-fill-what-is-left-out)). A values file
therefore carries only what a domain changes. The schema refuses unknown keys,
both in the chart's own blocks and in the config entries, so a typo fails at
`helm lint` rather than when corpus starts. Environment values (`env`,
`commonEnv.vars`) must be strings: Helm reads an unquoted number as a float and
would write `2000000` as `2e+06`, so the schema refuses it.

The file holds no secret, and neither does the chart. A tier names the variable
that holds its DSN, and a source names its credential prefix. The chart wires
those variables from Secrets by name and key (`secretEnv`, `envFromSecrets`).
It never renders a Secret.

## Contracts

These properties of the rendered objects are relied on outside the chart, by
alerts, dashboards and gateway routes. The chart keeps them by default, or
offers a value that keeps them:

- **Names.** A served tier's Deployment takes the tier's server name: `corpus`
  and `corpus-index` for the built-in tiers, `serve.server_name` for any
  other. Its Service has the same name unless `deploy.serviceName` is set.
  Pipeline CronJobs are `corpus-pipeline-<source>`. `defaults.pipeline.namePrefix`
  changes the prefix for the whole release and `runtime.name` changes one
  source's name. A `jobs` entry is named by its key.
- **Selectors.** A Deployment's selector is `app.kubernetes.io/name: <name>`
  and nothing else. A selector cannot change, and NetworkPolicies select pods
  by this label.
- **Telemetry.** `OTEL_EXPORTER_OTLP_ENDPOINT` comes from `otel.endpoint`. The
  chart sets no `OTEL_SERVICE_NAME` unless `otel.serviceName` is set, so each
  command reports its own service name by default. A domain whose alerts expect
  one name sets it there.
- **Rollout on config change.** Server pods carry `checksum/config`, the sha256
  of both rendered ConfigMaps (the config file and the shared environment), so
  a change to either rolls them.
- **Secrets by name.** Environment variable names and `secretKeyRef`s are
  exactly what the values say. Secret contents never pass through the chart.
- **Network policy.** `networkPolicies.<name>` is a NetworkPolicy `spec`,
  rendered as written. That covers default-deny and egress to pinned IPs
  (a policy cannot match a hostname).
- **Pod details.** Node selectors, tolerations, resources, security contexts,
  probes, schedules, time zones, suspend flags, deadlines, backoff and restart
  policy, grace periods and the image tag can all be set per workload. The
  exception is the concurrency policy: it defaults to `Forbid` and can be
  overridden, but a pipeline should not run twice at once.

## Not in the chart

- Gateway resources (MCPRoute, SecurityPolicy) generated from `serve.access`.
  `gateway.enabled` exists and fails the render until they are implemented.
- The `worker` runtime for sources (a long-running consumer instead of a
  CronJob). `runtime.mode: worker` fails the render for now.
- Cluster-specific companions such as GPU scheduling swaps, digest jobs,
  egress scanners, and the RBAC that lets them create Jobs. These stay with the
  cluster's other manifests.
- Secrets and the Namespace.

## Migrating from plain manifests

Move one domain at a time, starting with the personal domain:

1. Write the domain's values beside its manifests. Copy the config file's
   `tiers:` and `sources:` into `tiers`/`sources`, and set the name overrides
   that keep today's object names. If the manifests set no security
   context, null the chart's hardening defaults
   (`defaults.pod.{podSecurityContext,securityContext,automountServiceAccountToken}: null`)
   so the render matches. This is required, not just tidier, while any job
   has an init container that runs as root: the default `runAsNonRoot` binds
   init containers too and would refuse it. Enable the hardening afterwards,
   once no pod needs root.
2. Render both and compare them with `deploy/helm/scripts/manifest_diff.py`.
   It matches objects by kind and name, ignores Helm and kustomize bookkeeping
   and Secret payloads, and compares the config file by content. Expect
   differences only in Secrets and the Namespace (not rendered),
   `checksum/config`, and the standard `app.kubernetes.io/*` labels on object
   metadata.
3. Replace the Flux `Kustomization` path for the workloads with a
   `HelmRelease` of this chart, in the same namespace. Keep the Secrets
   (SOPS) and anything listed under [Not in the chart](#not-in-the-chart)
   in the Kustomization. A `commonMetadata` label set on the Kustomization
   moves to the chart's `commonLabels`.
4. Hand the objects over. Helm refuses to install over objects that carry
   no release ownership, and kustomize-controller prunes whatever it applied
   once it leaves the Kustomization. There are two ways to hand over:
   - **Adopt.** Before the switch, annotate each object the chart will own
     with `meta.helm.sh/release-name` and `meta.helm.sh/release-namespace`,
     label it `app.kubernetes.io/managed-by: Helm`, and annotate it
     `kustomize.toolkit.fluxcd.io/prune: disabled` so that removing it from
     the Kustomization does not delete it. Names and selectors are unchanged,
     so Helm then updates the objects in place without recreating them.
   - **Replace.** Let Flux prune the old objects, then let the HelmRelease
     create them again. The servers are briefly down. Do it when no CronJob
     is running.

   Then check that the gateway routes resolve and that the next pipeline
   run succeeds.
