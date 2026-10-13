"""OpenTelemetry: export traces + metrics over OTLP.

No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so local runs and tests do not
export. Traces cover outgoing httpx calls; metrics add ingest counters and
histograms, MCP tool-call counts, a corpus-size gauge, and each sync's backlog
(:class:`SyncMetrics`). Heavy SDK/exporter imports are deferred to configure()
so importing this module (for the metric instruments) stays cheap.
"""

from __future__ import annotations

import atexit
import enum
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .tiers import Tier

log = logging.getLogger("corpus.telemetry")
audit_log = logging.getLogger("corpus.audit")
# Always on: the audit must not vanish if the root level is later raised.
audit_log.setLevel(logging.INFO)

_configured = False
_shutdown_done = False


def enabled() -> bool:
    """Report whether an OTLP endpoint is configured."""
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


# Auto-instrumentation emits standard semconv names (http.client.duration, …).
# Rename them under the corpus. prefix so every metric this service exports is
# discoverable as corpus_* (the custom instruments below are already prefixed).
_INSTRUMENTATION_METRICS = [
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


def _corpus_size_observations(_options, *, documents: Callable[[], Any] | None = None):
    """Report stored documents per data-class label for the corpus-size gauge.

    *documents* opens the store to read (default ``store.DocumentStore``). Never
    raises — a DB blip must not break metric export.
    """
    from opentelemetry.metrics import Observation

    try:
        from .store import DocumentStore

        with (documents or DocumentStore)() as store:
            by_label = store.stats().get("by_label", {})
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
enrich_instruction_like = _meter.create_counter(
    "corpus.enrich.instruction_like",
    unit="{document}",
    description="Enrichments whose free text reads like instructions to an AI, by model.",
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
    # Milliseconds, so calls spread across the default bucket bounds (0, 5, 10,
    # 25, ... 10000) instead of all landing in the first one.
    unit="ms",
    description="MCP tool call latency, by server, tier and tool.",
)


class SyncMetrics:
    """The instruments a sync reports its backlog and work on, from *meter*.

    One set of attributes per projected tier: its name and schema. Several
    sources share a tier (and its backlog), so no source is attached; a gauge
    labelled by source would report the same tier-wide value under each.

    Every instrument is a gauge holding the last sync's value. A sync is a
    one-shot process whose instruments start from nothing and export about
    once, and the syncs of a tier write one series, so a counter's rate would
    read the difference between two runs' work rather than either run's.
    """

    def __init__(self, meter: Meter) -> None:
        self.deferred = meter.create_gauge(
            "corpus.sync.deferred",
            unit="{row}",
            description="Rows still projected under another policy after a sync, "
            "served as placeholders.",
        )
        self.embed_stale = meter.create_gauge(
            "corpus.sync.embed_stale",
            unit="{row}",
            description="Rows awaiting a new vector after a sync, left out of semantic search.",
        )
        self.reembed_skipped = meter.create_gauge(
            "corpus.sync.reembed_skipped",
            unit="{run}",
            description="1 if the last sync skipped its re-embedding because another sync "
            "of the tier held the re-embed lock, else 0.",
        )
        self.reprojected = meter.create_gauge(
            "corpus.sync.reprojected",
            unit="{row}",
            description="Rows the last sync rewrote only because of the policy or a forced "
            "re-projection.",
        )
        self.embedded = meter.create_gauge(
            "corpus.sync.embedded",
            unit="{row}",
            description="Vectors the last sync stored, in either phase.",
        )
        self.capped = meter.create_gauge(
            "corpus.sync.capped",
            unit="{row}",
            description="New rows the last sync held back for its embedding limit, "
            "left for a later sync.",
        )

    def record(self, counts: Mapping[str, int], tier: Tier) -> None:
        """Record one sync's *counts* (``sanitize.run_sync``) into *tier*."""
        attrs = {"tier": tier.name, "schema": tier.schema}
        self.deferred.set(counts["pending"], attrs)
        self.embed_stale.set(counts["embed_stale"], attrs)
        self.reembed_skipped.set(counts.get("reembed_skipped", 0), attrs)
        self.reprojected.set(counts["reprojected"], attrs)
        self.embedded.set(counts["embedded"], attrs)
        self.capped.set(counts["capped"], attrs)


#: The sync instruments on the process's meter provider.
sync_metrics = SyncMetrics(_meter)


# --- MCP tool-call audit -------------------------------------------------------

#: The request header carrying the caller's principal. The gateway overwrites it
#: from the authenticated API key, so a client cannot choose its own.
PRINCIPAL_HEADER = "x-client-id"
UNKNOWN_PRINCIPAL = "unknown"  # no header: a caller that bypassed the gateway
AMBIGUOUS_PRINCIPAL = "ambiguous"  # the header sent more than once
OTHER_PRINCIPAL = "other"  # not shaped like a client id
# The principal is a metric attribute, so it must stay low-cardinality: anything
# not shaped like a gateway client id collapses to "other".
_PRINCIPAL = re.compile(r"^[a-z0-9_-]{1,32}$")

# A source id such as "gmail:personal"; anything else (an address) is not logged.
_SOURCE = re.compile(r"^[a-z0-9_-]{1,32}(?::[a-z0-9_-]{1,32})?$")
_DURATION = re.compile(r"^\d{1,4}[smhdw]$")
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]{2,15}(?:Z|[+-]\d{2}:?\d{2})?)?$")


