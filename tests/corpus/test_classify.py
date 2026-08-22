from datetime import UTC, datetime

from corpus.classify import classify
from corpus.models import Record


def _email(headers=None, from_addr="a@b.com") -> Record:
    return Record(
        source="imap:test",
        source_uid="1",
        kind="email",
        from_addr=from_addr,
        subject="hi",
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        headers=headers or {},
        body_text="body",
    )


def test_document_kind_is_labelled_document():
    rec = Record(source="fs", source_uid="x", kind="file", body_text="text")
    result = classify(rec)
    assert result.label == "document"
    assert result.confidence == 1.0


def test_plain_personal_email():
    result = classify(_email())
    assert result.label == "personal"
    assert result.signals == {}


def test_promotional_needs_unsubscribe_plus_bulk_marker():
    rec = _email(headers={"List-Unsubscribe": "<mailto:x>", "Precedence": "bulk"})
    result = classify(rec)
    assert result.label == "promotional"
    assert result.confidence >= 0.9
    assert result.signals["list_unsubscribe"] is True
    assert result.signals["precedence"] == "bulk"


def test_promotional_via_esp_sender():
    rec = _email(
        headers={"List-Unsubscribe": "<mailto:x>"},
        from_addr="news@bounce.mailchimp.com",
    )
    result = classify(rec)
    assert result.label == "promotional"
    assert result.signals["esp"] is True


def test_unsubscribe_only_is_newsletter():
    rec = _email(headers={"List-Unsubscribe": "<mailto:x>"})
    result = classify(rec)
    assert result.label == "newsletter"


def test_auto_submitted_is_notification():
    rec = _email(headers={"Auto-Submitted": "auto-generated"})
    result = classify(rec)
    assert result.label == "notification"


def test_auto_submitted_no_is_not_notification():
    rec = _email(headers={"Auto-Submitted": "no"})
    result = classify(rec)
    assert result.label == "personal"


def test_header_lookup_is_case_insensitive():
    rec = _email(headers={"list-unsubscribe": "<mailto:x>", "precedence": "BULK"})
    result = classify(rec)
    assert result.label == "promotional"
