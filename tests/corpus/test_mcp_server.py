"""Import-level guard: the MCP server module must import and register its tools.

This catches SDK API breakage (e.g. an incompatible mcp version) that a
mock-based API test would miss, since nothing else imports this module.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, create_autospec

import httpx
import pytest

from corpus import enrich_batch, mcp_server, scan, search
from corpus import secret_audit as audit_mod


def test_module_imports_and_registers_tools():
    from corpus import mcp_server

    assert mcp_server.mcp is not None
    # The tool functions are defined at import time.
    for name in ("corpus_search", "corpus_query", "corpus_get", "corpus_stats", "audit_secret"):
        assert hasattr(mcp_server, name)


def test_run_is_callable():
    from corpus import mcp_server

    assert callable(mcp_server.run)


# --- audit_secret tool tests ---


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
    monkeypatch.setattr(mcp_server, "EnrichStore", mock_class)
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


def test_audit_secret_returns_none_for_missing_doc(monkeypatch):
    mock = create_autospec(search.get_document)
    mock.return_value = None
    monkeypatch.setattr(search, "get_document", mock)

    result = mcp_server.audit_secret(id="missing::1")
    assert result is None


def test_audit_secret_runs_audit_and_saves(
    get_document, audit_candidates, enrich_store, audit_secrets_mock, enrich_model
):
    result = mcp_server.audit_secret(id="test::1")

    assert result is not None
    assert result["id"] == "test::1"
    assert result["candidates"] == ["us_ssn"]
    assert result["audit"]["contains_secret"] is True
    assert result["model"] == "test-model"
    enrich_store.save_audit.assert_called_once()


def test_audit_secret_no_candidates_does_not_store_a_verdict(
    get_document, enrich_store, enrich_model, monkeypatch
):
    """No candidates means no model call, so no verdict may be attributed to one."""
    mock = create_autospec(scan.audit_candidates)
    mock.return_value = []
    monkeypatch.setattr(scan, "audit_candidates", mock)

    result = mcp_server.audit_secret(id="test::1")

    assert result is not None
    assert result["candidates"] == []
    assert result["audit"] is None
    enrich_store.save_audit.assert_not_called()


def test_audit_secret_raises_without_model(
    get_document, audit_candidates, enrich_store, monkeypatch
):
    monkeypatch.setattr(enrich_batch.settings, "enrich_model", "")

    with pytest.raises(ValueError, match="no model configured"):
        mcp_server.audit_secret(id="test::1")
