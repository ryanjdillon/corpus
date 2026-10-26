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
  (`corpus.enrich.instruction_like`, by model), the credential scanner each
  enrich audit ran (`corpus.detect.leaks_scanner`, see below), each sync's
  backlog, and each pipeline run's enrichment backlog and model availability
  (see below).

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

## Enrichment backlog

The enrich stage of `corpus pipeline` records, for its source, what is left to
enrich and whether the model was there. Each is attributed by `source`, the
source id: unlike a sync's tier, the backlog belongs to one source.

| Metric (OTel) | Prometheus | Unit | Reads |
|---|---|---|---|
| `corpus.enrich.backlog` | `corpus_enrich_backlog` | `{document}` | the source's documents the next run would send to the model |
| `corpus.enrich.backlog_oldest` | `corpus_enrich_backlog_oldest_seconds` | `s` | unix time the oldest of them was first ingested; the run's own time when there are none; unset when not known |
| `corpus.enrich.last_success` | `corpus_enrich_last_success_seconds` | `s` | unix time the stage last finished without the model being unavailable |
| `corpus.enrich.unavailable` | `corpus_enrich_unavailable` | `{run}` | 1 if the stage stopped because the model was unavailable, else 0; also by `reason` |

They are gauges, one series per run, for the reasons given for the
[sync backlog](#sync-backlog): read them with `last_over_time`, never `rate`.
The `{document}` and `{run}` units add no suffix; `s` adds `_seconds`.

- **`backlog`** is read after the stage, however it ended: finished, capped by
  `--limit`, or stopped by an unavailable model. It is
  `DocumentStore.enrich_backlog`, one aggregate that makes the selection the
  stage makes: the source's documents with no enrichment under **any** schema
  version and no rejection by the run's model (a rejection recorded for a 429
  does not count). The pipeline does not upgrade stale records, so a
  `SCHEMA_VERSION` change does not grow it; `corpus enrich --upgrade-stale`
  and `--retry-rejected` send documents it does not count.
