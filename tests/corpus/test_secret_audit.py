"""The LLM secret auditor sends the candidates + schema and parses the verdict.

The endpoint is a MockTransport, so no network or model is involved."""

from __future__ import annotations

import json

import httpx
import pytest

from corpus import secret_audit as audit_mod
from corpus.enricher import EnrichError, EnrichUnavailableError
from corpus.enrichment import SecretSeverity, secret_audit_schema

_COMPLETION = {
    "choices": [
        {
            "message": {
                "content": json.dumps(
                    {
                        "contains_secret": True,
                        "findings": [
                            {"type": "us_ssn", "severity": "live", "note": "an SSN is present"}
                        ],
                    }
                )
            }
        }
    ]
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gw/v1")


def test_audit_sends_candidates_schema_and_frame(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_COMPLETION)

    result = audit_mod.audit_secrets(
        "My SSN is on the form.", ["us_ssn", "credit_card"], model="local", client=_client(handler)
    )

    assert result.contains_secret is True
    assert result.findings[0].severity is SecretSeverity.live
    body = captured["body"]
    assert body["response_format"]["json_schema"]["schema"] == secret_audit_schema()
    assert "UNTRUSTED DATA" in body["messages"][0]["content"]
    assert "us_ssn, credit_card" in body["messages"][1]["content"]


def test_client_error_is_non_retryable(monkeypatch):
    monkeypatch.setattr(audit_mod.time, "sleep", lambda *_: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad")

    with pytest.raises(EnrichError):
        audit_mod.audit_secrets("x", ["us_ssn"], model="local", client=_client(handler))


def test_server_error_retries_then_unavailable(monkeypatch):
    monkeypatch.setattr(audit_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")

    with pytest.raises(EnrichUnavailableError):
        audit_mod.audit_secrets("x", [], model="local", client=_client(handler))
    assert calls["n"] == 4


def test_unparseable_output_is_enrich_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "nope"}}]})

    with pytest.raises(EnrichError):
        audit_mod.audit_secrets("x", [], model="local", client=_client(handler))


def test_transport_error_is_unavailable(monkeypatch):
    monkeypatch.setattr(audit_mod.time, "sleep", lambda *_: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(EnrichUnavailableError):
        audit_mod.audit_secrets("x", [], model="local", client=_client(handler))


def test_uses_default_client_when_none_given(monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "openai_api_base", "http://gw/v1")
    monkeypatch.setattr(audit_mod.settings, "openai_api_key", "k")
    mock = _client(lambda r: httpx.Response(200, json=_COMPLETION))
    monkeypatch.setattr(audit_mod.httpx, "Client", lambda **kw: mock)
    result = audit_mod.audit_secrets("text", ["us_ssn"], model="local")
    assert result.contains_secret is True


def test_missing_model_raises():
    with pytest.raises(ValueError):
        audit_mod.audit_secrets("x", [], model="", client=_client(lambda r: httpx.Response(200)))
