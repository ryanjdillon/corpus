from datetime import UTC, datetime, timedelta

import pytest

from corpus import search
from corpus.embeddings import Embedder
from corpus.models import Classification, Record
from corpus.store import get_document_store, to_document

pytestmark = pytest.mark.integration


def _index(uid: str, subject: str, body: str, label: str, sent_at: str) -> None:
    rec = Record(
        source="imap:test",
        source_uid=uid,
        kind="email",
        account="me@x.org",
        from_addr="a@b.com",
        subject=subject,
        sent_at=datetime.fromisoformat(sent_at).replace(tzinfo=UTC),
        body_text=body,
    )
    embedder = Embedder()
    try:
        vec = embedder.embed_one(body)
    finally:
        embedder.close()
    doc = to_document(rec, Classification(label=label, confidence=0.9), vec)
    get_document_store().write_documents([doc])


def test_semantic_search_finds_nearest(pg, fake_embeddings):
    _index("1", "kayak trip", "planning a kayak trip on the fjord", "personal", "2026-01-10")
    _index("2", "sale", "50% off winter jackets today only", "promotional", "2026-01-11")
    results = search.semantic_search("planning a kayak trip on the fjord", top_k=1)
    assert results[0]["id"] == "imap:test::1"
    assert results[0]["label"] == "personal"


def test_structured_query_filters_by_label_and_date(pg, fake_embeddings):
    _index("1", "old promo", "buy now", "promotional", "2026-01-01")
    _index("2", "new promo", "buy again", "promotional", "2026-02-20")
    _index("3", "note", "personal note", "personal", "2026-01-01")
    results = search.structured_query(label="promotional", before="2026-02-01")
    ids = {r["id"] for r in results}
    assert ids == {"imap:test::1"}


def test_structured_query_filters_by_account(pg, fake_embeddings):
    _index("1", "note", "a note", "personal", "2026-01-01")

    assert len(search.structured_query(account="me@x.org")) == 1
    assert search.structured_query(account="other@x.org") == []


def test_structured_query_since_window(pg, fake_embeddings):
    now = datetime.now(UTC)
    _index("1", "recent", "recent mail", "personal", (now - timedelta(hours=2)).isoformat())
    _index("2", "old", "old mail", "personal", (now - timedelta(days=5)).isoformat())
    results = search.structured_query(since="1d")
    assert {r["id"] for r in results} == {"imap:test::1"}


def test_relative_cutoff_parses_and_rejects():
    cutoff = search.relative_cutoff("2h")
    assert cutoff < datetime.now(UTC).isoformat()
    with pytest.raises(ValueError):
        search.relative_cutoff("nonsense")


def test_get_document_returns_full_record(pg, fake_embeddings):
    body = "the complete body text, far longer than any 300-char snippet. " * 8
    _index("1", "full record", body, "personal", "2026-01-10")
    doc = search.get_document("imap:test::1")
    assert doc is not None
    assert doc["id"] == "imap:test::1"
    assert doc["content"] == body
    assert doc["meta"]["subject"] == "full record"
    assert doc["meta"]["label"] == "personal"


def test_get_document_missing_returns_none(pg, fake_embeddings):
    _index("1", "present", "present body", "personal", "2026-01-10")
    assert search.get_document("imap:test::nope") is None


def test_stats_counts_by_label(pg, fake_embeddings):
    _index("1", "a", "a", "promotional", "2026-01-01")
    _index("2", "b", "b", "promotional", "2026-01-02")
    _index("3", "c", "c", "personal", "2026-01-03")
    result = search.stats()
    assert result["total"] == 3
    assert result["by_label"]["promotional"] == 2
    assert result["by_label"]["personal"] == 1


def test_structured_query_keyset_pages_cover_every_row_once(pg, fake_embeddings):
    # Two rows share a sent_at, so only the (sent_at, id) keyset keeps them apart.
    for uid, sent_at in [("1", "2026-01-01"), ("2", "2026-01-02"), ("3", "2026-01-02"),
                         ("4", "2026-01-03"), ("5", "2026-01-04")]:
        _index(uid, f"mail {uid}", f"body {uid}", "personal", sent_at)

    seen, older_than = [], None
    while True:
        page = search.structured_query(limit=2, older_than=older_than)
        seen += [r["id"] for r in page]
        if len(page) < 2:
            break
        older_than = (page[-1]["sent_at"], page[-1]["id"])

    assert len(seen) == len(set(seen)) == 5
    assert seen[0] == "imap:test::5" and seen[-1] == "imap:test::1"
