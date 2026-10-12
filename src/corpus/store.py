"""Store documents and embeddings, with helpers for the per-source sync cursor.

Documents and embeddings live in a Haystack PgvectorDocumentStore table whose
metadata is one JSONB ``meta`` column. :class:`DocumentStore` is the only code that
knows that layout: every read of the table, and the Haystack handle that writes
and vector-searches it, go through it. Small direct-SQL helpers track the
per-source sync cursor. The document table lives in a dedicated schema
(settings.db_schema); the DB role owns that schema, so Haystack can create and
manage its table there.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
from haystack import Document
from haystack.document_stores.types import DuplicatePolicy
from haystack.utils import Secret
from haystack_integrations.components.retrievers.pgvector import (
    PgvectorEmbeddingRetriever,
)
from haystack_integrations.document_stores.pgvector import PgvectorDocumentStore

from .config import settings
from .query import (
    SEMANTIC_DEPTH,
    Aggregate,
    Layout,
    Page,
    Position,
    QueryPolicy,
    QuerySpec,
    build_aggregate,
    build_query,
)
from .store_base import Store


def _dsn() -> str:
    return settings.database_url


#: How many neighbours one vector search can rank (pgvector's ``hnsw.ef_search``).
#: An HNSW index scan returns at most this many rows whatever the LIMIT, so it is
#: also the deepest a caller can page into search results. pgvector's default, 40,
#: would cut paging off silently.
HNSW_EF_SEARCH = SEMANTIC_DEPTH

# The keys of each query's rows, in column order. The MCP surface classifies each
# key as trusted or not (see ``envelope``), so a key is added here, never inline.
SEARCH_KEYS = ("id", "score", "subject", "from_addr", "sent_at", "label", "snippet")
QUERY_KEYS = ("id", "subject", "from_addr", "sent_at", "label")
GET_KEYS = ("id", "content", "meta")


def layout(table: str) -> Layout:
    """The query model's view of the documents table: each dimension's SQL over ``meta``.

    ``sent_at`` is an ISO-8601 string that keeps the email header's UTC offset,
    so ranges, months and the min/max of a group compare it cast to a
    ``timestamptz`` (a missing or null date casts to NULL and matches no range).
    """
    sent_at = "(meta->>'sent_at')::timestamptz"
    return Layout(
        table=table,
        columns=("id", "meta->>'subject'", "meta->>'from_addr'", "meta->>'sent_at'",
                 "meta->>'label'"),
        keys=QUERY_KEYS,
        filters={name: f"meta->>'{name}' = %s" for name in ("label", "account", "source")},
        ranges={"sent_at": sent_at},
        groups={
            "label": "meta->>'label'",
            "account": "meta->>'account'",
            # In UTC, so a month's boundaries do not move with the header's offset.
            "month": f"to_char({sent_at} AT TIME ZONE 'UTC', 'YYYY-MM')",
        },
        text=("meta->>'subject'", "content"),
        sent_at=sent_at,
        # The keyset compares the string, which any cursor value can bind to: two
        # dates that differ only in offset may order slightly apart from true
        # time, but every row is still paged exactly once. An undated row sorts
        # last both ways: '' below every date, 'infinity' (a letter) above every
        # digit.
        newest="coalesce(meta->>'sent_at', '')",
        oldest="coalesce(meta->>'sent_at', 'infinity')",
        embedding="embedding",
    )


class DocumentStore(Store):
    """The sensitive tier's documents table: raw content, JSONB ``meta``, embedding.

    Haystack creates the table on its first write, so this store declares no DDL
    and every reader treats a missing table as an empty corpus (or, for the
    point queries, lets the error surface as before). Reads run on the store's
    own connection. The Haystack handle, built on first
    use, owns a second connection for vector writes and search.
    """

    def __init__(
        self, dsn: str | None = None, schema: str | None = None, table: str | None = None
    ) -> None:
        """Open the sensitive DB; *dsn*, *schema* and *table* default to settings."""
        self._dsn = dsn or settings.database_url
        self._schema = schema or settings.db_schema
        self._name = table or settings.documents_table
        self._table = f"{self._schema}.{self._name}"
        self._haystack: PgvectorDocumentStore | None = None
        self._layout = layout(self._table)
        super().__init__(self._dsn)

    def schema_ddl(self) -> str:
        """Return no DDL: Haystack creates the documents table lazily."""
        return ""

    def close(self) -> None:
        """Close the store's connection and the Haystack handle's, if one was built."""
        if self._haystack is not None:
            self._haystack.close()
        super().close()

    def _vectors(self) -> PgvectorDocumentStore:
        """Return the Haystack handle on this table, building it on first use."""
        if self._haystack is None:
            self._haystack = PgvectorDocumentStore(
                connection_string=Secret.from_token(self._dsn),
                schema_name=self._schema,
                table_name=self._name,
                embedding_dimension=settings.embedding_dimensions,
                vector_function="cosine_similarity",
                search_strategy="hnsw",
                hnsw_ef_search=HNSW_EF_SEARCH,
                recreate_table=False,
            )
        return self._haystack

    def _present(self, *tables: str) -> bool:
        """Whether every one of *tables* exists yet."""
        return all(self._read("SELECT to_regclass(%s)", (t,))[0][0] is not None for t in tables)

    def _stream(self, name: str, sql: str, params: list, *tables: str) -> Iterator[tuple]:
        """Yield *sql*'s rows through server-side cursor *name*, if *tables* exist.

        A server-side cursor streams the result, so the whole archive need not be
        held in memory.
        """
        if not self._present(*tables):
            return
        with self._conn.transaction(), self._conn.cursor(name=name) as cur:
            cur.itersize = 500
            cur.execute(sql, params)
            yield from cur

    def exists(self) -> bool:
        """Whether the documents table exists (Haystack creates it on first write)."""
        return self._present(self._table)

    def write(self, documents: list[Document]) -> None:
        """Upsert *documents*: an id already stored is overwritten, not duplicated."""
        self._vectors().write_documents(documents, policy=DuplicatePolicy.OVERWRITE)

    def existing_ids(self, source: str) -> set[str]:
        """Return document ids already stored for a source.

        Lets ingestion resume after a partial run instead of re-embedding everything.
        """
        if not self.exists():
            return set()
        rows = self._read(f"SELECT id FROM {self._table} WHERE meta->>'source' = %s", (source,))
        return {row[0] for row in rows}

    def iter(
        self, source: str | None = None, account: str | None = None
    ) -> Iterator[tuple[str, str, dict]]:
        """Stream (id, content, meta) for stored documents, filtered by source/account."""
        clauses: list[str] = []
        params: list[str] = []
        if source:
            clauses.append("meta->>'source' = %s")
            params.append(source)
        if account:
            clauses.append("meta->>'account' = %s")
            params.append(account)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        # Newest-first and deterministic: recent mail is enriched/exported first, and a
        # resumed run covers documents in a stable order. sent_at is an ISO-8601 string,
        # so it sorts correctly as text; id breaks ties. (For a much larger archive, a
        # functional index on (meta->>'sent_at') would let this scan avoid the sort.)
        order = " ORDER BY meta->>'sent_at' DESC NULLS LAST, id"
        sql = f"SELECT id, content, meta FROM {self._table}{where}{order}"
        yield from self._stream("corpus_scan", sql, params, self._table)

    def iter_enriched(self, source: str | None = None) -> Iterator[tuple[str, dict, dict, Any]]:
        """Stream (id, meta, enrichment, enriched_at) for enriched documents, newest first.

        A source that has never stored a document has no tables yet (the vector
        store creates them on first insert), which reads as no documents rather
        than an error.
        """
        enrichments = f"{self._schema}.enrichments"
        where = "WHERE e.enrichment IS NOT NULL"
        params: list = []
        if source:
            where += " AND d.meta->>'source' = %s"
            params.append(source)
        sql = (
            f"SELECT d.id, d.meta, e.enrichment, e.enriched_at FROM {self._table} d "
            f"JOIN {enrichments} e ON e.doc_id = d.id {where} "
            "ORDER BY d.meta->>'sent_at' DESC NULLS LAST, d.id"
        )
        yield from self._stream("corpus_sync_scan", sql, params, self._table, enrichments)

    def query_spec(
        self, spec: QuerySpec, policy: QueryPolicy, position: Position | None = None
    ) -> Page:
        """One page of the documents *spec* matches (``QUERY_KEYS`` rows), within *policy*."""
        built = build_query(spec, policy, self._layout, position)
        if not self.exists():
            return Page([], None)
        with self._conn.transaction():
            if spec.semantic is not None:
                # Match the ranking depth the Haystack handle uses, so a vector
                # page reaches SEMANTIC_DEPTH, not pgvector's default of 40.
                with self._conn.cursor() as cur:
                    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")
            rows = self._read(built.sql, built.params)
        return built.page(rows, self._layout.keys)

    def aggregate(self, spec: QuerySpec, policy: QueryPolicy) -> Aggregate:
        """Count the documents *spec* matches per ``group_by`` group, within *policy*."""
        built = build_aggregate(spec, policy, self._layout)
        if not self.exists():
            return Aggregate([], 0, False)
        with self._conn.transaction():
            groups = self._read(built.sql, built.params)
            total = self._read(built.count_sql, built.count_params)[0][0]
        return built.result(groups, total)

    def get(self, id: str) -> dict[str, Any] | None:
        """Fetch one document by id (``GET_KEYS``: full content and all metadata), or None."""
        rows = self._read(f"SELECT id, content, meta FROM {self._table} WHERE id = %s", (id,))
        return dict(zip(GET_KEYS, rows[0], strict=True)) if rows else None

    def stats(self) -> dict[str, Any]:
        """Return the total document count and the per-label counts."""
        with self._conn.transaction():
            total = self._read(f"SELECT count(*) FROM {self._table}")[0][0]
            by_label = self._read(
                f"SELECT meta->>'label', count(*) FROM {self._table} GROUP BY 1 ORDER BY 2 DESC"
            )
        return {"total": total, "by_label": {row[0]: row[1] for row in by_label}}

    def nearest(
        self, embedding: list[float], top_k: int = 10, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Rank documents by vector similarity to *embedding* (``SEARCH_KEYS`` rows).

        *filters* are Haystack metadata filters. The snippet is the first 300
        characters of the content.
        """
        retriever = PgvectorEmbeddingRetriever(document_store=self._vectors())
        result = retriever.run(query_embedding=embedding, top_k=top_k, filters=filters)
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


def _strip_nul(value):
    """Strip NUL (\\u0000) from any string, recursing into lists and dicts.

    Postgres text/jsonb cannot store NUL, so no metadata value can break a write.
    """
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


def get_cursor(source: str) -> str | None:
    """Return the stored sync cursor for a source, or None if unset."""
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT cursor FROM {settings.db_schema}.sync_state WHERE source = %s",
            (source,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_cursor(source: str, cursor: str) -> None:
    """Upsert the sync cursor for a source."""
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
