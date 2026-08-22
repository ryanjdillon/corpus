"""OpenTelemetry: export traces + metrics over OTLP.

No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so local runs and tests do not
export. Traces cover the FastAPI surface and outgoing httpx calls; metrics add
ingest counters/histograms and a corpus-size gauge that HTTP metrics can't
provide. Heavy SDK/exporter imports are deferred to configure() so importing this
module (for the metric instruments) stays cheap.
"""

from __future__ import annotations

import atexit
import logging
import os

from opentelemetry import metrics

log = logging.getLogger("corpus.telemetry")

_configured = False
_shutdown_done = False


def enabled() -> bool:
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


# Auto-instrumentation emits standard semconv names (http.server.duration, …).
# Rename them under the corpus. prefix so every metric this service exports is
# discoverable as corpus_* (the custom instruments below are already prefixed).
_INSTRUMENTATION_METRICS = [
    "http.server.duration",
    "http.server.active_requests",
    "http.server.request.size",
    "http.server.response.size",
    "http.client.duration",
    "http.client.request.size",
    "http.client.response.size",
]


def _instrumentation_views():
    from opentelemetry.sdk.metrics.view import View

    return [View(instrument_name=n, name=f"corpus.{n}") for n in _INSTRUMENTATION_METRICS]


def configure(default_service_name: str) -> None:
    """Install OTLP trace + metric providers and instrument httpx. No-op unless
    an OTLP endpoint is configured; safe to call more than once."""
    global _configured
    if _configured or not enabled():
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service = os.getenv("OTEL_SERVICE_NAME", default_service_name)
    resource = Resource.create({"service.name": service})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[reader], views=_instrumentation_views())
    )

    HTTPXClientInstrumentor().instrument()
    atexit.register(shutdown)
    _configured = True
    log.info("OpenTelemetry configured for %s", service)


def instrument_fastapi(app) -> None:
    """Instrument a FastAPI app for request traces + HTTP server metrics."""
    if not enabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def _corpus_size_observations(_options):
    """Callback for the corpus-size gauge: stored documents per data-class label.
    Never raises — a DB blip must not break metric export."""
    from opentelemetry.metrics import Observation

    try:
        from . import search

        by_label = search.stats().get("by_label", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("corpus-size gauge: %s", exc)
        return []
    return [Observation(count, {"label": label}) for label, count in by_label.items()]


def register_corpus_size_gauge() -> None:
    """Register an observable gauge of stored documents by data-class. Intended
    for a long-running process; the callback reads the store on each export."""
    if not enabled():
        return
    _meter.create_observable_gauge(
        "corpus.documents.count",
        callbacks=[_corpus_size_observations],
        unit="{document}",
        description="Stored documents by data-class label.",
    )


def shutdown() -> None:
    """Flush + shut down providers. Essential for a short-lived process (e.g. a
    one-shot ingest run); also runs at process exit via atexit."""
    global _shutdown_done
    if not _configured or _shutdown_done:
        return
    _shutdown_done = True
    from opentelemetry import trace

    trace.get_tracer_provider().shutdown()
    metrics.get_meter_provider().shutdown()


# --- metrics (proxy instruments; resolve once configure() sets the provider) ---
_meter = metrics.get_meter("corpus")

documents_counter = _meter.create_counter(
    "corpus.ingest.documents",
    unit="{document}",
    description="Documents processed during ingest, by source and outcome.",
)
embed_duration = _meter.create_histogram(
    "corpus.ingest.embed.duration",
    unit="s",
    description="Latency of a single embed request (one batch of records).",
)
embed_batch_size = _meter.create_histogram(
    "corpus.ingest.embed.batch_size",
    unit="{record}",
    description="Records per embed request.",
)
