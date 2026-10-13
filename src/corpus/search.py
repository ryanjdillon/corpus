"""Semantic search over a DocumentStore: embed the query text, then rank by it.

The SQL, the documents table's JSONB layout and its row keys live in
``store.DocumentStore``; relative time windows live in ``query``. This layer adds
only what is not storage: embedding the query. The caller owns the store's
lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .embeddings import embed_query
from .store import DocumentStore


def semantic_search(
    documents: DocumentStore,
    query: str,
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
    *,
    embed: Callable[[str], list[float]] = embed_query,
) -> list[dict[str, Any]]:
    """Vector search with optional Haystack metadata filters; *embed* embeds *query*."""
    return documents.nearest(embed(query), top_k, filters)
