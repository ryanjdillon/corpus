"""scan-gate: an Envoy ``ext_proc`` service that redacts LLM egress bodies.

Envoy's External Processing filter streams each request/response phase to an
out-of-process gRPC service that may mutate or halt it. Placed inline on the path
to an untrusted model provider, this service parses the OpenAI/Anthropic chat JSON
in the *request body* phase, runs the existing deterministic detectors over every
message's text via :func:`corpus.redact.redact`, and returns a ``body_mutation``
carrying the redacted body — redact-by-default. A small, config-driven policy
(``settings.scan_gate_block_types``) escalates the highest-confidence classes
(private keys by default) to an outright ``403`` instead of redaction.

The ext_proc gRPC stubs are vendored under ``corpus._ext_proc`` — the minimal
protoc-generated closure of ``envoy.service.ext_proc.v3`` (19 files across
``envoy``/``xds``/``udpa``/``validate``), with their imports rewritten to that
private namespace. The published ``xds-protos`` wheel would supply the same stubs
but also ships a stale top-level ``opentelemetry/proto`` package that shadows the
real ``opentelemetry-proto`` and breaks OTLP export; vendoring the ext_proc subset
keeps the stubs while touching no shared namespace, and depends only on the
already-present ``grpcio`` + ``protobuf``.

Only detection *types and counts* are ever logged — never a matched value, and
never the request body (raw or redacted). ``settings.scan_gate_fail_open`` selects
the failure mode for an unparseable or erroring body: fail-closed (the default)
blocks it; fail-open passes it through unchanged for a log-only shadow rollout.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from concurrent import futures
from typing import Any

import grpc

from ._ext_proc.envoy.service.ext_proc.v3 import external_processor_pb2 as ep
from ._ext_proc.envoy.service.ext_proc.v3 import external_processor_pb2_grpc as epg
from ._ext_proc.envoy.type.v3 import http_status_pb2 as hs
from .config import settings
from .redact import Span, redact

log = logging.getLogger(__name__)


def block_types() -> frozenset[str]:
    """Return the secret types that force a 403 block, read from settings.

    Parsed per call so a monkeypatched setting takes effect in tests and a config
    reload takes effect without a restart.
    """
    return frozenset(t.strip() for t in settings.scan_gate_block_types.split(",") if t.strip())


def _summary(findings: Iterable[Span]) -> dict[str, int]:
    """Reduce spans to a value-free ``{type: count}`` tally for logging/responses."""
    counts: dict[str, int] = {}
    for span in findings:
        counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
    return counts


def _redact_parts(parts: list[Any], findings: list[Span]) -> None:
    """Redact the ``text`` of each structured content part in place."""
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            result = redact(part["text"])
            part["text"] = result.text
            findings.extend(result.findings)


def redact_payload(data: dict[str, Any]) -> list[Span]:
    """Redact every text field of a chat-completion body in place.

    Handles the OpenAI and Anthropic shapes: an Anthropic top-level ``system``
    (string or content parts) and each message's ``content`` (a string or a list
    of ``{"type": ..., "text": ...}`` parts). Returns the applied spans.
    """
    findings: list[Span] = []
    system = data.get("system")
    if isinstance(system, str):
        result = redact(system)
        data["system"] = result.text
        findings.extend(result.findings)
    elif isinstance(system, list):
        _redact_parts(system, findings)

    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                result = redact(content)
                message["content"] = result.text
                findings.extend(result.findings)
            elif isinstance(content, list):
                _redact_parts(content, findings)
    return findings


# --------------------------------------------------------------------------- #
# ext_proc response builders
# --------------------------------------------------------------------------- #
def _continue_body() -> ep.ProcessingResponse:
    """A CONTINUE response leaving the request body untouched."""
    return ep.ProcessingResponse(request_body=ep.BodyResponse(response=ep.CommonResponse()))


def _replace_body(body: bytes) -> ep.ProcessingResponse:
    """A response that swaps the request body for the redacted ``body``.

    The ``content-length`` header is removed so Envoy recomputes it for the new
    body. Redaction changes the body length, and leaving the request's original
    ``content-length`` in place makes Envoy reject the mutated response with a 500
    (``mismatch_between_content_length_and_the_length_of_the_mutated_body``). The
    header mutation rides on the *same* ``CommonResponse`` as the body mutation so
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


def _blocked(counts: dict[str, int]) -> ep.ProcessingResponse:
    """A 403 ImmediateResponse halting the request; detail carries types only."""
    detail = "scan-gate blocked request: " + json.dumps(counts, sort_keys=True)
    return ep.ProcessingResponse(
        immediate_response=ep.ImmediateResponse(
            status=hs.HttpStatus(code=hs.Forbidden),
            body=b"scan-gate: request blocked by egress data policy",
            details=detail,
        )
    )


class RedactingProcessor(epg.ExternalProcessorServicer):
    """ext_proc processor that redacts (or blocks) the request body phase.

    Header, trailer, and response phases are acknowledged with a plain CONTINUE:
    the gate acts only on the buffered request body, where the chat JSON lives.
    """

    def _on_request_body(self, body: bytes) -> ep.ProcessingResponse:
        """Redact the buffered request body, or block/pass-through on policy."""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._on_error(str(exc))
        if not isinstance(data, dict):
            return self._on_error("request body is not a JSON object")

        findings = redact_payload(data)
        counts = _summary(findings)
        blocked = counts.keys() & block_types()
        if blocked:
            log.warning("scan-gate blocked request body; types=%s", sorted(blocked))
            return _blocked(counts)
        if not findings:
            return _continue_body()
        log.info("scan-gate redacted request body; findings=%s", counts)
        return _replace_body(json.dumps(data).encode())

    def _on_error(self, reason: str) -> ep.ProcessingResponse:
        """Apply the fail-open/closed policy to an unredactable body."""
        if settings.scan_gate_fail_open:
            log.warning("scan-gate fail-open: passing unredactable body through (%s)", reason)
            return _continue_body()
        log.warning("scan-gate fail-closed: blocking unredactable body (%s)", reason)
        return _blocked({"unredactable": 1})

    def Process(
        self,
        request_iterator: Iterable[ep.ProcessingRequest],
        context: grpc.ServicerContext,
    ) -> Iterator[ep.ProcessingResponse]:
        """Stream a response per request phase; act only on the request body.

        The method name is fixed by the ext_proc service definition (the ``Process``
        bidirectional stream), hence the capitalized identifier.
        """
        for request in request_iterator:
            phase = request.WhichOneof("request")
            if phase == "request_body":
                yield self._on_request_body(request.request_body.body)
            elif phase == "request_headers":
                yield ep.ProcessingResponse(request_headers=ep.HeadersResponse())
            elif phase == "response_headers":
                yield ep.ProcessingResponse(response_headers=ep.HeadersResponse())
            elif phase == "response_body":
                yield ep.ProcessingResponse(response_body=ep.BodyResponse())
            elif phase == "request_trailers":
                yield ep.ProcessingResponse(request_trailers=ep.TrailersResponse())
            elif phase == "response_trailers":
                yield ep.ProcessingResponse(response_trailers=ep.TrailersResponse())


def serve() -> None:  # pragma: no cover - binds a port and blocks on the reactor
    """Run the ext_proc gRPC server until terminated."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=settings.scan_gate_workers))
    epg.add_ExternalProcessorServicer_to_server(RedactingProcessor(), server)
    server.add_insecure_port(f"{settings.host}:{settings.scan_gate_port}")
    log.info("scan-gate ext_proc listening on %s:%s", settings.host, settings.scan_gate_port)
    server.start()
    server.wait_for_termination()
