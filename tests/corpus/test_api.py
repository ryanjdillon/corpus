"""REST surface, with the document store and query layer stubbed (no Docker)."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient

from corpus import api, search, store


@pytest.fixture
def client() -> TestClient:
    """Return a test client bound to the FastAPI app."""
    return TestClient(api.app)


@pytest.fixture
def documents(monkeypatch):
    """Autospec the ``DocumentStore`` each endpoint opens; return the instance it yields."""
    cls = create_autospec(store.DocumentStore)
    instance = create_autospec(store.DocumentStore, instance=True)
    instance.query.return_value = []
    instance.stats.return_value = {}
    cls.return_value.__enter__.return_value = instance
    monkeypatch.setattr(api.store, "DocumentStore", cls)
    return instance


@pytest.fixture
def semantic_search(monkeypatch):
    """Autospec ``search.semantic_search`` with a neutral default result."""
    mock = create_autospec(search.semantic_search)
    mock.return_value = []
    monkeypatch.setattr(search, "semantic_search", mock)
    return mock


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_search_endpoint(client, documents, semantic_search):
    semantic_search.return_value = [{"id": "1"}]
    resp = client.post("/search", json={"query": "boats", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json() == {"results": [{"id": "1"}]}
    assert semantic_search.call_args.args == (documents, "boats", 3, None)


def test_query_endpoint(client, documents):
    documents.query.return_value = [{"id": "1"}, {"id": "2"}]
    resp = client.post("/query", json={"label": "promotional", "before": "2026-01-01"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert documents.query.call_args.kwargs["label"] == "promotional"
    assert documents.query.call_args.kwargs["before"] == "2026-01-01"


def test_stats_endpoint(client, documents):
    documents.stats.return_value = {"total": 5, "by_label": {"personal": 5}}
    resp = client.get("/stats")
    assert resp.json() == {"total": 5, "by_label": {"personal": 5}}
