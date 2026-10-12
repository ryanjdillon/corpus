"""Telemetry: no-op without an endpoint, and provider wiring with one."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from corpus import telemetry
from corpus.store import DocumentStore


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


def test_instrument_fastapi_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    telemetry.instrument_fastapi(object())  # returns without touching the app


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
