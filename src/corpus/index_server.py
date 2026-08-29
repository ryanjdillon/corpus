"""corpus-index MCP surface: sanitized, whitelisted tools for a downgraded consumer.

These are the tools a trust-downgraded consumer (Hermes, on a cloud model) may
call. Every tool is backed only by the ``sanitized_documents`` view read as
``corpus_index_ro`` — summaries + priority signal, never a raw body or secret.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import index_query
from .config import settings

mcp = FastMCP(
    "corpus-index",
    host=settings.host,
    port=settings.mcp_port,
    instructions=(
        "Sanitized priority signal over the owner's private corpus. You receive "
        "secret-free summaries and classification only — never raw message bodies. "
        "Use it to build a prioritized plan: what needs action, what's due, what's "
        "awaiting a reply."
    ),
)


@mcp.tool()
def index_action_items(
    limit: int = Field(50, description="max items"),
    domain: str | None = Field(None, description="filter by life-area domain (banking, health, work…)"),
    importance: str | None = Field(None, description="filter: high | medium | low"),
) -> list[dict]:
    """Messages that need an action from you, ranked by importance then urgency."""
    return index_query.action_items(limit=limit, domain=domain, importance=importance)


@mcp.tool()
def index_due_soon(limit: int = Field(50, description="max items")) -> list[dict]:
    """Time-sensitive items, or items carrying a deadline."""
    return index_query.due_soon(limit=limit)


@mcp.tool()
def index_waiting_on(
    who: str = Field("them", description="'them' = you're waiting on someone; 'me' = they're waiting on you"),
    limit: int = Field(50, description="max items"),
) -> list[dict]:
    """Threads awaiting a reply."""
    return index_query.waiting_on(who=who, limit=limit)


@mcp.tool()
def index_by_domain(domain: str, limit: int = Field(50, description="max items")) -> list[dict]:
    """Recent sanitized items in a life-area domain."""
    return index_query.by_domain(domain, limit=limit)


@mcp.tool()
def index_summary(id: str) -> dict | None:
    """The sanitized summary + priority signal for one message id."""
    return index_query.summary(id)


@mcp.tool()
def index_stats() -> dict:
    """Counts of sanitized items by domain and suggested disposition."""
    return index_query.stats()


def run() -> None:
    """Serve the corpus-index MCP tools over streamable HTTP."""
    mcp.run(transport="streamable-http")
