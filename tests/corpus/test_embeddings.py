"""The Embedder against the in-process fake endpoint (no Docker, no model)."""

import json

import httpx
import pytest

from corpus import embeddings
from corpus.embeddings import Embedder, EmbedInputError


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=transport, **k))
    monkeypatch.setattr(embeddings.time, "sleep", lambda *_: None)


def test_embed_retries_on_5xx_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})

    _mock_transport(monkeypatch, handler)
    embedder = Embedder()
    try:
        assert embedder.embed_one("x") == [0.1, 0.2, 0.3, 0.4]
    finally:
        embedder.close()
    assert calls["n"] == 2  # retried once


def test_embed_fails_fast_on_4xx(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="bad")

    _mock_transport(monkeypatch, handler)
    embedder = Embedder()
    try:
        # 4xx becomes EmbedInputError so callers can skip the offending record.
        with pytest.raises(EmbedInputError):
            embedder.embed_one("x")
    finally:
        embedder.close()
    assert calls["n"] == 1  # no retry on client error


def test_embed_gives_up_after_retries(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="busy")

    _mock_transport(monkeypatch, handler)
    embedder = Embedder()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            embedder.embed_one("x")
    finally:
        embedder.close()


def test_embed_retries_transport_error(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]})

    _mock_transport(monkeypatch, handler)
    embedder = Embedder()
    try:
        assert len(embedder.embed_one("x")) == 4
    finally:
        embedder.close()
    assert calls["n"] == 2  # retried after a transport error


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


def test_embed_empty_short_circuits(fake_embeddings):
    embedder = Embedder()
    try:
        assert embedder.embed([]) == []
    finally:
        embedder.close()


def test_request_never_sends_null_encoding_format(fake_embeddings, monkeypatch):
    """Regression guard: the request body must carry a concrete encoding_format,
    never null (which strict OpenAI backends reject)."""
    seen = {}
    real_post = httpx.Client.post

    def spy(self, url, **kwargs):
        seen["json"] = kwargs.get("json")
        return real_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", spy)
    embedder = Embedder()
    try:
        embedder.embed_one("hi")
    finally:
        embedder.close()
    assert seen["json"]["encoding_format"] == "float"
    assert json.dumps(seen["json"])  # serialisable, no surprises
