"""The LLM secret auditor sends the candidates + schema and parses the verdict.

The httpx client is a spec-bound mock, so no network or model is involved.
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec

import httpx
import pytest

from corpus import enricher as enricher_mod
from corpus import scan
from corpus import secret_audit as audit_mod
from corpus.enricher import EnrichError, EnrichUnavailableError
from corpus.enrichment import (
    MAX_ITEMS,
    ConfirmedSecret,
    SecretAudit,
    SecretSeverity,
    secret_audit_schema,
)

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
    monkeypatch.setattr(enricher_mod.settings, "enrich_retries", 3)
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
    client.post.return_value = _response(503, text="down")

    with pytest.raises(EnrichUnavailableError):
        audit_mod.audit_secrets("x", [], model="local", client=client)
    assert client.post.call_count == 3


def test_unparseable_output_is_enrich_error(client):
    client.post.return_value = _response(
        200, json_body={"choices": [{"message": {"content": "nope"}}]}
    )

    with pytest.raises(EnrichError):
        audit_mod.audit_secrets("x", [], model="local", client=client)


def test_transport_error_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(enricher_mod.settings, "enrich_retries", 2)
    monkeypatch.setattr(enricher_mod.time, "sleep", lambda *_: None)
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


def test_audit_falls_back_to_the_audit_model(client, monkeypatch):
    # A caller that passes no model must get the local audit model, never the
    # (possibly remote) enrichment model.
    monkeypatch.setattr(audit_mod.settings, "audit_model", "local-auditor")
    monkeypatch.setattr(audit_mod.settings, "enrich_model", "remote")

    audit_mod.audit_secrets("x", ["us_ssn"], client=client)

    assert client.post.call_args.kwargs["json"]["model"] == "local-auditor"


# --------------------------------------------------------------------------- #
# Long documents: candidate-centred windows, merged verdicts
# --------------------------------------------------------------------------- #
_KEY = "AKIAQZX3PL7RKEXAMPLE"


def _long_doc(filler: int = 200_000) -> tuple[str, str]:
    body = ("lorem ipsum " * (filler // 12)) + f"\nthe deploy key is {_KEY}\n" + ("dolor " * 5000)
    return "Subject: deploy notes\n\n" + body, body


def test_audit_texts_returns_the_whole_text_when_it_fits(monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "model_options", {})
    text, body = _long_doc()

    assert audit_mod.audit_texts(text, body, scan.candidate_spans(body), "local") == [text]


def test_long_document_is_audited_on_windows_around_candidates(monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "model_options", {"small": {"context_tokens": 8192}})
    text, body = _long_doc()

    chunks = audit_mod.audit_texts(text, body, scan.candidate_spans(body), "small")

    budget = audit_mod.model_options("small").input_chars(
        len(audit_mod._SYSTEM) + audit_mod._AUDIT_PREAMBLE
    )
    assert all(len(c) <= budget for c in chunks)
    assert any(_KEY in c for c in chunks)            # the candidate survives
    assert sum(len(c) for c in chunks) < len(text) // 10  # most filler is dropped
    assert chunks[0].startswith("Subject: deploy notes")


def test_audit_texts_terminates_on_a_tiny_budget_or_huge_subject(monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "model_options", {"tiny": {"context_tokens": 4200}})
    body = f"key {_KEY} " + "x" * 50_000
    text = "Subject: " + "s" * 20_000 + "\n\n" + body

    chunks = audit_mod.audit_texts(text, body, scan.candidate_spans(body), "tiny")

    assert chunks and all(len(c) <= 10_000 for c in chunks)


def test_merge_audits_keeps_the_worst_severity_per_type():
    merged = audit_mod.merge_audits([
        SecretAudit(contains_secret=False, findings=[
            ConfirmedSecret(type="aws_access_key", severity=SecretSeverity.none)]),
        SecretAudit(contains_secret=True, findings=[
            ConfirmedSecret(type="aws_access_key", severity=SecretSeverity.live),
            ConfirmedSecret(type="us_ssn", severity=SecretSeverity.reference)]),
    ])

    assert merged.contains_secret is True
    assert {f.type: f.severity for f in merged.findings} == {
        "aws_access_key": SecretSeverity.live, "us_ssn": SecretSeverity.reference}


def test_audit_applies_model_options(client, monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "model_options",
                        {"bonsai": {"inline_schema_refs": True, "extra_body": {"reasoning_effort": "none"}}})

    audit_mod.audit_secrets("x", ["us_ssn"], model="bonsai", client=client)

    assert client.post.call_args.kwargs["json"]["reasoning_effort"] == "none"


def test_candidate_spans_include_recovery_wording_in_order():
    body = "your backup codes are below. Also AKIAQZX3PL7RKEXAMPLE was pasted."

    spans = scan.candidate_spans(body)

    assert [s.entity_type for s in spans] == ["recovery_code", "aws_access_key"]
    assert spans[0].start < spans[1].start


def test_candidate_spans_of_empty_content_is_empty():
    assert scan.candidate_spans("") == []


def test_nearby_candidates_share_a_window_and_spread_ones_overflow_into_chunks(monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "model_options", {"small": {"context_tokens": 6000}})
    near = f"one {_KEY} and close by {_KEY} again "
    far = "".join(f"{'pad ' * 1500} key {_KEY} " for _ in range(6))
    body = near + far
    text = "Subject: many keys\n\n" + body

    chunks = audit_mod.audit_texts(text, body, scan.candidate_spans(body), "small")

    budget = audit_mod.model_options("small").input_chars(
        len(audit_mod._SYSTEM) + audit_mod._AUDIT_PREAMBLE
    )
    assert len(chunks) > 1
    assert all(len(c) <= budget for c in chunks)
    assert sum(c.count(_KEY) for c in chunks) >= 7


def test_a_subject_too_long_to_repeat_is_dropped_from_chunks(monkeypatch):
    monkeypatch.setattr(audit_mod.settings, "model_options", {"small": {"context_tokens": 6000}})
    body = "x" * 40_000 + f" key {_KEY} " + "y" * 40_000
    text = "Subject: " + "s" * 5_000 + "\n\n" + body

    chunks = audit_mod.audit_texts(text, body, scan.candidate_spans(body), "small")

    assert not any(c.startswith("Subject:") for c in chunks)
    assert any(_KEY in c for c in chunks)


def test_merge_audits_keeps_the_findings_bound_worst_first():
    findings = [
        ConfirmedSecret(type=f"t{i}", severity=SecretSeverity.reference) for i in range(MAX_ITEMS)
    ] + [ConfirmedSecret(type="live", severity=SecretSeverity.live)]
    merged = audit_mod.merge_audits([SecretAudit(contains_secret=True, findings=findings)])
    assert len(merged.findings) == MAX_ITEMS
    assert merged.findings[0].type == "live"