- **`backlog_oldest`** is read in the same query: the earliest `ingested_at`
  (see [When a document arrived](database.md#when-a-document-arrived)) among
  the documents `backlog` counts. When the backlog is empty it is the run's
  own time, so the newest run holds the largest value and `max` across the
  runs' series picks it, rather than an older run's oldest document. It is
  recorded with `backlog`, every time `backlog` is, except when there is a
  backlog whose oldest time is not known: the documents table has no
  `ingested_at` yet (a migration that could not take its lock, or a role that
  does not own the table; see
  [When a document arrived](database.md#when-a-document-arrived)). Then the run
  sets no `backlog_oldest` and logs it once at info, since the run's own time
  would read as fresh and hide a backlog; `backlog` is still recorded.
- **`last_success`** is set whenever the stage finishes without the model being
  unavailable, including a run that enriched nothing or stopped at its
  `--limit`. A failure for any other reason fails the run and records none of
  the three.
- **`unavailable`** is 1 with a `reason` when the model was unavailable, and 0
  with `reason="none"` otherwise. `reason` is a bounded slug, never the error
  text (see below).

The order differs by outcome, so that a failing backlog query never leaves a
half-recorded run:

- **Finished:** the stage reads the backlog **first**, then records
  `unavailable` 0 and `last_success`, then `backlog` and `backlog_oldest`. If
  the backlog query fails, the run fails and records none of them.
- **Unavailable:** the stage records `unavailable` 1 with its reason
  **first**, which needs no database, then reads and records `backlog` and
  `backlog_oldest`. If the
  backlog query fails, the reason is still recorded. It does both before it
  fails the run or [defers](pipeline.md#an-unavailable-model).

The command flushes them on exit either way.

The `reason` slugs:

| `reason` | Means | Deferred |
|---|---|---|
| `none` | the model was available (`unavailable` 0) | n/a |
| `http_5xx`, as `http_500`, `http_503`, … | the endpoint failed through every retry | yes |
| `http_408`, `http_425`, `http_429` | a timeout or rate limit outlasted the retries | yes |
| `http_429_budget` | a rate limit asked for longer than the retry budget allows | yes |
| `timeout`, `connect`, `transport` | the request timed out, was refused, or failed in transit | yes |
| `http_401`, `http_403`, `http_404` | a bad key or a wrong route: a misconfiguration | no |
| `unknown` | raised without a cause; not expected from the enricher | no |

"Deferred" is what `--defer-when-model-unavailable` does with it
(`corpus.enricher.deferrable`); without the flag every reason fails the run.
The status is kept whole rather than collapsed into a class, so match a class
with a regex in rules, `reason=~"http_5.."`, rather than listing statuses.

Both timestamp alerts below age between runs even when all is well: just
before the next scheduled run, `time()` minus the last run's time is the whole
gap between runs, and an empty backlog's `backlog_oldest` is that run's own
time, so the gap is the floor of either age. **Set each threshold above the
longest gap between a source's scheduled runs**, with room for a run's own
duration, or the alert fires on a healthy schedule. The examples use 18 hours,
which suits a source run at least every twelve hours; derive yours from your
own schedule.

Alert on how long ago enrichment last succeeded:

```promql
# A source has not finished an enrichment for 18 hours (above the longest gap
# between its scheduled runs).
time() - max by (source) (last_over_time(corpus_enrich_last_success_seconds[7d])) > 18*3600
```

This works because `last_success` is a timestamp, not an age:

- While the CronJob is suspended or the model is away, no run sets it, so the
  newest value stays put and `time()` minus it keeps growing. An age gauge
  would freeze at whatever the last run reported.
- Each run is its own series, and the newest successful run holds the largest
  timestamp, so `max` picks it over the older runs still in the window.
- Past the 7-day window every series has aged out, the expression returns
  nothing and the alert goes silent. Keep the CronJob's own stale or failed-job
  alerts as the dead-man's switch for a source that has stopped running at all.

Alert on how long the oldest waiting document has waited, which catches a
backlog that runs keep finishing but never drain, as when each is capped by
`--limit` below the arrival rate:

```promql
# A source has had a document waiting for enrichment for 18 hours (above the
# longest gap between its scheduled runs).
time() - max by (source) (last_over_time(corpus_enrich_backlog_oldest_seconds[2d])) > 18*3600
```

It reads like the `last_success` alert: a timestamp, so it keeps ageing while
no run reports, and `max` picks the newest run's value, since while nothing is
put back the oldest waiting document only moves forward. Three caveats:

- **A backlog of unknown age sets no value**, so while the table lacks
  `ingested_at` the alert reads the last run that knew it, and goes silent
  once that ages out of the window. Watch `backlog` for that case.

- **Documents stored before the column existed** read as ingested when it was
  added (see [When a document arrived](database.md#when-a-document-arrived)),
  so until they are drained the age is "since the migration", a floor on
  the true wait.
- **Putting older documents back moves it backwards**, and `max` hides that
  for up to the window: an earlier run's later value still wins until it ages
  out. A `SCHEMA_VERSION` bump does not put any back, since the backlog counts
  a record under any schema version as done. Switching the enrichment model
  does: the old model's rejections no longer count, so documents it rejected
  wait again from when they were first ingested.

The others give the alerts their context, for example in their annotations:

```promql
# What is waiting: the largest backlog a run reported in the last day. While
# nothing is enriched the backlog only grows, so this is the newest run's.
max by (source) (last_over_time(corpus_enrich_backlog[1d]))

# Runs that found the model unavailable in the last day, and why. Each series
# holds one sample, so sum_over_time counts the runs.
sum by (source, reason) (sum_over_time(corpus_enrich_unavailable[1d]))
```

A misconfiguration fails the run even when it defers, but the job alert fires
only as fast as the scheduler's. Alert on it separately so a bad key or route
is caught on its first run:

```promql
# The newest run found the model refusing its key or route.
last_over_time(corpus_enrich_unavailable{reason=~"http_40[134]"}[1h]) == 1
```

## Credential scanner fallback

The secret audit (`corpus enrich`, `corpus audit-secrets`, the pipeline's enrich
stage) counts every document it scans in `corpus.detect.leaks_scanner`, with
attribute `scanner`:

- `betterleaks`: [Betterleaks](detect.md#settings) ran and its report was read,
  unioned with the local rules.
- `local`: only the local rules ran, because `CORPUS_LEAKS_BIN` is unset or
  because the binary was missing, failed or timed out.

A fallback narrows the audit to the local rules without failing it, so the
counter is the only signal besides a single warning per process from
`corpus_detect.leaks` (`betterleaks is configured but unavailable (missing |
failed | timeout)`), which names the reason and never the content. Later
fallbacks in the same process log at debug.

The image sets `CORPUS_LEAKS_BIN=betterleaks`, so any `scanner="local"` from a
job running it is a fallback. Each enrich or audit run is a one-shot process
that exports its counter about once, under its own `service.instance.id`, so
alert on any series being non-zero in the window rather than on a rate:

```promql
# The enrich audit fell back to the local rules while Betterleaks is configured.
max by (job) (max_over_time(corpus_detect_leaks_scanner_total{scanner="local"}[1d])) > 0
```

Scope the selector (by `job` or `service_name`) to deployments that run the
image or otherwise set `CORPUS_LEAKS_BIN`; elsewhere `local` is expected.

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
