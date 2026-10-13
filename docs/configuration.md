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

`corpus scan-gate` applies the egress policy (`corpus.egress.policy`) to LLM
request bodies before they leave the network. The policy is gateway-neutral; a
protocol adapter connects it to a gateway. The only adapter today is
`envoy-ext-proc`, Envoy's external-processing gRPC protocol, which any
Envoy-based proxy can call.

| Variable | Purpose |
|---|---|
| `CORPUS_SCAN_GATE_ADAPTER` | gateway protocol adapter (default `envoy-ext-proc`) |
| `CORPUS_SCAN_GATE_PORT` | listen port (default `9002`) |
| `CORPUS_SCAN_GATE_WORKERS` | concurrent request streams (default `8`) |
| `CORPUS_SCAN_GATE_FAIL_OPEN` | pass a body that is not JSON (e.g. multipart audio) through unchanged; the default refuses it |
| `CORPUS_SCAN_GATE_BLOCK_TYPES` | comma-separated secret types refused with a 403 instead of redacted (default `private_key`) |
| `CORPUS_SCAN_GATE_SKIP_MODELS` | comma-separated model names passed unscanned because they are served locally (default none) |
| `CORPUS_SCAN_GATE_BATCH_CLIENTS` | comma-separated client ids of batch jobs (default none) |
| `CORPUS_SCAN_GATE_BATCH_MAX_BYTES` | largest body a batch client may send to a scanned model; larger gets a 413 (default `65536`) |
| `CORPUS_SCAN_GATE_GRPC_MAX_MESSAGE_BYTES` | envoy-ext-proc: largest gRPC message accepted and returned (default 50 MiB) |

For each body, in order: a body that is not a JSON object is refused (or passed,
when failing open); a model in `SKIP_MODELS` passes unscanned; a batch client's
oversized body gets a 413; otherwise every message's text is redacted, and a
finding in `BLOCK_TYPES` gets a 403. Text is read from chat message content
(including nested tool results and tool-call arguments), Anthropic `system`,
embeddings `input` and completions `prompt`. An error inside the policy is
refused (or passed, when failing open) rather than left to the proxy. Refusals carry an OpenAI-style JSON error.
`SKIP_MODELS` takes exact names, not patterns, so a typo can only make the gate
scan more. Skipping trusts the body's `model` field, so it is sound only where the
gateway routes on that same field: a request naming a local model must not be
able to reach a cloud backend. Do not list a model if any route sends that name
off the network, and keep path-routed cloud endpoints off the skip path. The client id is the caller's `x-client-id` request header, which the
gateway sets after authenticating the API key.

What is redacted, each replaced by a `[REDACTED:<type>]` placeholder:

- credentials (`leaks`): private keys, API keys and tokens; a private key is
  blocked by default (`BLOCK_TYPES`) rather than redacted;
- identity and financial numbers (`pii`): IBANs and crypto wallets on format,
  and US SSNs, credit cards and US bank numbers only with a context word right
  beside them, so an order number that looks like a card passes. The context
  words are Presidio's English ones plus Norwegian ones for a card (`kort`,
  `kortnummer`…) and an account (`konto`, `kontonummer`…); see
  [Tiers](tiers.md#what-financial_id-does-not-find) for what still passes;
- Norwegian national id numbers (fødselsnummer, D-nummer, H-nummer, and the tax
  administration's Tenor synthetic test numbers), only when both mod-11 check
  digits and the birth date are valid;
- email addresses;
- phone numbers, only when they are clearly phones: an international prefix
  (`+47`, or `0047` followed by a separator), or a phone word within 32
  characters before the number (`tlf`, `telefon`, `mobil`, `mob`, `phone`, `tel`,
  `cell`, `direkte`, `fax`, including compounds such as `telefonnummer`, and
  allowing a bracket or one short word in between, as in `Tlf (privat)` or
  `Phone number:`). A bare digit run passes, because order numbers, references,
  dates and amounts look the same and redacting them would degrade what the
  model reads. The redaction ends once the number holds 8 digits after any
  country code, so a date or org number that follows stays readable.

Email addresses and phone numbers are redacted on egress only. They are too
common in mail to count as findings in the archive audit; the national id
number is counted there too.

The envoy-ext-proc adapter expects the request body in `Buffered` processing
mode, where the proxy sends the whole body as one gRPC message, so
`CORPUS_SCAN_GATE_GRPC_MAX_MESSAGE_BYTES` must be at least the proxy's body
buffer limit. Request headers must be sent too (the default), so the adapter sees
`x-client-id`. Only the `text` of each message and content part is redacted;
image and audio parts pass through byte-identical.

## Sanitized tier

`corpus sync` projects enriched documents into a separate, trust-downgraded
database: redacted summaries, the priority signal and an https message link,
never raw content, subject or sender. `corpus serve sanitized` serves that
database to cloud-side consumers over MCP. The sync's stores, source tier,
columns, redaction and sensitivity gate come from the `sanitized` tier's
`projection` (see [Tiers](tiers.md#redaction)); a changed projection policy
re-projects the rows over the next syncs, at most
`CORPUS_SYNC_REPROJECT_LIMIT` a run, `corpus sync --reproject` re-projects
every row at once, and `corpus sync --tier <name>` syncs into another
projected tier the file defines.

| Variable | Purpose |
|---|---|
| `CORPUS_CONFIG_FILE` | path of the config file (`tiers:` and `sources:`); unset means the built-in default tiers and the deprecated pre-file sources. `CORPUS_TIERS_FILE` is its deprecated name |
| `CORPUS_SYNC_REPROJECT_LIMIT` | most rows one sync re-projects only because the projection policy changed (default 500; 0: no cap). New and re-enriched rows are never capped. See [Tiers](tiers.md#rolling-out-a-policy-change) |
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
