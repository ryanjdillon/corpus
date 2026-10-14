# IMAP

Each IMAP mailbox is one source in the config file (see
[Source configuration](config.md)). For `corpus ingest imap:example`:

```yaml
sources:
  - name: example
    kind: imap
    enrich: true
    options: {folders: [INBOX, Archive]}   # optional; unset = all but junk, trash, drafts
```

The connection and credentials are read from the variables under the source's
prefix, by default `CORPUS_IMAP_<NAME>`:

```
CORPUS_IMAP_EXAMPLE_HOST=imap.example.com
CORPUS_IMAP_EXAMPLE_PORT=993
CORPUS_IMAP_EXAMPLE_USER=user@example.com
CORPUS_IMAP_EXAMPLE_PASSWORD=...
CORPUS_IMAP_EXAMPLE_FOLDERS=INBOX,Archive   # optional; used when options.folders is unset
CORPUS_IMAP_EXAMPLE_SSL=true                 # optional, default true
```

## Folder selection

`folders` (or `FOLDERS`) selects which mailboxes to catalog. Give a list to
restrict it, or leave both unset (or set `FOLDERS` to `all` / `*`) to discover
the account's folders.

Discovery catalogs every selectable folder **except junk, trash and drafts**.
Mail in a junk folder is mail the server's own filter already judged to be spam
and kept out of the inbox. Ingesting it would put it back in front of every
consumer of the index: a briefing that summarises it is noise, and one that
surfaces a phishing message's call to action does the attacker's work. Trash is
mail the owner discarded, and drafts are unsent text, not correspondence.

A folder is skipped when it, or a folder above it, is an **excluded root**, so
`Trash/Old project` (where a deleted folder lands) goes with `Trash`. Which
folders are excluded roots depends on what the server advertises:

- **With special-use attributes** (any folder in the listing carries an RFC 6154
  attribute: `\All`, `\Archive`, `\Drafts`, `\Flagged`, `\Junk`, `\Sent` or
  `\Trash`), the flags decide alone: a folder flagged `\Junk`, `\Trash` or
  `\Drafts`, whatever its name. An unflagged folder named `Junk Mail` is kept.
- **Without them**, the name decides: a folder whose **whole** name is one of
  `Junk`, `Junk Mail`, `Junk E-mail`, `Spam`, `Trash`, `Deleted Items`,
  `Deleted Messages`, `Søppelpost`, `Slettede elementer`, `Papirkurv`, `Drafts`,
  `Utkast` (`fetchers.imap.EXCLUDED_FOLDER_NAMES`), compared case-insensitively,
  at the top level or directly under INBOX (`INBOX/Junk`, `INBOX.Spam`; the name
  INBOX is itself case-insensitive). A nested name elsewhere, such as
  `Clients/Drafts`, is a folder the owner made, and is kept.

INBOX itself and a folder flagged `\Sent` (or `\Inbox`) are never skipped by
these defaults: outgoing mail is how a thread's `waiting_on` is known.

The source's `exclude_folders` adds names to skip. Each matches a folder's
**whole** name, case-insensitively, and every folder beneath it; it is not
matched against a single path segment, so `Noise` skips `Noise` and
`Noise/Old` but not `Lists/Noise`.

| Folder | Server lists special-use? | Skipped? | Why |
| --- | --- | --- | --- |
| `Quarantine` flagged `\Junk` | yes | yes | flagged junk |
| `Bin/Old project` under a `\Trash` folder `Bin` | yes | yes | beneath a root |
| `Junk Mail`, unflagged | yes | no | the flags decide |
| `Trash` flagged `\Sent` | yes | no | sent mail is kept |
| `Junk Mail` | no | yes | top-level name |
| `INBOX/Junk`, `inbox.Spam` | no | yes | child of INBOX |
| `Trash/Old project` | no | yes | beneath `Trash` |
| `INBOX/Clients/Drafts`, `Work/Trash` | no | no | nested elsewhere |
| `Spam reports` | no | no | not a whole name |

Each run logs the folders it skipped, by name, at INFO.

```yaml
    options: {exclude_folders: [Newsletters, Lists/Noise]}   # skipped on top of the defaults
```

An explicit `folders` list is fetched exactly as given, so listing a junk folder
there fetches it. That is also the way to turn the defaults off; there is no
separate switch. `exclude_folders` only refines discovery, so the config file
refuses it alongside `folders`, and a `FOLDERS` variable that lists folders
makes it ignored (with a warning).

The Gmail fetcher needs no such list: the Gmail API leaves spam and trash out
of a listing unless asked for them.

## Cursor

Incremental sync is tracked per folder in the `sync_state` table: the cursor is a
JSON map of folder to `UIDVALIDITY:UID`, so each folder resyncs independently. A
`UIDVALIDITY` change resets that folder's UID window.

A skipped folder is never selected, and its entry in the cursor, if an earlier
run made one, is carried forward unchanged rather than dropped. The entry is
inert while the folder is skipped; keeping it means that if the folder is later
fetched again (by listing it in `folders`), the fetch resumes after the mail
already seen there instead of re-downloading the folder from UID 1, which would
bring back the mail a purge removed.

## Ids and links

A record's id is `<folder>:<UIDVALIDITY>:<UID>`, and its `uri` is the RFC 5092
form `imap://<host>/<folder>;UIDVALIDITY=<v>/;UID=<uid>`. A UID is unique only
within one folder and one UIDVALIDITY epoch: after a change the server may hand an
old UID to a new message, and an id without the validity would match the stored
message and be skipped. Messages stored under the previous epoch stay as they are.

Folders are opened read-only (`EXAMINE`), so fetching never sets `\Seen` or
changes the mailbox in any other way.

## Stalwart and other self-hosted servers

Any IMAPS server works, Stalwart included. For a shared or role mailbox, log in as
the mailbox's own principal (its service password) instead of as a member with
access to it. Stalwart group principals log in by their bare principal name
(`inbox-a`), not the full address. The fetcher then sees the mailbox as its own INBOX, and
access revokes when that password is rotated. Configure one source per mailbox
(`imap:inbox-a`, `imap:inbox-b`, …), so each mailbox keeps its own cursor and can
be stored in its own schema or database.
