"""Command-line entrypoints: api | mcp | ingest."""

from __future__ import annotations

import logging

import click
import uvicorn

from . import telemetry
from .config import settings


@click.group()
def main() -> None:
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


if __name__ == "__main__":
    main()
