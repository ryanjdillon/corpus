"""REST surface, with the query layer stubbed (no Docker)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, create_autospec

import httpx
import pytest
from fastapi.testclient import TestClient

from corpus import api, enrich_batch, scan, search
from corpus import secret_audit as audit_mod


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


# --- audit endpoint ---


@pytest.fixture
def get_document(monkeypatch):
    """Autospec ``search.get_document`` with a default document."""
    mock = create_autospec(search.get_document)
    mock.return_value = {
        "id": "test::1",
        "content": "My SSN is 123-45-6789",
        "meta": {"subject": "Test", "source": "test"},
    }
    monkeypatch.setattr(search, "get_document", mock)
    return mock


@pytest.fixture
def audit_candidates(monkeypatch):
    """Autospec ``scan.audit_candidates`` with default candidates."""
    mock = create_autospec(scan.audit_candidates)
    mock.return_value = ["us_ssn"]
    monkeypatch.setattr(scan, "audit_candidates", mock)
    return mock


@pytest.fixture
def enrich_store(monkeypatch):
    """Mock the EnrichStore context manager."""
    store_instance = MagicMock()
    store_instance.__enter__ = MagicMock(return_value=store_instance)
    store_instance.__exit__ = MagicMock(return_value=False)
    mock_class = MagicMock(return_value=store_instance)
    monkeypatch.setattr(api, "EnrichStore", mock_class)
    return store_instance


@pytest.fixture
def audit_secrets_mock(monkeypatch):
    """Mock the audit_secrets function."""

    def _response(status: int, *, json_body=None) -> httpx.Response:
        request = httpx.Request("POST", "http://gw/v1/chat/completions")
        return httpx.Response(status, json=json_body, request=request)

    completion = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "contains_secret": True,
                            "findings": [
                                {"type": "us_ssn", "severity": "live", "note": "SSN present"}
                            ],
                        }
                    )
                }
            }
        ]
    }
    http_client = create_autospec(httpx.Client, instance=True)
    http_client.post.return_value = _response(200, json_body=completion)
    monkeypatch.setattr(audit_mod.httpx, "Client", lambda **kw: http_client)
    return http_client


@pytest.fixture
def enrich_model(monkeypatch):
    """Set the enrich_model setting the audit resolves its model from."""
    monkeypatch.setattr(enrich_batch.settings, "enrich_model", "test-model")


def test_audit_endpoint_returns_none_for_missing_doc(client, monkeypatch):
    mock = create_autospec(search.get_document)
    mock.return_value = None
    monkeypatch.setattr(search, "get_document", mock)

    resp = client.post("/audit", json={"id": "missing::1"})
    assert resp.status_code == 200
    assert resp.json() is None


def test_audit_endpoint_runs_audit_and_saves(
    client, get_document, audit_candidates, enrich_store, audit_secrets_mock, enrich_model
):
    resp = client.post("/audit", json={"id": "test::1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "test::1"
    assert data["candidates"] == ["us_ssn"]
    assert data["audit"]["contains_secret"] is True
    assert data["audit"]["findings"][0]["type"] == "us_ssn"
    assert data["model"] == "test-model"

    # What is persisted is the model's own verdict, attributed to that model.
    doc_id, candidates, verdict, model, scan_version = enrich_store.save_audit.call_args.args
    assert doc_id == "test::1"
    assert candidates == ["us_ssn"]
    assert verdict == data["audit"]
    assert model == "test-model"
    assert scan_version == scan.SCAN_VERSION


def test_audit_endpoint_no_candidates_does_not_store_a_verdict(
    client, get_document, monkeypatch, enrich_store, enrich_model
):
    """No candidates means no model call, so no verdict may be attributed to one."""
    mock = create_autospec(scan.audit_candidates)
    mock.return_value = []
    monkeypatch.setattr(scan, "audit_candidates", mock)

    resp = client.post("/audit", json={"id": "test::1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidates"] == []
    assert data["audit"] is None
    enrich_store.save_audit.assert_not_called()


def test_audit_endpoint_raises_without_model(
    client, get_document, audit_candidates, enrich_store, monkeypatch
):
    monkeypatch.setattr(enrich_batch.settings, "enrich_model", "")

    with pytest.raises(ValueError, match="no model configured"):
        client.post("/audit", json={"id": "test::1"})
