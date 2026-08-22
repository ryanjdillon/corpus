"""Ingestion: fetch -> classify -> chunk -> embed -> upsert, per source."""

from __future__ import annotations

import logging
import time

from haystack.document_stores.types import DuplicatePolicy

from .classify import classify
from .config import settings
from .embeddings import Embedder, EmbedUnavailableError
from .fetchers import build_fetcher
from .models import Record
from .store import existing_ids, get_cursor, get_document_store, set_cursor, to_document
from .telemetry import documents_counter, embed_batch_size, embed_duration

log = logging.getLogger("corpus.ingest")

# Hard cap on the characters sent to the embedder. Word-count chunking does not
# bound a message with a giant unbroken string (base64/inline content becomes a
# single huge "word"), which the embedder rejects; this keeps every request well
# under the model's token limit. ~8000 chars is roughly 2k tokens.
_MAX_EMBED_CHARS = 8000

# Abort the run if this many records fail consecutively: a long unbroken run of
# failures means something systemic (e.g. the store is unreachable), not a few
# bad messages. The run is resumable from its persisted cursor.
_MAX_CONSECUTIVE_SKIPS = 25


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
    consecutive_skips = 0

    def _embed_and_store(records: list[Record]) -> None:
        # Embed the whole group in one call — far faster than per-message. First
        # chunk carries the document.
        nonlocal total
        texts = [_embed_text(r) for r in records]
        attrs = {"source": source}
        start = time.monotonic()
        vectors = embedder.embed(texts)
        embed_duration.record(time.monotonic() - start, attrs)
        embed_batch_size.record(len(records), attrs)
        docs = [to_document(r, classify(r), v) for r, v in zip(records, vectors)]
        store.write_documents(docs, policy=DuplicatePolicy.OVERWRITE)
        total += len(docs)
        documents_counter.add(len(docs), {"source": source, "outcome": "written"})
        log.info("ingest %s: wrote %d (%d total)", source, len(docs), total)

    def _process(records: list[Record]) -> None:
        """Embed + store a group. On a record-level error (bad input, unparseable
        content, a value the store rejects), bisect to isolate and skip only the
        offending record(s) so one bad message can't abort the whole backfill. A
        genuinely unavailable embedder aborts immediately; too many consecutive
        skips (another systemic failure) also aborts. The run is resumable."""
        nonlocal skipped, consecutive_skips
        if not records:
            return
        try:
            _embed_and_store(records)
            consecutive_skips = 0
        except EmbedUnavailableError:
            raise  # systemic, not a bad record — abort and resume later
        except Exception as exc:
            if len(records) > 1:
                mid = len(records) // 2
                _process(records[:mid])
                _process(records[mid:])
                return
            skipped += 1
            consecutive_skips += 1
            documents_counter.add(1, {"source": source, "outcome": "skipped"})
            log.warning(
                "ingest %s: skipping record %s (skipped=%d): %s",
                source, records[0].key(), skipped, exc,
            )
            if consecutive_skips >= _MAX_CONSECUTIVE_SKIPS:
                raise RuntimeError(
                    f"aborting: {consecutive_skips} records failed consecutively "
                    "(store or embedder likely unavailable)"
                ) from exc

    def flush() -> None:
        if not batch:
            return
        records = list(batch)
        batch.clear()
        _process(records)
        # Persist progress mid-run so an interrupted backfill resumes near where
        # it stopped, for fetchers that checkpoint incrementally (e.g. Gmail).
        cursor = fetcher.next_cursor()
        if cursor:
            set_cursor(source, cursor)

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
