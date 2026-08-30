"""The ext_proc redaction gate: payload walking, mutation, block, and fail modes.

The processor is driven directly (no server binds a port): a ``ProcessingRequest``
stream in, ``ProcessingResponse`` messages out. All secret vectors are fake.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

from corpus import scan_gate
from corpus._ext_proc.envoy.service.ext_proc.v3 import external_processor_pb2 as ep
from corpus.config import settings

_FAKE_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
    "-----END OPENSSH PRIVATE KEY-----"
)


def _body_request(payload: dict) -> ep.ProcessingRequest:
    return ep.ProcessingRequest(request_body=ep.HttpBody(body=json.dumps(payload).encode()))


def _drive(request: ep.ProcessingRequest) -> ep.ProcessingResponse:
    responses = list(scan_gate.RedactingProcessor().Process(iter([request]), Mock()))
    assert len(responses) == 1
    return responses[0]


# --------------------------------------------------------------------------- #
# payload walking
# --------------------------------------------------------------------------- #
def test_redact_payload_openai_string_content():
    data = {"messages": [{"role": "user", "content": "key AKIAIOSFODNN7EXAMPLE"}]}
    findings = scan_gate.redact_payload(data)
    assert "AKIAIOSFODNN7EXAMPLE" not in data["messages"][0]["content"]
    assert [f.entity_type for f in findings] == ["aws_access_key"]


def test_redact_payload_content_parts_and_anthropic_system():
    data = {
        "system": "contact alice@example.org",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "card 4111 1111 1111 1111 now"}]}
        ],
    }
    findings = scan_gate.redact_payload(data)
    assert "alice@example.org" not in data["system"]
    assert "4111" not in data["messages"][0]["content"][0]["text"]
    assert {f.entity_type for f in findings} == {"email", "credit_card"}


def test_redact_payload_anthropic_system_parts():
    data = {"system": [{"type": "text", "text": "mail bob@example.net"}], "messages": []}
    scan_gate.redact_payload(data)
    assert "bob@example.net" not in data["system"][0]["text"]


def test_redact_payload_ignores_non_dict_messages():
    data = {"messages": ["not a dict", {"role": "user", "content": "key AKIAIOSFODNN7EXAMPLE"}]}
    findings = scan_gate.redact_payload(data)
    assert [f.entity_type for f in findings] == ["aws_access_key"]


# --------------------------------------------------------------------------- #
# processor phases
# --------------------------------------------------------------------------- #
def test_dirty_body_returns_body_mutation():
    request = _body_request({"messages": [{"role": "user", "content": "key AKIAIOSFODNN7EXAMPLE"}]})
    response = _drive(request)
    common = response.request_body.response
    assert common.status == ep.CommonResponse.CONTINUE_AND_REPLACE
    new_body = json.loads(common.body_mutation.body)
    assert "AKIAIOSFODNN7EXAMPLE" not in new_body["messages"][0]["content"]


def test_clean_body_continues_without_mutation():
    request = _body_request({"messages": [{"role": "user", "content": "lunch at noon?"}]})
    response = _drive(request)
    common = response.request_body.response
    assert common.status == ep.CommonResponse.CONTINUE
    assert not common.HasField("body_mutation")


def test_private_key_is_blocked_403():
    request = _body_request({"messages": [{"role": "user", "content": _FAKE_PRIVATE_KEY}]})
    response = _drive(request)
    assert response.HasField("immediate_response")
    assert response.immediate_response.status.code == 403
    # the block detail reports the type, never the value
    assert "private_key" in response.immediate_response.details
    assert "PRIVATE KEY" not in response.immediate_response.details


def test_block_types_removed_from_policy_redacts_instead(monkeypatch):
    monkeypatch.setattr(settings, "scan_gate_block_types", "")
    request = _body_request({"messages": [{"role": "user", "content": _FAKE_PRIVATE_KEY}]})
    response = _drive(request)
    assert response.HasField("request_body")  # redacted, not blocked
    assert "PRIVATE KEY" not in response.request_body.response.body_mutation.body.decode()


def test_unparseable_body_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "scan_gate_fail_open", False)
    request = ep.ProcessingRequest(request_body=ep.HttpBody(body=b"not json at all"))
    response = _drive(request)
    assert response.immediate_response.status.code == 403


def test_unparseable_body_fails_open(monkeypatch):
    monkeypatch.setattr(settings, "scan_gate_fail_open", True)
    request = ep.ProcessingRequest(request_body=ep.HttpBody(body=b"not json at all"))
    response = _drive(request)
    assert response.request_body.response.status == ep.CommonResponse.CONTINUE
    assert not response.request_body.response.HasField("body_mutation")


def test_non_object_json_body_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "scan_gate_fail_open", False)
    request = ep.ProcessingRequest(request_body=ep.HttpBody(body=b"[1, 2, 3]"))
    response = _drive(request)
    assert response.immediate_response.status.code == 403


def test_empty_body_continues():
    request = ep.ProcessingRequest(request_body=ep.HttpBody(body=b""))
    response = _drive(request)
    assert response.request_body.response.status == ep.CommonResponse.CONTINUE


def test_header_and_trailer_phases_continue():
    processor = scan_gate.RedactingProcessor()
    phases = [
        ep.ProcessingRequest(request_headers=ep.HttpHeaders()),
        ep.ProcessingRequest(response_headers=ep.HttpHeaders()),
        ep.ProcessingRequest(response_body=ep.HttpBody(body=b"")),
        ep.ProcessingRequest(request_trailers=ep.HttpTrailers()),
        ep.ProcessingRequest(response_trailers=ep.HttpTrailers()),
    ]
    responses = list(processor.Process(iter(phases), Mock()))
    assert [r.WhichOneof("response") for r in responses] == [
        "request_headers",
        "response_headers",
        "response_body",
        "request_trailers",
        "response_trailers",
    ]


def test_block_types_parsing(monkeypatch):
    monkeypatch.setattr(settings, "scan_gate_block_types", " private_key , openai_key ,")
    assert scan_gate.block_types() == frozenset({"private_key", "openai_key"})
