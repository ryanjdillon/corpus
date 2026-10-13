# Tiers

A storage tier is a store plus the policy around it: the MCP server that exposes
it, the principals the gateway lets reach that server, the query policy the
server enforces and, for a downgraded tier, the projection that derives its rows
from a source tier. Each tier is defined once, under `tiers:` in the **config
file**, and
`corpus serve <tier>`, `corpus sync [--tier <tier>]` and the sync stage of
`corpus pipeline [--tier <tier>]` take everything from it (`tiers.tier(name)`).

Set `CORPUS_CONFIG_FILE` to the file's path. Without it, or in a file without
`tiers:`, the [built-in default](#the-default) applies, which is the
configuration corpus had before the file existed, so a deployment that sets no
file is unchanged, except that the sanitized tier [redacts](#redaction) its free text
rather than only stubbing high-sensitivity rows. The same file holds the sources, under `sources:` (see
[Source configuration](fetchers/config.md)). `CORPUS_TIERS_FILE`, the file's name
from when it held only tiers, still works and logs a deprecation warning;
setting it and `CORPUS_CONFIG_FILE` to different paths is an error.

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
    projection:
      from: sensitive
      redact:
        organization: keep
        person_name: role
        reference_id: mask
        financial_id: mask
        email_address: drop
        phone: drop
      sensitivity_gate: high
      gate_action: redact
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
| `projection.redact` | What each entity class becomes in the projected free text; see [Redaction](#redaction). A class left out takes its default. |
| `projection.sensitivity_gate` | The `sensitivity_level` (`none`, `low`, `medium`, `high`) at or above which `gate_action` applies (default `high`). |
| `projection.gate_action` | At or above the gate: `redact` the free text like any other row (default), or `stub` it, withholding `one_line`, `action_summary` and `organizations` behind a line naming only the domain and the action. |
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

The file is checked when `serve` or `sync` starts (its `sources:` against their
schema too), and an invalid file stops the command with an error that names the
problem. Nothing is ignored:

- an unknown key, at any level, is an error;
- every tier needs a complete `query` policy, since a tier without one would
  otherwise be permissive;
- a filter, `group_by` dimension or order that the query model does not know is
  an error, and so is a `search_depth` outside 1–200;
- `projection.from` must name another tier in the file, and that tier must be a
  source tier;
- `projection.fields` must be columns of the sanitized table;
- `projection.redact` takes only the six classes below, each only the actions
  listed for it (`person_name: mask` is an error), and `gate_action` only
  `redact` or `stub`;
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
| Projection | (source tier) | from `sensitive`, every column, the [default redaction](#the-default-policy), gate `high`, `gate_action: redact` |

`tiers.DEFAULT` is itself a valid config file. To start a file from it, encode it
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

## Redaction

A projected tier's free-text columns are `one_line`, `action_summary` and
`organizations` (`sanitize.REDACTED_COLS`): what a model wrote about the mail.
Before they are stored, each entity class in them is kept, masked, replaced or
dropped, as `projection.redact` says. The policy applies to every row, below
the gate as well as at it.

**Credentials are always removed.** Whatever the policy, including every class
`keep` and `gate_action: stub`, a credential the `leaks` rules find (an API key,
a GitHub or Slack token, a JWT, a private key) becomes `[REDACTED:<type>]`, the
placeholder `redact` uses for egress, e.g. `[REDACTED:github_token]`. A summary
should never hold one, but a model can copy a token from the mail; when high
rows were stubbed that was hidden as a side effect, and now that they are
redacted it is removed on purpose. It is not a class of `redact`, so no file
can keep it, and no match the policy keeps (an email address, say) can cover
one.

| Class | Actions | What it covers |
| --- | --- | --- |
| `organization` | `keep`, `drop` | The enrichment's `organizations`: the column, and their names in the text. |
| `person_name` | `keep`, `initial`, `role`, `drop` | The names in the enrichment's `people`. |
| `reference_id` | `keep`, `mask`, `drop` | The values in the enrichment's `references`: booking codes, order, case and ticket numbers. |
| `financial_id` | `keep`, `mask`, `drop` | What the `pii` scan finds: a card number, an IBAN, a US bank account or SSN, a crypto wallet, a Norwegian national id (see [its limits](#what-financial_id-does-not-find)). |
| `email_address` | `keep`, `drop` | Email addresses, found by the `pii` scan. |
| `phone` | `keep`, `drop` | Phone numbers, found by the `pii` scan (an international prefix, or a phone word just before the number). |

The actions:

- `keep` leaves the value as written.
- `drop` removes it, and tidies the space it leaves.
- `initial` shortens a name to its initials: "Kari Nordmann" becomes "K. N.",
  "Kari" alone "K.".
- `role` replaces a name with the person's `role` from the enrichment, with an
  article: "a friend", "an accountant". A person enriched before roles existed
  has the role `unknown`, which reads "someone".
- `mask` replaces a value with its kind and at most its last two letters or
  digits: `[booking ••••42]`, `[iban ••••47]`, `[credit_card ••••11]`. A
  reference takes its `kind` (`reference` for one outside the schema); a `pii`
  match takes its type. A value with fewer than eight letters and digits is
  masked whole, `[order ••••]`, so the tail never shows more than a quarter of
  a value. The tail lets a reader tell two bookings apart without learning
  either code.

A column that redaction leaves empty is stored as NULL, and `organizations`
keeps only the entries that are not.

### The default policy

| Class | Default | Why |
| --- | --- | --- |
| `organization` | `keep` | Who the mail is from or about is most of what makes a summary useful, and an organization is not a private person. |
| `person_name` | `role` | The role carries the context ("your landlord asks…"); the name identifies a private person. |
| `reference_id` | `mask` | A code is only an identifier; the kind and tail keep enough to match it to the mail. |
| `financial_id` | `mask` | As for references; the full number never leaves the sensitive tier. |
| `email_address` | `drop` | An address identifies a person and adds nothing to a summary. |
| `phone` | `drop` | As for addresses. |

The rule behind it: keep personal data where it carries context, and mask or
drop it where it is only an identifier. This is the default of any projected
tier whose file entry has no `redact`; the source tier (`sensitive`) has no
projection, so none applies there. The projection before this policy existed is
`gate_action: stub` with every class `keep` (`sanitize.STUB_POLICY`).

### How values are matched

- **Names and references come from the enrichment.** `people[].name` and
  `references[].value` are the authoritative list of what to find in the text;
  the summaries are not searched for name-like words. Financial ids, email
  addresses and phone numbers come from the same `pii` detectors the egress
  redactor uses.
- **Names** match case-insensitively, as whole words, as the full name and as
  each of its words, so "Kari" and "NORDMANN" are found when the person is
  "Kari Nordmann", and "Karianne" is not. Titles (Dr, Mr, Fru…) are not
  matched on their own, nor are one-letter words. A name word that is also an
  ordinary word ("Rose", "Bill") is redacted wherever it stands: the match errs
  towards hiding.
- **Names are matched folded** (`sanitize.fold`): both the name and the text
  are decomposed (NFKD), lose their accents, have `ø` → `o`, `æ` → `ae` and
  `œ` → `oe`, and are casefolded, so "bjorn as" and "Bjørn Ås" are one name, as
  are "Kjaer" and "Kjær". The match is mapped back to the original characters,
  so it is the text as written that is replaced, whatever the folding did to
  its length. One exception keeps folding from inventing ordinary words: a name
  word of fewer than four letters that folding changes ("Ås", which folds to
  "as") is matched alone only as it is spelled, ignoring case. Within the full
  name it is still matched folded. Organizations and references are matched
  folded in the same way.
- **References** match case-insensitively, as a whole value, with or without
  separators: `AB123442`, `ab-123442`, `AB 1234 42`, `AB - 123442` and
  `AB---123442` are one value. Between any two of its characters may stand up
  to two spaces, up to three punctuation marks, and up to two spaces again;
  never a line break, a letter or a digit, so a value is not assembled across
  a line or a word. A value with fewer than three letters and digits is not
  matched.
- **Credentials** are found first, by `leaks.scan_spans`; a match of any class
  that overlaps one is discarded.
- **Overlaps: the stricter action wins.** `drop` beats `mask` and the
  replacements (`initial`, `role`), which beat `keep`. A dropped match that
  overlaps a masked or replaced one takes the whole of both, so no part of
  either is left in the text. Matches of the same strictness are resolved by
  `redact.resolve_overlaps`: the leftmost, then the longest, wins, so a full
  name beats its first name. A kept match claims nothing: with
  `email_address: keep`, a name or reference inside an address is still
  redacted ("kari.nordmann@example.org" becomes "K..N.@example.org" under
  `person_name: initial`).

### What `financial_id` does not find

`financial_id` is only as good as the `pii` detectors, which favour precision:

- **A card or account number needs a context word beside it** (within 48
  characters): Presidio's English words (`card`, `visa`, `account`, `bank`…)
  or the Norwegian ones corpus adds (`pii._EXTRA_CONTEXT`): `kort`, `kortet`,
  `kortnummer`, `bankkort`, `kredittkort`, `betalingskort` for a card;
  `konto`, `kontoen`, `kontonummer`, `kontonr`, `bankkonto` for an account.
  "kort 4111 1111 1111 1111" is masked; "ordre 4111 1111 1111 1111" is not.
- **A Norwegian account number in its usual dotted form** (`1234.56.78903`)
  is not found at all: the US bank recognizer matches an unbroken digit run
  only.
- **A US SSN needs an English context word** (`ssn`, `social security`) beside
  it; a bare `536-90-4399` passes. A format-only SSN match is too noisy to
  trust (a nine-digit reading or id looks the same).
- **A password is not a class**, and the `leaks` rules find tokens with a
  recognisable shape, not a password a model copied into prose:
  "password: hunter2" passes under every policy.

### At the gate, and failing closed

At or above `sensitivity_gate`, `gate_action: redact` stores the redacted text,
like any other row, and `stub` stores the stub. Whatever the action, a row at
or above the gate whose enrichment has neither `people` nor `references` is
stubbed: with nothing to redact against, its summary cannot be shown to name
nobody. A record enriched before roles existed still has its `people` names
(role `unknown`), so it is redacted with them, and the `pii` detectors run on
it as on any row.

### Fields that are never projected

The enrichment's other free-text fields can still name people: `key_points`,
`appointments[].who`, `topics` and `locations` (see
[Enrichment](enrichment.md#people-references-and-role-based-summaries)), and
`abstract`. None is a column of the sanitized table, so no `fields` list can
project them. A model-written column added later must be added to
`sanitize.REDACTED_COLS`; a test fails until every untrusted column is either
redacted or header-derived (`sender_domain`, `thread_id`).

The redacted text is still third-party-derived, so its columns stay in
`untrusted_fields` and are checked for `instruction_like`. The placeholders
match none of the patterns, so redaction never raises the flag; an injection
that survives it is still flagged (see [MCP results](mcp-results.md#flags)).

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

## Re-projecting

`corpus sync` is incremental. It skips a document whose `enriched_at` equals the
version already in the sanitized table and whose row was projected under the
current policy. Each row stores `policy_hash`, a digest of the effective policy
(`sanitize.Policy.digest`: `fields`, the gate, `gate_action` and every
`redact` class, defaults included, plus `sanitize.REDACTION_VERSION`, which
changes when the redaction's output changes for the same policy). Changing any
of them in the tiers file re-projects every row on the next sync, with no flag.
Rows synced before the column existed have no digest, so the first sync after
an upgrade re-projects them all. The column is not a projected column: no
result returns it.

`--reproject` projects every document again even under an unchanged policy:
for a change the digest does not cover, such as a new column like `uri`.
(`--force` is its old name.)

### Serving a row not yet re-projected

The index knows its tier's projection policy (`serving.open_index_store`
passes `Tier.projection.policy` to the store), and serves a row whose
`policy_hash` is not that policy's digest (another policy's, or none)
degraded until the sync re-projects it:

- `one_line` is `[pending redaction] <domain> item`;
- `action_summary`, `organizations`, and every column outside the policy's
  `fields`, are NULL.

The store reads `messages` through a subquery that applies this
(`sanitized_store._served`), so every read path sees only the degraded
columns: `query`, `search`, `aggregate`, `get` and the `index_*` presets, and
with them every filter, keyword match and group. A keyword or an
`organization` filter cannot reveal what a stale row's stored text says. The
stored embedding still ranks the row in `search`, so it can be found by
meaning; it was computed from the old `one_line`, which leaks only as far as a
ranking can. A reader on a table without the `policy_hash` column serves every
row degraded. The sync, which serves nothing, opens the store without a
policy and reads the table as it is.

### Rolling out a policy change

A policy change makes every row stale at once, and most stale rows need a new
embedding (replacing names changes most `one_line`s). On a CPU embedder that is
hours of work, too much for a daily sync. So one sync re-projects at most
`CORPUS_SYNC_REPROJECT_LIMIT` rows (default 500; 0 means no cap) that are stale
only by policy:

- **New and re-enriched rows come first and are never capped**, so the tier
  stays current while the change rolls out.
- The stale rows then fill the cap, in the reader's stable order (newest mail
  first), so each run takes the next ones and the change spreads over days.
- The rest are left for later runs. The sync reports them (`N rows still
  projected under an older policy`), and they are served degraded until
  reached (see [above](#serving-a-row-not-yet-re-projected)), never with the
  free text of the old policy.

Rows synced before the digest existed count as stale too, so after that
upgrade every row is re-projected this way: at 500 a run, an archive of 50,000
rows takes about 100 daily syncs, and until its turn each row is served
degraded, with no summary. To apply a change at once, run
`corpus sync --reproject` in a window when a GPU embedder is available: it
projects every row, with no cap.

Either way, a projected row is embedded only when its `one_line`, the text the embedding is
computed from, differs from the stored one. Otherwise the sync rewrites only the
projected columns and keeps the stored embedding, and only when they differ: the
update's `WHERE` compares the stored columns with the new ones. A re-projection
that adds `uri` therefore costs no embedding calls, and one that changes
summaries (a stricter gate, a different `person_name`) embeds only the rows
whose summary changed. The rest have only their columns and digest rewritten.

That comparison, with the digest, is also what makes a re-projection
resumable. Each batch commits as it goes; run again after an interruption, the
rows already done carry the new digest, so they are neither embedded nor
written, and are counted as skipped. `--limit`
counts those rows too, so a resumed run with a limit spends it on rows already
done: resume without one, or split the work by `--source`.
