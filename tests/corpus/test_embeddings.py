"""The Embedder against a spec-bound httpx client (no Docker, no model).

The dimension/round-trip cases still run against the in-process ``fake_embeddings``
endpoint fixture.
"""

import json
from unittest.mock import create_autospec

import httpx
import pytest

from corpus import embeddings
from corpus.embeddings import Embedder, EmbedInputError, EmbedUnavailableError


def _response(status: int, *, json_body=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://gw/v1/embeddings")
    return httpx.Response(status, json=json_body, text=text, request=request)


@pytest.fixture
def client() -> httpx.Client:
    mock = create_autospec(httpx.Client, instance=True)
    mock.post.return_value = _response(200, json_body={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]})
    return mock


def test_embed_retries_on_5xx_then_succeeds(client, monkeypatch):
    monkeypatch.setattr(embeddings.time, "sleep", lambda *_: None)
    client.post.side_effect = [
        _response(503, text="busy"),
        _response(200, json_body={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}),
    ]

    assert Embedder(client=client).embed_one("x") == [0.1, 0.2, 0.3, 0.4]
    assert client.post.call_count == 2  # retried once


def test_embed_fails_fast_on_4xx(client):
    client.post.return_value = _response(400, text="bad")

    # 4xx becomes EmbedInputError so callers can skip the offending record.
    with pytest.raises(EmbedInputError):
        Embedder(client=client).embed_one("x")
    assert client.post.call_count == 1  # no retry on client error


def test_embed_gives_up_after_retries(client, monkeypatch):
    monkeypatch.setattr(embeddings.time, "sleep", lambda *_: None)
    client.post.return_value = _response(503, text="busy")

    # Exhausted 5xx retries surface as EmbedUnavailableError (systemic).
    with pytest.raises(EmbedUnavailableError):
        Embedder(client=client).embed_one("x")
    assert client.post.call_count == 4


def test_embed_retries_transport_error(client, monkeypatch):
    monkeypatch.setattr(embeddings.time, "sleep", lambda *_: None)
    request = httpx.Request("POST", "http://gw/v1/embeddings")
    client.post.side_effect = [
        httpx.ConnectError("boom", request=request),
        _response(200, json_body={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]}),
    ]

    assert len(Embedder(client=client).embed_one("x")) == 4
    assert client.post.call_count == 2  # retried after a transport error


def test_embed_returns_vectors_of_configured_dimension(fake_embeddings):
    from corpus.config import settings

    embedder = Embedder()
    try:
        vecs = embedder.embed(["hello", "world"])
    finally:
        embedder.close()
    assert len(vecs) == 2
    assert all(len(v) == settings.embedding_dimensions for v in vecs)


def test_embed_one(fake_embeddings):
    embedder = Embedder()
    try:
        vec = embedder.embed_one("hello")
    finally:
        embedder.close()
    assert isinstance(vec, list)
    assert len(vec) > 0


def test_embed_empty_short_circuits(client):
    assert Embedder(client=client).embed([]) == []
    client.post.assert_not_called()


def test_request_never_sends_null_encoding_format(client):
    """Regression guard: the request body must carry a concrete encoding_format,
    never null (which strict OpenAI backends reject)."""
    Embedder(client=client).embed_one("hi")

    body = client.post.call_args.kwargs["json"]
    assert body["encoding_format"] == "float"
    assert json.dumps(body)  # serialisable, no surprises
