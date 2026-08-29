# Architecture

corpus turns content from arbitrary sources into a searchable store of vectors +
metadata, then derives two read-only branches from that store — enrichment and
secret scanning. Every part is a **deep module**: a small interface hiding its
machinery, so a change to one (a new source, a different embedder, another store
backend, a new detector) doesn't ripple through the others.

```
Gmail/IMAP → fetchers → classify → embeddings → store (pgvector) ← search → api · mcp
                                                   │
                          enrichment  ── documents ┤   (LLM: guided JSON)
                          secret scan ── documents ┘   (pii · leaks · LLM confirm)
```

The rendered diagram of the runtime pipeline is
[`architecture.html`](architecture.html), generated from the checked
[`architecture.json`](architecture.json) specification (Archify). Regenerate it
whenever the module topology changes.

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
| `models` | `Record`, `Classification` | The normalized item types every stage passes, with `(source, source_uid)` identity. |
| `fetchers` | `build_fetcher(source) -> Fetcher`; `Fetcher.fetch(cursor)` yields Records | Per-source protocols (IMAP, Gmail API), auth, pagination, incremental cursors. See [extending-fetchers.md](extending-fetchers.md). |
| `classify` | `classify(record) -> Classification` | Header/rule heuristics (and an optional model tie-breaker) that assign a data-class label + confidence. |
| `embeddings` | `Embedder.embed(texts) -> vectors` | The OpenAI-compatible HTTP call, retries, and the typed `EmbedInputError` for rejected inputs. |
| `store` | `get_document_store()`, `to_document(...)`, `iter_documents(...)`, cursor helpers | The pgvector document store, the `sync_state` cursor, and a server-side streaming read cursor. |
| `vault` | `vault_path(id)`, `write(...)`, `read(id)` | The canonical raw markdown vault — source-fact frontmatter + body, one deterministic file per document, on a local-only volume. |
| `export` | `export_archive(...)` | Materializes the stored corpus into the vault (bootstrap; idempotent). |
| `ingest` | `ingest(source, batch_size) -> count` | Orchestration of the ingest line: fetch → classify → embed → write, with per-record resilience (see below). |
| `search` | `semantic_search(...)`, `structured_query(...)`, `stats()` | Vector similarity (HNSW) and analytical SQL over the store. |
| `api` / `mcp_server` | REST endpoints / MCP tools | Thin adapters over `search` — the **local (raw)** surface. |
| `index_query` | `action_items`, `due_soon`, `waiting_on`, `by_domain`, `summary`, `stats`; `ensure_view`, `view_ddl` | The **sanitized** query layer: whitelisted, parameterized reads of the `sanitized_documents` view (documents ⨝ enrichments, safe columns only) as the restricted `corpus_index_ro` role — the trust gate for downgraded consumers. |
| `index_server` | corpus-index MCP tools | The sanitized MCP surface (summaries + priority signal, never raw bodies or secrets) a cloud-model consumer may call. |
| `cli` | `main` — `api · mcp · index · index-init · ingest · scan · enrich · audit-secrets · export` | Click entrypoints that configure telemetry and launch each server or batch pipeline. |
| `enrichment` | `Enrichment` / `SecretAudit` structs, `json_schema()`, `SCHEMA_VERSION` | The msgspec enrichment + secret-audit schema; version fingerprints derived from the schema itself. |
| `enricher` | `Enricher.enrich(text) -> Enrichment` | The guided-decoding LLM call that produces structured, secret-free enrichment. |
| `secret_audit` | `audit_secrets(text, candidates) -> SecretAudit` | The LLM confirmation + severity pass over deterministic secret candidates. |
| `enrich_batch` | `run_enrich(store, …)`, `run_audit(store, …)` | Batch orchestration: enrich every document, audit only flagged ones; re-audit without re-enriching. |
| `enrich_store` | `EnrichStore` | The derived enrichments table — lazy DDL, upserts, resume ids, and per-stage provenance. |
| `sanitized_store` | `SanitizedStore` | The sanitized (trust-downgraded) store — a *separate* `ai_sanitized` database, `messages` table, lazy DDL + upsert; the one place a cloud consumer reads. |
| `sanitize` | `run_sync(store, …)`, `project(…)` | The one-way sync: projects `documents ⨝ enrichments` to cloud-safe fields (drops raw content/subject/sender, gates summaries by sensitivity), embeds `one_line`. |
| `scan` | `detect(text)`, `audit_candidates(text)`, `scan_archive(…)`, `SCAN_VERSION` | Merges `pii` + `leaks`; the audit candidate gate; the whole-archive secret report. |
| `pii` | `pii.scan(text) -> ScanResult` | Presidio identity/financial recognizers with adjacency-gated precision; types + counts, never values. |
| `leaks` | `leaks.scan(text) -> dict` | Local credential regexes plus an optional Betterleaks subprocess; rule types + counts, never values. |
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

## Derived branches: enrichment and secret scanning

Both branches read documents from the store and write nothing to it except a
*derived* index — the store's documents remain the source of truth.

- **Secret scanning** is deterministic and model-free. `pii` runs Presidio's
  identity/financial recognizers (SSN, card, IBAN, bank, crypto) with an
  adjacency gate that only counts a match when a context word sits beside it;
  `leaks` runs provider-prefixed credential regexes plus, when configured, the
  Betterleaks binary. `scan` merges the two and exposes `scan_archive` (the
  `corpus scan` audit) and `audit_candidates` (the gate that selects which
  messages are worth an LLM look). Only secret *types* and counts are ever
  produced — never the values.
- **Enrichment** (`enrich_batch`) does one LLM pass per document via `enricher`
  (guided decoding against the `enrichment` schema), and, only where
  `audit_candidates` flagged something, an LLM `secret_audit` that confirms which
  candidates are real and grades severity. Results are upserted into the derived
  table by `enrich_store`, with independent provenance for the enrichment and the
  audit so either can be regenerated alone. `run_audit` re-runs just the
  confirmation. The caller (`cli`) owns the `EnrichStore` lifecycle.

## Embedding and LLM endpoint

Both the embedder and the enrichment/secret-audit calls speak the
OpenAI-compatible API and are selected purely by configuration
(`CORPUS_OPENAI_API_BASE`, `CORPUS_OPENAI_API_KEY`, `CORPUS_EMBEDDING_MODEL`,
`CORPUS_ENRICH_MODEL`). One gateway therefore serves both `/embeddings` and
`/chat/completions`. A locally hosted model keeps content on your network; a
hosted provider trades that for their models. The pipeline is identical either way.

## Storage

Documents live in a pgvector-backed table inside `CORPUS_DB_SCHEMA` (the DB role
owns the schema). Per-source progress is tracked in a small `sync_state` table,
and the derived enrichment/secret-audit records live in an `enrichments` table
created lazily by `enrich_store`. Identity is `(source, source_uid)`, so
re-ingesting overwrites rather than duplicates.

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
