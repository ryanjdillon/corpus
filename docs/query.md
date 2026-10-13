# Query model

One structured query shape, `query.QuerySpec`, reads either storage tier. A tier's
`QueryPolicy` says which parts of it that tier allows. The store turns an allowed
spec into SQL from a fixed table of expressions. The spec is a `msgspec` struct,
so its JSON Schema (`msgspec.json.schema(QuerySpec)`) can serve as a tool's input
schema, and `query.parse_spec` validates decoded JSON into it.

```json
{
  "filters": {
    "domain": ["travel", "work"],
    "sent_at": {"since": "2025-01-01", "until": "2026-01-01"},
    "organization": "acme"
  },
  "order": "newest",
  "limit": 50
}
```

## The spec

| Field | Meaning |
| --- | --- |
| `filters` | What to match. Every filter set is applied (AND). |
| `text` | A keyword, matched case-insensitively in the tier's text fields: `one_line` and `action_summary` on the sanitized tier, the subject and the body on the sensitive tier. |
| `semantic` | A query embedding (a list of floats). Rows are ordered by vector distance to it. The caller embeds the text; the store never does. |
| `order` | `priority`, `newest`, `oldest` or `deadline`. Defaults to `newest`. Leave it unset with `semantic`. |
| `limit` | Rows per page. A larger value is reduced to the policy's `max_limit`. |
| `cursor` | The previous page's opaque cursor. The tool decodes it and passes the position to the store. |
| `group_by` | Dimensions to count by, instead of returning rows. |

Filters:

| Filter | Matches |
| --- | --- |
| `domain`, `category`, `transactional_type`, `importance`, `waiting_on`, `action_type`, `suggested_disposition` | One enrichment enum value, or a list meaning any of them. An empty list is refused. |
| `requires_action`, `time_sensitive` | A boolean. |
| `sender_domain`, `account`, `source`, `label` | An exact value. |
| `organization` | Any extracted organization containing the text, case-insensitively. `%` and `_` match literally. |
| `sent_at`, `deadline` | `{since, until}`, half-open (`since <= value < until`), with either end optional. Each end is an ISO date (midnight UTC), an ISO datetime (UTC if no zone is given), or a duration before now such as `14d`, `36h` or `2w`. A relative end is resolved when the query runs. Send times are compared as instants, whatever offset the email header carried. |
| `max_sensitivity` | `sensitivity_level` at or below this level (`none` < `low` < `medium` < `high`). A row with no level never passes. |
| `due` | Time-sensitive, or carrying a deadline. Only the index's preset tools use it. |

An unknown field, filter or value is refused when the spec is parsed.

`deadline` is a date with no time of day, so its bounds resolve to UTC dates
and a day matches when any part of it falls in the range. `since` becomes the
day it falls on (`14d` is the date 14 days ago). An `until` at exactly midnight
becomes that day, which is then excluded; any later `until` becomes the next
day, so its own day is included. So `{"until": "2025-03-10"}` excludes a deadline on 10 March and
`{"until": "2025-03-10T12:00"}` includes it. Comparing the date column to the
timestamp directly would truncate the bound and drop its day.

## What each tier allows

| | Sanitized (`corpus-index`) | Sensitive (`corpus`) |
| --- | --- | --- |
| Filters | `domain`, `category`, `transactional_type`, `importance`, `requires_action`, `time_sensitive`, `waiting_on`, `action_type`, `suggested_disposition`, `sender_domain`, `organization`, `account`, `source`, `sent_at`, `deadline`, `max_sensitivity` | `label`, `account`, `source`, `sent_at` |
| `group_by` | `domain`, `sender_domain`, `organization`, `month`, `category`, `importance` | `label`, `month`, `account` |
| Orders | `priority`, `newest`, `oldest`, `deadline` | `newest`, `oldest` |
| `text`, `semantic` | yes | yes |
| Most rows (or groups) per call | 100 | 100 |

