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
