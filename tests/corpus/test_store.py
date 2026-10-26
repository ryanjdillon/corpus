import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock, create_autospec

import psycopg
import pytest

from corpus.enrich_batch import run_enrich
from corpus.enrich_store import EnrichStore
from corpus.enricher import Enricher
from corpus.enrichment import RECOVERED_SCHEMA_VERSION, SCHEMA_VERSION, Category, Enrichment
from corpus.models import Classification, Record
from corpus.secret_audit import audit_secrets
from corpus.sources import ImapSource, Registry
from corpus.store import Backlog, DocumentStore, get_cursor, set_cursor, to_document

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


def _ingested_at(dsn: str) -> dict[str, datetime]:
    """Every stored document's ``ingested_at``, by id."""
    with psycopg.connect(dsn) as conn:
        return dict(conn.execute("SELECT id, ingested_at FROM corpus.documents").fetchall())


def _has_ingested_at(dsn: str) -> bool:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'corpus'"
            " AND table_name = 'documents' AND column_name = 'ingested_at'"
        ).fetchone() is not None


def _drop_ingested_at(dsn: str) -> None:
    """Leave the table as a release before the column created it."""
    with psycopg.connect(dsn) as conn:
        conn.execute("ALTER TABLE corpus.documents DROP COLUMN ingested_at")


def test_the_first_write_adds_the_ingest_time_to_the_table_it_creates(pg, documents):
    documents.write([_doc("1")])

    assert set(_ingested_at(pg)) == {"imap:test::1"}


def test_opening_adds_the_ingest_time_to_a_table_from_before_it(pg, documents):
    documents.write([_doc("1"), _doc("2")])
    _drop_ingested_at(pg)

    with DocumentStore():
        pass

    # Rows already stored read the time of the migration: one value, not NULL.
    stamped = _ingested_at(pg)
    assert set(stamped) == {"imap:test::1", "imap:test::2"}
    assert len(set(stamped.values())) == 1


def _hold_the_table(conn: psycopg.Connection) -> None:
    """Hold ACCESS SHARE on the documents table, as a long reader does, until rollback."""
    conn.execute("SELECT count(*) FROM corpus.documents")


def _wait_for_lock_waiters(dsn: str, waiters: int, timeout: float = 10.0) -> None:
    """Return once *waiters* sessions queue for the documents table's lock."""
    deadline = time.monotonic() + timeout
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < deadline:
            queued = conn.execute(
                "SELECT count(*) FROM pg_locks"
                " WHERE NOT granted AND relation = 'corpus.documents'::regclass"
            ).fetchone()
            if queued is not None and queued[0] >= waiters:
                return
            time.sleep(0.01)
    raise TimeoutError(f"{waiters} lock waiters never queued")


def test_opening_a_table_with_the_ingest_time_takes_no_lock(pg, documents, caplog):
    documents.write([_doc("1")])
    # A long reader holds the table: an ALTER would time out behind it.
    with psycopg.connect(pg) as reader:
        _hold_the_table(reader)

        with DocumentStore(lock_timeout="50ms") as reopened:
            assert reopened.enrich_backlog("imap:test", "local").count == 1

    assert "ingested_at not added" not in caplog.text


def test_a_writer_that_cannot_take_the_lock_goes_on_without_the_column(pg, documents, caplog):
    documents.write([_doc("1")])
    _drop_ingested_at(pg)
    with psycopg.connect(pg) as reader:
        _hold_the_table(reader)

        with DocumentStore(lock_timeout="50ms") as degraded:
            assert degraded.enrich_backlog("imap:test", "local") == Backlog(1, None)
        assert not _has_ingested_at(pg)
        assert "corpus.documents: ingested_at not added (LockNotAvailable)" in caplog.text

    # The next writable open, with the reader gone, adds it.
    with DocumentStore(lock_timeout="50ms") as retried:
        assert retried.enrich_backlog("imap:test", "local").oldest is not None
    assert _has_ingested_at(pg)


