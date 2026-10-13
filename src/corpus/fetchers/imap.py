"""Generic IMAP fetcher.

Credentials come from the environment, under the source's credential prefix
(``sources.Source.env_prefix``; by default ``CORPUS_IMAP_<NAME>``), so several
mailboxes can be configured independently, e.g. for "imap:example":

    CORPUS_IMAP_EXAMPLE_HOST=imap.example.com
    CORPUS_IMAP_EXAMPLE_PORT=993
    CORPUS_IMAP_EXAMPLE_USER=user@example.com
    CORPUS_IMAP_EXAMPLE_PASSWORD=...
    CORPUS_IMAP_EXAMPLE_FOLDERS=INBOX,Archive   # optional; default is all folders
    CORPUS_IMAP_EXAMPLE_SSL=true                 # optional, default true

FOLDERS selects which mailboxes to catalog: a comma-separated list, or unset
(or "all"/"*") to discover and catalog every selectable folder on the account.
The source's ``options.folders``, when set, is used instead.

Incremental sync is tracked per folder: the cursor is a JSON object mapping each
folder to "<uidvalidity>:<uid>" (its highest seen UID). A UIDVALIDITY change for
a folder resets that folder's UID window. Record ids carry the UIDVALIDITY too
("<folder>:<uidvalidity>:<uid>"): after a change the server may reuse old UIDs,
and an id without the validity would collide with an already-stored message and
be skipped.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Container, Iterator, Mapping
from urllib.parse import quote

from imapclient import IMAPClient

from ..models import Record
from ..sources import ImapSource
from ..sources.rfc822 import MESSAGE_ID, record_from_rfc822

log = logging.getLogger("corpus.fetchers.imap")

_ALL = {"", "all", "*"}


class ImapFetcher:
    """Catalog an IMAP account, tracking incremental sync per folder by UID."""

    def __init__(
        self,
        source: ImapSource,
        *,
        environ: Mapping[str, str] = os.environ,
        connect: Callable[..., IMAPClient] = IMAPClient,
    ) -> None:
        self.name = source.name
        self._connect = connect
        self.source = source.id

        def env(key: str, default: str = "") -> str:
            return environ.get(source.env_name(key), default)

        self.host = env("HOST")
        self.port = int(env("PORT", "993"))
        self.user = env("USER")
        self.password = env("PASSWORD")
        # None => discover all selectable folders at fetch time.
        self.folders: list[str] | None
        if source.options.folders is not None:
            self.folders = list(source.options.folders)
        else:
            folders = env("FOLDERS")
            self.folders = (
                None
                if folders.strip().lower() in _ALL
                else [f.strip() for f in folders.split(",") if f.strip()]
            )
        self.ssl = env("SSL", "true").lower() not in {"false", "0", "no"}
        if not (self.host and self.user and self.password):
            raise ValueError(
                f"IMAP source {source.id!r} missing host/user/password env: set "
                f"{source.env_name('HOST')}, {source.env_name('USER')} and "
                f"{source.env_name('PASSWORD')}"
            )
        self._next_cursor: str | None = None

    def fetch(self, cursor: str | None, known: Container[str] = frozenset()) -> Iterator[Record]:
        """Yield records newer than the cursor across the account's folders.

        UIDs whose record key is in *known* are not downloaded.
        """
        state = self._load_cursor(cursor)
        new_state = dict(state)
        with self._connect(self.host, port=self.port, ssl=self.ssl) as client:
            client.login(self.user, self.password)
            for folder in self._resolve_folders(client):
                prev_validity, prev_uid = self._parse_folder_cursor(state.get(folder))
                info = client.select_folder(folder, readonly=True)
                validity = int(info[b"UIDVALIDITY"])
                # A UIDVALIDITY change invalidates prior UIDs for this folder.
                same = validity == prev_validity
                start_uid = prev_uid + 1 if same else 1
                max_uid = prev_uid if same else 0
                listed = [u for u in client.search(["UID", f"{start_uid}:*"]) if u >= start_uid]
                uids = [
                    u
                    for u in listed
                    if Record.key_for(self.source, f"{folder}:{validity}:{u}") not in known
                ]
                # Already-stored UIDs still advance this folder's cursor.
                for uid in set(listed) - set(uids):
                    max_uid = max(max_uid, uid)
                if uids:
                    for uid, data in client.fetch(uids, ["RFC822"]).items():
                        raw = data.get(b"RFC822")
                        if not raw:
                            continue
                        try:
                            record = self._to_record(folder, validity, uid, raw)
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
        """Return the cursor to persist after a successful fetch pass."""
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

    def _to_record(self, folder: str, validity: int, uid: int, raw: bytes) -> Record:
        return record_from_rfc822(
            self.source,
            # A UID is unique only within one folder and one UIDVALIDITY epoch.
            f"{folder}:{validity}:{uid}",
            raw,
            account=self.user,
            folder=folder,
            # RFC 5092 IMAP URL.
            uri=f"imap://{self.host}/{quote(folder)};UIDVALIDITY={validity}/;UID={uid}",
            # IMAP has no thread id of its own; the Message-ID stands in for one.
            thread_id=MESSAGE_ID,
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
