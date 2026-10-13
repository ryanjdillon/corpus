"""IMAP record ids across a UIDVALIDITY change, with the IMAP connection injected."""

from email.message import EmailMessage
from unittest.mock import create_autospec

import pytest
from imapclient import IMAPClient

from corpus.fetchers.imap import ImapFetcher
from corpus.sources import ImapOptions, ImapSource


def _raw(subject: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.org"
    msg["To"] = "box@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 30 Sep 2026 10:00:00 +0000"
    msg.set_content("body")
    return msg.as_bytes()


@pytest.fixture
def env():
    settings = {
        "HOST": "mail.example.org",
        "USER": "box@example.org",
        "PASSWORD": "x",
        "FOLDERS": "INBOX",
    }
    return {f"CORPUS_IMAP_UNIT_{key}": value for key, value in settings.items()}


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
    source = ImapSource(name="unit", enrich=True)
    return ImapFetcher(source, environ=env, connect=lambda *_a, **_k: client)


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


def test_the_folders_option_wins_over_the_variable(env, client):
    source = ImapSource(name="unit", enrich=True, options=ImapOptions(folders=["Archive"]))
    fetcher = ImapFetcher(source, environ=env, connect=lambda *_a, **_k: client)
    list(fetcher.fetch(None))
    client.select_folder.assert_called_once_with("Archive", readonly=True)


@pytest.mark.parametrize(("value", "folders"), [("all", None), ("*", None), ("", None),
                                                ("INBOX, Sent", ["INBOX", "Sent"])])
def test_the_folders_variable_is_parsed_as_before(env, client, value, folders):
    environ = {**env, "CORPUS_IMAP_UNIT_FOLDERS": value}
    fetcher = ImapFetcher(ImapSource(name="unit", enrich=True), environ=environ)
    assert fetcher.folders == folders


def test_the_connection_variables_reach_the_client(env, client):
    environ = {**env, "CORPUS_IMAP_UNIT_PORT": "1143", "CORPUS_IMAP_UNIT_SSL": "false"}
    connect = create_autospec(IMAPClient, return_value=client)
    fetcher = ImapFetcher(ImapSource(name="unit", enrich=True), environ=environ, connect=connect)
    list(fetcher.fetch(None))
    connect.assert_called_once_with("mail.example.org", port=1143, ssl=False)
    client.login.assert_called_once_with("box@example.org", "x")


def test_missing_connection_variables_are_named(env):
    environ = {k: v for k, v in env.items() if not k.endswith("_PASSWORD")}
    with pytest.raises(ValueError, match="CORPUS_IMAP_UNIT_PASSWORD"):
        ImapFetcher(ImapSource(name="unit", enrich=True), environ=environ)
