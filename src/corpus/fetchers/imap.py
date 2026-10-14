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
(or "all"/"*") to discover every selectable folder on the account. The source's
``options.folders``, when set, is used instead. An explicit list is fetched as
given. Discovery skips the junk, trash and drafts folders (:class:`FolderPolicy`):
the server already filed that mail away from the inbox, so surfacing it in a
briefing is noise at best and a phishing vector at worst. Sent mail is kept; it
is how a thread's ``waiting_on`` is known.

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
from collections import Counter
from collections.abc import Callable, Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from imapclient import IMAPClient

from ..models import Record
from ..sources import ImapSource
from ..sources.rfc822 import MESSAGE_ID, record_from_rfc822

log = logging.getLogger("corpus.fetchers.imap")

_ALL = {"", "all", "*"}

#: One entry of ``IMAPClient.list_folders()``: the folder's attributes (flags),
#: its hierarchy delimiter (``None`` for a flat namespace) and its decoded name.
FolderEntry = tuple[Sequence[bytes], bytes | str | None, str]

#: RFC 6154 special-use attributes of the folders discovery skips, lower-cased
#: (attributes compare case-insensitively). ``\Sent`` is deliberately absent.
EXCLUDED_SPECIAL_USE: frozenset[bytes] = frozenset({b"\\junk", b"\\trash", b"\\drafts"})

#: Every RFC 6154 special-use attribute, lower-cased. A listing that carries any
#: of them comes from a server that advertises special-use, so its flags, not
#: folder names, say which folders are junk, trash or drafts.
SPECIAL_USE: frozenset[bytes] = frozenset(
    {b"\\all", b"\\archive", b"\\drafts", b"\\flagged", b"\\junk", b"\\sent", b"\\trash"}
)

#: Attributes of a folder discovery never skips by default: outgoing mail is how
#: a thread's ``waiting_on`` is known, and the inbox is the point of the source.
#: ``\Inbox`` is not RFC 6154 but some servers send it.
KEPT_SPECIAL_USE: frozenset[bytes] = frozenset({b"\\sent", b"\\inbox"})

#: Folder names discovery skips on servers that do not advertise special-use
#: attributes: the common English and Norwegian names of the junk, trash and
#: drafts folders across Dovecot, Exchange, Gmail-over-IMAP and Stalwart.
#: Matched case-insensitively against the whole name of a top-level folder or of
#: a direct child of INBOX, so ``Junk Mail`` and ``INBOX.Spam`` are skipped but
#: ``Spam reports`` and ``Clients/Drafts`` are not. Sent folders are not listed.
EXCLUDED_FOLDER_NAMES: frozenset[str] = frozenset(
    name.casefold()
    for name in (
        "Junk", "Junk Mail", "Junk E-mail", "Spam",
        "Trash", "Deleted Items", "Deleted Messages",
        "Søppelpost", "Slettede elementer", "Papirkurv",
        "Drafts", "Utkast",
    )
)

_INBOX = "inbox"  # RFC 3501: the name INBOX is case-insensitive


def _attrs(flags: Iterable[bytes | str]) -> frozenset[bytes]:
    return frozenset(
        (f.encode() if isinstance(f, str) else bytes(f)).lower() for f in flags
    )


