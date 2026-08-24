"""MCP server (streamable-HTTP) exposing the query layer as tools."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import search
from .config import settings

mcp = FastMCP(
    "corpus",
    host=settings.host,
    port=settings.mcp_port,
    instructions=(
        "corpus indexes the user's own email and documents. For ANY question about "
        "their mail or documents — recent messages, who wrote, what arrived, finding "
        "or summarizing a message — use these tools instead of saying you lack "
        "access: corpus_query (structured / time-ranged; the last day is since='1d'), "
        "corpus_search (semantic 'find X'), corpus_get (full body by id), "
        "corpus_stats (totals)."
    ),
)


@mcp.tool()
def corpus_search(
    query: Annotated[
        str, Field(description="Natural-language text, matched semantically against content.")
    ],
    top_k: Annotated[int, Field(description="Maximum number of ranked matches to return.")] = 10,
) -> list[dict[str, Any]]:
    """Semantic (vector) search over the indexed mail/documents.

    Use for meaning-based lookup ("find emails about X"). For time windows like
    "the last day/week", use ``corpus_query`` with ``since`` instead.

    Returns:
        Ranked matches, each with subject, from_addr, sent_at, label, and a
        300-character snippet.
    """
    return search.semantic_search(query, top_k)


@mcp.tool()
def corpus_query(
    label: Annotated[
        str | None,
        Field(description="Exact data-class label, e.g. 'personal', 'promotional', 'newsletter'."),
    ] = None,
    account: Annotated[
        str | None, Field(description="Exact account address to filter by.")
    ] = None,
    before: Annotated[
        str | None, Field(description="Upper bound on sent time, an absolute ISO-8601 timestamp.")
    ] = None,
    after: Annotated[
        str | None, Field(description="Lower bound on sent time, an absolute ISO-8601 timestamp.")
    ] = None,
    since: Annotated[
        str | None,
        Field(
            description="Relative lower bound (now - window): '30m', '24h', '7d', '2w'. Preferred "
            "for 'recent'/'last day' queries; ignored when `after` is given."
        ),
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum number of matches to return (newest first).")
    ] = 500,
) -> dict[str, Any]:
    """Structured, non-semantic metadata query over the indexed mail/documents.

    Use for time-ranged or "recent" questions (the last day is ``since='1d'``) and
    for filtering by label or account.

    Returns:
        A dict with ``count`` and ``results`` — metadata rows (id, subject,
        from_addr, sent_at, label), newest first.
    """
    results = search.structured_query(
        label=label, account=account, before=before, after=after, since=since, limit=limit
    )
    return {"count": len(results), "results": results}


@mcp.tool()
def corpus_get(
    id: Annotated[
        str, Field(description="Document id from a corpus_search / corpus_query result.")
    ],
) -> dict[str, Any] | None:
    """Fetch one indexed document by id: full body content and all metadata.

    Use to escalate from a ``corpus_search`` snippet to the complete record.

    Returns:
        A dict with ``id``, ``content`` (full body), and ``meta``; or ``None`` if
        no document has that id.
    """
    return search.get_document(id)


@mcp.tool()
def corpus_stats() -> dict[str, Any]:
    """Index totals and per-label document counts.

    Returns:
        A dict with ``total`` and ``by_label`` (label -> count).
    """
    return search.stats()


def run() -> None:
    mcp.run(transport="streamable-http")
