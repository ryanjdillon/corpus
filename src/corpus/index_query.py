"""corpus-index: the sanitized query surface.

The trust gate for a cloud-model consumer (Hermes). It reads only the sanitized
``messages`` table in the separate ``ai_sanitized`` database — cloud-safe fields
plus the enrichment priority signal, never raw content, subject, sender, or secret
material — as the restricted ``corpus_index_ro`` role. That table is written by the
one-way sync (``corpus sync`` → ``SanitizedStore``); nothing here writes.

Queries connect via ``CORPUS_INDEX_DATABASE_URL`` (the sanitized DB, read-only
role). ``COLS`` from ``sanitized_store`` is the single projection contract, so the
returned columns track the tier's schema and never include the embedding.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from .config import settings
from .sanitized_store import COLS

# The safe columns to return: the sanitized projection, never the embedding.
_SELECT = ", ".join(COLS)

Connect = Callable[..., psycopg.Connection]


def _table() -> str:
    return f"{settings.db_schema}.messages"


def _rows(sql: str, params: list, *, connect: Connect = psycopg.connect) -> list[dict]:
    """Run a query as the restricted index role; rows come back as dicts."""
    if not settings.index_database_url:
        raise RuntimeError("CORPUS_INDEX_DATABASE_URL not set (corpus-index disabled)")
    with (
        connect(settings.index_database_url, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        return cur.fetchall()


# Priority ordering: importance, then urgency, then recency. Constant SQL (no input).
_ORDER = (
    "CASE importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
    "time_sensitive DESC NULLS LAST, sent_at DESC NULLS LAST"
)

Rows = Callable[[str, list], list[dict]]


def action_items(
    limit: int = 50, domain: str | None = None, importance: str | None = None, *, rows: Rows = _rows
) -> list[dict]:
    """Items that need an action from the owner, ranked by importance + urgency."""
    clauses, params = ["requires_action = true"], []
    if domain:
        clauses.append("domain = %s")
        params.append(domain)
    if importance:
        clauses.append("importance = %s")
        params.append(importance)
    params.append(limit)
    where = " AND ".join(clauses)
    return rows(f"SELECT {_SELECT} FROM {_table()} WHERE {where} ORDER BY {_ORDER} LIMIT %s", params)


def due_soon(limit: int = 50, *, rows: Rows = _rows) -> list[dict]:
    """Time-sensitive items, or items carrying a deadline."""
    return rows(
        f"SELECT {_SELECT} FROM {_table()} WHERE time_sensitive = true OR deadline IS NOT NULL "
        f"ORDER BY deadline NULLS LAST, {_ORDER} LIMIT %s",
        [limit],
    )


def waiting_on(who: str = "them", limit: int = 50, *, rows: Rows = _rows) -> list[dict]:
    """Items flagged as awaiting a reply.

    'them' = owner is waiting on someone, 'me' = someone is waiting on the owner.
    """
    return rows(
        f"SELECT {_SELECT} FROM {_table()} WHERE waiting_on = %s ORDER BY {_ORDER} LIMIT %s",
        [who, limit],
    )


def by_domain(domain: str, limit: int = 50, *, rows: Rows = _rows) -> list[dict]:
    """Recent sanitized items in a life-area domain (banking, health, work…)."""
    return rows(
        f"SELECT {_SELECT} FROM {_table()} WHERE domain = %s ORDER BY {_ORDER} LIMIT %s",
        [domain, limit],
    )


def summary(doc_id: str, *, rows: Rows = _rows) -> dict | None:
    """The sanitized summary + priority signal for one item id."""
    got = rows(f"SELECT {_SELECT} FROM {_table()} WHERE id = %s", [doc_id])
    return got[0] if got else None


def stats(*, rows: Rows = _rows) -> dict:
    """Counts of sanitized items by domain and suggested disposition."""
    total = rows(f"SELECT count(*) AS total FROM {_table()}", [])[0]["total"]
    by_dom = {r["domain"]: r["n"] for r in rows(f"SELECT domain, count(*) AS n FROM {_table()} GROUP BY 1", [])}
    by_disp = {
        r["d"]: r["n"]
        for r in rows(f"SELECT suggested_disposition AS d, count(*) AS n FROM {_table()} GROUP BY 1", [])
    }
    return {"total": total, "by_domain": by_dom, "by_disposition": by_disp}
