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

from corpus.fetchers.gmail import MAX_CONSECUTIVE_REFUSALS, GmailFetcher

RAW = base64.urlsafe_b64encode(
    b"From: a@example.org\r\nTo: b@example.org\r\nSubject: hi\r\n"
    b"Date: Wed, 30 Sep 2026 10:00:00 +0000\r\n\r\nbody\r\n"
).decode()


def ok(mid: str = "m1") -> httpx.Response:
    return httpx.Response(200, json={"id": mid, "threadId": "t1", "raw": RAW, "labelIds": []})


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


@pytest.mark.parametrize(
    "response",
    [httpx.Response(403), httpx.Response(403, text="<html>Forbidden</html>")],
    ids=["empty", "html"],
)
def test_403_without_a_json_body_skips_the_message(fetcher, sleep, response):
    client, _ = api(response)
    assert fetcher._fetch_message(client, "m1", sleep=sleep) is None


@pytest.mark.parametrize("reason", ["dailyLimitExceeded", "insufficientPermissions"])
def test_account_wide_refusal_stops_the_run_instead_of_skipping(fetcher, sleep, reason):
    client, calls = api(refusal(403, reason))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher._fetch_message(client, "m1", sleep=sleep)
    assert calls() == 1
    sleep.assert_not_called()


def test_refusal_repeating_for_every_message_stops_the_run(fetcher, sleep):
    # A token whose scope cannot read raw mail is refused for every message;
    # skipping them all would move the cursor past the whole mailbox.
    client, _ = api(*[refusal(403, "forbidden") for _ in range(MAX_CONSECUTIVE_REFUSALS)])
    for _ in range(MAX_CONSECUTIVE_REFUSALS - 1):
        assert fetcher._fetch_message(client, "m", sleep=sleep) is None
    with pytest.raises(httpx.HTTPStatusError):
        fetcher._fetch_message(client, "m", sleep=sleep)


def test_a_fetched_message_resets_the_refusal_count(fetcher, sleep):
    refused = [refusal(403, "forbidden") for _ in range(MAX_CONSECUTIVE_REFUSALS - 1)]
    client, _ = api(*refused, ok(), *refused)
    for _ in range(MAX_CONSECUTIVE_REFUSALS - 1):
        fetcher._fetch_message(client, "m", sleep=sleep)
    assert fetcher._fetch_message(client, "m1", sleep=sleep) is not None
    for _ in range(MAX_CONSECUTIVE_REFUSALS - 1):
        assert fetcher._fetch_message(client, "m", sleep=sleep) is None


def test_backfill_continues_past_a_refused_message(fetcher):
    # The original failure: one refused message ended the whole backfill.
    listing = httpx.Response(200, json={"messages": [{"id": "a"}, {"id": "bad"}, {"id": "c"}]})
    client, _ = api(listing, ok("a"), refusal(403, "forbidden"), ok("c"))
    fetcher._label_names = {}
    ids = [r.source_uid for r in fetcher._backfill(client, None)]
    assert ids == ["a", "c"]


@pytest.mark.parametrize(
    "limited",
    [refusal(403, "userRateLimitExceeded"), refusal(403, "rateLimitExceeded"), refusal(429, "x")],
    ids=["user-rate-403", "rate-403", "429"],
)
def test_rate_limit_retries_with_backoff_then_succeeds(fetcher, sleep, limited):
    client, calls = api(limited, limited, ok())
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
    assert [c.args[0] for c in sleep.call_args_list] == [1.0, 2.0]


def test_server_error_still_raises(fetcher, sleep):
    client, _ = api(httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher._fetch_message(client, "m1", sleep=sleep)


def test_a_deleted_message_neither_counts_nor_resets_refusals(fetcher, sleep):
    refused = [refusal(403, "forbidden") for _ in range(MAX_CONSECUTIVE_REFUSALS - 1)]
    client, _ = api(*refused, httpx.Response(404, json={}), refusal(403, "forbidden"))
    for _ in range(MAX_CONSECUTIVE_REFUSALS - 1):
        fetcher._fetch_message(client, "m", sleep=sleep)
    assert fetcher._fetch_message(client, "gone", sleep=sleep) is None
    with pytest.raises(httpx.HTTPStatusError):
        fetcher._fetch_message(client, "m", sleep=sleep)
