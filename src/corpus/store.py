"""Storage: a Haystack PgvectorDocumentStore for the documents/embeddings, plus
small direct-SQL helpers for the per-source sync cursor.

The document table lives in a dedicated schema (settings.db_schema); the DB role
owns that schema, so Haystack can create and manage its table there.
"""

from __future__ import annotations

import psycopg
from haystack import Document
from haystack.utils import Secret
from haystack_integrations.document_stores.pgvector import PgvectorDocumentStore

from .config import settings


def _dsn() -> str:
    return settings.database_url


def get_document_store() -> PgvectorDocumentStore:
    return PgvectorDocumentStore(
        connection_string=Secret.from_token(_dsn()),
        schema_name=settings.db_schema,
        table_name=settings.documents_table,
        embedding_dimension=settings.embedding_dimensions,
        vector_function="cosine_similarity",
        search_strategy="hnsw",
        recreate_table=False,
    )


def _strip_nul(value):
    """Postgres text/jsonb cannot store NUL (\\u0000); strip it from any string,
    recursing into lists and dicts so no metadata value can break a write."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_nul(v) for k, v in value.items()}
    return value


def to_document(record, classification, embedding) -> Document:
    """Map a normalized record + classification into a Haystack Document."""
    meta = {
        "source": record.source,
        "source_uid": record.source_uid,
        "kind": record.kind,
        "account": record.account,
        "folder": record.folder,
        "thread_id": record.thread_id,
        "from_addr": record.from_addr,
        "to_addrs": record.to_addrs,
        "labels": record.labels,
        "subject": record.subject,
        "sent_at": record.sent_at.isoformat() if record.sent_at else None,
        "uri": record.uri,
        "label": classification.label,
        "label_confidence": classification.confidence,
        "signals": classification.signals,
    }
    return Document(
        id=record.key(),
        content=_strip_nul(record.body_text),
        meta={k: _strip_nul(v) for k, v in meta.items() if v is not None},
        embedding=embedding,
    )


# --- sync cursor (bespoke sync_state table, created out-of-band) ---


def existing_ids(source: str) -> set[str]:
    """Document ids already stored for a source, so ingestion can resume after a
    partial run instead of re-embedding everything."""
    table = f"{settings.db_schema}.{settings.documents_table}"
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            return set()
        cur.execute(f"SELECT id FROM {table} WHERE meta->>'source' = %s", (source,))
        return {row[0] for row in cur.fetchall()}


def get_cursor(source: str) -> str | None:
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT cursor FROM {settings.db_schema}.sync_state WHERE source = %s",
            (source,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_cursor(source: str, cursor: str) -> None:
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {settings.db_schema}.sync_state (source, cursor, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (source) DO UPDATE
              SET cursor = EXCLUDED.cursor, updated_at = now()
            """,
            (source, cursor),
        )
        conn.commit()
