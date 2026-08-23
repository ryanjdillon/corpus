"""MCP server (streamable-HTTP) exposing the query layer as tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import search
from .config import settings

mcp = FastMCP("corpus", host=settings.host, port=settings.mcp_port)


@mcp.tool()
def corpus_search(query: str, top_k: int = 10) -> list[dict[str, Any]]:
    """Semantic search over indexed documents and email. Returns ranked matches."""
    return search.semantic_search(query, top_k)


@mcp.tool()
def corpus_query(
    label: str | None = None,
    account: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Structured metadata query (non-semantic). Returns ALL matches up to limit,
    e.g. label='promotional', before an ISO timestamp. Dates are ISO 8601."""
    results = search.structured_query(
        label=label, account=account, before=before, after=after, limit=limit
    )
    return {"count": len(results), "results": results}


@mcp.tool()
def corpus_get(id: str) -> dict[str, Any] | None:
    """Fetch one indexed document by id: full body content and all metadata.
    Use to escalate from a corpus_search snippet to the complete record.
    Returns null if no document has that id."""
    return search.get_document(id)


@mcp.tool()
def corpus_stats() -> dict[str, Any]:
    """Index totals and per-label counts."""
    return search.stats()


def run() -> None:
    mcp.run(transport="streamable-http")
