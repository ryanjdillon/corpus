"""REST surface, with the query layer stubbed (no Docker)."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient

from corpus import api, search


@pytest.fixture
def client() -> TestClient:
    """Return a test client bound to the FastAPI app."""
    return TestClient(api.app)


@pytest.fixture
def semantic_search(monkeypatch):
    """Autospec ``search.semantic_search`` with a neutral default result."""
    mock = create_autospec(search.semantic_search)
    mock.return_value = []
    monkeypatch.setattr(search, "semantic_search", mock)
    return mock


@pytest.fixture
def structured_query(monkeypatch):
    """Autospec ``search.structured_query`` with a neutral default result."""
    mock = create_autospec(search.structured_query)
    mock.return_value = []
    monkeypatch.setattr(search, "structured_query", mock)
    return mock


@pytest.fixture
def stats(monkeypatch):
    """Autospec ``search.stats`` with a neutral default result."""
    mock = create_autospec(search.stats)
    mock.return_value = {}
    monkeypatch.setattr(search, "stats", mock)
    return mock


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_search_endpoint(client, semantic_search):
    semantic_search.return_value = [{"id": "1"}]
    resp = client.post("/search", json={"query": "boats", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json() == {"results": [{"id": "1"}]}
    assert semantic_search.call_args.args == ("boats", 3, None)


def test_query_endpoint(client, structured_query):
    structured_query.return_value = [{"id": "1"}, {"id": "2"}]
    resp = client.post("/query", json={"label": "promotional", "before": "2026-01-01"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert structured_query.call_args.kwargs["label"] == "promotional"
    assert structured_query.call_args.kwargs["before"] == "2026-01-01"


def test_stats_endpoint(client, stats):
    stats.return_value = {"total": 5, "by_label": {"personal": 5}}
    resp = client.get("/stats")
    assert resp.json() == {"total": 5, "by_label": {"personal": 5}}
