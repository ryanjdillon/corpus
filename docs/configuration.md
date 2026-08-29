# Configuration

Every setting is an environment variable prefixed `CORPUS_`. The full set lives
in `src/corpus/config.py`; the common ones:

| Variable | Purpose |
|---|---|
| `CORPUS_DATABASE_URL` | Postgres DSN (pgvector) |
| `CORPUS_DB_SCHEMA` | schema for the document table (default `corpus`) |
| `CORPUS_OPENAI_API_BASE` | OpenAI-compatible embedding endpoint (`…/v1`) |
| `CORPUS_OPENAI_API_KEY` | key for that endpoint |
| `CORPUS_EMBEDDING_MODEL` | embedding model name (default `local-embed`) |
| `CORPUS_EMBEDDING_DIMENSIONS` | vector dimension (default `1024`) |

The embedding endpoint is any OpenAI-compatible API. Point it at a local model
and nothing leaves your network; point it at a hosted provider and the pipeline
is unchanged.

Per-source variables are namespaced by fetcher name — see [IMAP](fetchers/imap.md)
and [Gmail](fetchers/gmail.md).
