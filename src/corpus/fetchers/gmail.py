"""Gmail API fetcher (OAuth), used instead of IMAP so message labels are
available.

Credentials come from the environment, namespaced by fetcher name. For
"gmail:personal":

    CORPUS_GMAIL_PERSONAL_CLIENT_ID=...
    CORPUS_GMAIL_PERSONAL_CLIENT_SECRET=...
    CORPUS_GMAIL_PERSONAL_REFRESH_TOKEN=...          # from scripts/gmail_oauth.py
    CORPUS_GMAIL_PERSONAL_LABELS=INBOX,Receipts      # optional; empty = all mail

Only a refresh token is stored; access tokens are minted per run. Incremental
sync uses Gmail's historyId: a full backfill on the first run records the
mailbox's current historyId as the cursor, and later runs pull only changes
since then (falling back to a full backfill if the historyId has expired).
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import mailparser

from ..models import Record
from .base import as_text

log = logging.getLogger("corpus.fetchers.gmail")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Cursor prefix marking an in-progress backfill: the remainder is the Gmail
# messages.list page token to resume from. A plain (numeric) cursor is a
# completed backfill's historyId, used for incremental sync.
_BACKFILL_PREFIX = "backfill:"


def _env(name: str, key: str, default: str = "") -> str:
    return os.environ.get(f"CORPUS_GMAIL_{name.upper()}_{key}", default)


class GmailFetcher:
    def __init__(self, name: str) -> None:
        self.name = name
        self.source = f"gmail:{name}"
        self.client_id = _env(name, "CLIENT_ID")
        self.client_secret = _env(name, "CLIENT_SECRET")
        self.refresh_token = _env(name, "REFRESH_TOKEN")
        labels = _env(name, "LABELS", "")
        self.labels = [x.strip() for x in labels.split(",") if x.strip()] or None
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise ValueError(f"Gmail fetcher {name!r} missing client id/secret/refresh token")
        self._next_cursor: str | None = None
        self._label_names: dict[str, str] = {}
        self._account: str | None = None

    def fetch(self, cursor: str | None) -> Iterator[Record]:
        api = httpx.Client(
            base_url=_API,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=60.0,
        )
        try:
            labels = api.get("/labels").raise_for_status().json().get("labels", [])
            self._label_names = {x["id"]: x["name"] for x in labels}
            wanted_ids = None
            if self.labels:
                by_name = {x["name"]: x["id"] for x in labels}
                wanted_ids = [by_name[n] for n in self.labels if n in by_name]

            profile = api.get("/profile").raise_for_status().json()
            self._account = profile.get("emailAddress")
            current_history = profile.get("historyId")

            if not cursor:
                yield from self._backfill(api, wanted_ids)
                self._next_cursor = current_history
            elif cursor.startswith(_BACKFILL_PREFIX):
                # Resume an interrupted backfill from the saved page token.
                yield from self._backfill(
                    api, wanted_ids, start_page=cursor[len(_BACKFILL_PREFIX) :]
                )
                self._next_cursor = current_history
            else:
                yield from self._incremental(api, cursor, wanted_ids, current_history)
        finally:
            api.close()

    def next_cursor(self) -> str | None:
        return self._next_cursor

    def _access_token(self) -> str:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _backfill(
        self, api: httpx.Client, wanted_ids: list[str] | None, start_page: str | None = None
    ) -> Iterator[Record]:
        params: dict[str, object] = {"maxResults": 500}
        if wanted_ids:
            params["labelIds"] = wanted_ids
        page: str | None = start_page
        while True:
            if page:
                params["pageToken"] = page
            data = api.get("/messages", params=params).raise_for_status().json()
            for m in data.get("messages", []):
                rec = self._fetch_message(api, m["id"])
                if rec:
                    yield rec
            page = data.get("nextPageToken")
            if not page:
                break
            # Checkpoint the next page so an interrupted backfill resumes from
            # here instead of re-listing the whole mailbox. The ingest persists
            # this after each flush; earlier pages are already stored, and
            # existing_ids skips any overlap.
            self._next_cursor = f"{_BACKFILL_PREFIX}{page}"

    def _incremental(
        self,
        api: httpx.Client,
        cursor: str,
        wanted_ids: list[str] | None,
        current_history: str | None,
    ) -> Iterator[Record]:
        params: dict[str, object] = {
            "startHistoryId": cursor,
            "historyTypes": "messageAdded",
            "maxResults": 500,
        }
        wanted = set(wanted_ids) if wanted_ids else None
        seen: set[str] = set()
        latest = cursor
        page: str | None = None
        while True:
            if page:
                params["pageToken"] = page
            resp = api.get("/history", params=params)
            if resp.status_code == 404:
                # historyId too old to be usable — re-backfill from scratch.
                yield from self._backfill(api, wanted_ids)
                self._next_cursor = current_history
                return
            data = resp.raise_for_status().json()
            for h in data.get("history", []):
                for added in h.get("messagesAdded", []):
                    msg = added.get("message", {})
                    mid = msg.get("id")
                    if not mid or mid in seen:
                        continue
                    if wanted and not (set(msg.get("labelIds", [])) & wanted):
                        continue
                    seen.add(mid)
                    rec = self._fetch_message(api, mid)
                    if rec:
                        yield rec
            latest = data.get("historyId", latest)
            page = data.get("nextPageToken")
            if not page:
                break
        self._next_cursor = latest

    def _fetch_message(self, api: httpx.Client, mid: str) -> Record | None:
        resp = api.get(f"/messages/{mid}", params={"format": "raw"})
        if resp.status_code == 404:
            return None  # deleted between listing and fetch
        d = resp.raise_for_status().json()
        try:
            raw = base64.urlsafe_b64decode(d["raw"])
            label_names = [self._label_names.get(i, i) for i in d.get("labelIds", [])]
            return self._to_record(d["id"], d.get("threadId"), raw, label_names)
        except Exception:
            # A single unprocessable message (missing/invalid body, unparseable
            # headers, …) must not abort a backfill.
            log.warning("gmail: skipping unprocessable message %s", mid, exc_info=True)
            return None

    def _to_record(
        self, mid: str, thread_id: str | None, raw: bytes, labels: list[str]
    ) -> Record:
        parsed = mailparser.parse_from_bytes(raw)
        headers = {k: str(v) for k, v in (parsed.headers or {}).items()}
        sent_at: datetime | None = parsed.date
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        from_addr = parsed.from_[0][1] if parsed.from_ else None
        to_addrs = [addr for _, addr in (parsed.to or [])]
        body = parsed.text_plain[0] if parsed.text_plain else (parsed.body or "")
        return Record(
            source=self.source,
            source_uid=mid,  # Gmail message id is unique per account
            kind="email",
            account=self._account,
            thread_id=thread_id,
            from_addr=from_addr,
            to_addrs=to_addrs,
            subject=as_text(parsed.subject),
            sent_at=sent_at,
            labels=labels,
            headers=headers,
            uri=f"https://mail.google.com/mail/u/0/#all/{mid}",
            body_text=body or "",
        )
