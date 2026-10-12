# Database

corpus stores documents in a pgvector-backed table inside `CORPUS_DB_SCHEMA`. The
pgvector document store creates and manages that table, so the DB role must own
the schema.

That table keeps each document's metadata in one JSONB `meta` column, so a query
on it has to know the layout: `meta->>'source'`, and `meta->>'sent_at'` as an
ISO-8601 string that sorts correctly as text. `DocumentStore` (`store.py`) is the
only code that knows it. Every read of the table (the structured and keyset-paged
query, a fetch by id, stats, streaming for enrichment, export, the secret scan and
the sanitized sync) and the Haystack handle that writes and vector-searches it go
through that class; no other module holds SQL against the table. Because the
table appears only on the first write, `DocumentStore` declares no DDL, and its
streaming readers treat a missing table as an empty corpus. A caller opens the
store as a context manager and owns its lifecycle.

A small `sync_state` table (source, cursor, updated_at) tracks per-source
progress. The cursor is opaque — each fetcher encodes whatever it needs to resume
incrementally (see [Sources](fetchers/index.md)). Identity is `(source,
source_uid)`, so re-ingesting an item overwrites its row rather than adding a
duplicate.

The derived enrichment and secret-audit records live in a separate `enrichments`
table, created lazily on first write. The [architecture diagram](architecture.html)
shows how the derived branches relate to the source-of-truth documents.
