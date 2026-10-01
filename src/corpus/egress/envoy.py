"""Envoy ``ext_proc`` adapter for the egress policy.

Envoy's External Processing filter streams each request phase to this gRPC
service, which may rewrite or halt it. The adapter remembers the caller's
``x-client-id`` from the request-headers phase (the gateway's API-key
authentication sets it before ext_proc runs), applies
:func:`corpus.egress.policy.inspect` to the buffered request body, and turns the
:class:`~corpus.egress.policy.Verdict` into a ProcessingResponse. Every other
phase is acknowledged unchanged.

The ext_proc stubs are vendored under ``corpus._ext_proc``: the minimal
protoc-generated closure of ``envoy.service.ext_proc.v3``, with imports rewritten
to that private namespace. The published ``xds-protos`` wheel also ships a stale
top-level ``opentelemetry/proto`` package that breaks OTLP export, so it is not
used.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from concurrent import futures

import grpc

from .._ext_proc.envoy.config.core.v3 import base_pb2 as core
from .._ext_proc.envoy.service.ext_proc.v3 import external_processor_pb2 as ep
from .._ext_proc.envoy.service.ext_proc.v3 import external_processor_pb2_grpc as epg
from .._ext_proc.envoy.type.v3 import http_status_pb2 as hs
from ..config import settings
from .policy import PASS, EgressPolicy, Verdict, inspect, refusal

log = logging.getLogger(__name__)

CLIENT_ID_HEADER = "x-client-id"


def _continue_body() -> ep.ProcessingResponse:
    """A CONTINUE response leaving the request body untouched."""
    return ep.ProcessingResponse(request_body=ep.BodyResponse(response=ep.CommonResponse()))


def _replace_body(body: bytes) -> ep.ProcessingResponse:
    """A response that swaps the request body for ``body``.

    The ``content-length`` header is removed so Envoy recomputes it. Leaving the
    original in place makes Envoy reject the mutated body with a 500
    (``mismatch_between_content_length_and_the_length_of_the_mutated_body``). The
    header mutation rides on the same ``CommonResponse`` as the body mutation so
    Envoy applies both atomically.
    """
    return ep.ProcessingResponse(
        request_body=ep.BodyResponse(
            response=ep.CommonResponse(
                status=ep.CommonResponse.CONTINUE_AND_REPLACE,
                header_mutation=ep.HeaderMutation(remove_headers=["content-length"]),
                body_mutation=ep.BodyMutation(body=body),
            )
        )
    )


def _immediate(verdict: Verdict) -> ep.ProcessingResponse:
    """An ImmediateResponse answering the client and halting the request."""
    return ep.ProcessingResponse(
        immediate_response=ep.ImmediateResponse(
            status=hs.HttpStatus(code=verdict.status),
            headers=ep.HeaderMutation(
                set_headers=[
                    core.HeaderValueOption(
                        header=core.HeaderValue(key="content-type", raw_value=b"application/json")
                    )
                ]
            ),
            body=verdict.body,
            details=f"egress policy: {verdict.detail}",
        )
    )


def to_response(verdict: Verdict) -> ep.ProcessingResponse:
    """Map a policy verdict onto the request-body phase response."""
    if verdict.action == "replace":
        return _replace_body(verdict.body)
    if verdict.action == "refuse":
        return _immediate(verdict)
    return _continue_body()


def client_id(headers: ep.HttpHeaders) -> str | None:
    """Return the ``x-client-id`` request header, if the proxy sent one."""
    for h in headers.headers.headers:
        if h.key.lower() == CLIENT_ID_HEADER:
            return h.raw_value.decode(errors="replace") if h.raw_value else h.value
    return None


class EgressProcessor(epg.ExternalProcessorServicer):
    """ext_proc servicer applying the egress policy to the request body."""

    def __init__(
        self,
        policy: Callable[[], EgressPolicy] = EgressPolicy.from_settings,
        inspect: Callable[..., Verdict] = inspect,
    ) -> None:
        self._policy = policy
        self._inspect = inspect

    def _decide(self, body: bytes, caller: str | None) -> Verdict:
        """Apply the policy; an error inside it is answered, never left to the proxy.

        An exception would end the gRPC stream, and a fail-closed proxy would then
        return a bare 5xx with nothing in the egress log. Refuse instead (or pass,
        when failing open), logging only the exception's type.
        """
        policy = self._policy()
        try:
            return self._inspect(body, caller, policy=policy)
        except Exception as exc:  # noqa: BLE001 - any failure must become an answer
            log.warning(
                "egress: policy raised %s; %s",
                type(exc).__name__,
                "passing" if policy.fail_open else "refusing",
            )
            if policy.fail_open:
                return PASS
            return refusal(
                403,
                "egress_policy",
                "request could not be inspected",
                f"error {type(exc).__name__}",
            )

    def Process(
        self,
        request_iterator: Iterable[ep.ProcessingRequest],
        context: grpc.ServicerContext,
    ) -> Iterator[ep.ProcessingResponse]:
        """Stream one response per phase; act only on the request body.

        The method name is fixed by the ext_proc service definition. One stream
        carries one HTTP request, so the client id seen in its headers phase
        belongs to its body phase.
        """
        caller: str | None = None
        for request in request_iterator:
            phase = request.WhichOneof("request")
            if phase == "request_body":
                yield to_response(self._decide(request.request_body.body, caller))
            elif phase == "request_headers":
                caller = client_id(request.request_headers)
                yield ep.ProcessingResponse(request_headers=ep.HeadersResponse())
            elif phase == "response_headers":
                yield ep.ProcessingResponse(response_headers=ep.HeadersResponse())
            elif phase == "response_body":
                yield ep.ProcessingResponse(response_body=ep.BodyResponse())
            elif phase == "request_trailers":
                yield ep.ProcessingResponse(request_trailers=ep.TrailersResponse())
            elif phase == "response_trailers":
                yield ep.ProcessingResponse(response_trailers=ep.TrailersResponse())


def server_options(limit: int | None = None) -> list[tuple[str, int]]:
    """gRPC options sizing messages to the largest body the gate buffers.

    In Buffered mode the proxy sends the whole body as one message, and the
    rewritten body goes back as one too, so send and receive are raised together.
    ``limit`` defaults to ``settings.scan_gate_grpc_max_message_bytes``.
    """
    limit = limit or settings.scan_gate_grpc_max_message_bytes
    return [
        ("grpc.max_receive_message_length", limit),
        ("grpc.max_send_message_length", limit),
    ]


def serve() -> None:  # pragma: no cover - binds a port and blocks on the reactor
    """Run the ext_proc gRPC server until terminated."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.scan_gate_workers),
        options=server_options(),
    )
    epg.add_ExternalProcessorServicer_to_server(EgressProcessor(), server)
    server.add_insecure_port(f"{settings.host}:{settings.scan_gate_port}")
    log.info(
        "scan-gate (envoy ext_proc) listening on %s:%s", settings.host, settings.scan_gate_port
    )
    server.start()
    server.wait_for_termination()
