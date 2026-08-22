"""Generic IMAP fetcher against a real GreenMail server."""

import time

import pytest

from corpus.fetchers import build_fetcher

pytestmark = pytest.mark.integration

USER = "user@example.org"


def _configure(monkeypatch, imap_port: int) -> None:
    monkeypatch.setenv("CORPUS_IMAP_TEST_HOST", "127.0.0.1")
    monkeypatch.setenv("CORPUS_IMAP_TEST_PORT", str(imap_port))
    monkeypatch.setenv("CORPUS_IMAP_TEST_USER", USER)
    monkeypatch.setenv("CORPUS_IMAP_TEST_PASSWORD", "test")
    monkeypatch.setenv("CORPUS_IMAP_TEST_SSL", "false")


def _fetch_with_retry(fetcher, cursor, expected, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        records = list(fetcher.fetch(cursor))
        if len(records) >= expected:
            return records
        time.sleep(0.5)
    return list(fetcher.fetch(cursor))


def test_fetch_parses_messages(greenmail, monkeypatch):
    _configure(monkeypatch, greenmail["imap_port"])
    greenmail["send"](USER, "Kayak trip", "Let's paddle Saturday.")
    greenmail["send"](
        USER,
        "Winter sale",
        "50% off",
        from_addr="news@mailchimp.com",
        extra_headers={"List-Unsubscribe": "<mailto:unsub@x>"},
    )

    fetcher = build_fetcher("imap:test")
    records = _fetch_with_retry(fetcher, None, expected=2)

    assert len(records) == 2
    subjects = {r.subject for r in records}
    assert subjects == {"Kayak trip", "Winter sale"}
    for r in records:
        assert r.kind == "email"
        assert r.account == USER
        assert r.body_text.strip()
        assert r.source_uid.isdigit()
    # Headers survive parsing (needed downstream for classification).
    promo = next(r for r in records if r.subject == "Winter sale")
    assert any(k.lower() == "list-unsubscribe" for k in promo.headers)


def test_incremental_cursor(greenmail, monkeypatch):
    _configure(monkeypatch, greenmail["imap_port"])
    greenmail["send"](USER, "First", "one")
    greenmail["send"](USER, "Second", "two")

    fetcher = build_fetcher("imap:test")
    first = _fetch_with_retry(fetcher, None, expected=2)
    assert len(first) == 2
    cursor = fetcher.next_cursor()
    assert cursor and ":" in cursor

    greenmail["send"](USER, "Third", "three")
    fetcher2 = build_fetcher("imap:test")
    later = _fetch_with_retry(fetcher2, cursor, expected=1)
    assert [r.subject for r in later] == ["Third"]
