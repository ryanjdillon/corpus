"""Telemetry: no-op without an endpoint, and provider wiring with one."""

from __future__ import annotations

from corpus import telemetry


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


def test_corpus_size_observations(monkeypatch):
    monkeypatch.setattr(
        "corpus.search.stats",
        lambda: {"total": 3, "by_label": {"personal": 2, "bulk": 1}},
    )
    obs = telemetry._corpus_size_observations(None)
    assert {o.attributes["label"]: o.value for o in obs} == {"personal": 2, "bulk": 1}


def test_corpus_size_observations_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("corpus.search.stats", boom)
    assert telemetry._corpus_size_observations(None) == []


def test_configure_sets_providers_and_is_idempotent(monkeypatch):
    import opentelemetry.exporter.otlp.proto.grpc.metric_exporter as me
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as te
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Avoid any network: swap the OTLP exporters for local ones.
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
