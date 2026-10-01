"""The Envoy ext_proc adapter: phases in, ProcessingResponses out.

The processor is driven directly (no server binds a port), with the policy
injected.
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec

import grpc
import pytest

from corpus._ext_proc.envoy.config.core.v3 import base_pb2 as core
from corpus._ext_proc.envoy.service.ext_proc.v3 import external_processor_pb2 as ep
from corpus.egress import envoy
from corpus.egress.policy import EgressPolicy, Verdict


@pytest.fixture
def policy():
    return EgressPolicy(batch_clients=frozenset({"eval"}), batch_max_bytes=100)


@pytest.fixture
def processor(policy):
    return envoy.EgressProcessor(policy=lambda: policy)


@pytest.fixture
def context():
    return create_autospec(grpc.ServicerContext, instance=True)


def headers(**values: str) -> ep.ProcessingRequest:
    return ep.ProcessingRequest(
        request_headers=ep.HttpHeaders(
            headers=core.HeaderMap(
                headers=[
                    core.HeaderValue(key=k.replace("_", "-"), raw_value=v.encode())
                    for k, v in values.items()
                ]
            )
        )
    )


def body(content: str) -> ep.ProcessingRequest:
    payload = {"model": "DeepSeek-V4-Flash", "messages": [{"role": "user", "content": content}]}
    return ep.ProcessingRequest(request_body=ep.HttpBody(body=json.dumps(payload).encode()))


def test_redacted_body_replaces_and_drops_content_length(processor, context):
    (response,) = processor.Process(iter([body("key AKIAIOSFODNN7EXAMPLE")]), context)
    common = response.request_body.response
    assert common.status == ep.CommonResponse.CONTINUE_AND_REPLACE
    assert b"AKIAIOSFODNN7EXAMPLE" not in common.body_mutation.body
    # Redaction changes the length; Envoy must recompute content-length or it
    # rejects the mutated body with a 500.
    assert list(common.header_mutation.remove_headers) == ["content-length"]


def test_clean_body_continues_unchanged(processor, context):
    (response,) = processor.Process(iter([body("lunch at noon?")]), context)
    common = response.request_body.response
    assert common.status == ep.CommonResponse.CONTINUE
    assert not common.HasField("body_mutation") and not common.HasField("header_mutation")


def test_refusal_is_an_immediate_json_response(processor, context):
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    (response,) = processor.Process(iter([body(secret)]), context)
    immediate = response.immediate_response
    assert immediate.status.code == 403
    assert json.loads(immediate.body)["error"]["type"] == "egress_policy"
    assert [h.header.key for h in immediate.headers.set_headers] == ["content-type"]


def test_client_id_from_the_headers_phase_reaches_the_policy(processor, context):
    responses = list(
        processor.Process(iter([headers(x_client_id="eval"), body("x" * 200)]), context)
    )
    assert responses[0].WhichOneof("response") == "request_headers"
    assert responses[1].immediate_response.status.code == 413


def test_other_client_is_not_capped(processor, context):
    responses = list(
        processor.Process(iter([headers(x_client_id="orchestrator"), body("x" * 200)]), context)
    )
    assert responses[1].request_body.response.status == ep.CommonResponse.CONTINUE


def test_client_id_reads_plain_value_too():
    request = ep.HttpHeaders(
        headers=core.HeaderMap(headers=[core.HeaderValue(key="X-Client-Id", value="pi")])
    )
    assert envoy.client_id(request) == "pi"
    assert envoy.client_id(ep.HttpHeaders()) is None


def test_other_phases_are_acknowledged(processor, context):
    phases = [
        ep.ProcessingRequest(response_headers=ep.HttpHeaders()),
        ep.ProcessingRequest(response_body=ep.HttpBody(body=b"")),
        ep.ProcessingRequest(request_trailers=ep.HttpTrailers()),
        ep.ProcessingRequest(response_trailers=ep.HttpTrailers()),
    ]
    responses = list(processor.Process(iter(phases), context))
    assert [r.WhichOneof("response") for r in responses] == [
        "response_headers",
        "response_body",
        "request_trailers",
        "response_trailers",
    ]


@pytest.fixture
def failing_inspect():
    return create_autospec(envoy.inspect, side_effect=ValueError)


def test_inspection_error_is_refused_when_failing_closed(context, failing_inspect):
    processor = envoy.EgressProcessor(
        policy=lambda: EgressPolicy(fail_open=False), inspect=failing_inspect
    )
    (response,) = processor.Process(iter([body("hi")]), context)
    assert response.immediate_response.status.code == 403
    assert "ValueError" in response.immediate_response.details


def test_inspection_error_passes_when_failing_open(context, failing_inspect):
    processor = envoy.EgressProcessor(
        policy=lambda: EgressPolicy(fail_open=True), inspect=failing_inspect
    )
    (response,) = processor.Process(iter([body("hi")]), context)
    assert response.request_body.response.status == ep.CommonResponse.CONTINUE


def test_pass_verdict_maps_to_continue():
    assert envoy.to_response(Verdict("pass")).request_body.response.status == (
        ep.CommonResponse.CONTINUE
    )


def test_server_options_size_both_directions():
    assert dict(envoy.server_options(1234)) == {
        "grpc.max_receive_message_length": 1234,
        "grpc.max_send_message_length": 1234,
    }


def test_unavailable_policy_is_refused(context):
    def broken_policy():
        raise RuntimeError("bad config")

    processor = envoy.EgressProcessor(policy=broken_policy)
    (response,) = processor.Process(iter([body("hi")]), context)
    assert response.immediate_response.status.code == 403
