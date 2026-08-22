# corpus

Local-only semantic search over email.

Fetches messages over IMAP, classifies each with cheap header/rule heuristics,
embeds it with a local embedding model behind an OpenAI-compatible endpoint, and
stores vectors + metadata in Postgres/pgvector. Content is only ever sent to the
configured local endpoint — nothing leaves the network for processing.

Two query modes:

- **Semantic search** — vector similarity with optional metadata filters.
- **Structured query** — analytical metadata queries that return *every* match
  (e.g. every message with a given label before a date), as plain SQL.

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

Each IMAP mailbox is configured by name. The name becomes the env-var prefix and
the source id passed to `corpus ingest`. For `corpus ingest imap:example`:

```
CORPUS_IMAP_EXAMPLE_HOST=imap.example.com
CORPUS_IMAP_EXAMPLE_PORT=993
CORPUS_IMAP_EXAMPLE_USER=user@example.com
CORPUS_IMAP_EXAMPLE_PASSWORD=...
CORPUS_IMAP_EXAMPLE_FOLDERS=INBOX,Archive   # optional; default is all folders
CORPUS_IMAP_EXAMPLE_SSL=true                 # optional, default true
```

`FOLDERS` selects which mailboxes to catalog. Give a comma-separated list to
restrict it, or leave it unset (or set `all` / `*`) to discover and catalog
every selectable folder on the account.

Incremental sync is tracked per folder in a `sync_state` table: the cursor is a
JSON map of folder to `UIDVALIDITY:UID`, so each folder resyncs independently.

## Database

The document table is created and managed by the pgvector document store inside
`CORPUS_DB_SCHEMA`; the DB role must own that schema. A small `sync_state`
table (source, cursor, updated_at) tracks per-source progress.

## Tests

```bash
pip install -e '.[test]'
pytest                  # fast unit tests
pytest -m integration   # Docker-backed: pgvector + GreenMail
```

Integration tests spin up pinned containers via the Docker SDK
(`pgvector/pgvector:pg17`, `greenmail/standalone:2.1.3`) and auto-skip when
Docker is unavailable. The embedding endpoint is faked in-process, so no model
is downloaded. Unit tests are the default; integration tests are opt-in via
`-m integration`.

## Build

`deploy/Dockerfile` builds a wheel and installs it. Pushing a `v*` tag builds
and publishes `ghcr.io/<owner>/corpus` (see `.github/workflows/docker.yml`).
