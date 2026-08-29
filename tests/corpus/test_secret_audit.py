"""The LLM secret auditor sends the candidates + schema and parses the verdict.

The httpx client is a spec-bound mock, so no network or model is involved.
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec

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


def _response(status: int, *, json_body=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://gw/v1/chat/completions")
    return httpx.Response(status, json=json_body, text=text, request=request)


@pytest.fixture
def client() -> httpx.Client:
    mock = create_autospec(httpx.Client, instance=True)
    mock.post.return_value = _response(200, json_body=_COMPLETION)
    return mock


def test_audit_sends_candidates_schema_and_frame(client):
    result = audit_mod.audit_secrets(
        "My SSN is on the form.", ["us_ssn", "credit_card"], model="local", client=client
    )

    assert result.contains_secret is True
    assert result.findings[0].severity is SecretSeverity.live
    body = client.post.call_args.kwargs["json"]
    assert body["response_format"]["json_schema"]["schema"] == secret_audit_schema()
    assert "UNTRUSTED DATA" in body["messages"][0]["content"]
    assert "us_ssn, credit_card" in body["messages"][1]["content"]


def test_client_error_is_non_retryable(client):
    client.post.return_value = _response(400, text="bad")

    with pytest.raises(EnrichError):
        audit_mod.audit_secrets("x", ["us_ssn"], model="local", client=client)
    assert client.post.call_count == 1


def test_server_error_retries_then_unavailable(client, monkeypatch):
    monkeypatch.setattr(audit_mod.time, "sleep", lambda *_: None)
    client.post.return_value = _response(503, text="down")

    with pytest.raises(EnrichUnavailableError):
        audit_mod.audit_secrets("x", [], model="local", client=client)
    assert client.post.call_count == 4


def test_unparseable_output_is_enrich_error(client):
    client.post.return_value = _response(
        200, json_body={"choices": [{"message": {"content": "nope"}}]}
    )

    with pytest.raises(EnrichError):
        audit_mod.audit_secrets("x", [], model="local", client=client)


def test_transport_error_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(audit_mod.time, "sleep", lambda *_: None)
    client.post.side_effect = httpx.ConnectError("boom")

    with pytest.raises(EnrichUnavailableError):
        audit_mod.audit_secrets("x", [], model="local", client=client)


def test_uses_default_client_when_none_given(monkeypatch):
    # The default-client branch cannot be reached by injection (that is the very
    # collaborator being defaulted), so intercept its construction with a spec mock.
    monkeypatch.setattr(audit_mod.settings, "openai_api_base", "http://gw/v1")
    monkeypatch.setattr(audit_mod.settings, "openai_api_key", "k")
    client = create_autospec(httpx.Client, instance=True)
    client.post.return_value = _response(200, json_body=_COMPLETION)
    monkeypatch.setattr(audit_mod.httpx, "Client", lambda **kw: client)

    result = audit_mod.audit_secrets("text", ["us_ssn"], model="local")

    assert result.contains_secret is True
    client.close.assert_called_once()


def test_missing_model_raises(client):
    with pytest.raises(ValueError):
        audit_mod.audit_secrets("x", [], model="", client=client)
