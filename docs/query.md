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

The policies are in-code defaults, `query.SANITIZED_POLICY` and
`query.SENSITIVE_POLICY`. For now the caller passes the policy to the store
with each query; the tier config (DIL-637, step 637a) will bind each tier to
its policy, so a caller can no longer choose one. A spec that uses a filter, `group_by` dimension or order
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
| (`semantic`) | vector distance, then `id` | offset, stopping after 200 rows (`query.SEMANTIC_DEPTH`) |

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

## The index presets

`index_query`'s tools (`action_items`, `due_soon`, `waiting_on`, `by_domain`,
`summary`, `stats`) are fixed specs over `SanitizedStore.query`, `aggregate` and
`get`. The tier's SQL therefore lives in one place. They run under
`index_query.PRESETS`: the sanitized policy plus the `due` filter and a count by
`suggested_disposition`, which only these fixed queries use. Its row cap is
`index_query.MAX_LIMIT + 1` (101): the MCP surface clamps a page to `MAX_LIMIT`
and fetches one row beyond it to learn whether another page exists.

The index reads through `CORPUS_INDEX_DATABASE_URL` as a read-only role. It opens
`SanitizedStore(..., read_only=True)`, which runs no DDL (even `CREATE ... IF NOT
EXISTS` needs `CREATE` on the schema) and marks its transactions read-only.
