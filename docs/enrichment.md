# Enrichment

`corpus enrich` asks a model for structured metadata about each stored document:
summaries, classification axes, the action it asks for, entities, and sensitivity.
It also runs a secret audit on documents the deterministic detectors flagged. The
schema is `corpus.enrichment.Enrichment`, and guided decoding constrains the
model's output to it, so every response parses. Only sources the policy declares
enrichable are processed (see [Enrichment policy](fetchers/policy.md)).

## Running it

```sh
corpus enrich                # every eligible document not yet enriched
corpus enrich --limit 400    # at most 400 documents sent to the model
corpus enrich --force        # re-enrich documents that already have a record
corpus enrich --upgrade-stale    # also redo records from an older schema version
corpus enrich --retry-rejected   # also retry documents this model rejected
```

A run is resumable: documents that already have an enrichment are skipped, so a
re-run continues rather than restarts. `--limit` counts documents sent to the
model, not documents scanned, so a capped scheduled run keeps making progress past
the ones already done.

Requests run `CORPUS_ENRICH_CONCURRENCY` at a time. A transient endpoint failure
is retried in-process (see [Configuration](configuration.md)), and an endpoint
that stays down aborts the run so it can resume later.

A per-document rejection (a 4xx such as an input over the model's context, or
unparseable output) is counted as skipped and recorded against the model that
rejected it, with the reason. Later runs on that model pass the document over
instead of re-sending it every time; `--retry-rejected` sends them again, and a
different model is always given a try. A later successful enrichment clears the
record. If the secret audit fails for a document, the enrichment is kept and the
audit is counted as `audit_failed`; configuring the audit model's
`context_tokens` avoids the overflow case (see below).

Every request caps the model's output at 4096 tokens (`max_tokens`; a model's
`extra_body` can override it). Guided decoding guarantees the shape, not the
length: a document can send a model into a loop that keeps generating, and an
uncapped request then runs until the gateway's request timeout, is retried as an
outage, and can stall the run. Capped, the cut-off output fails to parse and the
document is rejected like any other. The same 4096 is the output reserve that
`context_tokens` budgeting subtracts when sizing the input, so raising one model's
`max_tokens` above it can make prompt plus output exceed that model's context;
lower its `context_tokens` by the difference when you do.

## Models

`CORPUS_ENRICH_MODEL` does the enrichment, and `CORPUS_AUDIT_MODEL` does the
secret audit. The audit model defaults to the enrichment model. Set it apart when
enrichment runs on a remote endpoint. The audit reads the secrets that an egress
gate redacts, so on a redacting route it would judge text with the secrets
already removed, and on a non-redacting one it would send them off-prem. Both
calls use the same `CORPUS_OPENAI_API_BASE`, so the audit is local only if the
gateway serves the named model on-prem.

`CORPUS_MODEL_OPTIONS` tunes how each model is called, as JSON keyed by model
name. A model with no entry is called exactly as before.

```sh
CORPUS_MODEL_OPTIONS='{"bonsai-2-27b": {"inline_schema_refs": true,
  "extra_body": {"reasoning_effort": "none"}, "context_tokens": 32768}}'
```

- `inline_schema_refs`: inline the response schema's `$ref`s. llama.cpp can't
  resolve the nested references msgspec emits and silently drops the grammar, so
  the model answers unconstrained. `SCHEMA_VERSION` is unaffected.
- `extra_body`: fields merged into every request, e.g. `reasoning_effort`.
- `context_tokens`: the model's per-request context. Enrichment input is capped
  to what fits (after `CORPUS_ENRICH_MAX_INPUT_CHARS`, whichever is tighter), and
  a secret audit that would overflow is run on windows of text around each
  detected candidate, chunked to fit, with the chunk verdicts merged (worst
  severity per type). A secret deep in a long message is still audited rather
  than cut off or skipped.

## Schema and `SCHEMA_VERSION`

Every field of every struct is required in the schema sent to the model, even
though the structs carry defaults. Under guided decoding, an optional field is one
the grammar lets the model skip, and which fields get skipped depends on the
server, not the document. The classification axes collapsed to their defaults
until they were forced required. Later, vLLM dropped every entity list and
llama.cpp every deadline. Required means the model must decide. An empty list or a
null deadline is still a valid answer.

`SCHEMA_VERSION` is a hash of that schema, stored with each enrichment, so it
changes whenever the schema does. Records written under an older version stay
readable, because decoding fills defaults, and a normal run still counts them as
enriched. `--upgrade-stale` treats them as not yet done, so they are re-enriched.
It is opt-in: a scheduled run on a remote model would otherwise re-send the whole
archive after every schema change. Run it where the model is local.
