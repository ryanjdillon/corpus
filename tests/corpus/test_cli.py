"""CLI entrypoints wire telemetry and hand off to the servers/ingest. The
servers and ingest are mocked so nothing binds a port or hits a DB."""

from __future__ import annotations

from click.testing import CliRunner

from corpus import cli


def test_api_command(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.telemetry, "configure", lambda n: calls.__setitem__("cfg", n))
    monkeypatch.setattr(cli.telemetry, "instrument_fastapi", lambda app: calls.__setitem__("instr", True))
    monkeypatch.setattr(cli.telemetry, "register_corpus_size_gauge", lambda: calls.__setitem__("gauge", True))
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: calls.__setitem__("run", True))

    result = CliRunner().invoke(cli.main, ["api"])
    assert result.exit_code == 0, result.output
    assert calls == {"cfg": "corpus", "instr": True, "gauge": True, "run": True}


def test_mcp_command(monkeypatch):
    from corpus import mcp_server

    calls = {}
    monkeypatch.setattr(cli.telemetry, "configure", lambda n: calls.__setitem__("cfg", n))
    monkeypatch.setattr(mcp_server, "run", lambda: calls.__setitem__("run", True))

    result = CliRunner().invoke(cli.main, ["mcp"])
    assert result.exit_code == 0, result.output
    assert calls == {"cfg": "corpus-mcp", "run": True}


def test_ingest_command(monkeypatch):
    import corpus.ingest as ingest_mod

    calls = {}
    monkeypatch.setattr(cli.telemetry, "configure", lambda n: calls.__setitem__("cfg", n))
    monkeypatch.setattr(cli.telemetry, "shutdown", lambda: calls.__setitem__("shut", True))
    monkeypatch.setattr(ingest_mod, "ingest", lambda source, batch_size: 7)

    result = CliRunner().invoke(cli.main, ["ingest", "imap:test", "--batch-size", "5"])
    assert result.exit_code == 0, result.output
    assert "ingested 7 documents from imap:test" in result.output
    assert calls == {"cfg": "corpus-ingest", "shut": True}


def test_ingest_flushes_telemetry_on_error(monkeypatch):
    import corpus.ingest as ingest_mod

    calls = {}
    monkeypatch.setattr(cli.telemetry, "configure", lambda n: None)
    monkeypatch.setattr(cli.telemetry, "shutdown", lambda: calls.__setitem__("shut", True))

    def boom(source, batch_size):
        raise RuntimeError("ingest failed")

    monkeypatch.setattr(ingest_mod, "ingest", boom)

    result = CliRunner().invoke(cli.main, ["ingest", "imap:test"])
    assert result.exit_code != 0
    assert calls.get("shut") is True  # flushed even on failure
