"""MCP server (streamable-HTTP) exposing the query layer as tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import search
from .config import settings
from .enrich_batch import audit_one
from .enrich_store import EnrichStore

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
    query: str = Field(description="Natural-language text, matched semantically against content."),
    top_k: int = Field(default=10, description="Maximum number of ranked matches to return."),
) -> list[dict[str, Any]]:
    """Semantic (vector) search over the indexed mail/documents, by MEANING.

    Use ONLY for topic/similarity lookups ("find emails about the invoice").
    Do NOT use for time-based questions — "today", "recent", "this week",
    "latest" — those MUST use ``corpus_query`` with ``since`` (semantic search
    ignores dates and will return old, irrelevant matches).

    Returns:
        Ranked matches, each with subject, from_addr, sent_at, label, and a
        300-character snippet.
    """
    return search.semantic_search(query, top_k)


@mcp.tool()
def corpus_query(
    label: str | None = Field(
        default=None,
        description="Exact data-class label, e.g. 'personal', 'promotional', 'newsletter'.",
    ),
    account: str | None = Field(default=None, description="Exact account address to filter by."),
    before: str | None = Field(
        default=None, description="Upper bound on sent time, an absolute ISO-8601 timestamp."
    ),
    after: str | None = Field(
        default=None, description="Lower bound on sent time, an absolute ISO-8601 timestamp."
    ),
    since: str | None = Field(
        default=None,
        description="Relative lower bound (now - window): '30m', '24h', '7d', '2w'. Preferred "
        "for 'recent'/'last day' queries; ignored when `after` is given.",
    ),
    limit: int = Field(
        default=500, description="Maximum number of matches to return (newest first)."
    ),
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
    id: str = Field(description="Document id from a corpus_search / corpus_query result."),
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


@mcp.tool()
def audit_secret(
    id: str = Field(description="Document id from a corpus_search / corpus_query result."),
) -> dict[str, Any] | None:
    """Re-run the LLM secret audit on one document, confirming its candidates.

    Re-scans with the current detectors and model and upserts the verdict. Use
    to re-confirm a single message once either has improved, rather than
    replaying the whole ``corpus audit-secrets`` job.

    Returns:
        A dict with ``id``, ``candidates`` (the types the detectors flagged),
        ``audit`` (the verdict with severity and notes), ``model``, and
        ``scan_version``; or ``None`` if no document has that id. ``audit`` is
        ``None`` when nothing was flagged -- the model is not consulted and no
        verdict is stored.
    """
    doc = search.get_document(id)
    if doc is None:
        return None
    with EnrichStore() as store:
        return audit_one(store, id, doc.get("content"), doc.get("meta"))


def run() -> None:
    """Serve the MCP tools over streamable-HTTP."""
    mcp.run(transport="streamable-http")
