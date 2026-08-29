<p align="center">
  <img src="docs/assets/logos/logo_crest.svg" alt="corpus" width="150">
</p>

<h1 align="center">corpus</h1>

<p align="center">Semantic search and structured query over your own content.</p>

<p align="center">
  <a href="https://github.com/ryanjdillon/corpus/actions/workflows/ci.yml"><img src="https://github.com/ryanjdillon/corpus/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://ryanjdillon.github.io/corpus/"><img src="https://img.shields.io/endpoint?url=https://ryanjdillon.github.io/corpus/coverage.json" alt="Coverage"></a>
  <a href="https://ryanjdillon.github.io/corpus/"><img src="https://img.shields.io/badge/docs-mkdocs-blue" alt="Docs"></a>
</p>

---

corpus turns your own content into something you can search by meaning and query
by fact. It ingests items from pluggable sources, classifies each with cheap
header and rule heuristics, embeds it through an OpenAI-compatible endpoint, and
stores the vectors and metadata in Postgres/pgvector.

The embedding endpoint is any OpenAI-compatible API. Run a local model and
nothing leaves your network; use a hosted provider and the pipeline is the same.

Email ships today over IMAP and Gmail. A source is anything that yields records,
so other document types fit the same model — see the [sources guide][sources].

## Two ways to query

- **Semantic search** — vector similarity with optional metadata filters.
- **Structured query** — analytical metadata queries that return every match
  (say, every message with a given label before a date) as plain SQL.

Both are served over a REST API and an MCP server.

## Quickstart

```bash
pip install -e .

export CORPUS_DATABASE_URL=postgresql://…      # pgvector
export CORPUS_OPENAI_API_BASE=https://…/v1     # embedding endpoint
export CORPUS_OPENAI_API_KEY=…

corpus ingest imap:example   # sync one source
corpus api                   # REST API   (default :8000)
corpus mcp                   # MCP server (default :9000)
```

Configuring a source takes a handful of env vars — [IMAP][imap], [Gmail][gmail].
See [Configuration][config] for the rest.

## Documentation

Full docs: **<https://ryanjdillon.github.io/corpus>**

- [Architecture][arch] — the pipeline, module by module
- [Configuration][config] — every `CORPUS_` setting
- [Sources][sources] — the fetcher protocol, IMAP, and Gmail
- [Database][db] — the pgvector store and sync state
- [Observability][obs] — OpenTelemetry traces and metrics
- [Development][dev] — tests, the pre-PR gate, and builds

## Development

```bash
nix develop      # Python, uv, just, ruff, commitlint
just setup       # create the venv, install deps
just check       # lint + coverage + architecture gate + commitlint
```

See [Development][dev] for the test tiers and release build.

[arch]: docs/architecture.md
[config]: docs/configuration.md
[sources]: docs/fetchers/index.md
[imap]: docs/fetchers/imap.md
[gmail]: docs/fetchers/gmail.md
[db]: docs/database.md
[obs]: docs/observability.md
[dev]: docs/development.md
