# corpus

Local-only semantic search over documents and email.

Fetches mail (and, later, local documents), classifies each message with cheap
header/rule heuristics, embeds it with a local embedding model behind an
OpenAI-compatible endpoint, and stores vectors + metadata in Postgres/pgvector.
Content is only ever sent to the configured local endpoint — nothing leaves the
network for processing.

Two query modes:

- **Semantic search** — vector similarity with optional metadata filters.
- **Structured query** — analytical metadata queries that return *every* match
  (e.g. all promotional mail older than two weeks), as plain SQL.

Both are exposed over a REST API and an MCP server.

## Commands

```bash
corpus api                 # REST API (default :8000)
corpus mcp                 # MCP server, streamable-HTTP (default :9000)
corpus ingest imap:<name>  # sync one source
```

## Configuration

All settings are environment variables, prefixed `CORPUS_` (see
`src/corpus/config.py`). Key ones:

| Variable | Purpose |
|---|---|
| `CORPUS_DATABASE_URL` | Postgres DSN (pgvector) |
| `CORPUS_DB_SCHEMA` | schema for the document table (default `corpus`) |
| `CORPUS_OPENAI_API_BASE` | OpenAI-compatible embedding endpoint (`…/v1`) |
| `CORPUS_OPENAI_API_KEY` | key for that endpoint |
| `CORPUS_EMBEDDING_MODEL` | embedding model name (default `local-embed`) |
| `CORPUS_EMBEDDING_DIMENSIONS` | vector dimension (default `1024`) |

### IMAP sources

Each IMAP mailbox is configured by name. For `corpus ingest imap:boatclub`:

```
CORPUS_IMAP_BOATCLUB_HOST=mail.example.org
CORPUS_IMAP_BOATCLUB_PORT=993
CORPUS_IMAP_BOATCLUB_USER=crew@example.org
CORPUS_IMAP_BOATCLUB_PASSWORD=...
CORPUS_IMAP_BOATCLUB_FOLDERS=INBOX,Archive   # optional
```

Incremental sync tracks IMAP `UIDVALIDITY:UID` per source in a `sync_state`
table.

## Database

The document table is created and managed by the pgvector document store inside
`CORPUS_DB_SCHEMA`; the DB role must own that schema. A small `sync_state`
table (source, cursor, updated_at) tracks per-source progress.

## Build

`deploy/Dockerfile` builds a wheel and installs it. Pushing a `v*` tag builds
and publishes `ghcr.io/<owner>/corpus` (see `.github/workflows/docker.yml`).
