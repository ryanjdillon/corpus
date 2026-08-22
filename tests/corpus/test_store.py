from datetime import datetime, timezone

import pytest

from corpus.models import Classification, Record
from corpus.store import get_cursor, set_cursor, to_document

pytestmark = pytest.mark.integration


def _record(uid: str, subject: str) -> Record:
    return Record(
        source="imap:test",
        source_uid=uid,
        kind="email",
        account="me@x.org",
        from_addr="a@b.com",
        subject=subject,
        sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        body_text=f"body of {subject}",
    )


def test_to_document_maps_metadata():
    rec = _record("7", "hello")
    doc = to_document(rec, Classification(label="personal", confidence=0.5), [0.0] * 4)
    assert doc.id == "imap:test::7"
    assert doc.content == "body of hello"
    assert doc.meta["label"] == "personal"
    assert doc.meta["source_uid"] == "7"
    assert doc.meta["sent_at"].startswith("2026-01-01")
    # None-valued fields are dropped, not stored as null.
    assert "thread_id" not in doc.meta


def test_cursor_roundtrip(pg):
    assert get_cursor("imap:test") is None
    set_cursor("imap:test", "12:99")
    assert get_cursor("imap:test") == "12:99"
    set_cursor("imap:test", "12:150")  # upsert
    assert get_cursor("imap:test") == "12:150"


def test_write_and_read_back(pg):
    from corpus.store import get_document_store

    store = get_document_store()
    rec = _record("1", "kayak")
    doc = to_document(rec, Classification(label="personal", confidence=0.5), [0.1] * 16)
    store.write_documents([doc])
    assert store.count_documents() == 1
    fetched = store.filter_documents()
    assert fetched[0].id == "imap:test::1"
    assert fetched[0].meta["subject"] == "kayak"
