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

The envoy-ext-proc adapter expects the request body in `Buffered` processing
mode, where the proxy sends the whole body as one gRPC message, so
`CORPUS_SCAN_GATE_GRPC_MAX_MESSAGE_BYTES` must be at least the proxy's body
buffer limit. Request headers must be sent too (the default), so the adapter sees
`x-client-id`. Only the `text` of each message and content part is redacted;
image and audio parts pass through byte-identical.

## Sanitized tier

`corpus sync` projects enriched documents into a separate, trust-downgraded
database: summaries and the priority signal, never raw content, subject or
sender. `corpus index` serves that database to cloud-side consumers over MCP.

| Variable | Purpose |
|---|---|
| `CORPUS_SANITIZED_DATABASE_URL` | DSN the sync writes to |
| `CORPUS_INDEX_DATABASE_URL` | DSN the index server reads with (a read-only role) |
| `CORPUS_SANITIZED_DB_SCHEMA` | schema of the sanitized `messages` table; empty means `CORPUS_DB_SCHEMA` |
| `CORPUS_INDEX_SENSITIVITY_GATE` | sensitivity at which free-text summaries are withheld (default `high`) |

Set `CORPUS_SANITIZED_DB_SCHEMA` when several raw schemas feed one sanitized
view. For example, one raw schema per mailbox (`mbx_kasserar`, `mbx_post`, …) can
each run `corpus sync` into a shared `kbl` schema. Record ids include the source
(`imap:<mailbox>::…`), so rows from different mailboxes never collide.
