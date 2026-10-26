"""Telemetry: no-op without an endpoint, and provider wiring with one."""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from corpus import telemetry
from corpus.store import DocumentStore
from corpus.tiers import tier


def test_enabled_reflects_env(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.enabled() is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    assert telemetry.enabled() is True


def test_configure_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_configured", False)
    telemetry.configure("corpus-test")
    assert telemetry._configured is False


def test_register_gauge_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    telemetry.register_corpus_size_gauge()  # no-op, no error


def test_shutdown_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_shutdown_done", False)
    telemetry.shutdown()  # no-op, no error


def test_instrumentation_views_prefix_all_names():
    views = telemetry._instrumentation_views()
    assert len(views) == len(telemetry._INSTRUMENTATION_METRICS)
    assert all(v._name.startswith("corpus.") for v in views)


@pytest.fixture
def documents():
    """Autospec ``DocumentStore`` as a context manager yielding an instance mock."""
    cls = create_autospec(DocumentStore)
    cls.return_value.__enter__.return_value = create_autospec(DocumentStore, instance=True)
    return cls


def test_corpus_size_observations(documents):
    stats = documents.return_value.__enter__.return_value.stats
    stats.return_value = {"total": 3, "by_label": {"personal": 2, "bulk": 1}}
    obs = telemetry._corpus_size_observations(None, documents=documents)
    assert {o.attributes["label"]: o.value for o in obs} == {"personal": 2, "bulk": 1}
    documents.return_value.__exit__.assert_called_once()  # the store is closed


def test_corpus_size_observations_swallows_errors(documents):
    stats = documents.return_value.__enter__.return_value.stats
    stats.side_effect = RuntimeError("db down")
    assert telemetry._corpus_size_observations(None, documents=documents) == []


def test_corpus_size_observations_swallows_an_unreachable_store(documents):
    documents.side_effect = OSError("connection refused")
    assert telemetry._corpus_size_observations(None, documents=documents) == []


def test_configure_sets_providers_and_is_idempotent(monkeypatch):
    import opentelemetry.exporter.otlp.proto.grpc.metric_exporter as me
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as te
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Boundary: the OTLP exporters drive real SDK provider internals, so they
    # cannot be autospec'd cleanly. Swap them for local SDK exporters instead.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(te, "OTLPSpanExporter", InMemorySpanExporter)
    monkeypatch.setattr(me, "OTLPMetricExporter", ConsoleMetricExporter)
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_shutdown_done", False)

    try:
        telemetry.configure("corpus-test")
        assert telemetry._configured is True
        telemetry.configure("corpus-test")  # second call is a no-op, no error
    finally:
        telemetry.shutdown()
        telemetry.shutdown()  # idempotent
        HTTPXClientInstrumentor().uninstrument()


# --- credential scanner ----------------------------------------------------------


def test_leaks_scanner_is_counted_by_scanner(reader):
    from opentelemetry.sdk.metrics import MeterProvider

    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    counter = meter.create_counter("corpus.detect.leaks_scanner")

    telemetry.record_leaks_scanner("betterleaks", counter=counter)
    telemetry.record_leaks_scanner("local", counter=counter)
    telemetry.record_leaks_scanner("local", counter=counter)

    points = _collect(reader)["corpus.detect.leaks_scanner"].data_points
    assert {p.attributes["scanner"]: p.value for p in points} == {"betterleaks": 1, "local": 2}


# --- sync backlog --------------------------------------------------------------


def test_register_gauge_reports_stored_documents_by_label(monkeypatch, documents):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    stats = documents.return_value.__enter__.return_value.stats
    stats.return_value = {"total": 3, "by_label": {"personal": 2, "bulk": 1}}
    gauge_reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[gauge_reader]).get_meter("test")

    telemetry.register_corpus_size_gauge(meter=meter, documents=documents)

    gauge = _collect(gauge_reader)["corpus.documents.count"]
    assert {p.attributes["label"]: p.value for p in gauge.data_points} == {
        "personal": 2,
        "bulk": 1,
    }
    documents.return_value.__exit__.assert_called_once()  # the store is closed per export


@pytest.fixture
def reader():
    """An in-memory reader on a provider of its own, so no global provider is set."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    return InMemoryMetricReader()


@pytest.fixture
def sync_metrics(reader):
    from opentelemetry.sdk.metrics import MeterProvider

    return telemetry.SyncMetrics(MeterProvider(metric_readers=[reader]).get_meter("test"))


def _collect(reader) -> dict[str, Any]:
    """Each metric's data (its type and points), by name, from one collection."""
    data = reader.get_metrics_data()
    return {
        m.name: m.data
        for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
    }


