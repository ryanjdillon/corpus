# corpus

Local-first semantic search and structured query over your own content.

corpus ingests items from pluggable sources, classifies each with cheap
header/rule heuristics, embeds it through an OpenAI-compatible endpoint, and
stores the vectors + metadata in Postgres/pgvector. The pipeline is
source-agnostic — a *source* is anything that yields records (see
[docs/extending-fetchers.md](docs/extending-fetchers.md)). Email ships today, via
IMAP and Gmail; other document sources fit the same model.

The embedding endpoint defaults to a locally hosted model, so nothing leaves your
network for processing. Point `CORPUS_OPENAI_API_BASE` at a hosted provider
instead if you prefer their models over that privacy — the pipeline is identical
either way.

See [docs/architecture.md](docs/architecture.md) for the design.

Two query modes:

- **Semantic search** — vector similarity with optional metadata filters.
- **Structured query** — analytical metadata queries that return *every* match
  (e.g. every message with a given label before a date), as plain SQL.

Both are exposed over a REST API and an MCP server.

## Commands

```bash
corpus api              # REST API (default :8000)
corpus mcp              # MCP server, streamable-HTTP (default :9000)
corpus ingest <source>  # sync one source, e.g. imap:<name> or gmail:<name>
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

### Gmail sources

Gmail is ingested through the Gmail API (OAuth) rather than IMAP, so message
**labels** are captured. Configure per fetcher name; for `corpus ingest
gmail:personal`:

```
CORPUS_GMAIL_PERSONAL_CLIENT_ID=...
CORPUS_GMAIL_PERSONAL_CLIENT_SECRET=...
CORPUS_GMAIL_PERSONAL_REFRESH_TOKEN=...        # see scripts/gmail_oauth.py
CORPUS_GMAIL_PERSONAL_LABELS=INBOX,Receipts    # optional; empty = all mail
```

Only a refresh token is stored; access tokens are minted per run. Obtain the
refresh token once with an OAuth *Desktop app* client (Gmail API enabled):

```bash
pip install google-auth-oauthlib
python scripts/gmail_oauth.py client_secret.json
```

Incremental sync uses Gmail's `historyId`: the first run backfills and records
the mailbox's current `historyId`; later runs pull only changes since, falling
back to a full backfill if the `historyId` has expired.

## Database

The document table is created and managed by the pgvector document store inside
`CORPUS_DB_SCHEMA`; the DB role must own that schema. A small `sync_state`
table (source, cursor, updated_at) tracks per-source progress.

## Observability

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (and optionally `OTEL_SERVICE_NAME`) and the
API, MCP, and ingest export OpenTelemetry traces + metrics over OTLP: FastAPI and
httpx spans, ingest counters/histograms (documents written/skipped, embed
latency, batch size), and a corpus-size gauge. It is a no-op when the endpoint is
unset. See [docs/architecture.md](docs/architecture.md#observability).

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
