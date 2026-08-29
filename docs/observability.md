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

See [Architecture → Observability](architecture.md#observability) for where the
instruments sit in the pipeline.
