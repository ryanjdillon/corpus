"""Command-line entrypoints: api | mcp | ingest."""

from __future__ import annotations

import logging

import click
import uvicorn

from .config import settings


@click.group()
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@main.command()
def api() -> None:
    """Run the REST API server."""
    uvicorn.run("corpus.api:app", host=settings.host, port=settings.port)


@main.command()
def mcp() -> None:
    """Run the MCP server (streamable-HTTP)."""
    from .mcp_server import run

    run()


@main.command()
@click.argument("source")
@click.option("--batch-size", default=50, show_default=True)
def ingest(source: str, batch_size: int) -> None:
    """Ingest from a source id, e.g. 'imap:boatclub'."""
    from .ingest import ingest as run_ingest

    count = run_ingest(source, batch_size=batch_size)
    click.echo(f"ingested {count} documents from {source}")


if __name__ == "__main__":
    main()
