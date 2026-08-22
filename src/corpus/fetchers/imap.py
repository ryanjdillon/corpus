"""Generic IMAP fetcher.

Credentials come from the environment, namespaced by the fetcher name so several
mailboxes can be configured independently, e.g. for name "boatclub":

    CORPUS_IMAP_BOATCLUB_HOST=mail.example.org
    CORPUS_IMAP_BOATCLUB_PORT=993
    CORPUS_IMAP_BOATCLUB_USER=crew@example.org
    CORPUS_IMAP_BOATCLUB_PASSWORD=...
    CORPUS_IMAP_BOATCLUB_FOLDERS=INBOX,Archive   # optional, default INBOX

Incremental sync uses IMAP UIDVALIDITY + the highest seen UID as the cursor,
encoded as "<uidvalidity>:<uid>".
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator

import mailparser
from imapclient import IMAPClient

from ..models import Record


def _env(name: str, key: str, default: str = "") -> str:
    return os.environ.get(f"CORPUS_IMAP_{name.upper()}_{key}", default)


class ImapFetcher:
    def __init__(self, name: str) -> None:
        self.name = name
        self.source = f"imap:{name}"
        self.host = _env(name, "HOST")
        self.port = int(_env(name, "PORT", "993"))
        self.user = _env(name, "USER")
        self.password = _env(name, "PASSWORD")
        folders = _env(name, "FOLDERS", "INBOX")
        self.folders = [f.strip() for f in folders.split(",") if f.strip()]
        if not (self.host and self.user and self.password):
            raise ValueError(f"IMAP fetcher {name!r} missing host/user/password env")
        self._next_cursor: str | None = None

    def fetch(self, cursor: str | None) -> Iterator[Record]:
        prev_validity, prev_uid = self._parse_cursor(cursor)
        max_uid = prev_uid
        with IMAPClient(self.host, port=self.port, ssl=True) as client:
            client.login(self.user, self.password)
            for folder in self.folders:
                info = client.select_folder(folder, readonly=True)
                validity = int(info[b"UIDVALIDITY"])
                # A UIDVALIDITY change invalidates prior UIDs for this mailbox.
                start_uid = prev_uid + 1 if validity == prev_validity else 1
                uids = client.search(["UID", f"{start_uid}:*"])
                uids = [u for u in uids if u >= start_uid]
                if not uids:
                    self._next_cursor = f"{validity}:{max_uid}"
                    continue
                for uid, data in client.fetch(uids, ["RFC822"]).items():
                    raw = data.get(b"RFC822")
                    if not raw:
                        continue
                    yield self._to_record(folder, uid, raw)
                    max_uid = max(max_uid, uid)
                self._next_cursor = f"{validity}:{max_uid}"

    def next_cursor(self) -> str | None:
        return self._next_cursor

    def _to_record(self, folder: str, uid: int, raw: bytes) -> Record:
        parsed = mailparser.parse_from_bytes(raw)
        headers = {k: str(v) for k, v in (parsed.headers or {}).items()}
        sent_at: datetime | None = parsed.date
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        from_addr = parsed.from_[0][1] if parsed.from_ else None
        to_addrs = [addr for _, addr in (parsed.to or [])]
        body = parsed.text_plain[0] if parsed.text_plain else (parsed.body or "")
        return Record(
            source=self.source,
            source_uid=str(uid),
            kind="email",
            account=self.user,
            folder=folder,
            thread_id=headers.get("Message-ID"),
            from_addr=from_addr,
            to_addrs=to_addrs,
            subject=parsed.subject,
            sent_at=sent_at,
            headers=headers,
            uri=f"imap://{self.host}/{folder}/{uid}",
            body_text=body or "",
        )

    @staticmethod
    def _parse_cursor(cursor: str | None) -> tuple[int, int]:
        if not cursor or ":" not in cursor:
            return (0, 0)
        validity, uid = cursor.split(":", 1)
        return (int(validity), int(uid))
