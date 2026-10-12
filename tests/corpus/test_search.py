from datetime import UTC, datetime, timedelta

import pytest

from corpus import query, search
from corpus.embeddings import Embedder
from corpus.models import Classification, Record
from corpus.store import DocumentStore, to_document

pytestmark = pytest.mark.integration


@pytest.fixture
def documents(pg):
    """An open DocumentStore on the test schema, closed after the test."""
    with DocumentStore() as store:
        yield store


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
    with DocumentStore() as store:
        store.write([doc])


def test_semantic_search_finds_nearest(documents, fake_embeddings):
    _index("1", "kayak trip", "planning a kayak trip on the fjord", "personal", "2026-01-10")
    _index("2", "sale", "50% off winter jackets today only", "promotional", "2026-01-11")
    results = search.semantic_search(documents, "planning a kayak trip on the fjord", top_k=1)
    assert results[0]["id"] == "imap:test::1"
    assert results[0]["label"] == "personal"


def test_structured_query_filters_by_label_and_date(documents, fake_embeddings):
    _index("1", "old promo", "buy now", "promotional", "2026-01-01")
    _index("2", "new promo", "buy again", "promotional", "2026-02-20")
    _index("3", "note", "personal note", "personal", "2026-01-01")
    results = documents.query(label="promotional", before="2026-02-01")
    ids = {r["id"] for r in results}
    assert ids == {"imap:test::1"}


def test_structured_query_filters_by_account(documents, fake_embeddings):
    _index("1", "note", "a note", "personal", "2026-01-01")

    assert len(documents.query(account="me@x.org")) == 1
    assert documents.query(account="other@x.org") == []


def test_structured_query_since_window(documents, fake_embeddings):
    now = datetime.now(UTC)
    _index("1", "recent", "recent mail", "personal", (now - timedelta(hours=2)).isoformat())
    _index("2", "old", "old mail", "personal", (now - timedelta(days=5)).isoformat())
    results = documents.query(after=query.relative_cutoff("1d"))
    assert {r["id"] for r in results} == {"imap:test::1"}


def test_relative_cutoff_parses_and_rejects():
    cutoff = query.relative_cutoff("2h")
    assert cutoff < datetime.now(UTC).isoformat()
    with pytest.raises(ValueError):
        query.relative_cutoff("nonsense")


def test_get_document_returns_full_record(documents, fake_embeddings):
    body = "the complete body text, far longer than any 300-char snippet. " * 8
    _index("1", "full record", body, "personal", "2026-01-10")
    doc = documents.get("imap:test::1")
    assert doc is not None
    assert doc["id"] == "imap:test::1"
    assert doc["content"] == body
    assert doc["meta"]["subject"] == "full record"
    assert doc["meta"]["label"] == "personal"


def test_get_document_missing_returns_none(documents, fake_embeddings):
    _index("1", "present", "present body", "personal", "2026-01-10")
    assert documents.get("imap:test::nope") is None


def test_stats_counts_by_label(documents, fake_embeddings):
    _index("1", "a", "a", "promotional", "2026-01-01")
    _index("2", "b", "b", "promotional", "2026-01-02")
    _index("3", "c", "c", "personal", "2026-01-03")
    result = documents.stats()
    assert result["total"] == 3
    assert result["by_label"]["promotional"] == 2
    assert result["by_label"]["personal"] == 1


def test_structured_query_keyset_pages_cover_every_row_once(documents, fake_embeddings):
    # Two rows share a sent_at, so only the (sent_at, id) keyset keeps them apart.
    for uid, sent_at in [("1", "2026-01-01"), ("2", "2026-01-02"), ("3", "2026-01-02"),
                         ("4", "2026-01-03"), ("5", "2026-01-04")]:
        _index(uid, f"mail {uid}", f"body {uid}", "personal", sent_at)

    seen, older_than = [], None
    while True:
        page = documents.query(limit=2, older_than=older_than)
        seen += [r["id"] for r in page]
        if len(page) < 2:
            break
        older_than = (page[-1]["sent_at"], page[-1]["id"])

    assert len(seen) == len(set(seen)) == 5
    assert seen[0] == "imap:test::5" and seen[-1] == "imap:test::1"


def test_structured_query_keyset_pages_reach_undated_rows_last(documents, fake_embeddings):
    # An undated row sorts after every dated one and still compares in the keyset.
    _index("1", "dated", "body 1", "personal", "2026-01-01")
    documents.write([to_document(
        Record(source="imap:test", source_uid="0", kind="email", body_text="undated"),
        Classification(label="personal", confidence=0.9),
        [0.1] * 16,
    )])

    first = documents.query(limit=1)
    rest = documents.query(limit=5, older_than=(first[0]["sent_at"], first[0]["id"]))

    assert [r["id"] for r in first + rest] == ["imap:test::1", "imap:test::0"]
    assert rest[0]["sent_at"] is None


def test_search_ranks_past_the_pgvector_default_on_the_index_path(pg, fake_embeddings, monkeypatch):
    # pgvector's default hnsw.ef_search (40) caps an index scan at 40 rows whatever
    # the LIMIT. A small table seqscans and hides that, so force the index path.
    import psycopg

    from corpus.config import settings
    from corpus.store import HNSW_EF_SEARCH

    for i in range(60):
        _index(str(i), f"note {i}", f"kayak trip note number {i}", "personal", "2026-01-01")
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute("ANALYZE corpus.documents")
    sep = "&" if "?" in pg else "?"
    monkeypatch.setattr(settings, "database_url", f"{pg}{sep}options=-c%20enable_seqscan%3Doff")

    with DocumentStore() as documents:
        results = search.semantic_search(documents, "kayak trip", top_k=55)

    assert HNSW_EF_SEARCH >= 55
    assert len(results) == 55
