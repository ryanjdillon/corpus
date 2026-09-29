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
```

A run is resumable: documents that already have an enrichment are skipped, so a
re-run continues rather than restarts. `--limit` counts documents sent to the
model, not documents scanned, so a capped scheduled run keeps making progress past
the ones already done.

Requests run `CORPUS_ENRICH_CONCURRENCY` at a time. A transient endpoint failure
is retried in-process (see [Configuration](configuration.md)). A per-document
rejection (4xx, or unparseable output) is skipped and counted, while an endpoint
that stays down aborts the run so it can resume later.

## Models

`CORPUS_ENRICH_MODEL` does the enrichment, and `CORPUS_AUDIT_MODEL` does the
secret audit. The audit model defaults to the enrichment model. Set it apart when
enrichment runs on a remote endpoint. The audit reads the secrets that an egress
gate redacts, so on a redacting route it would judge text with the secrets
already removed, and on a non-redacting one it would send them off-prem. Both
calls use the same `CORPUS_OPENAI_API_BASE`, so the audit is local only if the
gateway serves the named model on-prem.

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
