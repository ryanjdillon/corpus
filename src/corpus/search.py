"""Query layer: semantic search, structured metadata queries, and stats."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from haystack_integrations.components.retrievers.pgvector import (
    PgvectorEmbeddingRetriever,
)

from .config import settings
from .embeddings import Embedder
from .store import get_document_store

# The keys of each query's rows, in column order. The MCP surface classifies each
# key as trusted or not (see ``envelope``), so a key is added here, never inline.
SEARCH_KEYS = ("id", "score", "subject", "from_addr", "sent_at", "label", "snippet")
QUERY_KEYS = ("id", "subject", "from_addr", "sent_at", "label")
GET_KEYS = ("id", "content", "meta")


def semantic_search(
    query: str, top_k: int = 10, filters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Vector search with optional Haystack metadata filters."""
    store = get_document_store()
    embedder = Embedder()
    try:
        qvec = embedder.embed_one(query)
    finally:
        embedder.close()
    retriever = PgvectorEmbeddingRetriever(document_store=store)
    result = retriever.run(query_embedding=qvec, top_k=top_k, filters=filters)
    return [
        dict(
            zip(
                SEARCH_KEYS,
                (
                    d.id,
                    d.score,
                    d.meta.get("subject"),
                    d.meta.get("from_addr"),
                    d.meta.get("sent_at"),
                    d.meta.get("label"),
                    (d.content or "")[:300],
                ),
                strict=True,
            )
        )
        for d in result["documents"]
    ]


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def relative_cutoff(since: str) -> str:
    """Convert a relative window like '24h', '7d', '30m' into an ISO cutoff timestamp.

    Returns now - window, so callers need not know the current time. Units: s,
    m (minutes), h, d, w.
    """
    match = _DURATION_RE.match(since.lower())
    if not match:
        raise ValueError(f"invalid duration {since!r}; use e.g. '24h', '7d', '30m'")
    amount, unit = int(match.group(1)), match.group(2)
    cutoff = datetime.now(UTC) - timedelta(seconds=amount * _UNIT_SECONDS[unit])
    return cutoff.isoformat()


def structured_query(
    label: str | None = None,
    account: str | None = None,
    before: str | None = None,
    after: str | None = None,
    since: str | None = None,
    limit: int = 500,
    older_than: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Query metadata analytically (non-semantic), newest first.

    E.g. 'all promotional older than 2 weeks', or the last day's mail with
    since='1d'. Returns every match up to `limit`. `since` is a relative window
    (now - since), applied as the lower bound when `after` is not given.

    Rows are ordered by ``(sent_at, id)`` descending, a total order, so
    ``older_than`` (the last row's ``(sent_at, id)``) resumes after it: a keyset
    page, which mail arriving between pages cannot shift.
    """
    if since and not after:
        after = relative_cutoff(since)
    clauses = []
    params: list[Any] = []
    if label:
        clauses.append("meta->>'label' = %s")
        params.append(label)
    if account:
        clauses.append("meta->>'account' = %s")
        params.append(account)
    if before:
        clauses.append("meta->>'sent_at' < %s")
        params.append(before)
    if after:
        clauses.append("meta->>'sent_at' >= %s")
        params.append(after)
    if older_than:
        clauses.append("(coalesce(meta->>'sent_at', ''), id) < (%s, %s)")
        params += list(older_than)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    table = f"{settings.db_schema}.{settings.documents_table}"
    sql = (
        f"SELECT id, meta->>'subject', meta->>'from_addr', meta->>'sent_at', "
        f"meta->>'label' FROM {table}{where} "
        # coalesce: a row without a date sorts last and still compares in the
        # keyset (a NULL would compare as unknown and drop out of later pages).
        f"ORDER BY coalesce(meta->>'sent_at', '') DESC, id DESC LIMIT %s"
    )
    params.append(limit)
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [dict(zip(QUERY_KEYS, r, strict=True)) for r in rows]


def get_document(id: str) -> dict[str, Any] | None:
    """Fetch one stored document by id: full body content plus all metadata.

    Companion to `semantic_search`, which returns only a 300-char snippet — a
    caller can escalate to the complete record here when a snippet is too thin.
    Returns None if no document has that id.
    """
    table = f"{settings.db_schema}.{settings.documents_table}"
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id, content, meta FROM {table} WHERE id = %s", (id,))
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(GET_KEYS, row, strict=True))


def stats() -> dict[str, Any]:
    """Return the total document count and the per-label counts."""
    table = f"{settings.db_schema}.{settings.documents_table}"
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        total = cur.fetchone()[0]
        cur.execute(f"SELECT meta->>'label', count(*) FROM {table} GROUP BY 1 ORDER BY 2 DESC")
        by_label = {row[0]: row[1] for row in cur.fetchall()}
    return {"total": total, "by_label": by_label}
