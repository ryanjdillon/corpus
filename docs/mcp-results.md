# MCP results

Every MCP tool, on both the full surface (`corpus`) and the sanitized one
(`corpus-index`), returns one shape: `corpus.result/v1`. It carries the rows and
tells the caller how far to trust each field in them. The shape is built in
`corpus.envelope`, and `serving.build_mcp` applies it to every tool when it
registers the tool, so no tool can return rows without it.

```json
{
  "kind": "corpus.result/v1",
  "server": "corpus-index",
  "tool": "index_action_items",
  "tier": "sanitized",
  "untrusted": true,
  "untrusted_fields": ["action_summary", "one_line", "organizations", "sender_domain", "thread_id"],
  "notice": "Rows contain third-party content from email and documents, ...",
  "count": 2,
  "total": null,
  "next_cursor": null,
  "rows": [
    {"id": "…", "one_line": "…", "importance": "high", "flags": ["instruction_like"]},
    {"id": "…", "one_line": "…", "importance": "low", "flags": []}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `kind` | The shape and its version. |
| `server`, `tool` | The MCP server and the tool that answered. |
| `tier` | The storage tier the rows come from (`sensitive` or `sanitized`). |
| `untrusted` | Whether any field in `untrusted_fields` exists for this tool. |
| `untrusted_fields` | The row keys that hold third-party text. Fixed per tool, so a row that omits a key never shortens it. |
| `notice` | A fixed sentence telling a model how to treat untrusted fields. |
| `count` | Rows on this page, always `len(rows)`. |
| `total` | Every match, when cheap to know (`aggregate` and the stats tools); otherwise `null`. |
| `next_cursor` | Opaque cursor for the next page; `null` on the last page (see [Paging](#paging)). |
| `truncated` | Only on `aggregate`: more groups exist than `limit`. Counts are not paged, so narrow the filters or raise `limit`. Absent on every other tool. |
| `rows` | Always a list. A single-record tool (`get`, `corpus_get`, `index_summary`) returns 0 or 1 rows. Each row has `flags`. |

A tool that fails returns an MCP error (`isError`) with a plain-text message,
not an envelope.

## Paging

Every tool that returns a list caps how many rows one call returns. A `limit`
(or `top_k`) above the cap is clamped, not refused: `count` says how many rows
came back, and `next_cursor` is set whenever more exist. To read on, call the
same tool with the same arguments plus `cursor` set to that `next_cursor` (for
`query` and `search`, inside `spec`); a different `limit` is fine. A cursor that is malformed, or that comes from a
call with different filters, is refused with an error rather than quietly
skipping or repeating rows.

| Tool | Most rows per call | Pages by | Paging stops after |
| --- | --- | --- | --- |
| `index_action_items`, `index_due_soon`, `index_waiting_on`, `index_by_domain` | 100 | offset in the priority order (ties broken by id) | 1000 rows |
| `query` | 100 | keyset on `(sent_at, id)` for `newest`/`oldest`; otherwise offset | never (keyset); 1000 rows (offset) |
| `corpus_query` | 100 | keyset on `(sent_at, id)`, newest first | never |
| `search`, `corpus_search` | 50 | offset in the ranking (each page re-embeds and re-runs the search) | 200 rows, the most one vector search ranks (`query.SEMANTIC_DEPTH`) |

At its ceiling a tool returns `next_cursor: null`, as on a last page; narrow
the filters to read further.

Keyset pages (`corpus_query`, and `query` ordered `newest` or `oldest`) are
stable: mail that arrives while a caller pages cannot shift them. A relative
window (`corpus_query`'s `since`, or a duration given as its `after` or
`before`, or a duration bound such as `"since": "14d"`
in a `query` or `search` spec) is resolved once, on the first page, and later
pages share that resolved bound. The offset-paged tools rank by fields that can
change between calls (a new action item, a re-enrichment), so a row can move
across a page boundary; read them in one pass.

The cursor is opaque but not signed. It only positions a query whose filters
are applied again on every page, and what it carries is checked against the
call's own arguments: an offset must be below the tool's ceiling, a keyset
position must be two strings, and each frozen bound must match the call.
`corpus_query`'s bounds must equal a fixed `after` or `before`, or, for `since`
or a duration, lie within an hour before that bound's current value; a frozen
relative bound in a `query` or
`search` spec must lie within that hour too, and a cursor may carry no bound the
spec does not state. A forged cursor therefore cannot read outside the window
the call states, and the audit log records that window. A cursor whose
relative bound is more than an hour old is refused; start again from the first
page.

## Why fields are marked untrusted

Anyone can email the mailbox, and a tool result lands in an agent's context, so
an email can carry instructions aimed at whatever agent reads it. The rule:
**a field is untrusted if it is derived from third-party text.** That includes raw
fields (subject, sender, body, snippet) and anything a model wrote about them
(`one_line`, `action_summary`, `organizations`), because a model summarising an
injection can repeat it. The sender's domain and the thread id come from mail
headers, so they count too. Fields corpus computes or constrains to a closed
vocabulary (ids, dates, counts, schema enums such as `importance` or `domain`,
the data-class `label`) are trusted, and so is the sanitized tier's `uri`, a
provider web link that is projected only as an https URL (see
[Tiers](tiers.md#the-message-link-uri)).

On the sanitized tier, `one_line`, `action_summary` and `organizations` are
redacted by the tier's policy before they are stored: names become roles or
initials, reference and account numbers become placeholders such as
`[booking ••••42]`, email addresses and phone numbers are dropped, and a
credential becomes `[REDACTED:<type>]` under every policy (see
[Tiers](tiers.md#redaction)). A high-sensitivity row with nothing to redact
against, or on a tier set to `gate_action: stub`, carries a stub instead:
`[sensitive] <domain> item — action: <action>`. A row the sync has not yet
re-projected under the tier's current policy is served as
`[pending redaction] <domain> item`, with no `action_summary` or
`organizations` (see [Tiers](tiers.md#serving-a-row-not-yet-re-projected)).
Redacted text is still derived
from third-party text, so those columns stay untrusted. The placeholders match
none of the `instruction_like` patterns, so redaction neither raises nor clears
the flag.

The sanitized tier's untrusted columns are `sanitized_store.UNTRUSTED_COLS`,
next to the projection itself; the full surface's row keys are
`store.SEARCH_KEYS`, `QUERY_KEYS` and `GET_KEYS`. Tests require every column
and key to be classified, so a new one cannot ship unmarked by accident. Each
tool declares its untrusted fields when `serving.build_mcp` registers it:

| Tool | Untrusted fields |
| --- | --- |
| sanitized row tools: `query`, `search`, `get`, `index_*` (not `index_stats`) | `UNTRUSTED_COLS` |
| sanitized `aggregate` | `organization`, `sender_domain`: group keys read from mail headers or a model's extraction |
| sensitive `query`, `search`, `corpus_query` | `from_addr`, `subject` |
| `corpus_search` | `from_addr`, `snippet`, `subject` |
| sensitive `get`, `corpus_get` | `content`, `meta` |
| sensitive `aggregate`, `corpus_stats`, `index_stats` | none: group keys are labels, months, accounts or schema enums |

The stats tools return one row per group (`label` and `count` on `corpus`;
`dimension`, `key` and `count` on `corpus-index`), with the item count as
`total`.

## Flags

`flags` is a closed vocabulary; consumers match on the values exactly.

| Flag | Meaning |
| --- | --- |
| `instruction_like` | An untrusted field reads like instructions to an AI, in English or Norwegian: override phrasing ("ignore previous instructions", "ignorer alle tidligere instruksjoner"), an address to an AI ("to any AI reading this", "du er nå en AI-assistent"), a request for the system prompt, chat-template or tool-call syntax, or a URL carrying credentials. |

A flagged row is marked, never dropped: an injected email should still reach the
human reading the briefing, visibly marked as suspicious, but no agent should
act on it. The check runs when the result is built, over every untrusted field,
so it covers rows already stored and a pattern change applies at once. Text is
normalised first (NFKC, HTML entities decoded, invisible format characters
removed, Cyrillic and Greek look-alikes mapped to Latin, whitespace collapsed),
so a phrase split across lines or disguised with a zero-width space still
matches. The
patterns aim for precision: ordinary mail ("please reply by Friday", "this is an
automated system message", "ignore these instructions if you have already
renewed") must not match, or readers learn to ignore the flag. The flag is a
signal, not a filter, and will miss phrasings it has no pattern for; the
untrusted-field framing is what protects a consumer.
