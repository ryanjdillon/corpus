"""The shared RFC 822 mapping produces exactly the Records each adapter produced before.

The two reference functions are the adapters' own mappings as they were before
the mapping was shared, kept verbatim, and every adapter's Record is compared
field by field on the same synthetic messages: the ones the adapter tests use,
plus the edge cases each branch of the mapping handles.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import create_autospec
from urllib.parse import quote

import mailparser
import pytest
from imapclient import IMAPClient

from corpus.fetchers.base import as_text
from corpus.fetchers.gmail import GmailFetcher
from corpus.fetchers.imap import ImapFetcher
from corpus.models import Record
from corpus.sources import GmailSource, ImapSource
from corpus.sources.rfc822 import record_from_rfc822


def gmail_before(
    source: str, account: str | None, mid: str, thread_id: str | None, raw: bytes, labels: list[str]
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
        source=source,
        source_uid=mid,
        kind="email",
        account=account,
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


def imap_before(
    source: str, user: str, host: str, folder: str, validity: int, uid: int, raw: bytes
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
        source=source,
        source_uid=f"{folder}:{validity}:{uid}",
        kind="email",
        account=user,
        folder=folder,
        thread_id=headers.get("Message-ID"),
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject=as_text(parsed.subject),
        sent_at=sent_at,
        headers=headers,
        uri=f"imap://{host}/{quote(folder)};UIDVALIDITY={validity}/;UID={uid}",
        body_text=body or "",
    )


def _email_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.org"
    msg["To"] = "box@example.org"
    msg["Subject"] = "before"
    msg["Date"] = "Wed, 30 Sep 2026 10:00:00 +0000"
    msg.set_content("body")
    return msg.as_bytes()


def _html_only() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Club Rota <rota@example.com>"
    msg["To"] = "a@example.com, B <b@example.com>"
    msg["Subject"] = "=?utf-8?b?UsO4cm9zw6Ugw7h2ZWxzZQ==?="
    msg["Message-ID"] = "<invented-1@example.com>"
    msg.set_content("<p>An invented <b>rota</b> note.</p>", subtype="html")
    return msg.as_bytes()


def _alternative() -> bytes:
    msg = EmailMessage()
    msg["From"] = "news@example.com"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Winter sale"
    msg["List-Unsubscribe"] = "<mailto:unsub@example.com>"
    msg["Date"] = "Thu, 01 Oct 2026 08:30:00 +0200"
    msg.set_content("plain 50% off")
    msg.add_alternative("<p>html 50% off</p>", subtype="html")
    return msg.as_bytes()


#: Synthetic messages: the adapters' test fixtures, then one edge case per branch.
MESSAGES = {
    # test_gmail._raw: no Date, so no sent_at.
    "gmail-api": b"From: sender@example.org\r\nTo: me@example.com\r\nSubject: msg m1\r\n\r\n"
    b"body of msg m1\r\n",
    # test_gmail_message_errors.RAW
    "gmail-errors": b"From: a@example.org\r\nTo: b@example.org\r\nSubject: hi\r\n"
    b"Date: Wed, 30 Sep 2026 10:00:00 +0000\r\n\r\nbody\r\n",
    # test_imap_uidvalidity._raw
    "imap-uidvalidity": _email_message(),
    # test_imap's appended Archive message: no To, no Date.
    "imap-archive": b"From: sender@example.org\r\nSubject: Archived item\r\n\r\narchived body\r\n",
    "naive-date": b"From: a@example.com\r\nSubject: naive\r\nDate: Wed, 30 Sep 2026 10:00:00\r\n"
    b"\r\nbody\r\n",
    "offset-date": _alternative(),
    "html-only-encoded-subject": _html_only(),
    "duplicate-subject": b"From: a@example.com\r\nSubject: one\r\nSubject: two\r\n\r\nbody\r\n",
    "no-from-empty-body": b"To: a@example.com\r\nSubject: empty\r\n\r\n",
    "blank-subject": b"From: a@example.com\r\nSubject:   \r\n\r\nbody\r\n",
}


@pytest.fixture(params=list(MESSAGES), ids=list(MESSAGES))
def raw(request) -> bytes:
    return MESSAGES[request.param]


def test_the_gmail_adapter_maps_as_before(raw):
    environ = {
        "CORPUS_GMAIL_UNIT_CLIENT_ID": "cid",
        "CORPUS_GMAIL_UNIT_CLIENT_SECRET": "cs",
        "CORPUS_GMAIL_UNIT_REFRESH_TOKEN": "rt",
    }
    fetcher = GmailFetcher(GmailSource(name="unit", enrich=True), environ=environ)
    fetcher._account = "me@example.com"

    record = fetcher._to_record("18c2f0a1b2c3d4e5", "t1", raw, ["INBOX", "Receipts"])

    expected = gmail_before(
        "gmail:unit", "me@example.com", "18c2f0a1b2c3d4e5", "t1", raw, ["INBOX", "Receipts"]
    )
    assert record.model_dump() == expected.model_dump()


@pytest.mark.parametrize("folder", ["INBOX", "Arkiv/Båt 2026"])
def test_the_imap_adapter_maps_as_before(raw, folder):
    environ = {
        "CORPUS_IMAP_UNIT_HOST": "mail.example.org",
        "CORPUS_IMAP_UNIT_USER": "box@example.org",
        "CORPUS_IMAP_UNIT_PASSWORD": "x",
    }
    client = create_autospec(IMAPClient, instance=True)
    fetcher = ImapFetcher(
        ImapSource(name="unit", enrich=True), environ=environ, connect=lambda *_a, **_k: client
    )

    record = fetcher._to_record(folder, 7, 42, raw)

    expected = imap_before("imap:unit", "box@example.org", "mail.example.org", folder, 7, 42, raw)
    assert record.model_dump() == expected.model_dump()


def test_every_sent_at_is_aware(raw):
    sent_at = record_from_rfc822("imap:x", "1", raw).sent_at
    assert sent_at is None or sent_at.tzinfo is not None


def test_an_offset_date_is_kept_as_the_same_instant():
    record = record_from_rfc822("imap:x", "1", MESSAGES["offset-date"])
    assert record.sent_at == datetime(2026, 10, 1, 6, 30, tzinfo=UTC)


def test_the_plain_part_is_the_body_when_there_is_one():
    assert record_from_rfc822("imap:x", "1", MESSAGES["offset-date"]).body_text.strip() == (
        "plain 50% off"
    )


def test_provider_fields_are_taken_as_given():
    record = record_from_rfc822(
        "gmail:x", "u1", MESSAGES["gmail-errors"], account="me@example.com", folder=None,
        thread_id="t9", labels=("INBOX",), uri="https://mail.google.com/mail/u/0/#all/u1",
    )
    assert (record.source, record.source_uid, record.kind) == ("gmail:x", "u1", "email")
    assert (record.account, record.thread_id, record.labels) == ("me@example.com", "t9", ["INBOX"])
    assert record.uri == "https://mail.google.com/mail/u/0/#all/u1"


def test_a_gmail_message_decodes_from_the_api_encoding():
    # The API returns the message base64url-encoded; the adapter decodes before mapping.
    encoded = base64.urlsafe_b64encode(MESSAGES["gmail-errors"]).decode()
    record = record_from_rfc822("gmail:x", "u1", base64.urlsafe_b64decode(encoded))
    assert (record.subject, record.from_addr, record.to_addrs) == (
        "hi", "a@example.org", ["b@example.org"]
    )
