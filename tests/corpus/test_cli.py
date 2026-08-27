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


def test_scan_command(monkeypatch, tmp_path):
    import corpus.scan as scan_mod

    report = {
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
    monkeypatch.setattr(scan_mod, "scan_archive", lambda source, account, limit: report)

    out = tmp_path / "report.json"
    result = CliRunner().invoke(cli.main, ["scan", "--json", str(out)])
    assert result.exit_code == 0, result.output
    assert "scanned 3; 1 contain secrets" in result.output
    assert "us_ssn: 2 (1 messages)" in result.output
    assert "[us_ssn]  a@b.com  form" in result.output
    assert out.exists()
    assert '"us_ssn"' in out.read_text()


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


class _FakeStoreCM:
    """Stand-in for EnrichStore's context manager in CLI tests (no DB)."""

    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def test_enrich_command(monkeypatch):
    import corpus.enrich_batch as eb
    import corpus.enrich_store as es

    calls = {}
    monkeypatch.setattr(cli.telemetry, "configure", lambda n: calls.__setitem__("cfg", n))
    monkeypatch.setattr(cli.telemetry, "shutdown", lambda: calls.__setitem__("shut", True))
    monkeypatch.setattr(es, "EnrichStore", _FakeStoreCM)
    monkeypatch.setattr(
        eb,
        "run_enrich",
        lambda store, source, account, limit, force: {"scanned": 5, "enriched": 4, "audited": 1},
    )

    result = CliRunner().invoke(cli.main, ["enrich", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "enriched 4, audited 1 of 5 scanned" in result.output
    assert calls == {"cfg": "corpus-enrich", "shut": True}


def test_audit_secrets_command(monkeypatch):
    import corpus.enrich_batch as eb
    import corpus.enrich_store as es

    calls = {}
    monkeypatch.setattr(cli.telemetry, "configure", lambda n: calls.__setitem__("cfg", n))
    monkeypatch.setattr(cli.telemetry, "shutdown", lambda: calls.__setitem__("shut", True))
    monkeypatch.setattr(es, "EnrichStore", _FakeStoreCM)
    monkeypatch.setattr(
        eb, "run_audit", lambda store, source, account, limit: {"scanned": 3, "audited": 2}
    )

    result = CliRunner().invoke(cli.main, ["audit-secrets"])
    assert result.exit_code == 0, result.output
    assert "audited 2 of 3 scanned" in result.output
    assert calls == {"cfg": "corpus-audit", "shut": True}