That is the default; each tier's policy is its `query` block in the config file
(see [Tiers](tiers.md)), and without a file the defaults are
`query.SANITIZED_POLICY` and `query.SENSITIVE_POLICY`. The server takes the
policy from its tier and passes it to the store with each query, so a caller
cannot choose one. A policy also sets `search_depth`, how deep a vector-ordered
query pages (at most and by default 200). A spec that uses a filter, `group_by` dimension or order
its policy does not allow is **refused** with a `ValueError` that names it, never
quietly ignored. A caller that believes it filtered must never receive the
unfiltered result. The same holds for a dimension the store has no expression
for, whatever the policy says.

The SQL comes only from each store's `Layout`, which maps every dimension to a
fixed SQL expression: `sanitized_store.layout` for the `messages` columns and
`store.layout` for the documents table's JSONB `meta`. A spec selects among those
expressions and never supplies an identifier, and every value it carries is a
bound parameter.

## Orders and paging

A store returns a `Page` with `rows`, `next_cursor` and `total`. `next_cursor`
is the position the next page resumes at, or `null` on the last page. The tool
encodes it as the opaque cursor (see [MCP results](mcp-results.md#paging)).
`total` is not counted for row queries. The store fetches one row beyond the
page to learn whether another page exists.

| Order | Sorts by | Pages by |
| --- | --- | --- |
| `newest` | `sent_at` descending, then `id` descending; undated rows last | keyset on `(sent_at, id)`: position `[sent_at key, id]` |
| `oldest` | `sent_at` ascending, then `id` ascending; undated rows last | keyset on `(sent_at, id)` |
| `priority` | importance, then time-sensitive, then `sent_at` descending, then `id` | offset |
| `deadline` | `deadline` ascending (none last), then the priority order | offset |
| (`semantic`) | vector distance, then `id` | offset, stopping at the policy's `search_depth` (at most 200, `query.SEMANTIC_DEPTH`) |

Every order ends in `id`, so it is total and a page boundary can neither skip
nor repeat a row. Keyset pages cannot shift when new mail arrives. A position
whose key is not a valid sort key (on the sanitized tier, an ISO timestamp or
`infinity`/`-infinity`) or whose id is not a string is refused with a
`ValueError` before any SQL runs. The sensitive tier's keyset sorts the stored
`sent_at` string, which keeps the header's offset, so two rows whose times
differ only by offset can page slightly out of true time order. Each row is
still returned exactly once. Filters, months and `min_sent_at`/`max_sent_at`
cast it to a timestamp and are exact. Offset pages
can, because the ranking fields change on re-enrichment. The 200-row cap on
semantic pages is how many rows one HNSW index scan ranks (`hnsw.ef_search`).
Both stores set that value for each vector query, so an HNSW index cannot
quietly end a page at pgvector's default of 40.

## Counting by group

With `group_by`, a store's `aggregate` returns an `Aggregate`. `groups` holds one
entry per combination of the named dimensions, in dimension order (no value
last), each with `count`, `min_sent_at` and `max_sent_at`. `total` counts each
matching item once. `truncated` is set when there are more groups than the
limit. `month` is `YYYY-MM` in UTC. Grouping by `organization` counts an item
once under each organization it names, and once under no value if it names
none. `order` and `semantic` do not apply to a count and are refused with it.

```json
{"filters": {"domain": "travel", "sent_at": {"since": "2025-01-01", "until": "2026-01-01"}},
 "group_by": ["month"]}
```

## The MCP tools

`serving.build_mcp(tier)` builds one MCP server per tier (`corpus serve
sensitive`, `corpus serve sanitized`). The tier (`tiers.tier(name)`) fixes the
server name, the store and the policy; no tool takes a policy from its caller.
What a tier serves follows from its kind: a source tier serves the documents
table and the `corpus_*` presets, a projected tier its `messages` table and the
`index_*` presets. Every tier serves four
generic tools over this query model:

| Tool | Arguments | Returns |
| --- | --- | --- |
| `query` | `spec`: `filters`, `text`, `order`, `limit`, `cursor` | One page of rows. |
| `search` | `text` (at most 2000 characters), and an optional `spec`: `filters`, `text`, `limit`, `cursor` | One page of rows ranked by meaning. The server embeds `text` with the model the tier's vectors came from, at most 50 rows per page and 200 in all. |
| `aggregate` | `spec`: `filters`, `text`, `limit`; and `group_by`, a top-level argument beside `spec` | One row per group (see [Counting by group](#counting-by-group)), `total`, and `truncated`. |
| `get` | `id` | 0 or 1 rows. |

`aggregate` takes the dimensions as its own `group_by` argument, not inside
`spec`. A `group_by` inside `spec` is refused, as it is on `query` and
`search`. The travel count from [Counting by group](#counting-by-group), as
`aggregate` arguments:

```json
{"spec": {"filters": {"domain": "travel", "sent_at": {"since": "2025-01-01", "until": "2026-01-01"}}},
 "group_by": ["month"]}
```

The `spec` argument's JSON Schema is the `QuerySpec` schema cut down to what the
tier allows: only its filters, its orders, and its group-by dimensions are
listed. A spec that uses anything else is still refused when called, before
the store is opened. A field the tool does not take (`order` on `search`, a
`cursor` on `aggregate`, a `semantic` vector anywhere) is refused too.

Rows are the tier's projection: `sanitized_store.COLS` on the sanitized tier
(with `one_line`, `action_summary` and `organizations` redacted, see
[Tiers](tiers.md#redaction); and `uri`, an https link to the message, or null,
see [Tiers](tiers.md#the-message-link-uri)),
and `id`, `subject`, `from_addr`, `sent_at` and `label` on the sensitive tier
(`get` there returns the full `content` and `meta`).

A relative bound (`"since": "14d"`) is resolved once, on the first page, and
the resolved time travels in the cursor, so a window does not slide while a
caller pages. A later page checks it against its own arguments: it must lie
within an hour before the window's current start, or the cursor is refused
(see [MCP results](mcp-results.md#paging)). A cursor is also bound to its tier:
one from the sanitized tier's `query` is refused by the sensitive tier's.

`search` refuses a `text` longer than `serving.SEARCH_TEXT_MAX` (2000
characters) before embedding it; the input schema states it as `maxLength`.
The embedding is a call the caller waits on, so it makes a single attempt
within `CORPUS_EMBED_QUERY_TIMEOUT` (default 15 s) and an unavailable embedder
fails the call at once. Ingest and the sanitized sync keep the batch policy:
four attempts with backoff, each within `CORPUS_EMBED_TIMEOUT`.

### Presets

Each tier also keeps the fixed tools its consumers already call, with the same
names, arguments and results as before:

- **Sanitized** (`corpus-index`): `index_action_items`, `index_due_soon`,
  `index_waiting_on`, `index_by_domain`, `index_summary`, `index_stats`. Each
  is a fixed spec over `SanitizedStore.query`, `aggregate` and `get`. They run
  under `serving.index_presets_policy`: the sanitized policy plus the `due`
  filter and a count by `suggested_disposition`, which only these fixed
  queries use. Its row cap is `serving.MAX_LIMIT` (100), the most a preset
  page returns; the store fetches the one row beyond a page itself, to learn
  whether another page exists.
- **Sensitive** (`corpus`): `corpus_query` (a `newest` spec over
  `DocumentStore.query_spec`, whose keyset position the cursor carries),
  `corpus_search` (vector search, its `query` bounded like `search`'s `text`),
  `corpus_get` and `corpus_stats`. `corpus_query`'s `after` and `before`, like
  `since`, may be a duration before now; each is resolved on the first page
  and frozen in the cursor, as the generic tools' relative bounds are.

The index reads through its tier's reader DSN (`CORPUS_INDEX_DATABASE_URL` by
default) as a read-only role. It opens
`SanitizedStore(..., read_only=True)`, which runs no DDL (even `CREATE ... IF NOT
EXISTS` needs `CREATE` on the schema) and marks its transactions read-only.
