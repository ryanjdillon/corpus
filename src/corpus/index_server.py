"""corpus-index MCP surface: sanitized, whitelisted tools for a downgraded consumer.

These are the tools a trust-downgraded consumer (Hermes, on a cloud model) may
call. Every tool is backed only by the sanitized ``messages`` table (the separate
``ai_sanitized`` database) read as ``corpus_index_ro`` — summaries + priority
signal, never a raw body or secret.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import index_query
from .config import settings
from .envelope import CURSOR_HELP, clamp, decode_cursor, encode_cursor, offset_of, wrap
from .sanitized_store import UNTRUSTED_COLS
from .telemetry import audited
from .tiers import tier

TIER = tier("sanitized").name
#: The most rows one call returns; a larger ``limit`` is clamped, and the caller
#: pages on with ``next_cursor``. Bounds what one call can read.
MAX_LIMIT = 100
#: The deepest a caller can page. Paging stops (next_cursor is null) here, which
#: bounds the rows an offset scan skips.
MAX_OFFSET = 10 * MAX_LIMIT

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


_audited = audited(server=mcp.name, tier=TIER)


def _rows(
    tool: str, rows: list[dict], *, total: int | None = None, next_cursor: str | None = None
) -> dict[str, Any]:
    return wrap(
        rows, server=mcp.name, tool=tool, tier=TIER, untrusted_fields=UNTRUSTED_COLS,
        total=total, next_cursor=next_cursor,
    )


def _page(
    tool: str, fetch: Callable[[int, int], list[dict]], limit: int, cursor: str | None, **args: Any
) -> dict[str, Any]:
    """One offset page of *tool*'s query: ``fetch(n, offset)`` returns up to n rows.

    One row beyond the page is fetched to learn whether another page exists, so
    ``next_cursor`` is ``None`` exactly on the last page.
    """
    offset = offset_of(decode_cursor(cursor, tool, args), MAX_OFFSET)
    n = clamp(limit, MAX_LIMIT)
    rows = fetch(n + 1, offset)
    more = len(rows) > n and offset + n < MAX_OFFSET
    return _rows(
        tool, rows[:n], next_cursor=encode_cursor(tool, args, offset + n) if more else None
    )


@mcp.tool()
@_audited
def index_action_items(
    limit: Annotated[int, Field(description="max items")] = 50,
    domain: Annotated[str | None, Field(description="filter by life-area domain (banking, health, work…)")] = None,
    importance: Annotated[str | None, Field(description="filter: high | medium | low")] = None,
    cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
) -> dict[str, Any]:
    """Messages that need an action from you, ranked by importance then urgency."""
    return _page(
        "index_action_items",
        lambda n, offset: index_query.action_items(
            limit=n, domain=domain, importance=importance, offset=offset
        ),
        limit, cursor, domain=domain, importance=importance,
    )


@mcp.tool()
@_audited
def index_due_soon(
    limit: Annotated[int, Field(description="max items")] = 50,
    cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
) -> dict[str, Any]:
    """Time-sensitive items, or items carrying a deadline."""
    return _page(
        "index_due_soon",
        lambda n, offset: index_query.due_soon(limit=n, offset=offset),
        limit, cursor,
    )


@mcp.tool()
@_audited
def index_waiting_on(
    who: Annotated[str, Field(description="'them' = you're waiting on someone; 'me' = they're waiting on you")] = "them",
    limit: Annotated[int, Field(description="max items")] = 50,
    cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
) -> dict[str, Any]:
    """Threads awaiting a reply."""
    return _page(
        "index_waiting_on",
        lambda n, offset: index_query.waiting_on(who=who, limit=n, offset=offset),
        limit, cursor, who=who,
    )


@mcp.tool()
@_audited
def index_by_domain(
    domain: str,
    limit: Annotated[int, Field(description="max items")] = 50,
    cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
) -> dict[str, Any]:
    """Recent sanitized items in a life-area domain."""
    return _page(
        "index_by_domain",
        lambda n, offset: index_query.by_domain(domain, limit=n, offset=offset),
        limit, cursor, domain=domain,
    )


@mcp.tool()
@_audited
def index_summary(id: str) -> dict[str, Any]:
    """The sanitized summary + priority signal for one message id (0 or 1 rows)."""
    row = index_query.summary(id)
    return _rows("index_summary", [row] if row else [])


@mcp.tool()
@_audited
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
