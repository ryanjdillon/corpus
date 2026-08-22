#!/usr/bin/env python3
"""One-time helper to obtain a Gmail refresh token for read-only ingestion.

Run locally (not in the cluster). Requires an OAuth *Desktop app* client from a
Google Cloud project with the Gmail API enabled, and a one-off extra dependency:

    pip install google-auth-oauthlib

Usage:

    python scripts/gmail_oauth.py /path/to/client_secret.json

Opens a browser for consent, then prints the client id/secret/refresh token to
store as the fetcher's CORPUS_GMAIL_<NAME>_* credentials. The refresh token is
long-lived; the access token is minted per run by the fetcher.
"""

from __future__ import annotations

import sys

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: gmail_oauth.py <client_secret.json>", file=sys.stderr)
        return 2
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the one-off dependency first: pip install google-auth-oauthlib", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(argv[1], SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n--- store these as the fetcher credentials ---")
    print(f"CLIENT_ID={creds.client_id}")
    print(f"CLIENT_SECRET={creds.client_secret}")
    print(f"REFRESH_TOKEN={creds.refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
