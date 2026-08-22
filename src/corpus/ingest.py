"""Ingestion: fetch -> classify -> chunk -> embed -> upsert, per source."""

from __future__ import annotations

import logging

from haystack.document_stores.types import DuplicatePolicy

from .classify import classify
from .config import settings
from .embeddings import Embedder
from .fetchers import build_fetcher
from .models import Record
from .store import get_cursor, get_document_store, set_cursor, to_document

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
    log.info("ingest %s from cursor=%s", source, cursor)

    batch: list[Record] = []
    total = 0

    def flush() -> None:
        nonlocal total
        if not batch:
            return
        docs = []
        for rec in batch:
            cls = classify(rec)
            # First chunk carries the document; long bodies index the head.
            head = _chunk(rec.body_text)[0]
            emb = embedder.embed_one(f"{rec.subject or ''}\n\n{head}")
            docs.append(to_document(rec, cls, emb))
        store.write_documents(docs, policy=DuplicatePolicy.OVERWRITE)
        total += len(docs)
        batch.clear()

    try:
        for rec in fetcher.fetch(cursor):
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