def _delim(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        value = value.decode()
    return value or None


@dataclass(frozen=True)
class _Listing:
    """What the folder rule needs from one ``list_folders()`` result.

    Attributes:
        attrs: Each listed folder's attributes, lower-cased.
        delimiters: Each listed folder's hierarchy delimiter.
        special_use: Whether any folder carries a special-use attribute.
        delimiter: The account's usual delimiter, for a folder it no longer lists.
    """

    attrs: Mapping[str, frozenset[bytes]]
    delimiters: Mapping[str, str | None]
    special_use: bool
    delimiter: str | None

    @classmethod
    def of(cls, listing: Iterable[FolderEntry]) -> _Listing:
        entries = list(listing)
        attrs = {name: _attrs(flags) for flags, _d, name in entries}
        delimiters = {name: _delim(d) for _f, d, name in entries}
        common = Counter(d for d in delimiters.values() if d).most_common(1)
        return cls(
            attrs=attrs,
            delimiters=delimiters,
            special_use=any(a & SPECIAL_USE for a in attrs.values()),
            delimiter=common[0][0] if common else None,
        )

    def delimiter_of(self, name: str) -> str | None:
        return self.delimiters.get(name, self.delimiter)

    def excluded_root(self, path: str, parts: Sequence[str]) -> bool:
        """Whether the folder at *path* (split into *parts*) is junk, trash or drafts."""
        if self.special_use:
            return bool(self.attrs.get(path, frozenset()) & EXCLUDED_SPECIAL_USE)
        if len(parts) == 1:
            return parts[0].casefold() in EXCLUDED_FOLDER_NAMES
        return (
            len(parts) == 2
            and parts[0].casefold() == _INBOX
            and parts[1].casefold() in EXCLUDED_FOLDER_NAMES
        )


@dataclass(frozen=True)
class FolderPolicy:
    """Which folders of an account a source fetches: the one source of truth.

    The fetcher asks it which folders to scan, and ``corpus purge-excluded-folders``
    asks it which stored folders the source no longer fetches; both apply the
    same predicate, so the two cannot disagree.

    A folder is skipped when it, or a folder above it, is an excluded root
    (``Trash/Old project`` goes with ``Trash``). On a server that advertises
    special-use attributes the roots are the folders flagged ``\\Junk``,
    ``\\Trash`` or ``\\Drafts``, whatever their names. Otherwise they are the
    folders named in :data:`EXCLUDED_FOLDER_NAMES`, at the top level or directly
    under INBOX. INBOX and a folder flagged ``\\Sent`` or ``\\Inbox`` are never
    skipped by default.

    Attributes:
        selected: The explicit folder list, fetched as given (even a junk folder
            named in it), or ``None`` to discover every selectable folder.
        exclude: Extra folder names discovery skips besides the defaults; each
            matches a folder's whole name, case-insensitively, and the folders
            beneath it.
    """

    selected: tuple[str, ...] | None = None
    exclude: frozenset[str] = frozenset()

    @classmethod
    def of(cls, selected: Iterable[str] | None, exclude: Iterable[str] = ()) -> FolderPolicy:
        """A policy for *selected* (``None`` to discover) plus the *exclude* names."""
        return cls(
            selected=None if selected is None else tuple(selected),
            exclude=frozenset(n.casefold() for n in exclude),
        )

    def _skipped(self, name: str, listing: _Listing) -> bool:
        """Whether discovery skips *name*, listed or not, given the account's *listing*."""
        delimiter = listing.delimiter_of(name)
        parts = name.split(delimiter) if delimiter else [name]
        paths = [delimiter.join(parts[:d]) if delimiter else name for d in range(1, len(parts) + 1)]
        if any(path.casefold() in self.exclude for path in paths):
            return True
        if name.casefold() == _INBOX or listing.attrs.get(name, frozenset()) & KEPT_SPECIAL_USE:
            return False
        return any(
            listing.excluded_root(path, parts[:depth])
            for depth, path in enumerate(paths, start=1)
        )

    def resolve(self, listing: Iterable[FolderEntry]) -> tuple[list[str], list[str]]:
        """The folders to scan and the folders skipped, from the server's *listing*.

        An explicit selection is returned as given and skips nothing. Otherwise
        every selectable folder is scanned except the excluded ones; a
        ``\\Noselect`` container holds no messages, so it is neither.
        """
        if self.selected is not None:
            return list(self.selected), []
        known = _Listing.of(listing)
        scan: list[str] = []
        skipped: list[str] = []
        for name, attrs in known.attrs.items():
            if b"\\noselect" in attrs:
                continue
            (skipped if self._skipped(name, known) else scan).append(name)
        return scan, skipped

    def excluded(self, folders: Iterable[str], listing: Iterable[FolderEntry]) -> set[str]:
        """The ones of *folders* (as record ids store them) skipped as junk, trash or drafts.

        A folder in an explicit selection is never excluded. One outside it is
        excluded only when discovery would skip it too: a folder dropped from the
        list is a narrower selection, not junk. A stored folder the server no
        longer lists is judged by the same rule, split on the account's usual
        delimiter; with an empty listing, by its whole name.
        """
        known = _Listing.of(listing)
        selected = set(self.selected or ())
        return {
            folder
            for folder in folders
            if folder not in selected and self._skipped(folder, known)
        }


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
        exclude = source.options.exclude_folders or ()
        if exclude and self.folders is not None:
            # options rejects both together; FOLDERS can still select explicitly.
            log.warning(
                "imap: %s fetches an explicit folder list (%s), so exclude_folders "
                "has no effect", self.source, source.env_name("FOLDERS"),
            )
        self.policy = FolderPolicy.of(self.folders, exclude)
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
        # A folder not scanned this run (one the policy skips) keeps its entry, so
        # fetching it again later resumes rather than restarting from UID 1.
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

    def list_folders(self) -> list[FolderEntry]:
        """The account's folders as the server lists them, with their attributes."""
        with self._connect(self.host, port=self.port, ssl=self.ssl) as client:
            client.login(self.user, self.password)
            return list(client.list_folders())

    def _resolve_folders(self, client: IMAPClient) -> list[str]:
        if self.policy.selected is not None:
            return list(self.policy.selected)
        scan, skipped = self.policy.resolve(client.list_folders())
        if skipped:
            # Names only, never content.
            log.info("imap: %s skipping excluded folders: %s", self.source, ", ".join(skipped))
        return scan

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
