"""The Embedder against a spec-bound httpx client (no Docker, no model).

The dimension/round-trip cases still run against the in-process ``fake_embeddings``
endpoint fixture.
"""

import json
import time
from unittest.mock import create_autospec

import httpx
import pytest

from corpus.config import settings
from corpus.embeddings import (
    Embedder,
    EmbedInputError,
    EmbedUnavailableError,
    embed_query,
    query_embedder,
)


def _response(status: int, *, json_body=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://gw/v1/embeddings")
    return httpx.Response(status, json=json_body, text=text, request=request)


@pytest.fixture
def client() -> httpx.Client:
    mock = create_autospec(httpx.Client, instance=True)
    mock.post.return_value = _response(200, json_body={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]})
    return mock


@pytest.fixture
def sleep():
    """A spec-bound ``time.sleep``: the backoff between attempts, recorded not slept."""
    return create_autospec(time.sleep)


def test_embed_retries_on_5xx_then_succeeds(client, sleep):
    client.post.side_effect = [
        _response(503, text="busy"),
        _response(200, json_body={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}),
    ]

    assert Embedder(client=client, sleep=sleep).embed_one("x") == [0.1, 0.2, 0.3, 0.4]
    assert client.post.call_count == 2  # retried once


def test_embed_fails_fast_on_4xx(client):
    client.post.return_value = _response(400, text="bad")

    # 4xx becomes EmbedInputError so callers can skip the offending record.
    with pytest.raises(EmbedInputError):
        Embedder(client=client).embed_one("x")
    assert client.post.call_count == 1  # no retry on client error


def test_embed_gives_up_after_retries(client, sleep):
    client.post.return_value = _response(503, text="busy")

    # Exhausted 5xx retries surface as EmbedUnavailableError (systemic).
    with pytest.raises(EmbedUnavailableError):
        Embedder(client=client, sleep=sleep).embed_one("x")
    assert client.post.call_count == 4
    # Backoff between attempts, none after the last.
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2, 4]


def test_the_query_embedder_makes_one_attempt(client):
    client.post.return_value = _response(503, text="busy")

    with pytest.raises(EmbedUnavailableError):
        embed_query("x", client=client)
    assert client.post.call_count == 1


def test_the_query_embedder_waits_less_than_a_batch():
    embedder = query_embedder()
    try:
        assert embedder._client.timeout == httpx.Timeout(settings.embed_query_timeout)
    finally:
        embedder.close()
    assert settings.embed_query_timeout < settings.embed_timeout


def test_embed_retries_transport_error(client, sleep):
    request = httpx.Request("POST", "http://gw/v1/embeddings")
    client.post.side_effect = [
        httpx.ConnectError("boom", request=request),
        _response(200, json_body={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]}),
    ]

    assert len(Embedder(client=client, sleep=sleep).embed_one("x")) == 4
    assert client.post.call_count == 2  # retried after a transport error


def test_embed_returns_vectors_of_configured_dimension(fake_embeddings):
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
