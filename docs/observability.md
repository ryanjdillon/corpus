# Observability

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (and optionally `OTEL_SERVICE_NAME`) and the
MCP servers and batch processes export OpenTelemetry over OTLP. A server reports
as `corpus-mcp` (`corpus serve sensitive`) or `corpus-index` (`corpus serve
sanitized`) unless `OTEL_SERVICE_NAME` says otherwise.

- **Traces** — outgoing httpx spans (embedding calls, source APIs).
- **Metrics** — ingest counters and histograms (documents written and skipped,
  embed latency, batch size), a corpus-size gauge by data-class (from the
  sensitive server), MCP tool calls by principal (see below), and enrichments
  whose free text reads like instructions to an AI
  (`corpus.enrich.instruction_like`, by model), and each sync's backlog (see
  below).

It is a no-op when the endpoint is unset. Heavy SDK imports are deferred until
telemetry is configured, and a one-shot process (an ingest run) flushes the
exporters on exit so its metrics are not lost.

See the [architecture diagram](architecture.html) for where the instruments sit
in the pipeline.

## Auditing MCP tool calls

Every MCP tool call, on both `corpus` and `corpus-index`, emits one audit event:
a JSON line on the `corpus.audit` logger, always (it needs no OTLP endpoint), and
the metrics below when telemetry is on. The event answers "which principal read
which tier, how much, and when", and carries no row content.

```
2026-01-01 07:00:01,042 corpus.audit {"duration_ms": 12.4, "event": "mcp.tool_call", \
    "filters": {"limit": 50, "query": {"chars": 21}}, "outcome": "ok", \
    "principal": "orchestrator", "rows": 12, "server": "corpus-index", \
    "tier": "sanitized", "tool": "index_action_items"}
```

- `principal` is the `x-client-id` request header. The gateway overwrites it
  from the caller's API key, so a client cannot pick its own. A caller that
  bypasses the gateway (an in-cluster job) is logged as `unknown`, not refused;
  a repeated header is `ambiguous`, and a value not shaped like a client id
  (`[a-z0-9_-]{1,32}`) is `other`, which keeps the metric label bounded.
- `filters` is the call's argument shape. A value is logged verbatim only when
  it is safe in itself: a number, a boolean, a member of the argument's closed
  vocabulary (`label`, `domain`, `importance`, `who`, `waiting_on`, `order`,
  `group_by`) or a list of them, or a date or duration. A document id keeps its
  source and is otherwise a short hash, since the rest can name a mail folder.
  Any other string, such as a search query or an address, is logged as its
  length, and any other list as its size. A query spec is summarised field by
  field, so the event states the filters and window a call read.
- `rows` is the envelope's `count` (see [MCP results](mcp-results.md));
  `outcome` is `ok` or the exception type of a failed call.

Only calls that reach a tool are audited: FastMCP rejects arguments that fail a
tool's schema, or an unknown tool name, before the tool runs. The gateway's
access log covers those.

Metrics, attributed by `server`, `tier`, `tool` and `principal`:
`corpus.mcp.calls` (also by `outcome`), `corpus.mcp.rows`, and the latency
histogram `corpus.mcp.duration` in milliseconds (by server, tier and tool only).

In Loki, the events are one query, for example rows per principal and tier:

```logql
sum by (principal, tier) (
  sum_over_time(
    {namespace=~"corpus.*", container=~"mcp|index"} |= "corpus.audit"
      | regexp "corpus.audit (?P<event>.*)" | line_format "{{.event}}" | json
      | outcome="ok" | unwrap rows [1h]))
```

## Sync backlog

Every sync, `corpus sync` and the sync stage of `corpus pipeline` alike,
records what it left behind and what it did, attributed by `tier` and its
`schema`. Not by source: the sources syncing into a tier share its backlog, so
a per-source label would report the same tier-wide value under each.

