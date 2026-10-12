"""Query layer: semantic search and relative time windows over a DocumentStore.

The SQL and the documents table's JSONB layout live in ``store.DocumentStore``;
this layer adds what is not storage: embedding the query text, and turning a
relative window ('24h') into a cutoff. The caller owns the store's lifecycle.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .embeddings import Embedder
from .store import GET_KEYS, QUERY_KEYS, SEARCH_KEYS, DocumentStore

__all__ = ["GET_KEYS", "QUERY_KEYS", "SEARCH_KEYS", "relative_cutoff", "semantic_search"]


def semantic_search(
    documents: DocumentStore,
    query: str,
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Vector search with optional Haystack metadata filters."""
    embedder = Embedder()
    try:
        qvec = embedder.embed_one(query)
    finally:
        embedder.close()
    return documents.nearest(qvec, top_k, filters)


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
