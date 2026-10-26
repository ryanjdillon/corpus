# Database

corpus stores documents in a pgvector-backed table inside `CORPUS_DB_SCHEMA`. The
pgvector document store creates and manages that table, so the writer role needs
`CREATE` on the schema (owning it is one way, not a requirement); the role that
creates the table then owns the table.

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

## When a document arrived

corpus adds one column of its own to that table: `ingested_at timestamptz NOT
NULL DEFAULT now()`, when the document was first stored. The enrichment backlog
reads its oldest (see [Enrichment backlog](observability.md#enrichment-backlog)).
It is added outside the pgvector document store because that store knows only
its own columns: its insert names them,

```sql
INSERT INTO {schema}.{table} (id, embedding, content, blob_data, blob_meta, blob_mime_type, meta)
VALUES (...)
ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, ..., meta = EXCLUDED.meta
```

and its upsert sets only them. So the default fills `ingested_at` on a
document's first insert, and re-ingesting it (an overwrite) keeps the first
time. Anything that read the table through the pgvector store's own
`SELECT *` (only `DocumentStore.nearest` does) also sees the column, as an
`ingested_at` key in each document's meta; `nearest` returns named keys only.
That needs `haystack-ai` 3.1 or later, the floor in `pyproject.toml`: 3.0's
`Document.from_dict` raises on a column it does not know, where 3.1 merges it
into meta.

A writable `DocumentStore` adds the column when it opens, or, if the table does
not exist yet, after the write that creates it:

- **The table's owner migrates it.** `ALTER TABLE` needs ownership of the
  table, not of the schema: it works because the role that created the table
  through the pgvector document store owns it, even when another role owns the
  schema and has only granted `CREATE` on it. The migration is additive and
  names one fully qualified table, so each per-domain table (see
  `GOVERNANCE.md`) migrates only itself, the first time its own writer opens it.
- **Catalog first.** It looks the column up in `information_schema.columns` and
  alters the table only when it is missing. `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS` takes the table's ACCESS EXCLUSIVE lock even when it adds nothing, so
  running it on every open would queue each run behind any long reader, and
  every new reader behind it.
- **No rewrite.** `now()` is stable, not volatile, so Postgres (11 and later)
  adds the column without rewriting the table: every row already stored reads
  the time of the `ALTER`, which also satisfies `NOT NULL`. Those rows did not
  arrive then; for them the column is a floor, not their true arrival.
- **Bounded wait.** The `ALTER` runs in its own transaction with `SET LOCAL
  lock_timeout = '5s'` (`store.INGESTED_AT_LOCK_TIMEOUT`). A queued ACCESS
  EXCLUSIVE request blocks every reader that comes after it, the MCP server's
  included, so waiting out a long reader (a backfill's open cursor, say) would
  stall them all. On a lock timeout, or when the role does not own the table,
  the store logs a warning and works without the column; the next writable
  open tries again. Any other error fails the open.
- **Races.** Two stores that both find the column missing both issue the
  `ALTER`; the second waits for the first, then finds the column and adds
  nothing.
- **Read-only stores run no migration.** `corpus serve`, `scan`, `export` and
  the source side of `sync` open the documents table read-only
  (`DocumentStore(read_only=True)`), `serve` as its reader role when the tier
  has one, so a role that may not alter the table can still read it. (The
  pgvector document store may still create its table and index on first use
  when they are absent, as it always has.)
- **Without the column.** A store that finds no `ingested_at` (a read-only
  store opened before any writer migrated the table, or a writer whose
  migration gave up) still counts the enrichment backlog, with no oldest time.
  It checks once per store, so it sees a later migration only when reopened.

The column is a property of the table, not of one deployment: a per-domain
instance or database gets it the same way, the first time a writer opens it.

Every store's reads (`Store._read`) run in a transaction that ends with the read,
so a long-lived store never sits idle in a transaction holding a snapshot; reads
that must agree (a count and its groups) share one explicit transaction. The
streaming readers (`iter`, `iter_enriched`) hold a server-side cursor inside a
transaction until the generator finishes, so a caller that may stop early wraps
the generator in `contextlib.closing` rather than leaving it to garbage
collection.

## Other tables

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
