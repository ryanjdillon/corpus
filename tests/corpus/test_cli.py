"""CLI entrypoints wire telemetry and hand off to the servers/ingest.

The servers, batch orchestrators, and stores are autospec'd so nothing binds a
port or hits a DB. Each collaborator has one fixture with a neutral default;
tests override the return value and assert on the parsed arguments.
"""

from __future__ import annotations

import textwrap
from unittest.mock import create_autospec

import pytest
from click.testing import CliRunner

from corpus import (
    cli,
    enrich_batch,
    enrich_store,
    export,
    ingest,
    pipeline,
    sanitize,
    sanitized_store,
    scan,
    scan_gate,
    serving,
    store,
    telemetry,
    tiers,
)
from corpus.config import settings
from corpus.tiers import tier


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
def register_gauge(monkeypatch):
    """Autospec ``telemetry.register_corpus_size_gauge``."""
    mock = create_autospec(telemetry.register_corpus_size_gauge)
    monkeypatch.setattr(telemetry, "register_corpus_size_gauge", mock)
    return mock


@pytest.fixture
def serve(monkeypatch):
    """Autospec ``serving.serve`` so no MCP server binds a port."""
    mock = create_autospec(serving.serve)
    monkeypatch.setattr(serving, "serve", mock)
    return mock


@pytest.fixture
def scan_gate_serve(monkeypatch):
    """Autospec the scan-gate ``serve`` so no gRPC server binds a port."""
    mock = create_autospec(scan_gate.serve)
    monkeypatch.setattr(scan_gate, "serve", mock)
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
def document_store_cls(monkeypatch):
    """Autospec ``DocumentStore`` as a context manager yielding an instance mock."""
    cls = create_autospec(store.DocumentStore)
    cls.return_value.__enter__.return_value = create_autospec(
        store.DocumentStore, instance=True
    )
    monkeypatch.setattr(store, "DocumentStore", cls)
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


#: A run_sync result with nothing left to do.
SYNCED = {"scanned": 0, "synced": 0, "skipped": 0, "reprojected": 0, "embedded": 0,
          "pending": 0, "embed_stale": 0}


