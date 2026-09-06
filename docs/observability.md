# Observability

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (and optionally `OTEL_SERVICE_NAME`) and the
API, MCP, and ingest processes export OpenTelemetry over OTLP:

- **Traces** — FastAPI request spans and outgoing httpx spans (embedding calls,
  source APIs).
- **Metrics** — ingest counters and histograms (documents written and skipped,
  embed latency, batch size), a corpus-size gauge by data-class, and FastAPI HTTP
  server metrics.

It is a no-op when the endpoint is unset. Heavy SDK imports are deferred until
telemetry is configured, and a one-shot process (an ingest run) flushes the
exporters on exit so its metrics are not lost.

See the [architecture diagram](architecture.html) for where the instruments sit
in the pipeline.

## Diagnosing a shedding enrichment endpoint

A steady concurrency-16 backfill sheds roughly one request per few hundred as a
5xx. The enricher [rides those out](configuration.md#riding-out-a-busy-enrichment-endpoint),
so the only trace they leave is a `corpus.enrich` warning per attempt. Each one
carries the evidence needed to say *which layer* shed the request without access
to the server:

```
2026-01-01 12:00:00 corpus.enrich endpoint unavailable (attempt 1/10), retrying in 0.7s: \
    HTTP 503 in 0.01s [server='envoy' retry-after='1'] 'upstream connect error ...'
```

| Signal | Reads as |
|---|---|
| `server=`/`via=` naming a proxy, and no upstream service time | the gateway answered; the request never reached the model server |
| an upstream service time (`x-envoy-upstream-service-time`) | the model server answered, so the 5xx is its own |
| `retry-after` set | deliberate admission control on queue depth, not a crash |
| elapsed in milliseconds | shed at admission, before any queueing |
| elapsed at the gateway's upstream timeout | queued behind a full server until the proxy gave up |
| body is the model server's JSON error envelope | the model server (vLLM `{"object": "error", …}`) |
| body is an HTML or proxy-shaped error page | the gateway |
| `TransportError` rather than an HTTP status | nothing answered — connection reset or client-side timeout |

Count and cluster a pass from its log:

```sh
grep -c 'endpoint unavailable' run.log
grep 'endpoint unavailable' run.log | cut -d' ' -f1,2
```

Timestamps bunched into a few seconds implicate a model swap on a shared GPU —
the endpoint is briefly gone, not overloaded. Timestamps spread evenly across the
pass implicate a concurrency limit sitting below the offered load, and the
attribution fields then say whether that limit is the gateway's queue or the
model server's batch (`max_num_seqs`, KV-cache headroom). Either remedy is a
server-side tunable and belongs with the deployment config, not here; raising
`CORPUS_ENRICH_CONCURRENCY` before that is settled just moves more load onto
whichever limit is already binding.
