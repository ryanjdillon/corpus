"""Generic IMAP fetcher against a real GreenMail server."""

import time

import pytest

from corpus.sources import ImapSource, build_fetcher
from corpus.sources import rfc822 as rfc822_mod

pytestmark = pytest.mark.integration

USER = "user@example.org"


SOURCE = ImapSource(name="test", enrich=True)


def _configure(imap_port: int, folders: str = "INBOX") -> dict[str, str]:
    """The variables of "imap:test" under its default prefix, for the GreenMail server."""
    return {
        "CORPUS_IMAP_TEST_HOST": "127.0.0.1",
        "CORPUS_IMAP_TEST_PORT": str(imap_port),
        "CORPUS_IMAP_TEST_USER": USER,
        "CORPUS_IMAP_TEST_PASSWORD": "test",
        "CORPUS_IMAP_TEST_SSL": "false",
        "CORPUS_IMAP_TEST_FOLDERS": folders,
    }


def _fetch_with_retry(fetcher, cursor, expected, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        records = list(fetcher.fetch(cursor))
        if len(records) >= expected:
            return records
        time.sleep(0.5)
    return list(fetcher.fetch(cursor))


def test_fetch_parses_messages(greenmail):
    env = _configure(greenmail["imap_port"])
    greenmail["send"](USER, "Kayak trip", "Let's paddle Saturday.")
    greenmail["send"](
        USER,
        "Winter sale",
        "50% off",
        from_addr="news@mailchimp.com",
        extra_headers={"List-Unsubscribe": "<mailto:unsub@x>"},
    )

    fetcher = build_fetcher(SOURCE, environ=env)
    records = _fetch_with_retry(fetcher, None, expected=2)

    assert len(records) == 2
    subjects = {r.subject for r in records}
    assert subjects == {"Kayak trip", "Winter sale"}
    for r in records:
        assert r.kind == "email"
        assert r.account == USER
        assert r.body_text.strip()
        folder, validity, uid = r.source_uid.split(":")
        assert folder == "INBOX"
        assert validity.isdigit() and uid.isdigit()
    # Headers survive parsing (needed downstream for classification).
    promo = next(r for r in records if r.subject == "Winter sale")
    assert any(k.lower() == "list-unsubscribe" for k in promo.headers)


def test_incremental_cursor(greenmail):
    env = _configure(greenmail["imap_port"])
    greenmail["send"](USER, "First", "one")
    greenmail["send"](USER, "Second", "two")

    fetcher = build_fetcher(SOURCE, environ=env)
    first = _fetch_with_retry(fetcher, None, expected=2)
    assert len(first) == 2
    cursor = fetcher.next_cursor()
    assert cursor and ":" in cursor

    greenmail["send"](USER, "Third", "three")
    fetcher2 = build_fetcher(SOURCE, environ=env)
    later = _fetch_with_retry(fetcher2, cursor, expected=1)
    assert [r.subject for r in later] == ["Third"]


def test_unparseable_message_is_skipped(greenmail, monkeypatch):
    # A message that fails to parse must be skipped, not abort the fetch.
    env = _configure(greenmail["imap_port"])
    greenmail["send"](USER, "Boom", "body")

    from imapclient import IMAPClient

    deadline = time.time() + 15
    while time.time() < deadline:  # wait until the message is in the mailbox
        with IMAPClient("127.0.0.1", port=greenmail["imap_port"], ssl=False) as c:
            c.login(USER, "test")
            c.select_folder("INBOX")
            if c.search(["ALL"]):
                break
        time.sleep(0.5)

    def boom(_raw):
        raise ValueError("bad message")

    monkeypatch.setattr(rfc822_mod.mailparser, "parse_from_bytes", boom)
    fetcher = build_fetcher(SOURCE, environ=env)
    assert list(fetcher.fetch(None)) == []  # skipped, no exception
    cursor = fetcher.next_cursor()
    assert cursor and ":" in cursor  # cursor still advances past the bad message


def test_catalogs_all_folders(greenmail):
    # Unset folder selection => discover and catalog every folder.
    env = _configure(greenmail["imap_port"], folders="all")
    greenmail["send"](USER, "Inbox item", "in the inbox")

    from imapclient import IMAPClient

    with IMAPClient("127.0.0.1", port=greenmail["imap_port"], ssl=False) as client:
        client.login(USER, "test")
        client.create_folder("Archive")
        client.append(
            "Archive",
            b"From: sender@example.org\r\nSubject: Archived item\r\n\r\narchived body\r\n",
        )

    fetcher = build_fetcher(SOURCE, environ=env)
    records = _fetch_with_retry(fetcher, None, expected=2)

    by_folder = {r.folder: r for r in records}
    assert "INBOX" in by_folder
    assert "Archive" in by_folder
    assert by_folder["Archive"].subject == "Archived item"
    # Ids are unique across folders even when per-folder UIDs coincide.
    assert len({r.source_uid for r in records}) == len(records)


def test_discovery_skips_junk_and_keeps_sent(greenmail):
    # GreenMail lists no special-use attributes, so the names decide.
    env = _configure(greenmail["imap_port"], folders="all")
    greenmail["send"](USER, "Inbox item", "in the inbox")

    from imapclient import IMAPClient

    with IMAPClient("127.0.0.1", port=greenmail["imap_port"], ssl=False) as client:
        client.login(USER, "test")
        for folder, subject in (("Junk Mail", "Filtered item"), ("Sent", "Sent item")):
            client.create_folder(folder)
            client.append(
                folder,
                f"From: {USER}\r\nSubject: {subject}\r\n\r\nsynthetic body\r\n".encode(),
            )

    fetcher = build_fetcher(SOURCE, environ=env)
    records = _fetch_with_retry(fetcher, None, expected=2)

    assert {r.folder for r in records} == {"INBOX", "Sent"}
    assert "Junk Mail" not in (fetcher.next_cursor() or "")
