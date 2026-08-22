from datetime import datetime, timezone

from corpus.models import Classification, Record


def test_record_key_combines_source_and_uid():
    rec = Record(source="imap:boat", source_uid="42", kind="email")
    assert rec.key() == "imap:boat::42"


def test_record_defaults():
    rec = Record(source="s", source_uid="1", kind="email")
    assert rec.to_addrs == []
    assert rec.headers == {}
    assert rec.body_text == ""
    assert rec.sent_at is None


def test_record_accepts_full_payload():
    rec = Record(
        source="imap:x",
        source_uid="9",
        kind="email",
        account="me@x.org",
        folder="INBOX",
        from_addr="a@b.com",
        to_addrs=["c@d.com", "e@f.com"],
        subject="hello",
        sent_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        headers={"X-Test": "1"},
        body_text="hi there",
    )
    assert rec.to_addrs == ["c@d.com", "e@f.com"]
    assert rec.headers["X-Test"] == "1"


def test_classification_defaults_signals():
    c = Classification(label="personal", confidence=0.5)
    assert c.signals == {}
