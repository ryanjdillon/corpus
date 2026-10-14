# Database

corpus stores documents in a pgvector-backed table inside `CORPUS_DB_SCHEMA`. The
pgvector document store creates and manages that table, so the DB role must own
the schema.

That table keeps each document's metadata in one JSONB `meta` column, so a query
on it has to know the layout: `meta->>'source'`, and `meta->>'sent_at'` as an
ISO-8601 string that sorts correctly as text. `DocumentStore` (`store.py`) is the
only code that knows it. Every read of the table (the structured and keyset-paged
query, the [query model](query.md)'s pages and group counts, a fetch by id, stats,
streaming for enrichment, export, the secret scan and the sanitized sync) and the
Haystack handle that writes and vector-searches it go through that class; no
other module holds SQL against the table. Because the table appears only on the
first write, `DocumentStore` declares no DDL, and its streaming readers treat a
missing table as an empty corpus. A caller opens the store as a context manager
and owns its lifecycle.

Every store's reads (`Store._read`) run in a transaction that ends with the read,
so a long-lived store never sits idle in a transaction holding a snapshot; reads
that must agree (a count and its groups) share one explicit transaction. The
streaming readers (`iter`, `iter_enriched`) hold a server-side cursor inside a
transaction until the generator finishes, so a caller that may stop early wraps
the generator in `contextlib.closing` rather than leaving it to garbage
collection.

A small `sync_state` table (source, cursor, updated_at) tracks per-source
progress. The cursor is opaque — each fetcher encodes whatever it needs to resume
incrementally (see [Sources](fetchers/index.md)). Identity is `(source,
source_uid)`, so re-ingesting an item overwrites its row rather than adding a
duplicate.

The derived enrichment and secret-audit records live in a separate `enrichments`
table, created lazily on first write. Rows are deleted only by
`corpus purge-excluded-folders` (see [IMAP](fetchers/imap.md#purging-mail-from-excluded-folders)):
`DocumentStore.delete` removes a document and its `enrichments` row in one
transaction, and `SanitizedStore.delete` its projected row. The [architecture diagram](architecture.html)
shows how the derived branches relate to the source-of-truth documents.
