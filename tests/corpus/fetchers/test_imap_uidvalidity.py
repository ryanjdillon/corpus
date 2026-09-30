"""IMAP record ids across a UIDVALIDITY change, against a scripted client."""

from email.message import EmailMessage
from typing import ClassVar

import pytest

from corpus.fetchers import imap as imap_mod


def _raw(subject: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.org"
    msg["To"] = "box@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 30 Sep 2026 10:00:00 +0000"
    msg.set_content("body")
    return msg.as_bytes()


class FakeClient:
    """Serves one folder whose UIDVALIDITY and messages the test controls."""

    validity = 1
    messages: ClassVar[dict[int, bytes]] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def login(self, _user, _password) -> None:
        pass

    def select_folder(self, _folder, readonly=False):
        assert readonly, "the fetcher must open folders read-only"
        return {b"UIDVALIDITY": self.validity}

    def search(self, criteria):
        start = int(criteria[1].split(":")[0])
        return [uid for uid in sorted(self.messages) if uid >= start]

    def fetch(self, uids, _parts):
        return {uid: {b"RFC822": self.messages[uid]} for uid in uids}


@pytest.fixture
def fetcher(monkeypatch):
    for key, value in {
        "HOST": "mail.example.org",
        "USER": "box@example.org",
        "PASSWORD": "x",
        "FOLDERS": "INBOX",
    }.items():
        monkeypatch.setenv(f"CORPUS_IMAP_UNIT_{key}", value)
    monkeypatch.setattr(imap_mod, "IMAPClient", FakeClient)
    return imap_mod.ImapFetcher("unit")


def test_reused_uid_after_uidvalidity_change_gets_a_new_id(fetcher):
    FakeClient.validity, FakeClient.messages = 7, {1: _raw("before")}
    first = list(fetcher.fetch(None))
    cursor = fetcher.next_cursor()

    # The server renumbers the folder: UID 1 now holds a different message.
    FakeClient.validity, FakeClient.messages = 8, {1: _raw("after")}
    second = list(fetcher.fetch(cursor))

    assert [r.source_uid for r in first] == ["INBOX:7:1"]
    assert [(r.source_uid, r.subject) for r in second] == [("INBOX:8:1", "after")]
    assert second[0].uri == "imap://mail.example.org/INBOX;UIDVALIDITY=8/;UID=1"


def test_same_uidvalidity_resumes_after_the_cursor(fetcher):
    FakeClient.validity, FakeClient.messages = 7, {1: _raw("one")}
    list(fetcher.fetch(None))
    cursor = fetcher.next_cursor()

    FakeClient.messages = {1: _raw("one"), 2: _raw("two")}
    assert [r.source_uid for r in fetcher.fetch(cursor)] == ["INBOX:7:2"]