@functools.cache
def _closed_values() -> dict[str, frozenset[str]]:
    """The closed vocabularies a string argument may be logged verbatim from."""
    from typing import get_args

    from .classify import LABELS
    from .enrichment import Domain, Importance, WaitingOn
    from .query import GroupBy, Order

    waiting = frozenset(w.value for w in WaitingOn)
    return {
        "label": LABELS,
        "domain": frozenset(d.value for d in Domain),
        "importance": frozenset(i.value for i in Importance),
        "who": waiting,
        "waiting_on": waiting,
        "order": frozenset(get_args(Order)),
        "group_by": frozenset(get_args(GroupBy)),
    }


def _summarise(name: str, value: Any) -> Any:
    """One argument, safe to log.

    Verbatim only when the value itself is safe: None, a bool or a number, a member
    of the argument's closed vocabulary (or a list of them), or a date or duration.
    A document id keeps its source and is otherwise hashed, since the rest can name
    a mail folder. Any other string (a search query, an address, a value outside
    the vocabulary) is logged as its length, so the log never carries what a caller
    searched for. A mapping (a query spec, its filters, a range) is summarised
    field by field, so the log states the window and filters a call read.
    """
    if value is None or isinstance(value, (bool, int, float, enum.Enum)):
        return value.value if isinstance(value, enum.Enum) else value
    if isinstance(value, str):
        if value in _closed_values().get(name, ()):
            return value
        if name in {"since", "until"} and _DURATION.match(value):
            return value
        if name in {"before", "after", "since", "until"} and _ISO_TIME.match(value):
            return value
        if name == "id":
            source, _, _ = value.partition("::")
            digest = hashlib.sha256(value.encode()).hexdigest()[:12]
            return {"source": source if _SOURCE.match(source) else None, "sha256": digest}
        return {"chars": len(value)}
    if isinstance(value, (list, tuple)):
        vocabulary = _closed_values().get(name, ())
        if value and all(isinstance(v, str) and v in vocabulary for v in value):
            return list(value)
        return {"items": len(value)}
    if isinstance(value, Mapping):
        return filter_summary(value)
    return {"type": type(value).__name__}


def filter_summary(args: Mapping[str, Any]) -> dict[str, Any]:
    """The shape of a tool call's arguments, safe to log (see ``_summarise``)."""
    return {name: _summarise(name, value) for name, value in args.items()}


def _principal() -> str:
    """The calling principal from the current MCP request.

    ``unknown`` without a header (an in-cluster caller that bypassed the gateway,
    logged rather than refused), ``ambiguous`` if it was sent more than once, and
    ``other`` if it is not shaped like a client id.
    """
    from mcp.server.lowlevel.server import request_ctx

    try:
        request = request_ctx.get().request
    except LookupError:
        return UNKNOWN_PRINCIPAL
    headers = getattr(request, "headers", None)
    values = headers.getlist(PRINCIPAL_HEADER) if headers is not None else []
    if not values or not values[0]:
        return UNKNOWN_PRINCIPAL
    if len(values) > 1:
        return AMBIGUOUS_PRINCIPAL
    return values[0] if _PRINCIPAL.match(values[0]) else OTHER_PRINCIPAL


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
    mcp_duration.record(duration * 1000, {"server": server, "tier": tier, "tool": tool})


def audited(*, server: str, tier: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate an MCP tool so every call is recorded by :func:`record_tool_call`.

    Apply it beneath ``@mcp.tool()``: ``functools.wraps`` keeps the signature, so
    the tool's schema is unchanged. The row count is the envelope's ``count``. A
    call that raises is recorded with the exception type and re-raised.

    Only calls that reach the tool are recorded: FastMCP rejects arguments that
    fail its schema, or an unknown tool name, before the tool runs.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):
            raise TypeError(f"audited supports sync tools only; {fn.__name__} is async")
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
