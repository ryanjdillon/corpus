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
        {
            "id": d.id,
            "score": d.score,
            "subject": d.meta.get("subject"),
            "from_addr": d.meta.get("from_addr"),
            "sent_at": d.meta.get("sent_at"),
            "label": d.meta.get("label"),
            "snippet": (d.content or "")[:300],
        }
        for d in result["documents"]
    ]


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _relative_cutoff(since: str) -> str:
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
) -> list[dict[str, Any]]:
    """Query metadata analytically (non-semantic), newest first.

    E.g. 'all promotional older than 2 weeks', or the last day's mail with
    since='1d'. Returns every match up to `limit`. `since` is a relative window
    (now - since), applied as the lower bound when `after` is not given.
    """
    if since and not after:
        after = _relative_cutoff(since)
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
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    table = f"{settings.db_schema}.{settings.documents_table}"
    sql = (
        f"SELECT id, meta->>'subject', meta->>'from_addr', meta->>'sent_at', "
        f"meta->>'label' FROM {table}{where} "
        f"ORDER BY meta->>'sent_at' DESC LIMIT %s"
    )
    params.append(limit)
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {"id": r[0], "subject": r[1], "from_addr": r[2], "sent_at": r[3], "label": r[4]}
        for r in rows
    ]


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
    return {"id": row[0], "content": row[1], "meta": row[2]}


def stats() -> dict[str, Any]:
    """Return the total document count and the per-label counts."""
    table = f"{settings.db_schema}.{settings.documents_table}"
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        total = cur.fetchone()[0]
        cur.execute(f"SELECT meta->>'label', count(*) FROM {table} GROUP BY 1 ORDER BY 2 DESC")
        by_label = {row[0]: row[1] for row in cur.fetchall()}
    return {"total": total, "by_label": by_label}
