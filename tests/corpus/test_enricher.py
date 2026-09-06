"""The enricher sends the schema + fixed frame and parses guided-decoding output.

The httpx client is a spec-bound mock, so no network or model is involved.
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec

import httpx
import pytest

from corpus import enricher as enricher_mod
from corpus.enricher import Enricher, EnrichError, EnrichUnavailableError, cap_input
from corpus.enrichment import Category, json_schema

_COMPLETION = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {"one_line": "hi", "abstract": "a note", "category": "personal"}
                )
            }
        }
    ]
}


def _response(status: int, *, json_body=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://gw/v1/chat/completions")
    return httpx.Response(status, json=json_body, text=text, request=request)


@pytest.fixture
def client() -> httpx.Client:
    mock = create_autospec(httpx.Client, instance=True)
    mock.post.return_value = _response(200, json_body=_COMPLETION)
    return mock


def test_enrich_sends_schema_and_frame_and_parses(client):
    doc = Enricher(model="local", client=client).enrich("Hello there")

    assert doc.category is Category.personal
    assert client.post.call_args.args[0] == "/chat/completions"
    body = client.post.call_args.kwargs["json"]
    assert body["model"] == "local"
    assert body["response_format"]["json_schema"]["schema"] == json_schema()
    assert body["messages"][0]["role"] == "system"
    assert "UNTRUSTED DATA" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "Hello there"


def test_client_error_is_non_retryable(client):
    client.post.return_value = _response(400, text="too long")

    with pytest.raises(EnrichError):
        Enricher(model="local", client=client).enrich("x")
    assert client.post.call_count == 1  # not retried


def test_server_error_retries_then_raises_unavailable(client, monkeypatch):
    monkeypatch.setattr(enricher_mod.settings, "enrich_retries", 3)
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
    client.post.return_value = _response(503, text="overloaded")

    with pytest.raises(EnrichUnavailableError):
        Enricher(model="local", client=client).enrich("x")
    assert client.post.call_count == 3  # enrich_retries attempts


def test_server_error_recovers_within_the_retry_budget(client, monkeypatch):
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
    client.post.side_effect = [
        _response(503, text="overloaded"),
        _response(502, text="bad gateway"),
        _response(200, json_body=_COMPLETION),
    ]

    doc = Enricher(model="local", client=client).enrich("x")

    assert doc.category is Category.personal
    assert client.post.call_count == 3


def test_backoff_grows_but_is_capped_and_jittered(client, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(enricher_mod.settings, "enrich_retries", 6)
    monkeypatch.setattr(enricher_mod.settings, "enrich_retry_max_wait", 4.0)
    monkeypatch.setattr(enricher_mod.time, "sleep", waits.append)
    client.post.return_value = _response(503, text="overloaded")

    with pytest.raises(EnrichUnavailableError):
        Enricher(model="local", client=client).enrich("x")

    assert len(waits) == 5  # no sleep after the final attempt
    assert all(0.5 <= w <= 4.0 for w in waits)  # jittered into [wait/2, wait]
    assert waits[-1] > waits[0]


def test_a_negative_backoff_cap_degrades_to_no_wait(client, monkeypatch):
    # A misconfigured cap must not turn every retry into a ValueError from sleep().
    waits: list[float] = []
    monkeypatch.setattr(enricher_mod.settings, "enrich_retries", 3)
    monkeypatch.setattr(enricher_mod.settings, "enrich_retry_max_wait", -1.0)
    monkeypatch.setattr(enricher_mod.time, "sleep", waits.append)
    client.post.return_value = _response(503, text="overloaded")

    with pytest.raises(EnrichUnavailableError):
        Enricher(model="local", client=client).enrich("x")

    assert waits == [0.0, 0.0]
    assert client.post.call_count == 3


def test_unparseable_output_is_enrich_error(client):
    client.post.return_value = _response(
        200, json_body={"choices": [{"message": {"content": "not json"}}]}
    )

    with pytest.raises(EnrichError):
        Enricher(model="local", client=client).enrich("x")


def test_missing_model_raises(client):
    with pytest.raises(ValueError):
        Enricher(model="", client=client)


def test_input_is_uncapped_by_default(client, monkeypatch):
    monkeypatch.setattr(enricher_mod.settings, "enrich_max_input_chars", 0)
    text = "Subject: invoice\n\n" + "x" * 5000

    Enricher(model="local", client=client).enrich(text)

    assert client.post.call_args.kwargs["json"]["messages"][1]["content"] == text


def test_cap_keeps_the_head_and_marks_the_cut(client):
    text = "Subject: invoice\n\n" + "x" * 5000

    Enricher(model="local", client=client, max_input_chars=64).enrich(text)

    sent = client.post.call_args.kwargs["json"]["messages"][1]["content"]
    assert len(sent) == 64
    assert sent.startswith("Subject: invoice")
    assert sent.endswith("[truncated]")


def test_cap_leaves_text_within_the_limit_untouched():
    assert cap_input("Subject: hi\n\nshort", 64) == "Subject: hi\n\nshort"


def test_cap_smaller_than_the_note_is_still_honoured():
    assert cap_input("abcdefghij", 4) == "abcd"
