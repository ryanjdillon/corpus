"""REST surface, with the query layer stubbed (no Docker)."""

from fastapi.testclient import TestClient

from corpus import api, search


def _client() -> TestClient:
    return TestClient(api.app)


def test_health():
    assert _client().get("/health").json() == {"status": "ok"}


def test_search_endpoint(monkeypatch):
    monkeypatch.setattr(
        search,
        "semantic_search",
        lambda query, top_k=10, filters=None: [{"id": "1", "query": query, "top_k": top_k}],
    )
    resp = _client().post("/search", json={"query": "boats", "top_k": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["query"] == "boats"
    assert body["results"][0]["top_k"] == 3


def test_query_endpoint(monkeypatch):
    monkeypatch.setattr(
        search,
        "structured_query",
        lambda **kw: [{"id": "1"}, {"id": "2"}],
    )
    resp = _client().post("/query", json={"label": "promotional", "before": "2026-01-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2


def test_stats_endpoint(monkeypatch):
    monkeypatch.setattr(search, "stats", lambda: {"total": 5, "by_label": {"personal": 5}})
    resp = _client().get("/stats")
    assert resp.json() == {"total": 5, "by_label": {"personal": 5}}
