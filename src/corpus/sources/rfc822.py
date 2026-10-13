"""An RFC 822 message as a Record: the mapping every mail adapter shares.

The adapters differ in what the provider knows about a message (its id, thread,
labels, folder, link), and pass that in; everything read from the message itself
is mapped here, once.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

import mailparser

from ..fetchers.base import as_text
from ..models import Record


class _ThreadBy(Enum):
    MESSAGE_ID = "Message-ID"


#: Pass as ``thread_id`` to thread a message by its own ``Message-ID`` header:
#: for a provider with no thread id of its own (IMAP). Resolved from the headers
#: this module parses, so the message is parsed once.
MESSAGE_ID = _ThreadBy.MESSAGE_ID


def record_from_rfc822(
    source: str,
    uid: str,
    raw: bytes,
    *,
    account: str | None = None,
    folder: str | None = None,
    thread_id: str | Literal[_ThreadBy.MESSAGE_ID] | None = None,
    labels: Iterable[str] = (),
    uri: str | None = None,
) -> Record:
    """Parse the raw message *raw* into a Record of *source* with id *uid*.

    The body is the first ``text/plain`` part, else the parsed body (HTML-only
    mail). A ``Date`` without a zone is taken as UTC, so every ``sent_at`` is
    aware. *uri* must come from provider identifiers only, never from the
    message (see ``models.Record.uri``). *thread_id* is the provider's thread
    id, or :data:`MESSAGE_ID` for the message's ``Message-ID`` header.

    Raises:
        Exception: Whatever the parser raises on a message it cannot read; an
            adapter skips that one message.
    """
    parsed = mailparser.parse_from_bytes(raw)
    headers = {k: str(v) for k, v in (parsed.headers or {}).items()}
    if thread_id is MESSAGE_ID:
        thread_id = headers.get(MESSAGE_ID.value)
    sent_at: datetime | None = parsed.date
    if sent_at and sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    from_addr = parsed.from_[0][1] if parsed.from_ else None
    to_addrs = [addr for _, addr in (parsed.to or [])]
    body = parsed.text_plain[0] if parsed.text_plain else (parsed.body or "")
    return Record(
        source=source,
        source_uid=uid,
        kind="email",
        account=account,
        folder=folder,
        thread_id=thread_id,
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject=as_text(parsed.subject),
        sent_at=sent_at,
        labels=list(labels),
        headers=headers,
        uri=uri,
        body_text=body or "",
    )
