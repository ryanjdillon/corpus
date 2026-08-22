"""Ingestion pipeline against real Postgres + the fake embedder, with a stubbed
fetcher so the test doesn't depend on a mail server."""

from datetime import UTC, datetime

import pytest

from corpus import ingest as ingest_mod
from corpus.models import Record

pytestmark = pytest.mark.integration


class _FakeFetcher:
    source = "imap:test"

    def __init__(self, records, cursor="1:2"):
        self._records = records
        self._cursor = cursor

    def fetch(self, cursor):
        yield from self._records

    def next_cursor(self):
        return self._cursor


def _records(n: int) -> list[Record]:
    return [
        Record(
            source="imap:test",
            source_uid=str(i),
            kind="email",
            from_addr="a@b.com",
            subject=f"subject {i}",
            sent_at=datetime(2026, 1, i + 1, tzinfo=UTC),
            headers={"List-Unsubscribe": "<mailto:x>", "Precedence": "bulk"},
            body_text=f"promotional body {i}",
        )
        for i in range(n)
    ]


def test_ingest_writes_and_advances_cursor(pg, fake_embeddings, monkeypatch):
    fetcher = _FakeFetcher(_records(3), cursor="1:3")
    monkeypatch.setattr(ingest_mod, "build_fetcher", lambda source: fetcher)

    count = ingest_mod.ingest("imap:test", batch_size=2)
    assert count == 3

    from corpus.store import get_cursor, get_document_store

    assert get_document_store().count_documents() == 3
    assert get_cursor("imap:test") == "1:3"


def test_ingest_applies_classification(pg, fake_embeddings, monkeypatch):
    fetcher = _FakeFetcher(_records(1), cursor="1:1")
    monkeypatch.setattr(ingest_mod, "build_fetcher", lambda source: fetcher)
    ingest_mod.ingest("imap:test")

    from corpus.store import get_document_store

    doc = get_document_store().filter_documents()[0]
    assert doc.meta["label"] == "promotional"


def test_ingest_is_idempotent(pg, fake_embeddings, monkeypatch):
    monkeypatch.setattr(
        ingest_mod, "build_fetcher", lambda source: _FakeFetcher(_records(2), "1:2")
    )
    ingest_mod.ingest("imap:test")
    ingest_mod.ingest("imap:test")  # same ids overwrite, not duplicate

    from corpus.store import get_document_store

    assert get_document_store().count_documents() == 2
