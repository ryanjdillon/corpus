# corpus

Semantic search and structured query over your own content.

corpus is a knowledge base for your own content — markdown, documents, or
structured records. It classifies each item, embeds it through an
OpenAI-compatible endpoint, and stores the vectors alongside structured metadata
in Postgres/pgvector. Email lands first (IMAP and Gmail); anything that yields
records fits the same pipeline.

A raw markdown layer keeps each item verbatim, and corpus derives a configurable
set of access-controlled storage tiers from it — you set each tier's projection,
the tool that exposes it, and who may read it, human or agent.

Query it two ways:

- **Semantic search** — vector similarity with optional metadata filters.
- **Structured query** — analytical metadata queries that return every match
  (say, every item with a given tag before a date) as plain SQL.

Both are served over a REST API and an MCP server.

## Where to go next

- [Architecture](architecture.html) — the rendered module diagram of the pipeline.
- [Configuration](configuration.md) — every `CORPUS_` setting.
- [Sources](fetchers/index.md) — the fetcher protocol, plus [IMAP](fetchers/imap.md) and [Gmail](fetchers/gmail.md).
- [Database](database.md) — the pgvector store and sync state.
- [Observability](observability.md) — OpenTelemetry traces and metrics.
- [Development](development.md) — tests, the pre-PR gate, and builds.
