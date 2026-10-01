"""Per-message Gmail API errors: rate limits retry, other refusals skip.

The HTTP client is passed to ``_fetch_message``, so each test builds one on a
MockTransport that serves the responses it needs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from unittest.mock import create_autospec

import httpx
import pytest

from corpus.fetchers.gmail import GmailFetcher

RAW = base64.urlsafe_b64encode(
    b"From: a@example.org\r\nTo: b@example.org\r\nSubject: hi\r\n"
    b"Date: Wed, 30 Sep 2026 10:00:00 +0000\r\n\r\nbody\r\n"
).decode()
OK = httpx.Response(200, json={"id": "m1", "threadId": "t1", "raw": RAW, "labelIds": []})


def refusal(status: int, reason: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": status, "errors": [{"reason": reason}], "status": "X"}}
    )


@pytest.fixture
def fetcher(monkeypatch):
    for key, value in {"CLIENT_ID": "cid", "CLIENT_SECRET": "cs", "REFRESH_TOKEN": "rt"}.items():
        monkeypatch.setenv(f"CORPUS_GMAIL_UNIT_{key}", value)
    return GmailFetcher("unit")


@pytest.fixture
def sleep():
    return create_autospec(lambda seconds: None)


def api(*responses: httpx.Response) -> tuple[httpx.Client, Callable[[], int]]:
    """A client answering each request with the next response; also its call count."""
    queue = list(responses)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return queue.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gmail.test")
    return client, lambda: len(calls)


def test_non_quota_403_skips_the_message(fetcher, sleep):
    client, calls = api(refusal(403, "forbidden"))
    assert fetcher._fetch_message(client, "m1", sleep=sleep) is None
    assert calls() == 1
    sleep.assert_not_called()


def test_403_without_a_body_skips_the_message(fetcher, sleep):
    client, _ = api(httpx.Response(403))
    assert fetcher._fetch_message(client, "m1", sleep=sleep) is None


@pytest.mark.parametrize(
    "limited",
    [refusal(403, "userRateLimitExceeded"), refusal(403, "rateLimitExceeded"), refusal(429, "x")],
    ids=["user-rate-403", "rate-403", "429"],
)
def test_rate_limit_retries_with_backoff_then_succeeds(fetcher, sleep, limited):
    client, calls = api(limited, limited, OK)
    record = fetcher._fetch_message(client, "m1", sleep=sleep)
    assert record is not None and record.subject == "hi"
    assert calls() == 3
    assert [c.args[0] for c in sleep.call_args_list] == [1.0, 2.0]


def test_rate_limit_exhausting_retries_raises(fetcher, sleep):
    limited = refusal(429, "x")
    client, calls = api(*([limited] * 3))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher._fetch_message(client, "m1", attempts=3, sleep=sleep)
    assert calls() == 3


def test_server_error_still_raises(fetcher, sleep):
    client, _ = api(httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher._fetch_message(client, "m1", sleep=sleep)
