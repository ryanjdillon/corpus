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

corpus answers three kinds of question, over a REST API and an MCP server:

- **Find by meaning** — semantic search: vector similarity with optional metadata
  filters.
- **Filter by fact** — structured query: exhaustive metadata queries that return
  every match (a given tag, an account, a time window) as plain SQL.
- **Ask what matters** — the enrichment priority signal: what needs an action,
  what's due or time-sensitive, what you're waiting on, what's happening in a
  domain like banking, health, or work.

The full surface (search, structured query, whole-document fetch, stats) is for
you and your local tools; the priority signal, free of raw content, is what a
governed cloud agent reads on the sanitized tier.

## Where to go next

- [Architecture](architecture.html) — the rendered module diagram of the pipeline.
- [Configuration](configuration.md) — every `CORPUS_` setting.
- [Sources](fetchers/index.md) — the fetcher protocol, plus [IMAP](fetchers/imap.md) and [Gmail](fetchers/gmail.md).
- [Database](database.md) — the pgvector store and sync state.
- [Observability](observability.md) — OpenTelemetry traces and metrics.
- [Development](development.md) — tests, the pre-PR gate, and builds.
