# IMAP

Each IMAP mailbox is configured by name. The name becomes the env-var prefix and
the source id passed to `corpus ingest`. For `corpus ingest imap:example`:

```
CORPUS_IMAP_EXAMPLE_HOST=imap.example.com
CORPUS_IMAP_EXAMPLE_PORT=993
CORPUS_IMAP_EXAMPLE_USER=user@example.com
CORPUS_IMAP_EXAMPLE_PASSWORD=...
CORPUS_IMAP_EXAMPLE_FOLDERS=INBOX,Archive   # optional; default is all folders
CORPUS_IMAP_EXAMPLE_SSL=true                 # optional, default true
```

`FOLDERS` selects which mailboxes to catalog. Give a comma-separated list to
restrict it, or leave it unset (or set `all` / `*`) to discover and catalog every
selectable folder on the account.

Incremental sync is tracked per folder in the `sync_state` table: the cursor is a
JSON map of folder to `UIDVALIDITY:UID`, so each folder resyncs independently. A
`UIDVALIDITY` change resets that folder's UID window.

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
(`kasserar`), not the full address. The fetcher then sees the mailbox as its own INBOX, and
access revokes when that password is rotated. Configure one fetcher name per
mailbox (`imap:kasserar`, `imap:post`, …), so each mailbox keeps its own cursor
and can be stored in its own schema or database.
