"""Command-line entrypoints for the corpus service.

Subcommands: api, mcp, index, index-init, ingest, scan, enrich, sync,
audit-secrets, export.
"""

from __future__ import annotations

import logging

import click
import uvicorn

from . import telemetry
from .config import settings


@click.group()
def main() -> None:
    """Dispatch corpus subcommands."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@main.command()
def api() -> None:
    """Run the REST API server."""
    telemetry.configure("corpus")
    from .api import app

    telemetry.instrument_fastapi(app)
    telemetry.register_corpus_size_gauge()
    uvicorn.run(app, host=settings.host, port=settings.port)


@main.command()
def mcp() -> None:
    """Run the MCP server (streamable-HTTP)."""
    telemetry.configure("corpus-mcp")
    from .mcp_server import run

    run()


@main.command()
def index() -> None:
    """Run the corpus-index MCP server.

    The sanitized surface a trust-downgraded consumer (Hermes) queries: summaries
    and priority signal, never raw bodies.
    """
    telemetry.configure("corpus-index")
    from .index_server import run

    run()


@main.command(name="index-init")
def index_init_cmd() -> None:
    """Create or refresh the sanitized_documents view and grant it to corpus_index_ro.

    Run as the schema owner (CORPUS_DATABASE_URL = corpus_app).
    """
    from .index_query import ensure_view

    ensure_view()
    click.echo("sanitized_documents view ensured")


@main.command()
@click.argument("source")
@click.option("--batch-size", default=50, show_default=True)
def ingest(source: str, batch_size: int) -> None:
    """Ingest from a source id, e.g. 'imap:example'."""
    telemetry.configure("corpus-ingest")
    from .ingest import ingest as run_ingest

    try:
        count = run_ingest(source, batch_size=batch_size)
        click.echo(f"ingested {count} documents from {source}")
    finally:
        telemetry.shutdown()  # flush metrics/traces before the process exits


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="enrich at most N messages (0 = all)")
@click.option("--force", is_flag=True, help="re-enrich documents already stored")
def enrich(source: str | None, account: str | None, limit: int, force: bool) -> None:
    """Batch-enrich stored documents with summary and classification.

    Also runs an LLM secret audit on any document with flagged candidates.
    """
    telemetry.configure("corpus-enrich")
    from .enrich_batch import run_enrich
    from .enrich_store import EnrichStore

    try:
        with EnrichStore() as store:
            r = run_enrich(store, source=source, account=account, limit=limit, force=force)
        click.echo(f"enriched {r['enriched']}, audited {r['audited']} of {r['scanned']} scanned")
    finally:
        telemetry.shutdown()


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--limit", default=0, type=int, help="sync at most N documents (0 = all)")
@click.option("--force", is_flag=True, help="re-project documents already synced")
def sync(source: str | None, limit: int, force: bool) -> None:
    """Project enriched documents into the sanitized DB.

    The sanitized DB is the trust-downgraded tier a cloud consumer may query.
    Drops raw content/subject/sender; gates summaries by sensitivity.
    """
    telemetry.configure("corpus-sync")
    from .sanitize import run_sync
    from .sanitized_store import SanitizedStore
    from .tiers import tier

    src, dst = tier("sensitive"), tier("sanitized")
    try:
        with SanitizedStore(dst.dsn) as store:
            r = run_sync(store, source=source, limit=limit, force=force, read_dsn=src.dsn)
        click.echo(f"synced {r['synced']}, skipped {r['skipped']} of {r['scanned']} scanned")
    finally:
        telemetry.shutdown()


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="export at most N documents (0 = all)")
@click.option("--force", is_flag=True, help="rewrite vault files even if unchanged")
def export(source: str | None, account: str | None, limit: int, force: bool) -> None:
    """Materialize stored documents into the local markdown vault (one file each)."""
    telemetry.configure("corpus-export")
    from .export import export_archive

    try:
        r = export_archive(source=source, account=account, limit=limit, force=force)
        click.echo(f"wrote {r['written']}, unchanged {r['unchanged']} of {r['scanned']} scanned")
    finally:
        telemetry.shutdown()


@main.command(name="audit-secrets")
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="scan at most N messages (0 = all)")
def audit_secrets_cmd(source: str | None, account: str | None, limit: int) -> None:
    """Re-run only the LLM secret confirmation over stored documents with candidates.

    No re-enrichment is performed.
    """
    telemetry.configure("corpus-audit")
    from .enrich_batch import run_audit
    from .enrich_store import EnrichStore

    try:
        with EnrichStore() as store:
            r = run_audit(store, source=source, account=account, limit=limit)
        click.echo(f"audited {r['audited']} of {r['scanned']} scanned")
    finally:
        telemetry.shutdown()


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="scan at most N messages (0 = all)")
@click.option(
    "--json",
    "json_out",
    default=None,
    type=click.Path(dir_okay=False),
    help="also write the full report (ids + types, no values) as JSON",
)
def scan(source: str | None, account: str | None, limit: int, json_out: str | None) -> None:
    """Scan stored documents for secrets — reports types + counts, never values."""
    from .scan import scan_archive

    report = scan_archive(source=source, account=account, limit=limit)
    click.echo(f"scanned {report['scanned']}; {report['with_secrets']} contain secrets")
    for secret_type, count in sorted(report["totals"].items(), key=lambda kv: -kv[1]):
        messages = sum(1 for h in report["hits"] if secret_type in h["secret_types"])
        click.echo(f"  {secret_type}: {count} ({messages} messages)")
    for hit in report["hits"]:
        types = ",".join(hit["secret_types"])
        click.echo(f"{hit['sent_at']}  [{types}]  {hit['from_addr']}  {hit['subject']}")
    if json_out:
        import json
        from pathlib import Path

        Path(json_out).write_text(json.dumps(report, indent=2))
        click.echo(f"wrote {json_out}")


if __name__ == "__main__":
    main()