#: A sync's counts (``sanitize.run_sync``): what it did, and what is left.
_COUNTS = {"scanned": 9, "synced": 4, "skipped": 5, "reprojected": 3, "embedded": 2,
           "capped": 6, "pending": 1, "embed_stale": 7}


def test_a_sync_records_its_backlog_and_work_by_tier(reader, sync_metrics):
    sanitized = tier("sanitized")

    sync_metrics.record(_COUNTS, sanitized)

    points = {name: list(d.data_points) for name, d in _collect(reader).items()}
    values = {name: [p.value for p in ps] for name, ps in points.items()}
    assert values == {
        "corpus.sync.deferred": [1],
        "corpus.sync.embed_stale": [7],
        "corpus.sync.reembed_skipped": [0],  # present from the first sync
        "corpus.sync.reprojected": [3],
        "corpus.sync.embedded": [2],
        "corpus.sync.capped": [6],
    }
    expected = {"tier": "sanitized", "schema": sanitized.schema}
    assert all(dict(p.attributes) == expected for ps in points.values() for p in ps)


def test_every_sync_metric_is_a_gauge_of_the_last_sync(reader, sync_metrics):
    sanitized = tier("sanitized")

    sync_metrics.record(_COUNTS, sanitized)
    sync_metrics.record({**_COUNTS, "pending": 0, "embed_stale": 2, "reembed_skipped": 1,
                         "reprojected": 0, "embedded": 5, "capped": 0}, sanitized)

    # Gauges, not counters: a one-shot sync's counter would start from zero in
    # each process, and the tier's syncs share one series, so its rate misleads.
    from opentelemetry.sdk.metrics.export import Gauge

    collected = _collect(reader)
    assert all(isinstance(d, Gauge) for d in collected.values())
    values = {name: [p.value for p in d.data_points] for name, d in collected.items()}
    assert values == {
        "corpus.sync.deferred": [0],
        "corpus.sync.embed_stale": [2],
        "corpus.sync.reembed_skipped": [1],
        "corpus.sync.reprojected": [0],
        "corpus.sync.embedded": [5],  # the last run's, not the sum
        "corpus.sync.capped": [0],
    }


#: A fixed "now" for the enrich instruments' clock.
_NOW = 1_767_225_600.0


@pytest.fixture
def enrich_metrics(reader):
    from opentelemetry.sdk.metrics import MeterProvider

    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    return telemetry.EnrichMetrics(meter, clock=lambda: _NOW)


def _points(reader) -> dict[str, list[tuple[float, dict]]]:
    return {
        name: [(p.value, dict(p.attributes)) for p in d.data_points]
        for name, d in _collect(reader).items()
    }


def test_an_enrichment_that_finished_records_its_success_time_and_backlog(
    reader, enrich_metrics
):
    enrich_metrics.record_outcome("imap:test", None)
    enrich_metrics.record_backlog("imap:test", 12)

    assert _points(reader) == {
        "corpus.enrich.unavailable": [(0, {"source": "imap:test", "reason": "none"})],
        "corpus.enrich.last_success": [(_NOW, {"source": "imap:test"})],
        "corpus.enrich.backlog": [(12, {"source": "imap:test"})],
    }


def test_an_unavailable_model_records_its_reason_and_no_success(reader, enrich_metrics):
    enrich_metrics.record_outcome("imap:test", "http_503")
    enrich_metrics.record_backlog("imap:test", 40)

    # No last_success: the newest success stays the one an earlier run reported.
    assert _points(reader) == {
        "corpus.enrich.unavailable": [(1, {"source": "imap:test", "reason": "http_503"})],
        "corpus.enrich.backlog": [(40, {"source": "imap:test"})],
    }


def test_every_enrich_metric_is_a_gauge(reader, enrich_metrics):
    from opentelemetry.sdk.metrics.export import Gauge

    enrich_metrics.record_outcome("imap:test", None)
    enrich_metrics.record_backlog("imap:test", 0)

    data = reader.get_metrics_data()
    metrics = [m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
    assert all(isinstance(m.data, Gauge) for m in metrics)
    # Units decide the Prometheus names docs/observability.md gives.
    assert {m.name: m.unit for m in metrics} == {
        "corpus.enrich.unavailable": "{run}",
        "corpus.enrich.last_success": "s",
        "corpus.enrich.backlog": "{document}",
    }
