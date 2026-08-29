"""CLI entrypoints wire telemetry and hand off to the servers/ingest.

The servers, batch orchestrators, and stores are autospec'd so nothing binds a
port or hits a DB. Each collaborator has one fixture with a neutral default;
tests override the return value and assert on the parsed arguments.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
import uvicorn
from click.testing import CliRunner

from corpus import (
    cli,
    enrich_batch,
    enrich_store,
    export,
    index_server,
    ingest,
    mcp_server,
    sanitize,
    sanitized_store,
    scan,
    telemetry,
)


@pytest.fixture
def runner() -> CliRunner:
    """Return a click CLI runner."""
    return CliRunner()


@pytest.fixture
def configure(monkeypatch):
    """Autospec ``telemetry.configure`` so no OTLP wiring runs."""
    mock = create_autospec(telemetry.configure)
    monkeypatch.setattr(telemetry, "configure", mock)
    return mock


@pytest.fixture
def shutdown(monkeypatch):
    """Autospec ``telemetry.shutdown`` so no provider flush runs."""
    mock = create_autospec(telemetry.shutdown)
    monkeypatch.setattr(telemetry, "shutdown", mock)
    return mock


@pytest.fixture
def instrument_fastapi(monkeypatch):
    """Autospec ``telemetry.instrument_fastapi``."""
    mock = create_autospec(telemetry.instrument_fastapi)
    monkeypatch.setattr(telemetry, "instrument_fastapi", mock)
    return mock


@pytest.fixture
def register_gauge(monkeypatch):
    """Autospec ``telemetry.register_corpus_size_gauge``."""
    mock = create_autospec(telemetry.register_corpus_size_gauge)
    monkeypatch.setattr(telemetry, "register_corpus_size_gauge", mock)
    return mock


@pytest.fixture
def uvicorn_run(monkeypatch):
    """Autospec ``uvicorn.run`` so no server binds a port."""
    mock = create_autospec(uvicorn.run)
    monkeypatch.setattr(uvicorn, "run", mock)
    return mock


@pytest.fixture
def mcp_run(monkeypatch):
    """Autospec the MCP server ``run``."""
    mock = create_autospec(mcp_server.run)
    monkeypatch.setattr(mcp_server, "run", mock)
    return mock


@pytest.fixture
def index_run(monkeypatch):
    """Autospec the corpus-index server ``run``."""
    mock = create_autospec(index_server.run)
    monkeypatch.setattr(index_server, "run", mock)
    return mock


@pytest.fixture
def ingest_fn(monkeypatch):
    """Autospec ``ingest.ingest`` with a neutral default count."""
    mock = create_autospec(ingest.ingest, return_value=0)
    monkeypatch.setattr(ingest, "ingest", mock)
    return mock


@pytest.fixture
def enrich_store_cls(monkeypatch):
    """Autospec ``EnrichStore`` as a context manager yielding an instance mock."""
    cls = create_autospec(enrich_store.EnrichStore)
    cls.return_value.__enter__.return_value = create_autospec(
        enrich_store.EnrichStore, instance=True
    )
    monkeypatch.setattr(enrich_store, "EnrichStore", cls)
    return cls


@pytest.fixture
def run_enrich(monkeypatch):
    """Autospec ``enrich_batch.run_enrich`` with a neutral default result."""
    mock = create_autospec(
        enrich_batch.run_enrich,
        return_value={"scanned": 0, "enriched": 0, "audited": 0},
    )
    monkeypatch.setattr(enrich_batch, "run_enrich", mock)
    return mock


@pytest.fixture
def run_audit(monkeypatch):
    """Autospec ``enrich_batch.run_audit`` with a neutral default result."""
    mock = create_autospec(
        enrich_batch.run_audit, return_value={"scanned": 0, "audited": 0}
    )
    monkeypatch.setattr(enrich_batch, "run_audit", mock)
    return mock


@pytest.fixture
def sanitized_store_cls(monkeypatch):
    """Autospec ``SanitizedStore`` as a context manager yielding an instance mock."""
    cls = create_autospec(sanitized_store.SanitizedStore)
    cls.return_value.__enter__.return_value = create_autospec(
        sanitized_store.SanitizedStore, instance=True
    )
    monkeypatch.setattr(sanitized_store, "SanitizedStore", cls)
    return cls


@pytest.fixture
def run_sync(monkeypatch):
    """Autospec ``sanitize.run_sync`` with a neutral default result."""
    mock = create_autospec(
        sanitize.run_sync, return_value={"synced": 0, "skipped": 0, "scanned": 0}
    )
    monkeypatch.setattr(sanitize, "run_sync", mock)
    return mock


@pytest.fixture
def export_archive(monkeypatch):
    """Autospec ``export.export_archive`` with a neutral default result."""
    mock = create_autospec(
        export.export_archive,
        return_value={"scanned": 0, "written": 0, "unchanged": 0},
    )
    monkeypatch.setattr(export, "export_archive", mock)
    return mock


@pytest.fixture
def scan_archive(monkeypatch):
    """Autospec ``scan.scan_archive`` with a neutral default report."""
    mock = create_autospec(
        scan.scan_archive,
        return_value={"scanned": 0, "with_secrets": 0, "totals": {}, "hits": []},
    )
    monkeypatch.setattr(scan, "scan_archive", mock)
    return mock


def test_api_command(runner, configure, instrument_fastapi, register_gauge, uvicorn_run):
    result = runner.invoke(cli.main, ["api"])
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus",)
    instrument_fastapi.assert_called_once()
    register_gauge.assert_called_once()
    uvicorn_run.assert_called_once()


def test_mcp_command(runner, configure, mcp_run):
    result = runner.invoke(cli.main, ["mcp"])
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus-mcp",)
    mcp_run.assert_called_once()


def test_index_command(runner, configure, index_run):
    result = runner.invoke(cli.main, ["index"])
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus-index",)
    index_run.assert_called_once()


def test_sync_command(runner, configure, shutdown, sanitized_store_cls, run_sync):
    run_sync.return_value = {"synced": 3, "skipped": 1, "scanned": 4}
    result = runner.invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus-sync",)
    assert "synced 3" in result.output
    assert run_sync.call_args.kwargs["source"] is None


def test_ingest_command(runner, configure, shutdown, ingest_fn):
    ingest_fn.return_value = 7
    result = runner.invoke(cli.main, ["ingest", "imap:test", "--batch-size", "5"])
    assert result.exit_code == 0, result.output
    assert "ingested 7 documents from imap:test" in result.output
    assert configure.call_args.args == ("corpus-ingest",)
    assert ingest_fn.call_args.args == ("imap:test",)
    assert ingest_fn.call_args.kwargs == {"batch_size": 5}
    shutdown.assert_called_once()


def test_scan_command(runner, scan_archive, tmp_path):
    scan_archive.return_value = {
        "scanned": 3,
        "with_secrets": 1,
        "totals": {"us_ssn": 2},
        "hits": [
            {
                "id": "imap:test::1",
                "secret_types": ["us_ssn"],
                "from_addr": "a@b.com",
                "subject": "form",
                "sent_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    out = tmp_path / "report.json"
    result = runner.invoke(cli.main, ["scan", "--json", str(out)])
    assert result.exit_code == 0, result.output
    assert "scanned 3; 1 contain secrets" in result.output
    assert "us_ssn: 2 (1 messages)" in result.output
    assert "[us_ssn]  a@b.com  form" in result.output
    assert out.exists()
    assert '"us_ssn"' in out.read_text()


def test_ingest_flushes_telemetry_on_error(runner, configure, shutdown, ingest_fn):
    ingest_fn.side_effect = RuntimeError("ingest failed")
    result = runner.invoke(cli.main, ["ingest", "imap:test"])
    assert result.exit_code != 0
    shutdown.assert_called_once()  # flushed even on failure


def test_enrich_command(runner, configure, shutdown, enrich_store_cls, run_enrich):
    run_enrich.return_value = {"scanned": 5, "enriched": 4, "audited": 1}
    result = runner.invoke(cli.main, ["enrich", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "enriched 4, audited 1 of 5 scanned" in result.output
    assert configure.call_args.args == ("corpus-enrich",)
    assert run_enrich.call_args.kwargs["limit"] == 5
    shutdown.assert_called_once()


def test_audit_secrets_command(runner, configure, shutdown, enrich_store_cls, run_audit):
    run_audit.return_value = {"scanned": 3, "audited": 2}
    result = runner.invoke(cli.main, ["audit-secrets"])
    assert result.exit_code == 0, result.output
    assert "audited 2 of 3 scanned" in result.output
    assert configure.call_args.args == ("corpus-audit",)
    shutdown.assert_called_once()


def test_export_command(runner, configure, shutdown, export_archive):
    export_archive.return_value = {"scanned": 5, "written": 4, "unchanged": 1}
    result = runner.invoke(cli.main, ["export", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "wrote 4, unchanged 1 of 5 scanned" in result.output
    assert configure.call_args.args == ("corpus-export",)
    assert export_archive.call_args.kwargs["limit"] == 5
    shutdown.assert_called_once()
