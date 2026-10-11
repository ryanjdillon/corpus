"""corpus-index MCP surface: sanitized, whitelisted tools for a downgraded consumer.

These are the tools a trust-downgraded consumer (Hermes, on a cloud model) may
call. Every tool is backed only by the sanitized ``messages`` table (the separate
``ai_sanitized`` database) read as ``corpus_index_ro`` — summaries + priority
signal, never a raw body or secret.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import index_query
from .config import settings
from .envelope import wrap
from .sanitized_store import UNTRUSTED_COLS
from .tiers import tier

TIER = tier("sanitized").name

mcp = FastMCP(
    settings.index_server_name,
    host=settings.host,
    port=settings.mcp_port,
    # Every tool is a self-contained query, so no session state is needed. A
    # stateful server expires idle sessions, and a long-lived client (a gateway
    # MCP proxy, an agent polling once a day) then gets 404 on its next call.
    stateless_http=True,
    instructions=(
        "Sanitized priority signal over the owner's private corpus. You receive "
        "secret-free summaries and classification only — never raw message bodies. "
        "Use it to build a prioritized plan: what needs action, what's due, what's "
        "awaiting a reply."
    ),
)


def _rows(tool: str, rows: list[dict], *, total: int | None = None) -> dict[str, Any]:
    return wrap(
        rows, server=mcp.name, tool=tool, tier=TIER, untrusted_fields=UNTRUSTED_COLS, total=total
    )


@mcp.tool()
def index_action_items(
    limit: int = Field(50, description="max items"),
    domain: str | None = Field(None, description="filter by life-area domain (banking, health, work…)"),
    importance: str | None = Field(None, description="filter: high | medium | low"),
) -> dict[str, Any]:
    """Messages that need an action from you, ranked by importance then urgency."""
    return _rows(
        "index_action_items",
        index_query.action_items(limit=limit, domain=domain, importance=importance),
    )


@mcp.tool()
def index_due_soon(limit: int = Field(50, description="max items")) -> dict[str, Any]:
    """Time-sensitive items, or items carrying a deadline."""
    return _rows("index_due_soon", index_query.due_soon(limit=limit))


@mcp.tool()
def index_waiting_on(
    who: str = Field("them", description="'them' = you're waiting on someone; 'me' = they're waiting on you"),
    limit: int = Field(50, description="max items"),
) -> dict[str, Any]:
    """Threads awaiting a reply."""
    return _rows("index_waiting_on", index_query.waiting_on(who=who, limit=limit))


@mcp.tool()
def index_by_domain(domain: str, limit: int = Field(50, description="max items")) -> dict[str, Any]:
    """Recent sanitized items in a life-area domain."""
    return _rows("index_by_domain", index_query.by_domain(domain, limit=limit))


@mcp.tool()
def index_summary(id: str) -> dict[str, Any]:
    """The sanitized summary + priority signal for one message id (0 or 1 rows)."""
    row = index_query.summary(id)
    return _rows("index_summary", [row] if row else [])


@mcp.tool()
def index_stats() -> dict[str, Any]:
    """Counts of sanitized items by domain and suggested disposition.

    One row per group: ``dimension`` (``domain`` or ``disposition``), ``key`` and
    ``count``; ``total`` is the item count. Both keys are schema enums.
    """
    s = index_query.stats()
    rows = [
        {"dimension": dimension, "key": key, "count": n}
        for dimension, groups in (("domain", s["by_domain"]), ("disposition", s["by_disposition"]))
        for key, n in groups.items()
    ]
    return wrap(
        rows, server=mcp.name, tool="index_stats", tier=TIER, untrusted_fields=(), total=s["total"]
    )


def run() -> None:
    """Serve the corpus-index MCP tools over streamable HTTP."""
    mcp.run(transport="streamable-http")
