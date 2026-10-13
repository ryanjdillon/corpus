"""Each model client's endpoint and key: its own when set, else the shared pair.

No request leaves the process: a default client's request is only built, and the
audit's client is a spec-bound mock whose construction is recorded.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import create_autospec

import httpx
import pytest

from corpus import secret_audit as audit_mod
from corpus.config import Endpoint, Settings, settings
from corpus.embeddings import Embedder, query_embedder
from corpus.enricher import Enricher

SHARED = Endpoint("http://shared.example/v1", "shared-key")
EMBED = Endpoint("http://embed.example/v1", "embed-key")
ENRICH = Endpoint("http://enrich.example/v1", "enrich-key")

_PER_CLIENT_ENV = (
    "CORPUS_EMBED_API_BASE", "CORPUS_EMBED_API_KEY",
    "CORPUS_ENRICH_API_BASE", "CORPUS_ENRICH_API_KEY",
)


@pytest.fixture
def shared_only(monkeypatch) -> None:
    """Only the shared endpoint and key are configured."""
    monkeypatch.setattr(settings, "openai_api_base", SHARED.base)
    monkeypatch.setattr(settings, "openai_api_key", SHARED.key)
    for name in ("embed_api_base", "embed_api_key", "enrich_api_base", "enrich_api_key"):
        monkeypatch.setattr(settings, name, "")


@pytest.fixture
def per_client(shared_only, monkeypatch) -> None:
    """The embedder and the enricher each have their own endpoint and key."""
    monkeypatch.setattr(settings, "embed_api_base", EMBED.base)
    monkeypatch.setattr(settings, "embed_api_key", EMBED.key)
    monkeypatch.setattr(settings, "enrich_api_base", ENRICH.base)
    monkeypatch.setattr(settings, "enrich_api_key", ENRICH.key)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def test_an_unset_client_endpoint_falls_back_to_the_shared_one(monkeypatch):
    for name in _PER_CLIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CORPUS_OPENAI_API_BASE", SHARED.base)
    monkeypatch.setenv("CORPUS_OPENAI_API_KEY", SHARED.key)

    s = Settings()

    assert s.embed_endpoint == SHARED
    assert s.enrich_endpoint == SHARED


def test_each_client_endpoint_reads_its_own_variables(monkeypatch):
    monkeypatch.setenv("CORPUS_OPENAI_API_BASE", SHARED.base)
    monkeypatch.setenv("CORPUS_OPENAI_API_KEY", SHARED.key)
    monkeypatch.setenv("CORPUS_EMBED_API_BASE", EMBED.base)
    monkeypatch.setenv("CORPUS_EMBED_API_KEY", EMBED.key)
    monkeypatch.setenv("CORPUS_ENRICH_API_BASE", ENRICH.base)
    monkeypatch.setenv("CORPUS_ENRICH_API_KEY", ENRICH.key)

    s = Settings()

    assert s.embed_endpoint == EMBED
    assert s.enrich_endpoint == ENRICH


def test_a_key_alone_overrides_while_the_base_falls_back(monkeypatch):
    for name in _PER_CLIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CORPUS_OPENAI_API_BASE", SHARED.base)
    monkeypatch.setenv("CORPUS_OPENAI_API_KEY", SHARED.key)
    monkeypatch.setenv("CORPUS_EMBED_API_KEY", EMBED.key)

    s = Settings()

    assert s.embed_endpoint == Endpoint(SHARED.base, EMBED.key)
    assert s.enrich_endpoint == SHARED


# --------------------------------------------------------------------------- #
# the clients
# --------------------------------------------------------------------------- #
def _embedder() -> httpx.Client:
    return Embedder()._client


def _query_embedder() -> httpx.Client:
    return query_embedder()._client


def _enricher() -> httpx.Client:
    return Enricher(model="test-model")._client


def _sent(client: httpx.Client, path: str) -> tuple[str, str]:
    """The URL and Authorization header a request on *client* would carry."""
    try:
        request = client.build_request("POST", path)
    finally:
        client.close()
    return str(request.url), request.headers["Authorization"]


_CLIENTS: list[tuple[Callable[[], httpx.Client], str, str]] = [
    (_embedder, "/embeddings", "embed"),
    (_query_embedder, "/embeddings", "embed"),
    (_enricher, "/chat/completions", "enrich"),
]


@pytest.mark.parametrize(("build", "path", "_kind"), _CLIENTS)
def test_a_client_uses_the_shared_endpoint_by_default(shared_only, build, path, _kind):
    assert _sent(build(), path) == (SHARED.base + path, f"Bearer {SHARED.key}")


@pytest.mark.parametrize(("build", "path", "kind"), _CLIENTS)
def test_a_client_uses_its_own_endpoint_when_set(per_client, build, path, kind):
    own = EMBED if kind == "embed" else ENRICH
    assert _sent(build(), path) == (own.base + path, f"Bearer {own.key}")


@pytest.fixture
def audit_client(monkeypatch) -> httpx.Client:
    """The audit's default client: a spec-bound mock, built by a spec-bound constructor."""
    client = create_autospec(httpx.Client, instance=True)
    client.post.return_value = httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(
            {"contains_secret": False, "findings": []})}}]},
        request=httpx.Request("POST", "http://audit.example/v1/chat/completions"),
    )
    monkeypatch.setattr(
        audit_mod.httpx, "Client", create_autospec(httpx.Client, return_value=client)
    )
    return client


@pytest.mark.parametrize(("fixture", "expected"), [("shared_only", SHARED), ("per_client", ENRICH)])
def test_the_secret_audit_uses_the_enrichment_endpoint(request, audit_client, fixture, expected):
    request.getfixturevalue(fixture)

    audit_mod.audit_secrets("an invented note", [], model="test-model")

    kwargs = audit_mod.httpx.Client.call_args.kwargs
    assert kwargs["base_url"] == expected.base
    assert kwargs["headers"] == {"Authorization": f"Bearer {expected.key}"}
