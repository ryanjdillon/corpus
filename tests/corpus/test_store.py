from datetime import UTC, datetime

import pytest

from corpus.enrich_store import EnrichStore
from corpus.models import Classification, Record
from corpus.store import DocumentStore, get_cursor, set_cursor, to_document

pytestmark = pytest.mark.integration


def _record(uid: str, subject: str) -> Record:
    return Record(
        source="imap:test",
        source_uid=uid,
        kind="email",
        account="me@x.org",
        from_addr="a@b.com",
        subject=subject,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
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


def _doc(uid: str, *, source: str = "imap:test", account: str = "me@x.org", sent_at=None):
    rec = Record(
        source=source,
        source_uid=uid,
        kind="email",
        account=account,
        subject=f"mail {uid}",
        sent_at=sent_at,
        body_text=f"body {uid}",
    )
    return to_document(rec, Classification(label="personal", confidence=0.5), [0.1] * 16)


@pytest.fixture
def documents(pg):
    """An open DocumentStore on the test schema, closed after the test."""
    with DocumentStore() as store:
        yield store


def test_write_and_read_back(documents):
    rec = _record("1", "kayak")
    doc = to_document(rec, Classification(label="personal", confidence=0.5), [0.1] * 16)
    documents.write([doc])
    assert documents.stats()["total"] == 1
    fetched = list(documents.iter())
    assert fetched[0][0] == "imap:test::1"
    assert fetched[0][2]["subject"] == "kayak"


def test_write_overwrites_an_existing_id(documents):
    documents.write([_doc("1")])
    documents.write([_doc("1")])
    assert documents.stats()["total"] == 1


def test_a_missing_table_reads_as_an_empty_corpus(documents):
    # Haystack creates the table on its first write, and the store must not.
    assert documents.exists() is False
    assert documents.existing_ids("imap:test") == set()
    assert list(documents.iter()) == []
    assert list(documents.iter_enriched()) == []


def test_existing_ids_are_per_source(documents):
    documents.write([_doc("1"), _doc("2"), _doc("3", source="gmail:other")])
    assert documents.exists() is True
    assert documents.existing_ids("imap:test") == {"imap:test::1", "imap:test::2"}


def test_iter_is_newest_first_undated_last_ties_by_id(documents):
    day = datetime(2026, 1, 2, tzinfo=UTC)
    documents.write([
        _doc("a", sent_at=datetime(2026, 1, 1, tzinfo=UTC)),
        _doc("c", sent_at=day),
        _doc("b", sent_at=day),
        _doc("z"),  # undated
    ])
    assert [i for i, _, _ in documents.iter()] == [
        "imap:test::b", "imap:test::c", "imap:test::a", "imap:test::z",
    ]


def test_iter_filters_by_source_and_account(documents):
    documents.write([
        _doc("1"), _doc("2", account="you@x.org"), _doc("3", source="gmail:other"),
    ])
    assert {i for i, _, _ in documents.iter(source="imap:test")} == {
        "imap:test::1", "imap:test::2",
    }
    assert {i for i, _, _ in documents.iter(account="you@x.org")} == {"imap:test::2"}
    assert [i for i, _, _ in documents.iter(source="imap:test", account="me@x.org")] == [
        "imap:test::1"
    ]


def test_an_abandoned_iteration_leaves_the_store_usable(documents):
    documents.write([_doc("1"), _doc("2")])
    stream = documents.iter()
    next(stream)
    stream.close()
    assert documents.get("imap:test::2")["id"] == "imap:test::2"


def test_iter_enriched_joins_documents_and_enrichments(documents):
    documents.write([
        _doc("1", sent_at=datetime(2026, 1, 1, tzinfo=UTC)),
        _doc("2", sent_at=datetime(2026, 1, 2, tzinfo=UTC)),
        _doc("3", source="gmail:other"),
        _doc("4"),  # never enriched
    ])
    # Only the documents table exists yet: nothing is enriched.
    assert list(documents.iter_enriched()) == []
    with EnrichStore() as enrichments:
        for uid in ("imap:test::1", "imap:test::2", "gmail:other::3"):
            enrichments.save_enrichment(uid, {"one_line": uid}, "local", "sv1")
        enrichments.save_rejection("imap:test::4", "400: too long", "local")

    rows = list(documents.iter_enriched(source="imap:test"))

    assert [(r[0], r[1]["source"], r[2]["one_line"]) for r in rows] == [
        ("imap:test::2", "imap:test", "imap:test::2"),
        ("imap:test::1", "imap:test", "imap:test::1"),
    ]
    assert all(r[3] is not None for r in rows)  # enriched_at, the sync's version
    assert len(list(documents.iter_enriched())) == 3


def test_iter_enriched_reads_a_record_from_before_roles_in_the_current_shape(documents):
    documents.write([_doc("1")])
    old = {"one_line": "notes from a classmate", "category": "personal",
           "people": [{"name": "Pat Example", "role": None}, {"name": "Kim Example"}]}
    with EnrichStore() as enrichments:
        enrichments.save_enrichment("imap:test::1", old, "local", "sv0")

    [(_, _, enr, _)] = documents.iter_enriched()

    assert enr["people"] == [{"name": "Pat Example", "role": "unknown"},
                             {"name": "Kim Example", "role": "unknown"}]
    assert enr["references"] == []
    assert enr["one_line"] == "notes from a classmate"


def test_iter_enriched_closed_early_releases_its_cursor(documents):
    documents.write([_doc("1"), _doc("2")])
    with EnrichStore() as enrichments:
        for uid in ("imap:test::1", "imap:test::2"):
            enrichments.save_enrichment(uid, {"one_line": uid}, "local", "sv1")

    rows = documents.iter_enriched()
    next(rows)
    rows.close()

    # The server-side cursor and its transaction are gone: the connection reads again.
    assert len(list(documents.iter_enriched())) == 2
