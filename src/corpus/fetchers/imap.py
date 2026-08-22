"""Generic IMAP fetcher.

Credentials come from the environment, namespaced by the fetcher name so several
mailboxes can be configured independently, e.g. for name "example":

    CORPUS_IMAP_EXAMPLE_HOST=imap.example.com
    CORPUS_IMAP_EXAMPLE_PORT=993
    CORPUS_IMAP_EXAMPLE_USER=user@example.com
    CORPUS_IMAP_EXAMPLE_PASSWORD=...
    CORPUS_IMAP_EXAMPLE_FOLDERS=INBOX,Archive   # optional; default is all folders
    CORPUS_IMAP_EXAMPLE_SSL=true                 # optional, default true

FOLDERS selects which mailboxes to catalog: a comma-separated list, or unset
(or "all"/"*") to discover and catalog every selectable folder on the account.

Incremental sync is tracked per folder: the cursor is a JSON object mapping each
folder to "<uidvalidity>:<uid>" (its highest seen UID). A UIDVALIDITY change for
a folder resets that folder's UID window.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime

import mailparser
from imapclient import IMAPClient

from ..models import Record
from .base import as_text

log = logging.getLogger("corpus.fetchers.imap")

_ALL = {"", "all", "*"}


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
        folders = _env(name, "FOLDERS", "")
        # None => discover all selectable folders at fetch time.
        self.folders: list[str] | None = (
            None
            if folders.strip().lower() in _ALL
            else [f.strip() for f in folders.split(",") if f.strip()]
        )
        self.ssl = _env(name, "SSL", "true").lower() not in {"false", "0", "no"}
        if not (self.host and self.user and self.password):
            raise ValueError(f"IMAP fetcher {name!r} missing host/user/password env")
        self._next_cursor: str | None = None

    def fetch(self, cursor: str | None) -> Iterator[Record]:
        state = self._load_cursor(cursor)
        new_state = dict(state)
        with IMAPClient(self.host, port=self.port, ssl=self.ssl) as client:
            client.login(self.user, self.password)
            for folder in self._resolve_folders(client):
                prev_validity, prev_uid = self._parse_folder_cursor(state.get(folder))
                info = client.select_folder(folder, readonly=True)
                validity = int(info[b"UIDVALIDITY"])
                # A UIDVALIDITY change invalidates prior UIDs for this folder.
                same = validity == prev_validity
                start_uid = prev_uid + 1 if same else 1
                max_uid = prev_uid if same else 0
                uids = [u for u in client.search(["UID", f"{start_uid}:*"]) if u >= start_uid]
                if uids:
                    for uid, data in client.fetch(uids, ["RFC822"]).items():
                        raw = data.get(b"RFC822")
                        if not raw:
                            continue
                        try:
                            record = self._to_record(folder, uid, raw)
                        except Exception:
                            # Skip one unparseable message rather than abort.
                            log.warning(
                                "imap: skipping unparseable %s:%s", folder, uid, exc_info=True
                            )
                            max_uid = max(max_uid, uid)
                            continue
                        yield record
                        max_uid = max(max_uid, uid)
                new_state[folder] = f"{validity}:{max_uid}"
        self._next_cursor = json.dumps(new_state, sort_keys=True)

    def next_cursor(self) -> str | None:
        return self._next_cursor

    def _resolve_folders(self, client: IMAPClient) -> list[str]:
        if self.folders is not None:
            return self.folders
        discovered = []
        for flags, _delim, name in client.list_folders():
            if b"\\Noselect" in flags:  # containers that can't hold messages
                continue
            discovered.append(name)
        return discovered

    def _to_record(self, folder: str, uid: int, raw: bytes) -> Record:
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
            # Folder-qualified so ids stay unique across folders (UIDs are only
            # unique within a folder).
            source_uid=f"{folder}:{uid}",
            kind="email",
            account=self.user,
            folder=folder,
            thread_id=headers.get("Message-ID"),
            from_addr=from_addr,
            to_addrs=to_addrs,
            subject=as_text(parsed.subject),
            sent_at=sent_at,
            headers=headers,
            uri=f"imap://{self.host}/{folder}/{uid}",
            body_text=body or "",
        )

    @staticmethod
    def _load_cursor(cursor: str | None) -> dict[str, str]:
        if not cursor:
            return {}
        try:
            value = json.loads(cursor)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _parse_folder_cursor(value: str | None) -> tuple[int, int]:
        if not value or ":" not in value:
            return (0, 0)
        validity, uid = value.split(":", 1)
        return (int(validity), int(uid))
