"""The enricher sends the schema + fixed frame and parses guided-decoding output.

The endpoint is a MockTransport, so no network or model is involved."""

from __future__ import annotations

import json

import httpx
import pytest

from corpus import enricher as enricher_mod
from corpus.enricher import Enricher, EnrichError, EnrichUnavailableError
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


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gw/v1")


def test_enrich_sends_schema_and_frame_and_parses(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_COMPLETION)

    doc = Enricher(model="local", client=_client(handler)).enrich("Hello there")

    assert doc.category is Category.personal
    body = captured["body"]
    assert body["model"] == "local"
    assert body["response_format"]["json_schema"]["schema"] == json_schema()
    assert body["messages"][0]["role"] == "system"
    assert "UNTRUSTED DATA" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "Hello there"


def test_client_error_is_non_retryable(monkeypatch):
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="too long")

    with pytest.raises(EnrichError):
        Enricher(model="local", client=_client(handler)).enrich("x")
    assert calls["n"] == 1  # not retried


def test_server_error_retries_then_raises_unavailable(monkeypatch):
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    with pytest.raises(EnrichUnavailableError):
        Enricher(model="local", client=_client(handler)).enrich("x")
    assert calls["n"] == 4  # _RETRIES attempts


def test_unparseable_output_is_enrich_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(EnrichError):
        Enricher(model="local", client=_client(handler)).enrich("x")


def test_missing_model_raises():
    with pytest.raises(ValueError):
        Enricher(model="", client=_client(lambda r: httpx.Response(200, json=_COMPLETION)))
