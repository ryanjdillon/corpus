"""MCP server (streamable-HTTP) exposing the query layer as tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import search
from .config import settings

mcp = FastMCP("corpus", host=settings.host, port=settings.mcp_port)


@mcp.tool()
def corpus_search(query: str, top_k: int = 10) -> list[dict[str, Any]]:
    """Semantic (vector) search over the indexed mail/documents by meaning — use
    for 'find emails about X'. For time windows like 'the last day/week', use
    corpus_query with `since` instead. Returns ranked matches, each with a snippet."""
    return search.semantic_search(query, top_k)


@mcp.tool()
def corpus_query(
    label: str | None = None,
    account: str | None = None,
    before: str | None = None,
    after: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Structured metadata query over the indexed mail/documents (non-semantic).
    Use this for time-ranged or 'recent' questions — e.g. the last day's mail is
    since='1d' (relative windows: '30m', '24h', '7d', '2w'). Also filter by label,
    account, or absolute ISO after/before timestamps. Returns ALL matches up to
    limit, newest first."""
    results = search.structured_query(
        label=label, account=account, before=before, after=after, since=since, limit=limit
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
