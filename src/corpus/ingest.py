"""Ingestion: fetch -> classify -> chunk -> embed -> upsert, per source."""

from __future__ import annotations

import logging

from haystack.document_stores.types import DuplicatePolicy

from .classify import classify
from .config import settings
from .embeddings import Embedder
from .fetchers import build_fetcher
from .models import Record
from .store import existing_ids, get_cursor, get_document_store, set_cursor, to_document

log = logging.getLogger("corpus.ingest")


def _chunk(text: str) -> list[str]:
    """Naive whitespace chunking; good enough before a tokenizer is wired in."""
    words = text.split()
    if not words:
        return [""]
    size = settings.chunk_tokens
    overlap = settings.chunk_overlap
    step = max(1, size - overlap)
    return [" ".join(words[i : i + size]) for i in range(0, len(words), step)]


def ingest(source: str, batch_size: int = 50) -> int:
    fetcher = build_fetcher(source)
    store = get_document_store()
    embedder = Embedder()
    cursor = get_cursor(source)
    seen = existing_ids(source)
    log.info("ingest %s from cursor=%s (%d already stored)", source, cursor, len(seen))

    batch: list[Record] = []
    total = 0

    def flush() -> None:
        nonlocal total
        if not batch:
            return
        # Embed the whole batch in one call — far faster than per-message and
        # kinder to a CPU embedder. First chunk carries the document.
        texts = [f"{r.subject or ''}\n\n{_chunk(r.body_text)[0]}" for r in batch]
        vectors = embedder.embed(texts)
        docs = [to_document(r, classify(r), v) for r, v in zip(batch, vectors)]
        store.write_documents(docs, policy=DuplicatePolicy.OVERWRITE)
        total += len(docs)
        log.info("ingest %s: wrote %d (%d total)", source, len(docs), total)
        batch.clear()

    try:
        for rec in fetcher.fetch(cursor):
            if rec.key() in seen:
                continue  # already stored (resume a partial backfill)
            batch.append(rec)
            if len(batch) >= batch_size:
                flush()
        flush()
        new_cursor = fetcher.next_cursor()
        if new_cursor:
            set_cursor(source, new_cursor)
        log.info("ingest %s complete: %d documents, cursor=%s", source, total, new_cursor)
    finally:
        embedder.close()
    return total
