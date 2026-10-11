"""MCP server (streamable-HTTP) exposing the query layer as tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import search
from .config import settings
from .envelope import wrap
from .tiers import tier

TIER = tier("sensitive").name
# Raw mail: the subject, sender and body are the third-party text; ids, dates,
# scores and the computed data-class label are not.
SEARCH_UNTRUSTED = ("from_addr", "snippet", "subject")
QUERY_UNTRUSTED = ("from_addr", "subject")
GET_UNTRUSTED = ("content", "meta")

mcp = FastMCP(
    "corpus",
    host=settings.host,
    port=settings.mcp_port,
    # Every tool is a self-contained query, so no session state is needed. A
    # stateful server expires idle sessions, and a long-lived client (a gateway
    # MCP proxy, an agent polling once a day) then gets 404 on its next call.
    stateless_http=True,
    instructions=(
        "corpus indexes the user's own email and documents. For ANY question about "
        "their mail or documents — recent messages, who wrote, what arrived, finding "
        "or summarizing a message — use these tools instead of saying you lack "
        "access: corpus_query (structured / time-ranged; the last day is since='1d'), "
        "corpus_search (semantic 'find X'), corpus_get (full body by id), "
        "corpus_stats (totals)."
    ),
)


def _rows(tool: str, rows: list[dict[str, Any]], untrusted: tuple[str, ...], **kw: Any) -> dict:
    return wrap(rows, server=mcp.name, tool=tool, tier=TIER, untrusted_fields=untrusted, **kw)


@mcp.tool()
def corpus_search(
    query: str = Field(description="Natural-language text, matched semantically against content."),
    top_k: int = Field(default=10, description="Maximum number of ranked matches to return."),
) -> dict[str, Any]:
    """Semantic (vector) search over the indexed mail/documents, by MEANING.

    Use ONLY for topic/similarity lookups ("find emails about the invoice").
    Do NOT use for time-based questions — "today", "recent", "this week",
    "latest" — those MUST use ``corpus_query`` with ``since`` (semantic search
    ignores dates and will return old, irrelevant matches).

    Returns:
        A ``corpus.result/v1`` envelope whose ``rows`` are ranked matches, each
        with subject, from_addr, sent_at, label, and a 300-character snippet.
    """
    return _rows("corpus_search", search.semantic_search(query, top_k), SEARCH_UNTRUSTED)


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
        A ``corpus.result/v1`` envelope whose ``rows`` are metadata (id, subject,
        from_addr, sent_at, label), newest first.
    """
    results = search.structured_query(
        label=label, account=account, before=before, after=after, since=since, limit=limit
    )
    return _rows("corpus_query", results, QUERY_UNTRUSTED)


@mcp.tool()
def corpus_get(
    id: str = Field(description="Document id from a corpus_search / corpus_query result."),
) -> dict[str, Any]:
    """Fetch one indexed document by id: full body content and all metadata.

    Use to escalate from a ``corpus_search`` snippet to the complete record.

    Returns:
        A ``corpus.result/v1`` envelope with 0 or 1 ``rows``, each with ``id``,
        ``content`` (full body), and ``meta``.
    """
    doc = search.get_document(id)
    return _rows("corpus_get", [doc] if doc else [], GET_UNTRUSTED)


@mcp.tool()
def corpus_stats() -> dict[str, Any]:
    """Index totals and per-label document counts.

    Returns:
        A ``corpus.result/v1`` envelope with one row per label (``label`` and
        ``count``) and the document count as ``total``.
    """
    s = search.stats()
    rows = [{"label": label, "count": n} for label, n in s["by_label"].items()]
    return _rows("corpus_stats", rows, (), total=s["total"])


def run() -> None:
    """Serve the MCP tools over streamable-HTTP."""
    mcp.run(transport="streamable-http")
