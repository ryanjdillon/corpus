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

## Per-message errors

A message that disappears between listing and fetching (404) is skipped. Gmail
also refuses some individual messages with a 403 whose reason is not a quota
error; those are skipped too, with the message id and Google's reason logged.
A backfill resumes from the same saved page on every run, so a single refused
message would otherwise stop all later mail from being ingested.

Quota errors (429, or 403 with reason `rateLimitExceeded` or
`userRateLimitExceeded`) are retried with exponential backoff (1 s, 2 s, 4 s, …,
at most 30 s) and fail the run only after the retries are used up.

Two kinds of refusal stop the run instead of skipping, because they apply to the
whole account and skipping would move the sync cursor past every message:

- `dailyLimitExceeded` (the daily quota) or `insufficientPermissions`;
- a refusal for five messages in a row (e.g. a token whose scope cannot
  read raw mail, which still lists messages fine).

The next run resumes from the saved page once the cause is fixed.
