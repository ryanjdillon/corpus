# Database

corpus stores documents in a pgvector-backed table inside `CORPUS_DB_SCHEMA`. The
pgvector document store creates and manages that table, so the DB role must own
the schema.

A small `sync_state` table (source, cursor, updated_at) tracks per-source
progress. The cursor is opaque — each fetcher encodes whatever it needs to resume
incrementally (see [Sources](fetchers/index.md)). Identity is `(source,
source_uid)`, so re-ingesting a message overwrites its row rather than adding a
duplicate.

The derived enrichment and secret-audit records live in a separate `enrichments`
table, created lazily on first write. The [Architecture](architecture.md) page
covers how the derived branches relate to the source-of-truth documents.
