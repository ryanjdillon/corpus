# Observability

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (and optionally `OTEL_SERVICE_NAME`) and the
API, MCP, and ingest processes export OpenTelemetry over OTLP:

- **Traces** — FastAPI request spans and outgoing httpx spans (embedding calls,
  source APIs).
- **Metrics** — ingest counters and histograms (documents written and skipped,
  embed latency, batch size), a corpus-size gauge by data-class, FastAPI HTTP
  server metrics, MCP tool calls by principal (see below), and enrichments
  whose free text reads like instructions to an AI
  (`corpus.enrich.instruction_like`, by model).

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
  vocabulary (`label`, `domain`, `importance`, `who`), or a date or duration. A
  document id keeps its source and is otherwise a short hash, since the rest can
  name a mail folder. Any other string, such as a search query or an address, is
  logged as its length, and a list as its size.
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
