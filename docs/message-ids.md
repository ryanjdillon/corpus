# Message ids

A message reference names one message in one mailbox. It is the only thing
corpus shares with services that act on mail (mail-actions), so it is written
down here as a versioned contract with test vectors, and neither side imports
the other's code.

## `corpus.id/v1`

```
<kind>:<mailbox>::<provider-uid>
```

| Part | Grammar | Meaning |
| --- | --- | --- |
| `kind` | `[a-z0-9_-]{1,32}`; v1 defines `gmail`, `imap`, `jmap` and `graph` | the provider protocol |
| `mailbox` | `[a-z0-9_-]{1,32}` | the configured source name, e.g. `personal` or `kbl-post`; corpus refuses to build a source whose name is outside this grammar |
| `::` | | separates the source from the provider's uid |
| `provider-uid` | per kind, below | the provider's own id for the message |

`<kind>:<mailbox>` is the record's `source` (for example `gmail:personal`), and
the whole reference is `Record.key()`. Only the four mail kinds are message
references; other sources' record keys (for example `files:<name>`) are out of
scope for v1. The reference is the whole string: no surrounding whitespace, no
trailing newline. The mailbox name resolves to a credential
in each service's own configuration; a reference never carries one.

Provider uid, per kind:

| Kind | Uid | Example |
| --- | --- | --- |
| `gmail` | the Gmail API message id, `[0-9a-f]{8,32}` | `18c2f4a9b0d1e7f3` |
| `imap` | `<folder>:<uidvalidity>:<uid>`; the folder is the Unicode name (IMAPClient decodes modified UTF-7), non-empty, without control characters, and may contain `/`, `.`, spaces, `:` or `::`, so the two integers are read from the right; both are non-zero and written without leading zeros (RFC 9051), so a reference has one spelling | `Styret/Lønn:1:7` |
| `jmap` | the JMAP Email id, `[A-Za-z0-9_-]{1,255}` (inferred; no JMAP source yet) | `Mf3a9b2c1-d4e5_f6` |
| `graph` | the Microsoft Graph immutable id (`Prefer: IdType="ImmutableId"`), `[A-Za-z0-9_=+/-]{1,512}` (inferred; no Graph source yet) | `AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AAAA=` |

## Stability

- **gmail, jmap, graph:** a reference stays valid when the message's labels
  change or it moves between folders.
- **imap:** the reference embeds the folder and the folder's UIDVALIDITY, so it is
  valid only until the message moves (a MOVE gives it a new UID in a new folder)
  or the server resets UIDVALIDITY. A service that moves a message must return the
  new reference.

A row's `uri` (the provider's web or IMAP URL) is a human link, not a reference.
Never parse it to find a message.

## Parsing

corpus parses references with `models.parse_id(ref) -> MessageRef`, which raises
`ValueError` on anything outside the grammar: "not a corpus.id/v1 message
reference" when the `<kind>:<mailbox>::` frame doesn't match, "unknown message
kind" for a kind v1 does not define, and "malformed <kind> uid" when the uid
breaks its kind's grammar. `MessageRef.source` and `str(ref)`
rebuild the source and the reference, and `imap_parts()` returns
`(folder, uidvalidity, uid)`.

Another service implements its own codec against the same vectors rather than
importing corpus.

## Test vectors

[`message-ids.vectors.json`](message-ids.vectors.json) lists valid references with
their parts, and invalid ones with the reason. corpus's tests pin
`parse_id` and `Record.key_for` to it; a consumer copies the file and runs its own
codec against it. A change to the grammar is a new version (`corpus.id/v2`) with
its own vectors, never an edit to v1's.
