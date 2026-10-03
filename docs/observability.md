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
