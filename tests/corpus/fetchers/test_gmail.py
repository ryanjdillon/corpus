"""Gmail fetcher against a mocked Gmail API (no network, no Docker)."""

import base64

import httpx
import pytest

from corpus.fetchers import build_fetcher

LABELS = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "Receipts"}]


def _raw(subject: str) -> str:
    msg = (
        f"From: sender@example.org\r\nTo: me@gmail.com\r\n"
        f"Subject: {subject}\r\n\r\nbody of {subject}\r\n"
    ).encode()
    return base64.urlsafe_b64encode(msg).decode()


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.startswith("https://oauth2.googleapis.com/token"):
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    path = request.url.path
    if path.endswith("/labels"):
        return httpx.Response(200, json={"labels": LABELS})
    if path.endswith("/profile"):
        return httpx.Response(200, json={"emailAddress": "me@gmail.com", "historyId": "1000"})
    if path.endswith("/messages"):
        # If a label filter is applied, return a single message; else two.
        ids = ["m1"] if "labelIds" in request.url.params else ["m1", "m2"]
        return httpx.Response(200, json={"messages": [{"id": i} for i in ids]})
    if path.endswith("/history"):
        return httpx.Response(
            200,
            json={
                "history": [{"messagesAdded": [{"message": {"id": "m3", "labelIds": ["INBOX"]}}]}],
                "historyId": "1005",
            },
        )
    if "/messages/" in path:
        mid = path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json={
                "id": mid,
                "threadId": f"t-{mid}",
                "labelIds": ["INBOX", "Label_1"],
                "raw": _raw(f"msg {mid}"),
            },
        )
    return httpx.Response(404, json={})


@pytest.fixture
def gmail_env(monkeypatch):
    monkeypatch.setenv("CORPUS_GMAIL_TEST_CLIENT_ID", "cid")
    monkeypatch.setenv("CORPUS_GMAIL_TEST_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("CORPUS_GMAIL_TEST_REFRESH_TOKEN", "rtok")


@pytest.fixture
def mock_httpx(monkeypatch):
    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    def post(url, **kwargs):
        with real_client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(httpx, "post", post)


def test_missing_creds_raises(monkeypatch):
    monkeypatch.delenv("CORPUS_GMAIL_TEST_CLIENT_ID", raising=False)
    with pytest.raises(ValueError):
        build_fetcher("gmail:test")


def test_backfill_yields_records_with_labels(gmail_env, mock_httpx):
    fetcher = build_fetcher("gmail:test")
    records = list(fetcher.fetch(None))
    assert [r.source_uid for r in records] == ["m1", "m2"]
    r = records[0]
    assert r.source == "gmail:test"
    assert r.kind == "email"
    assert r.account == "me@gmail.com"
    assert r.thread_id == "t-m1"
    assert r.labels == ["INBOX", "Receipts"]  # ids resolved to names
    assert r.subject == "msg m1"
    assert r.body_text.strip() == "body of msg m1"
    assert fetcher.next_cursor() == "1000"  # mailbox historyId recorded


def test_label_filter_restricts_backfill(gmail_env, mock_httpx, monkeypatch):
    monkeypatch.setenv("CORPUS_GMAIL_TEST_LABELS", "Receipts")
    fetcher = build_fetcher("gmail:test")
    records = list(fetcher.fetch(None))
    assert [r.source_uid for r in records] == ["m1"]  # label filter applied


def test_incremental_uses_history(gmail_env, mock_httpx):
    fetcher = build_fetcher("gmail:test")
    records = list(fetcher.fetch("1000"))
    assert [r.source_uid for r in records] == ["m3"]
    assert fetcher.next_cursor() == "1005"


def _paginated_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.startswith("https://oauth2.googleapis.com/token"):
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    path = request.url.path
    if path.endswith("/labels"):
        return httpx.Response(200, json={"labels": LABELS})
    if path.endswith("/profile"):
        return httpx.Response(200, json={"emailAddress": "me@gmail.com", "historyId": "1000"})
    if path.endswith("/messages"):
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"messages": [{"id": "m3"}, {"id": "m4"}]})
        return httpx.Response(
            200, json={"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"}
        )
    if "/messages/" in path:
        mid = path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json={"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"], "raw": _raw(f"msg {mid}")},
        )
    return httpx.Response(404, json={})


@pytest.fixture
def paginated_httpx(monkeypatch):
    transport = httpx.MockTransport(_paginated_handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(*a, transport=transport, **k))
    monkeypatch.setattr(
        httpx, "post", lambda url, **k: real_client(transport=transport).post(url, **k)
    )


def test_backfill_checkpoints_page_token(gmail_env, paginated_httpx):
    fetcher = build_fetcher("gmail:test")
    gen = fetcher.fetch(None)
    assert [next(gen).source_uid for _ in range(3)] == ["m1", "m2", "m3"]
    # Crossing the page boundary checkpoints the next page token mid-backfill.
    assert fetcher.next_cursor() == "backfill:p2"
    assert next(gen).source_uid == "m4"
    with pytest.raises(StopIteration):
        next(gen)
    assert fetcher.next_cursor() == "1000"  # completed backfill -> historyId


def test_backfill_resumes_from_page_token(gmail_env, paginated_httpx):
    fetcher = build_fetcher("gmail:test")
    # Resuming skips page 1 entirely — only page 2's messages are fetched.
    records = list(fetcher.fetch("backfill:p2"))
    assert [r.source_uid for r in records] == ["m3", "m4"]
    assert fetcher.next_cursor() == "1000"