def test_two_writers_racing_the_migration_add_one_column(pg, documents):
    documents.write([_doc("1")])
    _drop_ingested_at(pg)
    with psycopg.connect(pg) as reader, ThreadPoolExecutor(2) as pool:
        _hold_the_table(reader)
        # Both find the column missing and queue their ALTER behind the reader.
        opening = [pool.submit(DocumentStore, lock_timeout="30s") for _ in range(2)]
        _wait_for_lock_waiters(pg, 2)
        reader.rollback()
        writers = [o.result(timeout=30) for o in opening]

    try:
        # The second waited for the first, then found the column and added nothing.
        with psycopg.connect(pg) as conn:
            columns = conn.execute(
                "SELECT count(*) FROM pg_attribute WHERE attrelid = 'corpus.documents'::regclass"
                " AND attname = 'ingested_at' AND NOT attisdropped"
            ).fetchone()
        assert columns == (1,)
        for writer in writers:
            assert writer.enrich_backlog("imap:test", "local").oldest is not None
    finally:
        for writer in writers:
            writer.close()


def test_a_writer_that_does_not_own_the_table_goes_on_without_the_column(pg, documents, caplog):
    documents.write([_doc("1")])
    _drop_ingested_at(pg)
    # A role that may write the table but, not owning it, may not alter it.
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'docs_rw') "
            "THEN CREATE ROLE docs_rw LOGIN PASSWORD 'rw'; END IF; END $$"
        )
        conn.execute("GRANT USAGE ON SCHEMA corpus TO docs_rw")
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON corpus.documents TO docs_rw")

    with DocumentStore(pg.replace("postgres:test@", "docs_rw:rw@")) as writer:
        assert writer.enrich_backlog("imap:test", "local") == Backlog(1, None)

    assert not _has_ingested_at(pg)
    assert "ingested_at not added (InsufficientPrivilege)" in caplog.text


def test_any_other_migration_error_fails_the_open(pg, documents):
    documents.write([_doc("1")])
    _drop_ingested_at(pg)

    with pytest.raises(psycopg.errors.InvalidParameterValue):
        DocumentStore(lock_timeout="not an interval")


def test_a_read_only_store_runs_no_migration(pg, documents):
    documents.write([_doc("1")])
    _drop_ingested_at(pg)

    with DocumentStore(read_only=True) as reader:
        assert reader.stats()["total"] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            reader.delete(["imap:test::1"])

    assert not _has_ingested_at(pg)


def test_an_overwrite_keeps_the_first_ingest_time_and_a_new_document_gets_its_own(
    pg, documents
):
    documents.write([_doc("1")])
    first = _ingested_at(pg)["imap:test::1"]

    rewritten = replace(_doc("1"), content="body 1, edited")
    documents.write([rewritten, _doc("2")])

    stamped = _ingested_at(pg)
    assert stamped["imap:test::1"] == first
    assert stamped["imap:test::2"] > first
    assert documents.get("imap:test::1")["content"] == "body 1, edited"


def test_enrich_backlog_of_a_missing_table_is_nothing(documents):
    assert documents.enrich_backlog("imap:test", "local") == Backlog(0, None)


def test_a_reader_opened_before_any_writer_migrated_counts_the_backlog_alone(pg, documents):
    documents.write([_doc("1"), _doc("2")])
    _drop_ingested_at(pg)

    with DocumentStore(read_only=True) as reader:
        assert reader.enrich_backlog("imap:test", "local") == Backlog(2, None)
        # It looked once: a writer migrating the table now shows on its reopen.
        with DocumentStore():
            pass
        assert reader.enrich_backlog("imap:test", "local") == Backlog(2, None)

    with DocumentStore(read_only=True) as reopened:
        assert reopened.enrich_backlog("imap:test", "local").oldest is not None


def test_enrich_backlog_of_an_ingest_time_with_no_values_has_no_oldest(pg, documents):
    documents.write([_doc("1")])
    # A nullable column of that name, as some earlier migration might leave it.
    _drop_ingested_at(pg)
    with psycopg.connect(pg) as conn:
        conn.execute("ALTER TABLE corpus.documents ADD COLUMN ingested_at timestamptz")

    with DocumentStore() as writer:
        assert writer.enrich_backlog("imap:test", "local") == Backlog(1, None)


def test_enrich_backlog_adds_the_ingest_time_to_a_table_created_after_it_opened(pg, documents):
    with DocumentStore() as other:
        other.write([_doc("1")])
    _drop_ingested_at(pg)

    assert documents.enrich_backlog("imap:test", "local").count == 1
    assert _has_ingested_at(pg)


