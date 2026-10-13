# Tiers

A storage tier is a store plus the policy around it: the MCP server that exposes
it, the principals the gateway lets reach that server, the query policy the
server enforces and, for a downgraded tier, the projection that derives its rows
from a source tier. Each tier is defined once, in the **tiers file**, and
`corpus serve <tier>`, `corpus sync [--tier <tier>]` and the sync stage of
`corpus pipeline [--tier <tier>]` take everything from it (`tiers.tier(name)`).

Set `CORPUS_TIERS_FILE` to the file's path. Without it, the [built-in
default](#the-default) applies, which is the configuration corpus had before the
file existed, so a deployment that sets no file is unchanged.

## The file

```yaml
tiers:
  - name: sensitive
    store: {dsn_env: CORPUS_DATABASE_URL}
    serve: {server_name: corpus, access: [pi-local]}
    query:
      max_limit: 100
      filterable: [label, account, source, sent_at]
      aggregatable: [label, month, account]
      orders: [newest, oldest]
      text: true
      semantic: true
  - name: sanitized
    store:
      dsn_env: CORPUS_SANITIZED_DATABASE_URL
      reader_dsn_env: CORPUS_INDEX_DATABASE_URL
      schema: corpus
    projection: {from: sensitive, sensitivity_gate: high}
    serve: {server_name: corpus-index, access: [orchestrator, pi]}
    query:
      max_limit: 100
      search_depth: 200
      filterable: [domain, category, importance, requires_action, sent_at, deadline]
      aggregatable: [domain, month, importance]
      orders: [priority, newest, oldest, deadline]
      text: true
      semantic: true
```

Tiers are listed most sensitive first.

| Key | Meaning |
| --- | --- |
| `name` | The tier's name: `corpus serve <name>`, `corpus sync --tier <name>`, and `tier` in every [result envelope](mcp-results.md). `sync` defaults to `sanitized`; the deprecated `mcp` and `index` commands serve `sensitive` and `sanitized`. |
| `store.dsn_env` | The environment variable that holds the DSN the tier is written with. |
| `store.reader_dsn_env` | The variable holding a read-only DSN the server reads with. Unset means the server reads with `dsn_env`. |
| `store.schema` | The schema of the tier's table. Unset means `CORPUS_DB_SCHEMA` for a source tier, and the source tier's schema for a projected one. |
| `projection.from` | The source tier the rows are projected from. A tier without `projection` is a source tier, populated by ingest. |
| `projection.fields` | The columns projected (default: all of `sanitized_store.COLS`). Any other column is stored as NULL. `id` and `enriched_at` are always kept. |
| `projection.sensitivity_gate` | The `sensitivity_level` (`none`, `low`, `medium`, `high`) at or above which free text (`one_line`, `action_summary`, `organizations`) is withheld (default `high`). |
| `serve.server_name` | The MCP server name, which is also reported in every result. |
| `serve.access` | The principals the gateway lets reach the server. See [Access](#access). |
| `query` | The tier's `QueryPolicy` (see [Query model](query.md#what-each-tier-allows)): `max_limit`, `filterable`, `aggregatable`, `orders`, `text`, `semantic`, and optionally `search_depth`. |

`query.search_depth` is the deepest a vector-ordered query pages (default and
maximum 200, `query.SEMANTIC_DEPTH`). That is how many rows one HNSW index scan is
set to rank, so a tier can lower the depth but not raise it.

### Secrets stay out of the file

The file never holds a DSN. Each store names the variable that does, so a
secret keeps its existing injection path (a Kubernetes secret, a systemd
credential) and the file can be committed and reviewed like any other config.
A key named `dsn` is an unknown key, and is refused.

A DSN is read when a store is opened, not when the file is loaded. Each server
therefore needs only its own tier's variable: the index's pod need not hold
`CORPUS_DATABASE_URL`, nor the full server's `CORPUS_INDEX_DATABASE_URL`. A
store whose variable is unset fails when it is opened, with an error that names
the variable.

### Validation

The file is checked when `serve` or `sync` starts, and an invalid file stops the
command with an error that names the problem. Nothing is ignored:

- an unknown key, at any level, is an error;
- every tier needs a complete `query` policy, since a tier without one would
  otherwise be permissive;
- a filter, `group_by` dimension or order that the query model does not know is
  an error, and so is a `search_depth` outside 1–200;
- `projection.from` must name another tier in the file, and that tier must be a
  source tier;
- `projection.fields` must be columns of the sanitized table;
- tier names and server names must be unique;
- a source tier must use the store that ingest writes: `dsn_env:
  CORPUS_DATABASE_URL`, and no `schema` other than `CORPUS_DB_SCHEMA`. Ingest
  and enrichment still read those two variables directly, and this rule stops
  `serve` and ingest from silently using different databases.

## The default

Without a file, the tiers are:

| | `sensitive` | `sanitized` |
| --- | --- | --- |
| Store | `CORPUS_DATABASE_URL`, schema `CORPUS_DB_SCHEMA` | written with `CORPUS_SANITIZED_DATABASE_URL`, read with `CORPUS_INDEX_DATABASE_URL`, schema `CORPUS_DB_SCHEMA` |
| Server name | `corpus` | `corpus-index` |
| Access | none | none |
| Query policy | `query.SENSITIVE_POLICY` | `query.SANITIZED_POLICY` |
| Projection | (source tier) | from `sensitive`, every column, gate `high` |

`tiers.DEFAULT` is itself a valid tiers file. To start a file from it, encode it
as YAML: `python -c "import msgspec, corpus.tiers as t;
print(msgspec.yaml.encode(t.DEFAULT).decode())"`.

## Environment overrides (deprecated)

These variables override the file (or the default) when they are set, for one
release. Move each into the file. A process that has any of them set logs a
deprecation warning when it loads the tiers.

| Variable | Overrides |
| --- | --- |
| `CORPUS_INDEX_SERVER_NAME` | `serve.server_name` of the `sanitized` tier |
| `CORPUS_SANITIZED_DB_SCHEMA` | `store.schema` of the `sanitized` tier (empty does not override) |
| `CORPUS_INDEX_SENSITIVITY_GATE` | `projection.sensitivity_gate` of the `sanitized` tier |
| `CORPUS_TIER_ACCESS` | `serve.access` of each tier the JSON map names, e.g. `{"sanitized": ["orchestrator"]}` |

The DSN variables (`CORPUS_DATABASE_URL`, `CORPUS_SANITIZED_DATABASE_URL`,
`CORPUS_INDEX_DATABASE_URL`) are not overrides and are not deprecated. They are
where the default tiers read their DSNs from, and a file may name other
variables for a projected tier.

## Adding a tier

A new tier is configuration, not code. Add it to the file, then `corpus sync
--tier <name>` fills it (when it has a `projection`) and `corpus serve <name>`
serves it. `sync --tier` refuses a source tier, and both commands refuse a name
the file does not define.

What a tier serves follows from its kind:

- a **source tier** (no `projection`) serves the documents table with the
  `corpus_*` presets, like `sensitive`, and reports telemetry as `corpus-mcp`;
- a **projected tier** serves its `messages` table, read-only through its reader
  DSN, with the `index_*` presets, like `sanitized`, and reports telemetry as
  `corpus-index`.

Its own server name, query policy, store and projection apply in either case;
only the tool set and the store class are shared by kind.

## Access

`serve.access` is declarative. The gateway in front of each server authenticates
callers and enforces it. corpus does not refuse callers in-process, because some
callers (the daily digest, which calls in-cluster) carry no principal, and
because enforcement belongs to the gateway and, later, to a policy decision
point (see `GOVERNANCE.md`, roadmap stages 3–4). corpus exposes the list
(`tiers.tier(name).access`) for the deployment to generate gateway policy from.
A principal is an opaque id: an API-key client id at the gateway today, and an
OIDC subject or group later.

## The message link: `uri`

The sanitized tier projects a `uri` column, so a consumer can link the owner to a
message without parsing its id. It is projected only when the source document's
`uri` is an https URL with a host and no credentials, which is the Gmail web link
(`https://mail.google.com/mail/u/0/#all/<id>`). Any other uri is stored as NULL.
An IMAP uri (`imap://<host>/<folder>;UIDVALIDITY=…`) would expose the mail host
and the private folder names to the cloud-facing tier.

`uri` is trusted: the fetcher derives it from provider identifiers only (the
host, folder and message id), never from the message body or headers (the
invariant on `models.Record.uri`). So it is not listed in `untrusted_fields` and
is not scanned for `instruction_like` (see
[MCP results](mcp-results.md#why-fields-are-marked-untrusted)). A fetcher that
built a uri from message content would break that assumption. As a second line,
the projection refuses a uri longer than 2048 characters or containing any
whitespace or unprintable character anywhere: `urlsplit` silently drops tab, CR
and LF, so a check on the parsed parts alone would pass text after a line break.

The sync adds the column to an existing table (`ADD COLUMN IF NOT EXISTS`) the
first time it opens the store. The index's read-only role cannot run DDL, so
after an upgrade the index can start before any sync has added the column. It
does not fail: a read-only store checks the catalog for the columns it selects
and reads a missing one as NULL, so every row keeps every key and `uri` is
`null` until the next sync. While a column is missing it re-checks on each read,
so it serves the column as soon as the sync adds it, without a restart. A
table-level `SELECT` grant covers the new column.

## Re-projecting: `corpus sync --reproject`

`corpus sync` is incremental. It skips a document whose `enriched_at` equals the
version already in the sanitized table, so a document is re-projected only after
it is re-enriched. A change to the projection itself, such as the new `uri`
column, a different `fields` list or a stricter gate, therefore reaches existing
rows only with `--reproject`, which projects every document again. (`--force` is
its old name.)

A projected row is embedded only when its `one_line`, the text the embedding is
computed from, differs from the stored one. Otherwise the sync rewrites only the
projected columns and keeps the stored embedding, and only when they differ: the
update's `WHERE` compares the stored columns with the new ones. A re-projection
that adds `uri` therefore costs no embedding calls, and one that changes
summaries (a stricter gate) embeds only the rows whose summary changed.

That comparison is also what makes `--reproject` resumable. Each batch commits
as it goes; run again after an interruption, the rows already done match, so
they are neither embedded nor written, and are counted as skipped. `--limit`
counts those rows too, so a resumed run with a limit spends it on rows already
done: resume without one, or split the work by `--source`.
