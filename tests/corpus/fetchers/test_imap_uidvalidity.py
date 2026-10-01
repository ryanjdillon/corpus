"""IMAP record ids across a UIDVALIDITY change, with the IMAP connection injected."""

from email.message import EmailMessage
from unittest.mock import create_autospec

import pytest
from imapclient import IMAPClient

from corpus.fetchers.imap import ImapFetcher


def _raw(subject: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.org"
    msg["To"] = "box@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 30 Sep 2026 10:00:00 +0000"
    msg.set_content("body")
    return msg.as_bytes()


@pytest.fixture
def env(monkeypatch):
    settings = {
        "HOST": "mail.example.org",
        "USER": "box@example.org",
        "PASSWORD": "x",
        "FOLDERS": "INBOX",
    }
    for key, value in settings.items():
        monkeypatch.setenv(f"CORPUS_IMAP_UNIT_{key}", value)


@pytest.fixture
def client():
    m = create_autospec(IMAPClient, instance=True)
    m.__enter__.return_value = m
    m.select_folder.return_value = {b"UIDVALIDITY": 7}
    m.search.return_value = []
    m.fetch.return_value = {}
    return m


@pytest.fixture
def fetcher(env, client):
    return ImapFetcher("unit", connect=lambda *_a, **_k: client)


def test_reused_uid_after_uidvalidity_change_gets_a_new_id(fetcher, client):
    client.search.return_value = [1]
    client.fetch.return_value = {1: {b"RFC822": _raw("before")}}
    first = list(fetcher.fetch(None))
    cursor = fetcher.next_cursor()

    # The server renumbers the folder: UID 1 now holds a different message.
    client.select_folder.return_value = {b"UIDVALIDITY": 8}
    client.fetch.return_value = {1: {b"RFC822": _raw("after")}}
    second = list(fetcher.fetch(cursor))

    assert [r.source_uid for r in first] == ["INBOX:7:1"]
    assert [(r.source_uid, r.subject) for r in second] == [("INBOX:8:1", "after")]
    assert second[0].uri == "imap://mail.example.org/INBOX;UIDVALIDITY=8/;UID=1"
    # A new epoch rescans the folder from UID 1.
    assert client.search.call_args.args[0] == ["UID", "1:*"]


def test_same_uidvalidity_resumes_after_the_cursor(fetcher, client):
    client.search.return_value = [1]
    client.fetch.return_value = {1: {b"RFC822": _raw("one")}}
    list(fetcher.fetch(None))
    cursor = fetcher.next_cursor()

    client.search.return_value = [2]
    client.fetch.return_value = {2: {b"RFC822": _raw("two")}}
    records = list(fetcher.fetch(cursor))

    assert [r.source_uid for r in records] == ["INBOX:7:2"]
    assert client.search.call_args.args[0] == ["UID", "2:*"]


def test_folders_are_opened_read_only(fetcher, client):
    list(fetcher.fetch(None))
    client.select_folder.assert_called_once_with("INBOX", readonly=True)


def test_known_uids_are_not_downloaded_but_advance_the_cursor(fetcher, client):
    client.search.return_value = [1, 2, 3]
    client.fetch.return_value = {2: {b"RFC822": _raw("two")}}
    known = {"imap:unit::INBOX:7:1", "imap:unit::INBOX:7:3"}
    records = list(fetcher.fetch(None, known=known))
    assert [r.source_uid for r in records] == ["INBOX:7:2"]
    assert client.fetch.call_args.args[0] == [2]
    assert fetcher.next_cursor() == '{"INBOX": "7:3"}'
