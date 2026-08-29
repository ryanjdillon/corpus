"""The enricher sends the schema + fixed frame and parses guided-decoding output.

The httpx client is a spec-bound mock, so no network or model is involved.
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec

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
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
    client.post.return_value = _response(503, text="overloaded")

    with pytest.raises(EnrichUnavailableError):
        Enricher(model="local", client=client).enrich("x")
    assert client.post.call_count == 4  # _RETRIES attempts


def test_unparseable_output_is_enrich_error(client):
    client.post.return_value = _response(
        200, json_body={"choices": [{"message": {"content": "not json"}}]}
    )

    with pytest.raises(EnrichError):
        Enricher(model="local", client=client).enrich("x")


def test_missing_model_raises(client):
    with pytest.raises(ValueError):
        Enricher(model="", client=client)
