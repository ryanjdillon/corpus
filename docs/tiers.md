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
        place_name: drop
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

A projected tier's free-text columns are `one_line`, `action_summary`,
`organizations` and the place and carrier names in `facts`
(`sanitize.REDACTED_COLS`): what a model wrote about the mail.
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
| `place_name` | `keep`, `drop` | The name of a trip's origin and destination in `facts`; its country is kept either way (see [Typed facts](#typed-facts)). |

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
| `place_name` | `drop` | A place a message names can be someone's home, and no class finds a street address; the country is what the query model filters and counts by. |

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

## Typed facts

`facts` is a `jsonb` column holding the enrichment's typed facts (see
[Enrichment](enrichment.md#typed-facts)), so far `{"travel": {start, end,
origin, destination, carrier}}`. The query model filters and counts by them
(see [Query model](query.md#typed-facts)). It is projected like any column:
a tier whose `fields` name `facts` carries it, any other stores NULL. The
default `fields` (every column) include it.

The projection (`sanitize.project_facts`) checks the record against the
schema again, so the column holds only ISO dates and upper-case country
codes, which the query model compares and casts; a malformed record projects
as NULL. Dates and country codes are kept as they are: a date is not an
identifier, and a country is too coarse to locate anyone.

A place's name is not safe to keep. The model writes the place as the message
names it, and for a stay that can be someone's home: "Kari's place, Storgata 5,
0155 Oslo". Redaction finds the names of the enrichment's `people`, references
and what the `pii` scan knows, but no class finds a street address, and a
person the enrichment does not list ("Anna Berg's flat, 12 Rue de Rivoli")
passes whole. So by default (`place_name: drop`) a place keeps only its
country: `{"country": "NO"}`. A tier that sets `place_name: keep` gets
`{"name", "country"}`, the name redacted like the free text (a credential is
always removed; "Kari's cabin" follows `person_name`), and with it the risk of
an address. The query model reads only the countries, the dates and the
carrier, so the setting changes no filter or group.

The carrier is kept and redacted like the free text. A carrier that reads as
an address or a person rather than an operator is dropped: one with a
possessive ("Kari's", "Jonas'"), or a number beside a street word ("Storgata
5", "12 Rue de Rivoli", "1 Main Street"; `sanitize._NOT_A_CARRIER`). That is
a heuristic, not a recogniser: an address in another form, or a bare personal
name, still passes, and an operator with a possessive in its name is dropped.

The column is NULL when a row has no travel facts, and when the row is stubbed
at the gate.

`facts` is in `untrusted_fields`, and a stale row (see
[below](#serving-a-row-not-yet-re-projected)) serves it NULL, so no `facts`
filter or group reaches the facts of a row the current policy has not
projected. A reader on a table the sync has not yet given the column reads it
as a typed NULL, so the `facts` filters still run.

The digest covers `facts` only through `fields`, and `place_name` only when
`fields` include `facts`: a tier whose file lists its `fields` without `facts`
keeps its digest and is not re-projected. A tier on
the default `fields` gains the column, so its digest changes and every row is
stale after the upgrade. Its `one_line`s do not change, so re-projecting embeds
nothing, and the next sync re-projects every row whatever its `--limit`, in
each source store (see
[Rolling out a policy change](#rolling-out-a-policy-change)).

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
an upgrade re-projects them all.

The digest covers the policy, not the code that applies it. A fix to the
redaction code that changes its output (a new pattern, a corrected match) must
bump `REDACTION_VERSION` in the same change: that is the only way it reaches
rows already projected, as nothing else in the digest moves. The column is not a projected column: no
result returns it.

`--reproject` projects every document again even under an unchanged policy:
for a change the digest does not cover, such as a new column like `uri`.
(`--force` is its old name.)

### Text now, embeddings later

A sync runs in two phases.

1. **The text phase** projects each new, re-enriched or stale row and writes
   its columns with the current `policy_hash`. A new or re-enriched row whose
   `one_line` changed is embedded here, as it always was. A row re-projected
   only because of the policy (or `--reproject`) is never embedded here: its
   columns are rewritten and its stored vector kept.
2. **The embedding phase** then re-embeds the rows the text phase left
   *embed-stale*, newest first, at most `CORPUS_SYNC_REEMBED_LIMIT` (default
   500) a run, or `--reembed-limit N` (0: all).

Each row also stores `embedded_policy_hash`: the policy its vector was computed
under. A row is embed-stale when that differs from its `policy_hash`, that is,
when its text was re-projected since its vector was computed and the
`one_line` changed. The text phase sets it as follows:

- `one_line` unchanged and the vector current: the vector still holds, so it
  becomes the new digest and the row is not stale. A re-projection that adds
  `uri`, or a policy change that leaves a summary as it was, costs no
  embedding.
- `one_line` changed: it keeps the policy the vector was computed under, so the
  row is stale. Should the policy later be reverted to that one, the
  projection writes the same text again and the row is current, with no
  re-embedding. A forced re-projection that changes the text under an
  unchanged policy clears it instead, since a value equal to `policy_hash`
  would read as current.
- A new or re-enriched row is embedded under the current policy, so a row's
  `embedded_policy_hash` always names a policy applied to its current
  enrichment. A re-enriched row that was embed-stale is embedded even if its
  text did not change, as its vector came from an older text.

The embedding phase works only on rows projected under the current policy (a
row still pending would be re-projected, and its new vector thrown away), and
stores a vector only while the row still holds the text it was computed from.
It takes the tier's embed-stale rows whatever `--source` says: a policy change
makes every source's rows stale at once, and the cap bounds the embedder's
load per run either way.

One sync re-embeds a tier at a time. Several syncs can share a tier (one per
source, scheduled together), and each would otherwise take the same newest
embed-stale rows and embed them, with all but one write discarded. The
embedding phase first takes a session-level advisory lock keyed on the tier's
table (`pg_try_advisory_lock(hashtext('<schema>.messages reembed'))`, on the
sync's own connection). A sync that finds it held skips the phase rather than
wait: it embeds nothing, its counts carry `reembed_skipped 1`, and `corpus
sync` and `corpus pipeline` print `re-embedding skipped: another sync of this
tier holds the re-embed lock`. Its rows are left for the next sync. A
scheduled sync that skips still exits 0, but a `corpus sync` given an explicit
`--reembed-limit` exits 1 when its phase is skipped, so a requested drain never
fails silently. `corpus sync --reembed-wait` waits for the lock instead (a
blocking `pg_advisory_lock` on the same key), first printing `waiting for the
re-embed lock held by another sync of this tier…`, and is never skipped. The
lock is released when the phase ends, on error too, and with the connection should
the sync die. The text phase takes no lock, so it never waits on an embedder,
and the compare-and-set on each stored vector still guards a re-projection
that races a re-embedding.

The text phase is cheap. Projecting a row is the `pii` scan and the entity
matching on three short enrichment fields, about half a millisecond, so an
archive of 50,000 rows is re-projected in well under a minute of CPU plus one
`UPDATE` a row. That is why it has no cap: every placeholder a policy change
creates is replaced on the next sync. Embedding is the expensive part (about
200 ms a text on a CPU embedder, hours for the whole archive), and is the part
the cap spreads over runs.

`--limit` caps only the text phase's embedding calls, for new rows and
re-enriched rows whose `one_line` changed. It bounds the embedder's load per
run, so it counts rows queued for embedding, not rows scanned: the read order
is stable, and a scan cap would re-read the same synced prefix on every run.
A new row past the cap is not written and is counted as `capped`; it has no
stored row, so nothing is served for it, and the next run takes it, newest
first. A re-enriched row past the cap gets its new text now, keeps its old
vector, and is left embed-stale (out of vector ranking) for the embedding
phase, like a policy re-projection. Every other row keeps its vector, so
`--limit` never holds it back: a capped scheduled sync
(`corpus pipeline --limit 400`) still replaces every placeholder a policy
change created.

Each run reports its counts: `scanned`, `synced` (rows written), `skipped`,
`reprojected` (rows rewritten only because of the policy or `--reproject`),
`embedded` (vectors stored, in either phase), `capped` (new rows `--limit`
left for a later run), and what is left after it:
`pending` (rows still projected under an older policy, served as
placeholders; non-zero only for rows the run did not scan, such as another
source's, or after one that failed part-way) and
`embed_stale` (rows awaiting a new vector). `corpus sync` prints them;
`corpus pipeline` prints its sync stage's. `SanitizedStore.backlog(digest)`
returns the last two, for a gauge.

### Serving a row not yet re-projected

The index knows its tier's projection policy (`serving.open_index_store`
passes `Tier.projection.policy` to the store), and serves a row whose
`policy_hash` is not that policy's digest (another policy's, or none)
degraded until the sync re-projects it:

- `one_line` is `[pending redaction] <domain> item`;
- `action_summary`, `organizations`, `facts`, and every column outside the
  policy's `fields`, are NULL.

The store reads `messages` through a subquery that applies this
(`sanitized_store._served`), so every read path sees only the degraded
columns: `query`, `search`, `aggregate`, `get` and the `index_*` presets, and
with them every filter, keyword match and group. A keyword or an
`organization` filter cannot reveal what a stale row's stored text says. Nor
can `search`: a placeholder row is never ranked by its stored embedding, which
was computed from the text the placeholder hides (see below). A reader on a table without the `policy_hash` column serves every
row degraded. The sync, which serves nothing, opens the store without a
policy and reads the table as it is.

The text gate is on `policy_hash` alone: an embed-stale row is served with its
current, redacted text. Its vector, though, was computed from an earlier
`one_line`, which may hold what the policy now redacts (a name, say). Ranked
by it, a semantic query for that name would surface the row and link the name
to its date, domain, organizations and sender domain. The same holds for a
placeholder row, whose vector is of its pre-policy text. So a serving store's
semantic query ranks only fully current rows: `policy_hash` is the serving
policy's digest **and** `embedded_policy_hash IS NOT DISTINCT FROM
policy_hash`. Any other row (embed-stale, projected under another policy, or
with no hash at all) is left out of `search` until the sync brings it current,
while every filter, keyword match, `get` and aggregate still returns it, with
its current text or its placeholder. No row ranks by a vector of text the
serving policy would not show, whether before the first text phase after an
upgrade or after a later policy change; since the text phase takes seconds,
the cost is mostly the embedding backlog. On a table without
`embedded_policy_hash`, the second half reads true: its vectors were all
stored by a sync that embedded every row it wrote. The sync's own store, which
has no projection policy and serves nothing, applies only the second half.

The exclusion is a `WHERE` on the semantic query, not a NULLed embedding
column. An expression around `summary_embedding` hides it from the planner, so
an HNSW index on the column could no longer order the scan; with the filter it
still does (an `Index Scan … Order By` with the predicate as its `Filter`,
which `test_a_served_semantic_query_is_ordered_by_the_vector_index` checks).
As with any filtered HNSW scan, the index ranks `hnsw.ef_search` candidates
before the filter, so a large embed-stale backlog thins semantic pages until
it drains.

### Rolling out a policy change

A policy change makes every row stale at once, and most stale rows need a new
embedding (replacing names changes most `one_line`s). To roll one out, or to
upgrade from a release before `policy_hash`:

1. **Roll the image** with the new tiers file. Until the next sync, every row
   is served as a placeholder, never with the free text of the old policy,
   and `search` returns nothing.
2. **Run `corpus sync --reproject`** (or wait for the scheduled sync, which
   re-projects every stale row too). The text phase replaces every placeholder
   from its source store in one run, in minutes, with no embedding calls. It
   reads the documents from the source store the sync is configured with
   (`CORPUS_DATABASE_URL` and `CORPUS_DB_SCHEMA`), so a sanitized tier fed from
   several source stores (one database or schema per mailbox, say) needs one
   run per source store, each with that store's settings: run each mailbox's
   own sync job with `corpus sync --reproject`. A run against one store
   re-projects only that store's rows. Leave out `--limit`, which caps a
   `--reproject` run too. The embedding phase, by contrast, covers the whole
   tier, so the drain in step 3 runs once per tier.
3. **Let the embeddings catch up.** Each sync, and each `corpus pipeline` sync
   stage, re-embeds up to `CORPUS_SYNC_REEMBED_LIMIT` rows; at 500 a run, the
   rows of a 50,000-row archive whose summaries changed take up to 100 syncs.
   Until a row is reached, it is served with its current text but left out of
   `search` (it stays reachable by keyword and filter), so drain the backlog
   promptly: run `corpus sync --reembed-limit 0 --reembed-wait` in a window
   when a GPU embedder is available, rather than letting the capped runs
   spread it over days. `--reembed-wait` makes the drain wait for a scheduled
   sync that is re-embedding at the time instead of skipping; without it, a
   drain that finds the lock held exits 1.

A table from before `embedded_policy_hash` gains the column the first time a
sync opens it, set to each row's `policy_hash`: every vector the earlier code
stored was computed from the row's stored `one_line`. A row with no
`policy_hash` therefore gets none either, and the copy skips it (it already
reads NULL), so on a table never hashed the migration rewrites no row while
it holds the `ALTER`'s exclusive lock; the text phase then marks it current
if its `one_line` comes out the same, and embed-stale if it changes. The copy
runs once, when the column is added, under a lock that serializes two syncs
opening the store at the same moment.

### Resuming

Rows whose written columns are unchanged are not rewritten: the update's
`WHERE` compares the stored columns with the new ones. With the digest, that is
what makes a re-projection resumable. Each batch commits as it goes; run again
after an interruption, the rows already done carry the new digest, so they are
neither written nor embedded, and are counted as skipped. `--limit` counts
only rows it embeds, so a resumed run spends none of it on rows already done.
The embedding phase
resumes the same way: a stored vector clears the row's staleness, so the next
run takes the rows after it.
