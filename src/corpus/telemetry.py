"""OpenTelemetry: export traces + metrics over OTLP.

No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so local runs and tests do not
export. Traces cover the FastAPI surface and outgoing httpx calls; metrics add
ingest counters/histograms and a corpus-size gauge that HTTP metrics can't
provide. Heavy SDK/exporter imports are deferred to configure() so importing this
module (for the metric instruments) stays cheap.
"""

from __future__ import annotations

import atexit
import functools
import inspect
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from opentelemetry import metrics

log = logging.getLogger("corpus.telemetry")
audit_log = logging.getLogger("corpus.audit")

_configured = False
_shutdown_done = False


def enabled() -> bool:
    """Report whether an OTLP endpoint is configured."""
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
    """Build views that rename auto-instrumentation metrics under ``corpus.``."""
    from opentelemetry.sdk.metrics.view import View

    return [View(instrument_name=n, name=f"corpus.{n}") for n in _INSTRUMENTATION_METRICS]


def configure(default_service_name: str) -> None:
    """Install OTLP trace + metric providers and instrument httpx.

    No-op unless an OTLP endpoint is configured; safe to call more than once.
    """
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
    """Report stored documents per data-class label for the corpus-size gauge.

    Never raises — a DB blip must not break metric export.
    """
    from opentelemetry.metrics import Observation

    try:
        from . import search

        by_label = search.stats().get("by_label", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("corpus-size gauge: %s", exc)
        return []
    return [Observation(count, {"label": label}) for label, count in by_label.items()]


def register_corpus_size_gauge() -> None:
    """Register an observable gauge of stored documents by data-class.

    Intended for a long-running process; the callback reads the store on each
    export.
    """
    if not enabled():
        return
    _meter.create_observable_gauge(
        "corpus.documents.count",
        callbacks=[_corpus_size_observations],
        unit="{document}",
        description="Stored documents by data-class label.",
    )


def shutdown() -> None:
    """Flush and shut down providers.

    Essential for a short-lived process (e.g. a one-shot ingest run); also runs
    at process exit via atexit.
    """
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
mcp_calls = _meter.create_counter(
    "corpus.mcp.calls",
    unit="{call}",
    description="MCP tool calls, by server, tier, tool, principal and outcome.",
)
mcp_rows = _meter.create_counter(
    "corpus.mcp.rows",
    unit="{row}",
    description="Rows returned by MCP tools, by server, tier, tool and principal.",
)
mcp_duration = _meter.create_histogram(
    "corpus.mcp.duration",
    unit="s",
    description="MCP tool call latency, by server, tier and tool.",
)


# --- MCP tool-call audit -------------------------------------------------------

#: The request header carrying the caller's principal. The gateway overwrites it
#: from the authenticated API key, so a client cannot choose its own.
PRINCIPAL_HEADER = "x-client-id"
UNKNOWN_PRINCIPAL = "unknown"

# Arguments logged verbatim: closed vocabularies, dates, ids and sizes. Any other
# string argument (a search query, a free-text term) is logged as its length
# only, so the audit log never carries what a caller searched for.
_VERBATIM_ARGS = frozenset(
    {"label", "account", "before", "after", "since", "domain", "importance", "who", "id",
     "limit", "top_k"}
)


def _principal() -> str:
    """The calling principal from the current MCP request, else ``unknown``.

    A caller reaching the server without the gateway (an in-cluster job) sends no
    header; it is logged as unknown rather than refused.
    """
    from mcp.server.lowlevel.server import request_ctx

    try:
        request = request_ctx.get().request
    except LookupError:
        return UNKNOWN_PRINCIPAL
    headers = getattr(request, "headers", None)
    value = headers.get(PRINCIPAL_HEADER) if headers is not None else None
    return value or UNKNOWN_PRINCIPAL


def filter_summary(args: Mapping[str, Any]) -> dict[str, Any]:
    """The shape of a tool call's arguments, safe to log: see ``_VERBATIM_ARGS``."""
    summary: dict[str, Any] = {}
    for name, value in args.items():
        if isinstance(value, str) and name not in _VERBATIM_ARGS:
            summary[name] = {"chars": len(value)}
        else:
            summary[name] = value
    return summary


def record_tool_call(
    *,
    server: str,
    tier: str,
    tool: str,
    principal: str,
    args: Mapping[str, Any],
    rows: int | None,
    duration: float,
    outcome: str,
) -> None:
    """Emit one audit event for an MCP tool call: a JSON log line and metrics.

    The event answers "which principal read which tier, how much, and when"; it
    carries argument shapes and a row count, never row content.
    """
    event = {
        "event": "mcp.tool_call",
        "server": server,
        "tier": tier,
        "tool": tool,
        "principal": principal,
        "filters": filter_summary(args),
        "rows": rows,
        "duration_ms": round(duration * 1000, 1),
        "outcome": outcome,
    }
    audit_log.info(json.dumps(event, sort_keys=True, default=str))
    attrs = {"server": server, "tier": tier, "tool": tool, "principal": principal}
    mcp_calls.add(1, {**attrs, "outcome": outcome})
    if rows is not None:
        mcp_rows.add(rows, attrs)
    mcp_duration.record(duration, {"server": server, "tier": tier, "tool": tool})


def audited(*, server: str, tier: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate an MCP tool so every call is recorded by :func:`record_tool_call`.

    Apply it beneath ``@mcp.tool()``: ``functools.wraps`` keeps the signature, so
    the tool's schema is unchanged. The row count is the envelope's ``count``. A
    call that raises is recorded with the exception type and re-raised.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def call(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            principal = _principal()
            kwargs = dict(signature.bind_partial(*args, **kwargs).arguments)
            try:
                result = fn(**kwargs)
            except Exception as exc:
                record_tool_call(
                    server=server, tier=tier, tool=fn.__name__, principal=principal,
                    args=kwargs, rows=None, duration=time.perf_counter() - start,
                    outcome=type(exc).__name__,
                )
                raise
            record_tool_call(
                server=server, tier=tier, tool=fn.__name__, principal=principal, args=kwargs,
                rows=result.get("count") if isinstance(result, Mapping) else None,
                duration=time.perf_counter() - start, outcome="ok",
            )
            return result

        return call

    return decorate
