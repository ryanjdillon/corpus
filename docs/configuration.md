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
