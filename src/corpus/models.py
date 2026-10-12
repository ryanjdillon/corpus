"""Normalized record types shared across fetchers, classification, and storage."""

from __future__ import annotations

import re
from datetime import datetime
from typing import NamedTuple

from pydantic import BaseModel, Field


class Record(BaseModel):
    """A single ingestable item (an email or a document) before storage."""

    source: str  # fetcher id, e.g. "imap:example"
    source_uid: str  # stable per-source id; (source, source_uid) is unique
    kind: str  # "email" | "file"

    account: str | None = None
    folder: str | None = None
    thread_id: str | None = None

    from_addr: str | None = None
    to_addrs: list[str] = Field(default_factory=list)
    subject: str | None = None
    sent_at: datetime | None = None

    # Provider labels/categories (e.g. Gmail labels). Empty for sources without
    # them (plain IMAP conveys only the folder).
    labels: list[str] = Field(default_factory=list)

    headers: dict[str, str] = Field(default_factory=dict)
    uri: str | None = None
    body_text: str = ""

    def key(self) -> str:
        """Return the globally-unique key "source::source_uid"."""
        return self.key_for(self.source, self.source_uid)

    @staticmethod
    def key_for(source: str, source_uid: str) -> str:
        """Return the key a record from *source* with *source_uid* would have."""
        return f"{source}::{source_uid}"


class Classification(BaseModel):
    """A classifier's verdict for a record: label, confidence, and signals."""

    label: str
    confidence: float
    signals: dict[str, object] = Field(default_factory=dict)


# --- corpus.id/v1: the message reference (docs/message-ids.md) ---

ID_VERSION = "corpus.id/v1"
_NAME = r"[a-z0-9_-]{1,32}"
#: The grammar of a kind and a mailbox (source) name.
MAILBOX_NAME = re.compile(_NAME)
_ID = re.compile(rf"(?P<kind>{_NAME}):(?P<mailbox>{_NAME})::(?P<uid>.+)", re.DOTALL)
# The provider uid's grammar is per kind. An IMAP uid is "<folder>:<uidvalidity>:<uid>";
# the folder (decoded to Unicode) may itself contain ":", so it is read from the right.
# UIDVALIDITY and UID are non-zero (RFC 9051) and written without leading zeros, so
# a reference has exactly one spelling.
_UID = {
    "gmail": re.compile(r"[0-9a-f]{8,32}"),
    "imap": re.compile(r"(?P<folder>[^\x00-\x1f]+):(?P<uidvalidity>[1-9][0-9]*):(?P<uid>[1-9][0-9]*)"),
    "jmap": re.compile(r"[A-Za-z0-9_-]{1,255}"),
    "graph": re.compile(r"[A-Za-z0-9_=+/-]{1,512}"),
}


class MessageRef(NamedTuple):
    """A parsed ``corpus.id/v1`` message reference: ``<kind>:<mailbox>::<uid>``.

    ``mailbox`` is a configured source name; it resolves to a credential in each
    service's own config and is never carried in the id itself.
    """

    kind: str
    mailbox: str
    uid: str

    @property
    def source(self) -> str:
        """The record source this reference belongs to, e.g. ``gmail:personal``."""
        return f"{self.kind}:{self.mailbox}"

    def imap_parts(self) -> tuple[str, int, int]:
        """``(folder, uidvalidity, uid)`` of an ``imap`` reference."""
        if self.kind != "imap":
            raise ValueError(f"not an imap reference: {self.kind}")
        m = _UID["imap"].fullmatch(self.uid)
        assert m is not None  # parse_id validated it
        return m["folder"], int(m["uidvalidity"]), int(m["uid"])

    def __str__(self) -> str:
        return Record.key_for(self.source, self.uid)


def parse_id(ref: str) -> MessageRef:
    """Parse a ``corpus.id/v1`` message reference, or raise ``ValueError``.

    The inverse of :meth:`Record.key` for mail sources. A ``uri`` is a human link,
    not a reference, and is never parsed here.
    """
    m = _ID.fullmatch(ref)
    if m is None:
        raise ValueError("not a corpus.id/v1 message reference")
    kind, uid = m["kind"], m["uid"]
    grammar = _UID.get(kind)
    if grammar is None:
        raise ValueError(f"unknown message kind {kind!r}")
    if not grammar.fullmatch(uid):
        raise ValueError(f"malformed {kind} uid")
    return MessageRef(kind, m["mailbox"], uid)
