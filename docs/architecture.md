# Architecture

corpus turns content from arbitrary sources into a searchable store of vectors +
metadata. The pipeline is a straight line of **deep modules** — each has a small
interface and hides its machinery — so a change to one (a new source, a different
embedder, another store backend) doesn't ripple through the others.

```
source → Record → classify → embed → Document → store
                                                   ↑
                              search / query ──────┘   (API · MCP)
```

## The core type

Everything flows as a `Record` (`models.py`): a normalized item (email today,
any document in principle) with a stable `(source, source_uid)` identity,
optional metadata (subject, sender, timestamps, labels, folder…), and body text.
Fetchers produce Records; the rest of the pipeline only knows Records, never
where they came from.

## Modules

Each module below is meant to be used through the one entry point named; the rest
is implementation.

| Module | Interface | Hides |
|---|---|---|
| `fetchers` | `build_fetcher(source) -> Fetcher`; `Fetcher.fetch(cursor)` yields Records | Per-source protocols (IMAP, Gmail API), auth, pagination, incremental cursors. See [extending-fetchers.md](extending-fetchers.md). |
| `classify` | `classify(record) -> Classification` | Header/rule heuristics (and an optional model tie-breaker) that assign a data-class label + confidence. |
| `embeddings` | `Embedder.embed(texts) -> vectors` | The OpenAI-compatible HTTP call, retries, and the typed `EmbedInputError` for rejected inputs. |
| `store` | `get_document_store()`, `to_document(...)`, cursor helpers | The pgvector document store and the `sync_state` cursor table. |
| `ingest` | `ingest(source, batch_size) -> count` | Orchestration: fetch → skip-already-stored → batch → embed → write, with per-record resilience (see below). |
| `search` | `semantic_search(...)`, `structured_query(...)`, `stats()` | Vector similarity and analytical SQL over the store. |
| `api` / `mcp_server` | REST endpoints / MCP tools | Thin adapters over `search`. |
| `telemetry` | `configure(name)`, `instrument_fastapi(app)`, `shutdown()` | OpenTelemetry setup and metric instruments (see [Observability](#observability)). |
| `config` | `settings` | Environment parsing (all vars are `CORPUS_`-prefixed). |

## Ingestion contract

`ingest(source, batch_size)`:

- **Resumable.** Records whose id is already stored are skipped, so a re-run
  continues a partial backfill rather than restarting it.
- **Batched.** Records are embedded a batch at a time (one embed request per
  batch) and written together.
- **Resilient.** A batch that the embedder rejects is **bisected** to isolate and
  skip only the offending record(s); the rest are still stored. Infrastructure
  failures (timeouts, 5xx after retries, store errors) propagate and fail the run
  instead — so a transient outage is retried later, while one bad item never
  aborts a large backfill. Embed inputs are length-capped to bound pathological
  content (e.g. a huge unbroken string).

## Embedding endpoint

Any endpoint speaking the OpenAI embeddings API works, selected purely by
configuration (`CORPUS_OPENAI_API_BASE`, `CORPUS_OPENAI_API_KEY`,
`CORPUS_EMBEDDING_MODEL`, `CORPUS_EMBEDDING_DIMENSIONS`). A locally hosted model
keeps content on your network; a hosted provider trades that for their models.
The pipeline is identical either way.

## Storage

Documents live in a pgvector-backed table inside `CORPUS_DB_SCHEMA` (the DB role
owns the schema). Per-source progress is tracked in a small `sync_state` table.
Identity is `(source, source_uid)`, so re-ingesting overwrites rather than
duplicates.

## Observability

`telemetry` is a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. When it is,
each entrypoint exports over OTLP:

- **Traces** — FastAPI request spans and outgoing httpx spans (embedding calls,
  source APIs).
- **Metrics** — ingest counters/histograms (`corpus.ingest.documents{source,
  outcome}`, `corpus.ingest.embed.duration`, `corpus.ingest.embed.batch_size`),
  an observable corpus-size gauge (`corpus.documents.count` by data-class), and
  FastAPI HTTP server metrics.

Heavy SDK imports are deferred to `configure()`, and `shutdown()` flushes the
exporters — important for short-lived processes (a one-shot ingest run) whose
metrics would otherwise never be exported.
