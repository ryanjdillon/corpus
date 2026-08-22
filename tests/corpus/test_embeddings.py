"""The Embedder against the in-process fake endpoint (no Docker, no model)."""

import json

import httpx

from corpus.embeddings import Embedder


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