def test_enrich_backlog_is_since_its_oldest_document_was_first_ingested(documents):
    documents.write([_doc(uid) for uid in "abcd"] + [_doc("z", source="gmail:other")])
    # Distinct ingest times, oldest first: a on the 1st, b on the 2nd, ...
    for day, uid in enumerate("abcd", start=1):
        documents._write(
            "UPDATE corpus.documents SET ingested_at = %s WHERE id = %s",
            (datetime(2026, 1, day, tzinfo=UTC), f"imap:test::{uid}"),
        )
    with EnrichStore() as enrichments:
        enrichments.save_enrichment("imap:test::a", {"one_line": "a"}, "local", "sv1")
        enrichments.save_rejection("imap:test::b", "400: too long", "local")

        # Only what the run would send counts: a is done, b rejected by local.
        assert documents.enrich_backlog("imap:test", "local") == Backlog(
            2, datetime(2026, 1, 3, tzinfo=UTC)
        )
        assert documents.enrich_backlog("imap:test", "other") == Backlog(
            3, datetime(2026, 1, 2, tzinfo=UTC)
        )
        enrichments.save_enrichment("imap:test::c", {"one_line": "c"}, "local", "sv1")
        enrichments.save_enrichment("imap:test::d", {"one_line": "d"}, "local", "sv1")
        assert documents.enrich_backlog("imap:test", "local") == Backlog(0, None)


def test_enrich_backlog_counts_every_document_before_any_enrichment(documents):
    # The enrichments table does not exist until an EnrichStore first opens.
    documents.write([_doc("1"), _doc("2"), _doc("3", source="gmail:other")])

    assert documents.enrich_backlog("imap:test", "local").count == 2


def test_enrich_backlog_is_what_a_scheduled_enrichment_would_send(documents):
    documents.write([_doc(uid) for uid in "abcdef"] + [_doc("g", source="gmail:other")])
    with EnrichStore() as enrichments:
        enrichments.save_enrichment("imap:test::a", {"one_line": "a"}, "local", "sv1")
        # An older schema still counts as done: the pipeline does not upgrade stale.
        enrichments.save_enrichment("imap:test::b", {"one_line": "b"}, "older", "sv0")
        enrichments.save_rejection("imap:test::c", "400: too long", "local")
        # Rejected by another model, or for a rate limit: still to do.
        enrichments.save_rejection("imap:test::d", "400: too long", "other")
        enrichments.save_rejection("imap:test::e", "429: slow down", "local")
        # Only audited: no enrichment yet.
        enrichments.save_audit("imap:test::f", ["token"], {"verdict": "none"}, "local", "s1")

        assert documents.enrich_backlog("imap:test", "local").count == 3  # d, e and f
        assert documents.enrich_backlog("imap:test", "other").count == 3  # c, e and f
        assert documents.enrich_backlog("gmail:other", "local").count == 1


def test_enrich_backlog_is_exactly_what_run_enrich_then_sends(documents):
    # Every kind of row the selection weighs, against the selection itself.
    documents.write([_doc(uid) for uid in "abcdefgh"] + [_doc("z", source="gmail:other")])
    enricher = create_autospec(Enricher, instance=True)
    enricher.model = "local"  # an __init__ attribute, so set explicitly on the spec mock
    enricher.enrich_reporting.return_value = (
        Enrichment(one_line="x", abstract="y", category=Category.personal), False
    )
    with EnrichStore() as enrichments:
        enrichments.save_enrichment("imap:test::a", {"one_line": "a"}, "local", SCHEMA_VERSION)
        enrichments.save_enrichment("imap:test::b", {"one_line": "b"}, "older", "sv0")
        enrichments.save_enrichment(
            "imap:test::c", {"one_line": "c"}, "local", RECOVERED_SCHEMA_VERSION
        )
        enrichments.save_rejection("imap:test::d", "400: too long", "local")
        enrichments.save_rejection("imap:test::e", "429: slow down", "local")
        enrichments.save_rejection("imap:test::f", "400: too long", "other")
        enrichments.save_audit("imap:test::g", ["token"], {"verdict": "none"}, "local", "s1")
        backlog = documents.enrich_backlog("imap:test", "local").count

        # As the pipeline calls it: the source, no limit and no opt-in reselection.
        run_enrich(
            enrichments, documents=documents, source="imap:test", enricher=enricher,
            audit=create_autospec(audit_secrets), concurrency=1,
            sources=Registry([ImapSource(name="test", enrich=True)]), on_scanner=Mock(),
        )

        assert enricher.enrich_reporting.call_count == backlog == 4  # e, f, g and h
        assert documents.enrich_backlog("imap:test", "local") == Backlog(0, None)
