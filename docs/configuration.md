# Configuration

Every setting is an environment variable prefixed `CORPUS_`. The full set lives
in `src/corpus/config.py`; the common ones:

| Variable | Purpose |
|---|---|
| `CORPUS_DATABASE_URL` | Postgres DSN (pgvector) |
| `CORPUS_DB_SCHEMA` | schema for the document table (default `corpus`) |
| `CORPUS_OPENAI_API_BASE` | OpenAI-compatible endpoint (`…/v1`) for embedding and enrichment |
| `CORPUS_OPENAI_API_KEY` | key for that endpoint |
| `CORPUS_EMBED_API_BASE`, `CORPUS_EMBED_API_KEY` | the embedder's own endpoint and key (default: the two above) |
| `CORPUS_ENRICH_API_BASE`, `CORPUS_ENRICH_API_KEY` | enrichment's and the secret audit's own endpoint and key (default: the two above) |
| `CORPUS_EMBEDDING_MODEL` | embedding model name (default `local-embed`) |
| `CORPUS_EMBEDDING_DIMENSIONS` | vector dimension (default `1024`) |
| `CORPUS_ENRICH_MODEL` | model for batch enrichment (empty disables it) |
| `CORPUS_AUDIT_MODEL` | model for the secret audit (default: the enrichment model); keep it local when enrichment runs remotely |
| `CORPUS_ENRICH_CONCURRENCY` | enrichment requests in flight (default `8`) |
| `CORPUS_ENRICH_MAX_INPUT_CHARS` | characters sent per document for enrichment, `0` = unlimited (default) |
| `CORPUS_MODEL_OPTIONS` | per-model request options as JSON (see [Enrichment](enrichment.md#models)) |
| `CORPUS_ENRICH_RETRIES` | attempts per enrichment request (default `10`) |
| `CORPUS_ENRICH_RETRY_MAX_WAIT` | cap on the backoff between them, seconds (default `60`) |
| `CORPUS_LEAKS_BIN`, `CORPUS_LEAKS_TIMEOUT` | the Betterleaks credential scanner and its timeout, read by `corpus_detect` (see [Detectors](detect.md#settings)) |

The embedding endpoint is any OpenAI-compatible API. Point it at a local model
and nothing leaves your network; point it at a hosted provider and the pipeline
is unchanged.

## Endpoints and keys

Two clients call the model endpoint: the embedder (ingest, the sanitized sync,
and every `search` query) and the enricher (enrichment and its secret audit).
Each has its own base and key, and each falls back to the shared one when unset:

| Client | Base | Key |
|---|---|---|
| embedder | `CORPUS_EMBED_API_BASE`, else `CORPUS_OPENAI_API_BASE` | `CORPUS_EMBED_API_KEY`, else `CORPUS_OPENAI_API_KEY` |
| enricher, secret audit | `CORPUS_ENRICH_API_BASE`, else `CORPUS_OPENAI_API_BASE` | `CORPUS_ENRICH_API_KEY`, else `CORPUS_OPENAI_API_KEY` |

A deployment that sets only `CORPUS_OPENAI_API_BASE` and `CORPUS_OPENAI_API_KEY`
uses them for both. Set the per-client keys when the gateway scopes keys by
client, so each stage holds only the access it needs: a key that may only embed,
and another that may enrich. `corpus pipeline` runs both clients in one process,
so it is the case this exists for; give it both keys rather than one key allowed
to do both.

## Running a source

`corpus pipeline <source>` ingests, enriches and syncs one source in one
resumable run; schedule one per source. It needs the settings of all three
stages: the source's entry in the config file (see
[Source configuration](fetchers/config.md)) and its credential variables, the
database and embedding settings above, the enrichment model, and the sync tier's
DSN (see [Sanitized tier](#sanitized-tier)). See [Pipeline](pipeline.md) for the
stage order, `--stages`, `--limit`, `--batch-size` and how a failed run resumes.

Its embedding and enrichment stages can present separate keys (see
[Endpoints and keys](#endpoints-and-keys)). Those are model-endpoint keys, and
are unrelated to the source's credential variables (`<credentials_env_prefix>_*`),
which are the fetcher's mailbox login (Gmail, IMAP): neither falls back to the
other, and a source entry never names a model key. `corpus ingest`,
`corpus enrich` and `corpus sync` still run
one stage each.

## Serving

`corpus serve <tier>` serves one storage tier's MCP tools over streamable HTTP,
stateless (see [Query model](query.md#the-mcp-tools) for the tools). The server
name, the store, the read-only reader and the query policy all come from the
tier, defined in the config file (`CORPUS_CONFIG_FILE`; see [Tiers](tiers.md)). By
default:

| Command | Serves as | Reads with |
|---|---|---|
| `corpus serve sensitive` | `corpus` | `CORPUS_DATABASE_URL` |
| `corpus serve sanitized` | `corpus-index` | `CORPUS_INDEX_DATABASE_URL`, read-only |

| Variable | Purpose |
|---|---|
| `CORPUS_HOST` | listen address (default `0.0.0.0`) |
| `CORPUS_MCP_PORT` | listen port (default `9000`); the MCP endpoint is `/mcp` |

Both tiers' `search` tool embeds its text, so a server needs the embedding
settings above (the embedder's endpoint and key — `CORPUS_EMBED_API_BASE` and
`CORPUS_EMBED_API_KEY`, else the `CORPUS_OPENAI_API_*` pair — and
`CORPUS_EMBEDDING_MODEL`), pointing at the model the tier's vectors came from.
A query embedding makes one attempt within `CORPUS_EMBED_QUERY_TIMEOUT`
seconds (default `15`), so a down embedder fails a search at once; batch
embedding at ingest retries, each attempt within `CORPUS_EMBED_TIMEOUT`
(default `300`).
The sensitive server also reports the corpus-size gauge (see
[Observability](observability.md)).

`corpus mcp` and `corpus index` are deprecated aliases of `corpus serve
sensitive` and `corpus serve sanitized`, kept for one release. The REST API
(`corpus api`, port `8000`, `/health`) is gone: a readiness or liveness probe
should open a TCP connection to the MCP port instead.

## Riding out a busy enrichment endpoint

A model server saturated by a concurrent backfill sheds load as 5xx and drops
connections, then recovers within minutes. Rather than abort the pass, the
enricher retries such a failure in-process with exponential backoff — capped per
wait at `CORPUS_ENRICH_RETRY_MAX_WAIT` and jittered, so concurrent workers do not
return in one burst — over `CORPUS_ENRICH_RETRIES` attempts; the defaults wait
out roughly four minutes of outage before giving up with
`EnrichUnavailableError`. A 4xx is never retried: it is the request's own fault,
and the record is skipped instead.

Raise `CORPUS_ENRICH_RETRIES` where the endpoint shares a GPU with other work and
can be gone for longer; enrichment is resumable either way, so a run that does
give up continues from `enriched_ids` on the next pass.

Riding a 5xx out hides it, so each retry is logged with the response's
attribution detail — see
[diagnosing a shedding endpoint](observability.md#diagnosing-a-shedding-enrichment-endpoint)
for reading those lines before changing `CORPUS_ENRICH_CONCURRENCY`.

## Capping enrichment input

Enrichment sends the subject plus the whole body. On a KV-cache-bound backend —
a single consumer GPU at concurrency 16–32 — long bodies inflate prefill and
cache use until the server preempts, which caps throughput. Setting
`CORPUS_ENRICH_MAX_INPUT_CHARS` bounds each prompt: the head is kept (the subject
and opening lines, where the classification and summary signal concentrates) and
the dropped tail is replaced by a `[truncated]` marker. The subject is not
privileged, only first, so a limit shorter than the subject cuts the subject
itself — keep the cap comfortably above your longest subject. It trades fidelity on
long bodies for throughput, so it is off by default; a backend with cache
headroom should leave it at `0`.

The cap applies only to enrichment. The secret audit always sees the full text —
a credential can sit past the cap, and its candidates come from a full-body scan.
A text longer than the audit model's context window therefore fails its audit;
the enrichment is kept and the audit is counted as `audit_failed`.

Per-source variables are namespaced by fetcher name — see [IMAP](fetchers/imap.md)
and [Gmail](fetchers/gmail.md).

## scan-gate

The egress gate that redacts LLM request bodies before they leave the network
is its own service, [scan-gate](https://github.com/ryanjdillon/scan-gate), and is
configured there. It is a consumer of the [detectors](detect.md); what they
redact is listed in [Detectors](detect.md#what-is-redacted).

## Sanitized tier

`corpus sync` projects enriched documents into a separate, trust-downgraded
database: redacted summaries, the priority signal and an https message link,
never raw content, subject or sender. `corpus serve sanitized` serves that
database to cloud-side consumers over MCP. The sync's stores, source tier,
columns, redaction and sensitivity gate come from the `sanitized` tier's
`projection` (see [Tiers](tiers.md#redaction)). A changed projection policy
re-projects every row's text on the next sync, and the summaries it changed
are re-embedded over the following syncs, at most `CORPUS_SYNC_REEMBED_LIMIT`
a run (`corpus sync --reembed-limit 0 --reembed-wait` does all at once). Until
re-embedded, such a row is left out of `search` but stays reachable by keyword
and filter.
`corpus sync --reproject` re-projects every row even under an unchanged
policy, and `corpus sync --tier <name>` syncs into another projected tier the
file defines.

| Variable | Purpose |
|---|---|
| `CORPUS_CONFIG_FILE` | path of the config file (`tiers:` and `sources:`); unset means the built-in default tiers and the deprecated pre-file sources. `CORPUS_TIERS_FILE` is its deprecated name |
| `CORPUS_SYNC_REEMBED_LIMIT` | most embed-stale rows (text re-projected, vector from an earlier summary) one sync, or one `corpus pipeline` sync stage, re-embeds (default 500; 0: no cap). `corpus sync --reembed-limit` overrides it. It caps neither re-projecting text nor embedding new and re-enriched rows (`--limit` caps the latter). Replaces `CORPUS_SYNC_REPROJECT_LIMIT`, which capped the re-projection itself and is no longer read: its `0` would now mean an uncapped re-embedding on every sync. See [Tiers](tiers.md#text-now-embeddings-later) |
| `CORPUS_SANITIZED_DATABASE_URL` | DSN the sync writes to (the default tiers' `dsn_env`) |
| `CORPUS_INDEX_DATABASE_URL` | DSN the index server reads with, a read-only role (the default tiers' `reader_dsn_env`) |

`CORPUS_INDEX_SERVER_NAME`, `CORPUS_SANITIZED_DB_SCHEMA`,
`CORPUS_INDEX_SENSITIVITY_GATE` and `CORPUS_TIER_ACCESS` still work, as
deprecated overrides of the config file's tiers, for one release; see
[Tiers](tiers.md#environment-overrides-deprecated).

Give the sanitized tier its own `store.schema` when several raw schemas feed one
sanitized view. For example, one raw schema per mailbox (`mbx_kasserar`, `mbx_post`, …) can
each run `corpus sync` into a shared `kbl` schema. Record ids include the source
(`imap:<mailbox>::…`), so rows from different mailboxes never collide.
