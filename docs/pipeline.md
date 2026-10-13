# Pipeline

`corpus pipeline <source>` runs everything one source needs, in one process:
it ingests new mail, enriches it, and syncs the result into a projected tier.
One scheduled job per source replaces the separate ingest, enrich and sync jobs,
which were linked only by when each was scheduled to start.

```bash
corpus pipeline imap:example                       # ingest, enrich, sync
corpus pipeline imap:example --limit 400           # enrich and sync at most 400
corpus pipeline imap:example --stages ingest       # just ingest
corpus pipeline imap:example --batch-size 16       # embed 16 records per request
corpus pipeline imap:example --tier partner        # sync into another projected tier
```

## Stages

The stages always run in this order, each scoped to the source:

| Stage | Runs | `--limit` means |
|---|---|---|
| `ingest` | `corpus ingest <source> --batch-size <n>` | nothing: ingest takes everything newer than its cursor |
| `enrich` | `corpus enrich --source <source>`, including its [secret audit](enrichment.md) | at most N documents sent to the model |
| `sync` | `corpus sync --source <source> --tier <tier>` | at most N rows projected |

`--limit` does not cap ingest; `--batch-size` (default `50`, as for
`corpus ingest`) sets how many records ingest embeds per request, and so how
often it commits its cursor. Lower it when one batch would outlast the embedder's
or the gateway's timeout, as a CPU embedder behind a gateway can.

`--stages` selects a subset, written in that order: `ingest,sync` is fine, while
`sync,ingest` is refused rather than reordered, because the person who wrote it
expected an order the run does not have. An unknown or repeated stage is refused
too.

There is no `audit` stage. Enrich already audits every document it enriches that
the detectors flag. `corpus audit-secrets` re-audits every flagged document on
each run and keeps no record of what it has done, so as a scheduled stage it
would send the same documents to the model every time. Run it by hand when the
detectors or the audit model change.

The maintenance flags (`--force`, `--upgrade-stale`, `--reproject`, …) stay on
the individual commands. `corpus ingest`, `corpus enrich` and `corpus sync`
are unchanged.

## Before the first stage

The run checks every selected stage's configuration before it starts:

- a source whose kind may not be enriched (see the
  [enrichment policy](fetchers/policy.md)) is refused when `enrich` is selected;
  run it with `--stages ingest` instead;
- the sync tier must exist, must be a projected tier, and both its store and
  its source tier's store must have their DSN set.

A misconfigured sync therefore fails at once, not after hours of enrichment.

A run refused here exits with status 2, as does a bad `--stages` list, before
anything is fetched or sent.

## Failure and resumption

A stage that fails stops the run. The command prints the counts of the stages
that completed, names the stage that failed, and exits with status 1, so a
scheduler sees a failed job and can tell it from a refused one (status 2).

The pipeline keeps no state of its own. Running the same command again resumes,
because every stage already does:

- **ingest** commits its cursor as it goes and skips messages it has stored;
- **enrich** passes over documents already enriched, or rejected by the model;
- **sync** skips rows whose enrichment has not changed since they were synced.

So a run that fails in enrich keeps what ingest wrote. The next run fetches only
mail newer than the cursor, then enriches what is left.

## Stores

Each stage opens the stores it needs and closes them when it finishes or fails.
Enrichment can run for hours, so opening every store at the start would hold
idle connections for that long.

## Scheduling

Schedule one job per source, running `corpus pipeline <source>`, with the
environment that `corpus ingest`, `corpus enrich` and `corpus sync` each needed:
the source's fetcher settings, the database and embedding settings, the
enrichment model, and the sanitized tier's DSN. A job that fails is retried by
running it again.

The job embeds (ingest and sync) and enriches in one process, so set each
stage's key separately rather than giving it one key allowed to do both:
`CORPUS_EMBED_API_KEY` for a gateway client that may only embed, and
`CORPUS_ENRICH_API_KEY` for one that may enrich. Each falls back to
`CORPUS_OPENAI_API_KEY` when unset; see
[Endpoints and keys](configuration.md#endpoints-and-keys).

A one-off backfill that only ingests — for example while the embedder is
swapped onto a GPU — runs `corpus pipeline <source> --stages ingest`, with the
job's `--batch-size`. It replaces a job created from a standalone ingest
schedule, so the backfill and the scheduled run share one command.