| Metric | Type | Reads |
|---|---|---|
| `corpus.sync.deferred` | gauge | rows still projected under another policy, served as placeholders (the run's `pending`) |
| `corpus.sync.embed_stale` | gauge | rows whose vector is older than their text (embedded under a different policy from the one their text is projected under), left out of `search` until re-embedded (the run's `embed_stale`) |
| `corpus.sync.reembed_skipped` | gauge | 1 if the run's [re-embedding was skipped](tiers.md#text-now-embeddings-later) for another sync's lock, else 0 |
| `corpus.sync.reprojected` | gauge | rows the run rewrote only because of the policy or `--reproject` |
| `corpus.sync.embedded` | gauge | vectors the run stored, in either phase |
| `corpus.sync.capped` | gauge | new rows the run held back for `--limit`, left for a later sync (the run's `capped`) |

Every one is a gauge holding the **last sync's value**, set at the end of the
run; `deferred` and `embed_stale` are `SanitizedStore.backlog` read then. With
the default `corpus.` to `corpus_` name translation they export as
`corpus_sync_deferred`, `corpus_sync_embed_stale`, `corpus_sync_reembed_skipped`,
`corpus_sync_reprojected`, `corpus_sync_embedded` and `corpus_sync_capped`: gauges take no `_total`
suffix, and the `{row}` and `{run}` units add none either.

They are gauges rather than counters because of how a sync runs. Each sync is a
one-shot process, so its instruments start from nothing and are exported about
once, when it flushes on exit. And **every run is its own series**: the
OpenTelemetry SDK gives each process a random `service.instance.id`, which a
Prometheus backend turns into the `instance` label. A rate over one series
would see a single sample, so never apply `rate` or `increase` to these
metrics; read each series with `last_over_time` or `min_over_time`.

Several runs of one tier, such as one job per source syncing into a shared
tier, therefore leave several series at once, each holding that run's value.
Aggregate them per tier with `min by (job, schema, tier)`.

- **Backlog gauges.** Every run reports the backlog of the whole tier, since
  the store is shared. Between policy changes the backlog only shrinks, so the
  smallest value across the window's runs is the latest state, and a backlog
  that is really stuck stays non-zero in every run.
- **Work gauges** (`reprojected`, `embedded`, `capped`). These describe a
  single run. Read them per series, or sum the runs in a window for a total:
  `sum by (job, schema, tier) (last_over_time(corpus_sync_embedded[1d]))`.
  `sum_over_time` would add nothing, because each series holds a single
  sample.

Between runs an instant query finds a gauge only while the backend still
returns a series without new samples (Prometheus stops returning one five
minutes after its last sample), so alert over a window rather than on the
instant value. For example:

```promql
# Embeddings have not caught up for three days: semantic search is missing rows.
min by (job, schema, tier) (min_over_time(corpus_sync_embed_stale[3d])) > 0

# Rows are still served as placeholders. A sync re-projects every row under an
# older policy, whatever its --limit, so this stays non-zero only while the
# runs into a tier are still going, or when re-projection keeps failing.
min by (job, schema, tier) (last_over_time(corpus_sync_deferred[13h])) > 0
```

The aggregation does hide one case. A backlog that newly gets stuck after a
clean run stays hidden until that clean run's series leaves the window.

A skipped re-embedding is routine when several sources sync into one tier on
the same schedule (one of them holds the lock), so `reembed_skipped` explains
a lingering `embed_stale` rather than being an alert of its own.

**A failed sync records nothing.** The metrics are set only when a run
completes, so while every sync fails the series simply go quiet: the
`embed_stale` alert above sees no samples and stays silent. Pair it with an
alert on the failures themselves, such as one on `kube_job_status_failed` for
the sync CronJobs' jobs, or on the series going missing:

```promql
# No sync into the tier has completed for a day.
absent_over_time(corpus_sync_embed_stale{tier="sanitized"}[1d])
```

Pick the `embed_stale` window from the backlog a policy change creates: at the
default 500 rows a run, it takes as many syncs as the changed rows divided by
500, unless drained with `--reembed-limit 0` (see
[Rolling out a policy change](tiers.md#rolling-out-a-policy-change)).

## Diagnosing a shedding enrichment endpoint

A model server under enough concurrent load can shed a request as a 5xx. The
enricher [rides those out](configuration.md#riding-out-a-busy-enrichment-endpoint),
so the only trace they leave is a `corpus.enrich` warning per attempt. Each one
carries the evidence needed to say *which layer* shed the request without access
to the server:

```
2026-01-01 12:00:00 corpus.enrich endpoint unavailable (attempt 1/10), retrying in 0.7s: \
    HTTP 503 in 0.01s [server='envoy' retry-after='1'] 'upstream connect error ...'
```

| Signal | Reads as |
|---|---|
| `server=`/`via=` naming a proxy, and no upstream service time | a gateway answered; the request likely never reached the model server |
| an upstream service time (`x-envoy-upstream-service-time`) | the request reached the model server, so the 5xx came from behind the gateway |
| `retry-after` set | a layer shedding load deliberately rather than crashing |
| elapsed in milliseconds | shed at admission, before any queueing |
| elapsed near a timeout | queued until a timeout fired |
| body is JSON in the model server's own error format | the model server (vLLM, for example, returns `{"object": "error", …}`) |
| body is an HTML or proxy-shaped error page | a proxy or gateway |
| `TransportError` rather than an HTTP status | nothing answered: connection reset or client-side timeout |

These are hints, not proof: which headers a proxy or model server sets depends on
the deployment. A 4xx is not retried and does not appear here; it fails that record
straight away.

Count and cluster a pass from its log:

```sh
grep -c 'endpoint unavailable' run.log
grep 'endpoint unavailable' run.log | cut -d' ' -f1,2
```

Timestamps bunched into a few seconds implicate a model swap on a shared GPU —
the endpoint is briefly gone, not overloaded. Timestamps spread evenly across the
pass implicate a concurrency limit sitting below the offered load, and the
attribution fields then say whether that limit is the gateway's queue or the
model server's own capacity (for vLLM, e.g. `max_num_seqs` or KV-cache
headroom). Either remedy is a server-side tunable and belongs with the deployment config, not here; raising
`CORPUS_ENRICH_CONCURRENCY` before that is settled just moves more load onto
whichever limit is already binding.
