import json
from datetime import UTC, datetime

import pytest

from corpus import scan as scan_mod
from corpus.embeddings import Embedder
from corpus.models import Classification, Record
from corpus.store import get_document_store, to_document

pytestmark = pytest.mark.integration


def _index(uid: str, body: str, subject: str = "s") -> None:
    rec = Record(
        source="imap:test",
        source_uid=uid,
        kind="email",
        account="me@x.org",
        from_addr="a@b.com",
        subject=subject,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        body_text=body,
    )
    embedder = Embedder()
    try:
        vec = embedder.embed_one(body)
    finally:
        embedder.close()
    doc = to_document(rec, Classification(label="personal", confidence=0.9), vec)
    get_document_store().write_documents([doc])


def test_scan_archive_flags_secret_without_value(pg, fake_embeddings):
    _index("1", "My SSN is 900-12-3456 for the enrollment form.")
    _index("2", "Are we still on for lunch tomorrow?")
    report = scan_mod.scan_archive()
    assert report["scanned"] == 2
    assert report["with_secrets"] == 1
    assert "us_ssn" in report["totals"]
    hit = report["hits"][0]
    assert hit["id"] == "imap:test::1"
    assert "us_ssn" in hit["secret_types"]
    # the value must never appear anywhere in the report
    assert "900-12-3456" not in json.dumps(report)


def test_scan_archive_source_and_account_filters(pg, fake_embeddings):
    _index("1", "My SSN is 900-12-3456 for the form.")
    assert scan_mod.scan_archive(source="imap:test")["with_secrets"] == 1
    assert scan_mod.scan_archive(source="other:src")["scanned"] == 0
    assert scan_mod.scan_archive(account="me@x.org")["scanned"] == 1
    assert scan_mod.scan_archive(account="nobody@x.org")["scanned"] == 0


def test_scan_archive_no_table(pg):
    # nothing indexed yet -> the documents table doesn't exist -> empty, no crash
    report = scan_mod.scan_archive()
    assert report == {"scanned": 0, "with_secrets": 0, "totals": {}, "hits": []}


def test_scan_archive_limit(pg, fake_embeddings):
    _index("1", "one")
    _index("2", "two")
    _index("3", "three")
    assert scan_mod.scan_archive(limit=2)["scanned"] == 2
