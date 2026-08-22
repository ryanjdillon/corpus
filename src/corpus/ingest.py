"""Ingestion: fetch -> classify -> chunk -> embed -> upsert, per source."""

from __future__ import annotations

import logging

from haystack.document_stores.types import DuplicatePolicy

from .classify import classify
from .config import settings
from .embeddings import Embedder, EmbedInputError
from .fetchers import build_fetcher
from .models import Record
from .store import existing_ids, get_cursor, get_document_store, set_cursor, to_document

log = logging.getLogger("corpus.ingest")

# Hard cap on the characters sent to the embedder. Word-count chunking does not
# bound a message with a giant unbroken string (base64/inline content becomes a
# single huge "word"), which the embedder rejects; this keeps every request well
# under the model's token limit. ~8000 chars is roughly 2k tokens.
_MAX_EMBED_CHARS = 8000

# Errors that mean "this specific record is bad" — skip it, don't abort the run.
# Infra failures (5xx/timeout after retries, DB errors) are not listed, so they
# propagate and fail the run, to be retried later from where it left off.
_SKIPPABLE = (EmbedInputError, ValueError, UnicodeError)


def _chunk(text: str) -> list[str]:
    """Naive whitespace chunking; good enough before a tokenizer is wired in."""
    words = text.split()
    if not words:
        return [""]
    size = settings.chunk_tokens
    overlap = settings.chunk_overlap
    step = max(1, size - overlap)
    return [" ".join(words[i : i + size]) for i in range(0, len(words), step)]


def _embed_text(record: Record) -> str:
    """The text embedded for a record: subject + first chunk, length-capped."""
    return f"{record.subject or ''}\n\n{_chunk(record.body_text)[0]}"[:_MAX_EMBED_CHARS]


def ingest(source: str, batch_size: int = 50) -> int:
    fetcher = build_fetcher(source)
    store = get_document_store()
    embedder = Embedder()
    cursor = get_cursor(source)
    seen = existing_ids(source)
    log.info("ingest %s from cursor=%s (%d already stored)", source, cursor, len(seen))

    batch: list[Record] = []
    total = 0
    skipped = 0

    def _embed_and_store(records: list[Record]) -> None:
        # Embed the whole group in one call — far faster than per-message. First
        # chunk carries the document.
        nonlocal total
        texts = [_embed_text(r) for r in records]
        vectors = embedder.embed(texts)
        docs = [to_document(r, classify(r), v) for r, v in zip(records, vectors)]
        store.write_documents(docs, policy=DuplicatePolicy.OVERWRITE)
        total += len(docs)
        log.info("ingest %s: wrote %d (%d total)", source, len(docs), total)

    def _process(records: list[Record]) -> None:
        """Embed + store a group. On a record-level error, bisect to isolate and
        skip only the offending record(s) so one bad message can't abort the
        whole backfill. Infra errors propagate and fail the run (resumable)."""
        nonlocal skipped
        if not records:
            return
        try:
            _embed_and_store(records)
        except _SKIPPABLE as exc:
            if len(records) > 1:
                mid = len(records) // 2
                _process(records[:mid])
                _process(records[mid:])
            else:
                skipped += 1
                log.warning(
                    "ingest %s: skipping record %s (skipped=%d): %s",
                    source, records[0].key(), skipped, exc,
                )

    def flush() -> None:
        if not batch:
            return
        records = list(batch)
        batch.clear()
        _process(records)

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
        log.info(
            "ingest %s complete: %d documents, %d skipped, cursor=%s",
            source, total, skipped, new_cursor,
        )
    finally:
        embedder.close()
    return total
