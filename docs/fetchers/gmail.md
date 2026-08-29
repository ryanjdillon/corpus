# Gmail

Gmail is ingested through the Gmail API (OAuth) rather than IMAP, so message
**labels** are captured. Configure per fetcher name; for `corpus ingest gmail:personal`:

```
CORPUS_GMAIL_PERSONAL_CLIENT_ID=...
CORPUS_GMAIL_PERSONAL_CLIENT_SECRET=...
CORPUS_GMAIL_PERSONAL_REFRESH_TOKEN=...        # see scripts/gmail_oauth.py
CORPUS_GMAIL_PERSONAL_LABELS=INBOX,Receipts    # optional; empty = all mail
```

## Getting a refresh token

Only a refresh token is stored; access tokens are minted per run. Obtain the
refresh token once with an OAuth *Desktop app* client (Gmail API enabled):

```bash
pip install google-auth-oauthlib
python scripts/gmail_oauth.py client_secret.json
```

## Incremental sync

Sync uses Gmail's `historyId`: the first run backfills and records the mailbox's
current `historyId`; later runs pull only changes since, falling back to a full
backfill if the `historyId` has expired.