@pytest.fixture
def run_sync(monkeypatch):
    """Autospec ``sanitize.run_sync`` with a neutral default result."""
    mock = create_autospec(
        sanitize.run_sync, return_value=SYNCED,
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
def pipeline_run(monkeypatch):
    """Autospec ``pipeline.run`` with a neutral default result."""
    mock = create_autospec(pipeline.run, return_value={})
    monkeypatch.setattr(pipeline, "run", mock)
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


@pytest.mark.parametrize(
    ("args", "tier", "service", "gauge"),
    [
        (["serve", "sensitive"], "sensitive", "corpus-mcp", True),
        (["serve", "sanitized"], "sanitized", "corpus-index", False),
        (["mcp"], "sensitive", "corpus-mcp", True),
        (["index"], "sanitized", "corpus-index", False),
    ],
)
def test_serve_command(runner, configure, register_gauge, serve, args, tier, service, gauge):
    result = runner.invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == (service,)
    # The corpus-size gauge reads the sensitive store, so only its server reports it.
    assert register_gauge.called is gauge
    serve.assert_called_once_with(tier)


@pytest.mark.parametrize("alias", ["mcp", "index"])
def test_the_old_server_commands_are_deprecated(runner, configure, register_gauge, serve, alias):
    result = runner.invoke(cli.main, [alias])
    assert "deprecated" in result.output.lower()


def test_serve_refuses_an_unknown_tier(runner, serve):
    result = runner.invoke(cli.main, ["serve", "raw"])
    assert result.exit_code != 0
    serve.assert_not_called()


def test_scan_gate_command(runner, configure, shutdown, scan_gate_serve):
    result = runner.invoke(cli.main, ["scan-gate"])
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus-scan-gate",)
    scan_gate_serve.assert_called_once()
    shutdown.assert_called_once()


SANITIZED_DSN = "postgresql://sync-placeholder@db.example.com/ai_sanitized"


@pytest.fixture
def sanitized_dsn(monkeypatch):
    """The sanitized tier's DSN variable, set to a placeholder."""
    monkeypatch.setenv("CORPUS_SANITIZED_DATABASE_URL", SANITIZED_DSN)


def test_sync_command(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn
):
    run_sync.return_value = {**SYNCED, "synced": 3, "skipped": 1, "scanned": 4, "embedded": 3}
    result = runner.invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus-sync",)
    assert "synced 3, skipped 1 of 4 scanned; re-projected 0, embedded 3" in result.output
    # Everything comes from the sanitized tier: its store, its projection's
    # source tier and policy.
    dst = tier("sanitized")
    src = tier(dst.projection.source)
    assert sanitized_store_cls.call_args.args == (SANITIZED_DSN, dst.schema)
    assert document_store_cls.call_args.args == (src.dsn, src.schema)
    kwargs = run_sync.call_args.kwargs
    assert (kwargs["source"], kwargs["force"]) == (None, False)
    assert kwargs["policy"] == dst.projection.policy
    assert kwargs["reembed_limit"] == settings.sync_reembed_limit == 500
    assert "older policy" not in result.output
    assert "await a new embedding" not in result.output


def test_sync_reports_what_is_left_to_re_project_and_re_embed(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn
):
    run_sync.return_value = {**SYNCED, "scanned": 1700, "reprojected": 1700, "embedded": 500,
                             "pending": 12, "embed_stale": 1200}
    result = runner.invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output
    assert "re-projected 1700, embedded 500" in result.output
    assert "12 rows still projected under an older policy" in result.output
    assert "1200 rows await a new embedding; each sync re-embeds up to 500" in result.output


def test_sync_reports_a_re_embedding_skipped_for_another_sync(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn
):
    run_sync.return_value = {**SYNCED, "embed_stale": 9, "reembed_skipped": 1}
    result = runner.invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output
    assert "embedded 0" in result.output
    assert sanitize.REEMBED_SKIPPED in result.output


def test_sync_says_nothing_of_the_lock_when_it_re_embeds(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn
):
    result = runner.invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output
    assert "skipped:" not in result.output


@pytest.mark.parametrize("limit", ["0", "50"])
def test_sync_fails_when_a_requested_re_embedding_is_skipped(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn,
    limit,
):
    run_sync.return_value = {**SYNCED, "embed_stale": 9, "reembed_skipped": 1}
    result = runner.invoke(cli.main, ["sync", "--reembed-limit", limit])
    assert result.exit_code == 1, result.output
    assert sanitize.REEMBED_SKIPPED in result.output
    assert "rerun with --reembed-wait" in result.output
    shutdown.assert_called_once()


def test_sync_does_not_wait_for_the_re_embed_lock_by_default(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn
):
    result = runner.invoke(cli.main, ["sync", "--reembed-limit", "0"])
    assert result.exit_code == 0, result.output
    assert run_sync.call_args.kwargs["reembed_wait"] is False


def test_sync_reembed_wait_waits_and_says_so(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn
):
    def blocked(*args, **kwargs):
        kwargs["on_reembed_wait"]()  # the lock was held elsewhere
        return {**SYNCED, "embedded": 9}

    run_sync.side_effect = blocked
    result = runner.invoke(cli.main, ["sync", "--reembed-limit", "0", "--reembed-wait"])
    assert result.exit_code == 0, result.output
    assert run_sync.call_args.kwargs["reembed_wait"] is True
    assert result.output.index(sanitize.REEMBED_WAITING) < result.output.index("embedded 9")


@pytest.mark.parametrize("flag", ["--reproject", "--force"])
def test_sync_reproject_re_projects_every_row(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn,
    flag,
):
    result = runner.invoke(cli.main, ["sync", flag])
    assert result.exit_code == 0, result.output
    assert run_sync.call_args.kwargs["force"] is True
    # Text only: the embedding phase keeps its usual cap.
    assert run_sync.call_args.kwargs["reembed_limit"] == settings.sync_reembed_limit


@pytest.mark.parametrize(("value", "shown"), [("0", "all"), ("50", "50")])
def test_sync_reembed_limit_overrides_the_setting(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, sanitized_dsn,
    value, shown,
):
    run_sync.return_value = {**SYNCED, "embed_stale": 9}
    result = runner.invoke(cli.main, ["sync", "--reembed-limit", value])
    assert result.exit_code == 0, result.output
    assert run_sync.call_args.kwargs["reembed_limit"] == int(value)
    assert f"re-embeds up to {shown}" in result.output


def test_sync_refuses_a_negative_reembed_limit(runner, configure, run_sync, sanitized_dsn):
    result = runner.invoke(cli.main, ["sync", "--reembed-limit", "-1"])
    assert result.exit_code == 2
    run_sync.assert_not_called()


def test_sync_without_the_sanitized_dsn_fails_naming_it(
    runner, configure, sanitized_store_cls, run_sync, monkeypatch
):
    monkeypatch.delenv("CORPUS_SANITIZED_DATABASE_URL", raising=False)
    result = runner.invoke(cli.main, ["sync"])
    assert result.exit_code != 0
    assert "CORPUS_SANITIZED_DATABASE_URL not set" in result.output
    run_sync.assert_not_called()


def test_serve_with_a_bad_tiers_file_fails_clearly(runner, configure, serve, monkeypatch):
    def bad(name):
        raise ValueError("tiers file /etc/corpus/tiers.yaml: Object missing required field `query`")

    monkeypatch.setattr(serving, "tier_service", create_autospec(serving.tier_service, side_effect=bad))
    result = runner.invoke(cli.main, ["serve", "sanitized"])
    assert result.exit_code != 0
    assert "missing required field `query`" in result.output
    serve.assert_not_called()


def test_ingest_command(runner, configure, shutdown, ingest_fn):
    ingest_fn.return_value = 7
    result = runner.invoke(cli.main, ["ingest", "imap:test", "--batch-size", "5"])
    assert result.exit_code == 0, result.output
    assert "ingested 7 documents from imap:test" in result.output
    assert configure.call_args.args == ("corpus-ingest",)
    assert ingest_fn.call_args.args == ("imap:test",)
    assert ingest_fn.call_args.kwargs == {"batch_size": 5}
    shutdown.assert_called_once()


def test_scan_command(runner, scan_archive, tmp_path, document_store_cls):
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


def test_ingest_without_a_batch_size_takes_the_sources(runner, configure, shutdown, ingest_fn):
    result = runner.invoke(cli.main, ["ingest", "imap:test"])
    assert result.exit_code == 0, result.output
    assert ingest_fn.call_args.kwargs == {"batch_size": None}


def test_ingest_reports_an_unconfigured_source_without_a_traceback(
    runner, configure, shutdown, ingest_fn
):
    ingest_fn.side_effect = ValueError("no source 'imap:other' in the config file's sources:")
    result = runner.invoke(cli.main, ["ingest", "imap:other"])
    assert result.exit_code == 1
    assert "no source 'imap:other'" in result.output
    assert "Traceback" not in result.output
    shutdown.assert_called_once()


def test_ingest_flushes_telemetry_on_error(runner, configure, shutdown, ingest_fn):
    ingest_fn.side_effect = RuntimeError("ingest failed")
    result = runner.invoke(cli.main, ["ingest", "imap:test"])
    assert result.exit_code != 0
    shutdown.assert_called_once()  # flushed even on failure


def test_enrich_command(
    runner, configure, shutdown, enrich_store_cls, run_enrich, document_store_cls
):
    run_enrich.return_value = {"scanned": 5, "enriched": 4, "audited": 1, "ineligible": 0}
    result = runner.invoke(cli.main, ["enrich", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "enriched 4, audited 1 of 5 scanned" in result.output
    assert "not enrichable" not in result.output  # nothing excluded, nothing said
    assert configure.call_args.args == ("corpus-enrich",)
    assert run_enrich.call_args.kwargs["limit"] == 5
    shutdown.assert_called_once()


def test_enrich_command_passes_redo_recovered(
    runner, configure, shutdown, enrich_store_cls, run_enrich, document_store_cls
):
    run_enrich.return_value = {"scanned": 1, "enriched": 1, "audited": 0, "ineligible": 0}
    result = runner.invoke(cli.main, ["enrich", "--redo-recovered"])
    assert result.exit_code == 0, result.output
    assert run_enrich.call_args.kwargs["redo_recovered"] is True


def test_enrich_command_reports_ineligible_documents(
    runner, configure, shutdown, enrich_store_cls, run_enrich, document_store_cls
):
    run_enrich.return_value = {
        "scanned": 5, "enriched": 1, "audited": 0, "ineligible": 4, "declined": 3, "undeclared": 1,
    }
    result = runner.invoke(cli.main, ["enrich"])
    assert result.exit_code == 0, result.output
    assert (
        "4 not enrichable by source policy: 3 declared enrich: false, "
        "1 from sources with no entry"
    ) in result.output


def test_enrich_command_reports_a_policy_refusal_without_a_traceback(
    runner, configure, shutdown, enrich_store_cls, run_enrich, document_store_cls
):
    run_enrich.side_effect = ValueError("source 'youtube:@chan' is not declared enrichable")
    result = runner.invoke(cli.main, ["enrich", "--source", "youtube:@chan"])
    assert result.exit_code != 0
    assert "not declared enrichable" in result.output
    assert "Traceback" not in result.output
    shutdown.assert_called_once()  # telemetry still flushed on refusal


def test_audit_secrets_command(
    runner, configure, shutdown, enrich_store_cls, run_audit, document_store_cls
):
    run_audit.return_value = {"scanned": 3, "audited": 2}
    result = runner.invoke(cli.main, ["audit-secrets"])
    assert result.exit_code == 0, result.output
    assert "audited 2 of 3 scanned" in result.output
    assert configure.call_args.args == ("corpus-audit",)
    shutdown.assert_called_once()


def test_export_command(runner, configure, shutdown, export_archive, document_store_cls):
    export_archive.return_value = {"scanned": 5, "written": 4, "unchanged": 1}
    result = runner.invoke(cli.main, ["export", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "wrote 4, unchanged 1 of 5 scanned" in result.output
    assert configure.call_args.args == ("corpus-export",)
    assert export_archive.call_args.kwargs["limit"] == 5
    shutdown.assert_called_once()


PARTNER_DSN = "postgresql://partner-placeholder@db.example.com/ai_partner"
#: The default tiers plus a third, projected one, defined only in the file.
THREE_TIERS = """
tiers:
  - name: sensitive
    store: {dsn_env: CORPUS_DATABASE_URL}
    serve: {server_name: corpus}
    query: {max_limit: 100, filterable: [label], aggregatable: [label], orders: [newest], text: true, semantic: true}
  - name: sanitized
    store: {dsn_env: CORPUS_SANITIZED_DATABASE_URL}
    projection: {from: sensitive}
    serve: {server_name: corpus-index}
    query: {max_limit: 100, filterable: [domain], aggregatable: [domain], orders: [priority], text: true, semantic: true}
  - name: partner
    store: {dsn_env: PARTNER_DATABASE_URL, schema: partner}
    projection:
      from: sensitive
      fields: [domain, one_line]
      sensitivity_gate: low
      gate_action: stub
      redact: {person_name: initial}
    serve: {server_name: corpus-partner}
    query: {max_limit: 20, filterable: [domain], aggregatable: [domain], orders: [priority], text: true, semantic: true}
"""


@pytest.fixture
def three_tiers(monkeypatch, tmp_path):
    """The process's tiers loaded from a file defining a third tier, ``partner``."""
    path = tmp_path / "tiers.yaml"
    path.write_text(textwrap.dedent(THREE_TIERS))
    monkeypatch.setattr(settings, "tiers_file", str(path))
    monkeypatch.setenv("PARTNER_DATABASE_URL", PARTNER_DSN)
    tiers.registry.cache_clear()
    yield
    tiers.registry.cache_clear()


def test_sync_into_a_tier_defined_only_in_the_file(
    runner, configure, shutdown, sanitized_store_cls, run_sync, document_store_cls, three_tiers
):
    result = runner.invoke(cli.main, ["sync", "--tier", "partner"])
    assert result.exit_code == 0, result.output
    src = tier("sensitive")
    assert sanitized_store_cls.call_args.args == (PARTNER_DSN, "partner")
    assert document_store_cls.call_args.args == (src.dsn, src.schema)
    policy = run_sync.call_args.kwargs["policy"]
    assert (policy.sensitivity_gate.value, policy.fields) == ("low", ("domain", "one_line"))
    assert (policy.gate_action, policy.redact.person_name) == ("stub", "initial")


@pytest.mark.parametrize(
    ("name", "message"),
    [("sensitive", "source tier"), ("raw", "no tier named 'raw'")],
)
def test_sync_refuses_a_tier_without_a_projection(
    runner, configure, sanitized_store_cls, run_sync, three_tiers, name, message
):
    result = runner.invoke(cli.main, ["sync", "--tier", name])
    assert result.exit_code != 0
    assert message in result.output
    run_sync.assert_not_called()


def test_serve_a_tier_defined_only_in_the_file(
    runner, configure, register_gauge, serve, three_tiers
):
    result = runner.invoke(cli.main, ["serve", "partner"])
    assert result.exit_code == 0, result.output
    # A projected tier serves the sanitized surface, under its own name.
    assert configure.call_args.args == ("corpus-index",)
    assert not register_gauge.called
    serve.assert_called_once_with("partner")


def test_serve_refuses_a_tier_the_file_does_not_define(runner, serve, three_tiers):
    result = runner.invoke(cli.main, ["serve", "raw"])
    assert result.exit_code != 0
    assert "no tier named 'raw'" in result.output
    serve.assert_not_called()


def test_serve_refuses_a_file_declaring_a_source_twice(
    runner, serve, monkeypatch, tmp_path
):
    # serve reads only the tiers, but the whole file must still be valid.
    path = tmp_path / "corpus.yaml"
    path.write_text(textwrap.dedent("""
        sources:
          - {name: club, kind: imap, enrich: true}
          - {name: club, kind: imap, enrich: false}
    """))
    monkeypatch.setattr(settings, "config_file", str(path))
    monkeypatch.setattr(settings, "tiers_file", "")
    tiers.registry.cache_clear()
    try:
        result = runner.invoke(cli.main, ["serve", "sensitive"])
    finally:
        tiers.registry.cache_clear()
    assert result.exit_code != 0
    assert "source 'imap:club' is declared twice" in result.output
    serve.assert_not_called()


def test_pipeline_command(runner, configure, shutdown, pipeline_run):
    pipeline_run.return_value = {
        "ingest": {"ingested": 3},
        "sync": {"scanned": 3, "synced": 2, "skipped": 1},
    }
    result = runner.invoke(
        cli.main, ["pipeline", "imap:test", "--stages", "ingest,sync", "--limit", "5",
                   "--batch-size", "16", "--tier", "partner"],
    )
    assert result.exit_code == 0, result.output
    assert configure.call_args.args == ("corpus-pipeline",)
    assert pipeline_run.call_args.args == ("imap:test", ("ingest", "sync"))
    assert pipeline_run.call_args.kwargs == {
        "options": pipeline.Options(limit=5, batch_size=16, tier="partner")
    }
    assert "ingest: ingested 3" in result.output
    assert "sync: scanned 3, synced 2, skipped 1" in result.output
    shutdown.assert_called_once()


def test_pipeline_reports_a_re_embedding_skipped_for_another_sync(
    runner, configure, shutdown, pipeline_run
):
    pipeline_run.return_value = {"sync": {"scanned": 3, "embedded": 0, "reembed_skipped": 1}}
    result = runner.invoke(cli.main, ["pipeline", "imap:test", "--stages", "sync"])
    assert result.exit_code == 0, result.output
    assert "sync: scanned 3, embedded 0\n" in result.output
    assert f"sync: {sanitize.REEMBED_SKIPPED}" in result.output


def test_pipeline_runs_every_stage_by_default(runner, configure, shutdown, pipeline_run):
    result = runner.invoke(cli.main, ["pipeline", "imap:test"])
    assert result.exit_code == 0, result.output
    assert pipeline_run.call_args.args == ("imap:test", ("ingest", "enrich", "sync"))
    assert pipeline_run.call_args.kwargs == {"options": pipeline.Options()}


def test_pipeline_ingests_in_the_same_batches_as_ingest_by_default(
    runner, configure, shutdown, pipeline_run, ingest_fn
):
    assert runner.invoke(cli.main, ["ingest", "imap:test"]).exit_code == 0
    assert runner.invoke(cli.main, ["pipeline", "imap:test"]).exit_code == 0

    assert (pipeline_run.call_args.kwargs["options"].batch_size
            == ingest_fn.call_args.kwargs["batch_size"])


def test_pipeline_refuses_a_batch_size_below_one(runner, configure, shutdown, pipeline_run):
    result = runner.invoke(cli.main, ["pipeline", "imap:test", "--batch-size", "0"])
    assert result.exit_code == 2
    pipeline_run.assert_not_called()


@pytest.mark.parametrize(
    ("stages", "message"),
    [
        ("sync,ingest", "must be in the order ingest,enrich,sync"),
        ("ingest,export", "unknown stage(s) export"),
        ("enrich,audit", "'audit' is not a stage"),
        (",", "no stages selected"),
    ],
)
def test_pipeline_refuses_a_bad_stage_list(
    runner, configure, shutdown, pipeline_run, stages, message
):
    result = runner.invoke(cli.main, ["pipeline", "imap:test", "--stages", stages])
    assert result.exit_code == 2  # a usage error
    assert message in result.output
    pipeline_run.assert_not_called()


def test_pipeline_exits_non_zero_when_a_stage_fails(runner, configure, shutdown, pipeline_run):
    failed = pipeline.StageFailedError("enrich", {"ingest": {"ingested": 3}})
    failed.__cause__ = RuntimeError("endpoint unavailable")
    pipeline_run.side_effect = failed
    result = runner.invoke(cli.main, ["pipeline", "imap:test"])
    assert result.exit_code == 1
    assert "ingest: ingested 3" in result.output  # what completed is reported
    assert "enrich failed: endpoint unavailable" in result.output
    assert "Traceback" not in result.output
    shutdown.assert_called_once()  # flushed even on failure


@pytest.mark.parametrize(
    "refusal",
    [
        ValueError("source 'youtube:@chan' is not declared enrichable"),
        ValueError("no tier named 'nope'"),
        RuntimeError("CORPUS_SANITIZED_DATABASE_URL not set"),
    ],
)
def test_pipeline_exits_2_when_the_run_is_refused(
    runner, configure, shutdown, pipeline_run, refusal
):
    # A refusal before any stage runs is a usage error (2), distinct from a stage
    # failure (1), so a scheduler can tell the job's configuration is wrong.
    pipeline_run.side_effect = refusal
    result = runner.invoke(cli.main, ["pipeline", "youtube:@chan"])
    assert result.exit_code == 2
    assert str(refusal) in result.output
    assert "Traceback" not in result.output
    shutdown.assert_called_once()
