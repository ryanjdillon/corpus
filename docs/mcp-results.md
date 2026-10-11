# MCP results

Every MCP tool, on both the full surface (`corpus`) and the sanitized one
(`corpus-index`), returns one shape: `corpus.result/v1`. It carries the rows and
tells the caller how far to trust each field in them. The shape is built in
`corpus.envelope`.

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
| `total` | Every match, when cheap to know (the stats tools); otherwise `null`. |
| `next_cursor` | Opaque cursor for the next page; `null` on the last page. |
| `rows` | Always a list. A single-record tool (`corpus_get`, `index_summary`) returns 0 or 1 rows. Each row has `flags`. |

A tool that fails returns an MCP error (`isError`) with a plain-text message,
not an envelope.

## Why fields are marked untrusted

Anyone can email the mailbox, and a tool result lands in an agent's context, so
an email can carry instructions aimed at whatever agent reads it. The rule:
**a field is untrusted if it is derived from third-party text.** That includes raw
fields (subject, sender, body, snippet) and anything a model wrote about them
(`one_line`, `action_summary`, `organizations`), because a model summarising an
injection can repeat it. The sender's domain and the thread id come from mail
headers, so they count too. Fields corpus computes or constrains to a closed
vocabulary (ids, dates, counts, schema enums such as `importance` or `domain`,
the data-class `label`) are trusted.

The sanitized tier's untrusted columns are `sanitized_store.UNTRUSTED_COLS`,
next to the projection itself. On the full surface each tool names its own:

| Tool | Untrusted fields |
| --- | --- |
| `corpus_search` | `from_addr`, `snippet`, `subject` |
| `corpus_query` | `from_addr`, `subject` |
| `corpus_get` | `content`, `meta` |
| `corpus_stats`, `index_stats` | none: group keys are labels or schema enums |

The stats tools return one row per group (`label` and `count` on `corpus`;
`dimension`, `key` and `count` on `corpus-index`), with the item count as
`total`.

## Flags

`flags` is a closed vocabulary; consumers match on the values exactly.

| Flag | Meaning |
| --- | --- |
| `instruction_like` | An untrusted field reads like instructions to an AI: override phrasing ("ignore previous instructions"), chat-template or tool-call syntax, or a URL carrying credentials. |

A flagged row is marked, never dropped: an injected email should still reach the
human reading the briefing, visibly marked as suspicious, but no agent should
act on it. The check runs when the result is built, over every untrusted field,
so it covers rows already stored and a pattern change applies at once. The
patterns aim for precision: ordinary mail ("please reply by Friday") must not
match, or readers learn to ignore the flag.
