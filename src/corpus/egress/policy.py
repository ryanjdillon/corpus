"""The egress policy: what may leave the network in an LLM request body.

Gateway-neutral. :func:`inspect` takes a request body (and the calling client's
id, when the gateway forwards one) and returns a :class:`Verdict`: pass it
unchanged, replace it with a redacted body, or refuse it with a status code. The
protocol adapters in this package translate a verdict onto their wire format;
nothing here knows about Envoy, gRPC, or HTTP framing.

The policy, in order:

1. A body that is not a JSON object cannot be inspected. ``fail_open`` passes it
   through (multipart audio uploads take this path); otherwise it is refused.
2. A request for a model in ``skip_models`` passes unscanned: it is served
   locally and never leaves the network. The list is exact names, so a typo can
   only make the gate scan more.
3. A batch client (``batch_clients``) may not send a body over
   ``batch_max_bytes`` to a scanned model. Batch jobs must cap their input before
   sending; this refuses an uncapped one (413) before it reaches a provider.
4. Every message's text is redacted. A finding in ``block_types`` refuses the
   request (403) instead.

Only detection *types and counts* are logged, never a matched value or a body.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, settings
from ..redact import Span, redact

log = logging.getLogger(__name__)


def _names(csv: str) -> frozenset[str]:
    return frozenset(t.strip() for t in csv.split(",") if t.strip())


@dataclass(frozen=True)
class EgressPolicy:
    """The tunable parts of the policy; see the module docstring for the order."""

    fail_open: bool = False
    block_types: frozenset[str] = frozenset({"private_key"})
    skip_models: frozenset[str] = field(default_factory=frozenset)
    batch_clients: frozenset[str] = field(default_factory=frozenset)
    batch_max_bytes: int = 65536

    @classmethod
    def from_settings(cls, config: Settings | None = None) -> EgressPolicy:
        """Build the policy from ``CORPUS_SCAN_GATE_*`` settings (default: the loaded ones)."""
        config = config or settings
        return cls(
            fail_open=config.scan_gate_fail_open,
            block_types=_names(config.scan_gate_block_types),
            skip_models=_names(config.scan_gate_skip_models),
            batch_clients=_names(config.scan_gate_batch_clients),
            batch_max_bytes=config.scan_gate_batch_max_bytes,
        )


@dataclass(frozen=True)
class Verdict:
    """The outcome for one request body.

    ``action`` is ``"pass"`` (forward unchanged), ``"replace"`` (forward ``body``
    instead), or ``"refuse"`` (answer ``status`` with ``body`` and stop).
    ``detail`` is a value-free summary for logs and proxy access logs.
    """

    action: str
    status: int = 200
    body: bytes = b""
    detail: str = ""


PASS = Verdict("pass")


def refusal(status: int, error_type: str, message: str, detail: str) -> Verdict:
    """A refusing verdict with an OpenAI-style JSON error body."""
    body = json.dumps({"error": {"type": error_type, "message": message}}).encode()
    return Verdict("refuse", status=status, body=body, detail=detail)


def _summary(findings: Iterable[Span]) -> dict[str, int]:
    """Reduce spans to a value-free ``{type: count}`` tally for logging/responses."""
    counts: dict[str, int] = {}
    for span in findings:
        counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
    return counts


def _redact_field(container: dict[str, Any], key: str, findings: list[Span]) -> None:
    """Redact ``container[key]`` in place: a string, a list of strings, or parts."""
    value = container.get(key)
    if isinstance(value, str):
        result = redact(value)
        container[key] = result.text
        findings.extend(result.findings)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, str):
                result = redact(item)
                value[i] = result.text
                findings.extend(result.findings)
            elif isinstance(item, dict):
                _redact_part(item, findings)


def _redact_part(part: dict[str, Any], findings: list[Span]) -> None:
    """Redact one structured content part: its ``text``, and nested ``content``
    (an Anthropic ``tool_result`` carries its own string or part list)."""
    _redact_field(part, "text", findings)
    _redact_field(part, "content", findings)


def redact_payload(data: dict[str, Any]) -> list[Span]:
    """Redact every text field of a model request body in place.

    Covers the OpenAI and Anthropic shapes that carry user text:

    - chat: each message's ``content`` (a string or content parts, including an
      Anthropic ``tool_result`` part's nested ``content``) and each assistant
      ``tool_calls[].function.arguments``;
    - Anthropic's top-level ``system`` (a string or parts);
    - embeddings ``input`` and legacy completions ``prompt`` (a string or a list
      of strings; token-id arrays carry no text).

    Image and audio parts are left untouched. Returns the applied spans.
    """
    findings: list[Span] = []
    for key in ("system", "input", "prompt"):
        _redact_field(data, key, findings)

    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            _redact_field(message, "content", findings)
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    _redact_field(function, "arguments", findings)
    return findings


def _unparseable(policy: EgressPolicy, reason: str) -> Verdict:
    if policy.fail_open:
        # Routine for multipart audio uploads, so not a warning.
        log.info("egress: passing an uninspectable body through (%s)", reason)
        return PASS
    log.warning("egress: refusing an uninspectable body (%s)", reason)
    return refusal(403, "egress_policy", "request body could not be inspected", "unredactable")


def inspect(body: bytes, client_id: str | None = None, *, policy: EgressPolicy) -> Verdict:
    """Decide what may leave for one request body; see the module docstring."""
    if not body:
        return PASS
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _unparseable(policy, str(exc))
    if not isinstance(data, dict):
        return _unparseable(policy, "request body is not a JSON object")

    model = data.get("model")
    if isinstance(model, str) and model in policy.skip_models:
        return PASS

    if client_id in policy.batch_clients and len(body) > policy.batch_max_bytes:
        log.warning("egress: refusing %d-byte body from batch client %s", len(body), client_id)
        return refusal(
            413,
            "request_too_large",
            f"request body exceeds the batch-client limit of {policy.batch_max_bytes} bytes; "
            "cap the input before sending",
            f"batch body {len(body)} bytes",
        )

    findings = redact_payload(data)
    counts = _summary(findings)
    blocked = counts.keys() & policy.block_types
    if blocked:
        log.warning("egress: refusing request body; types=%s", sorted(blocked))
        return refusal(
            403,
            "egress_policy",
            "request blocked by egress data policy",
            "blocked " + json.dumps(counts, sort_keys=True),
        )
    if not findings:
        return PASS
    log.info("egress: redacted request body; findings=%s", counts)
    return Verdict("replace", body=json.dumps(data).encode(), detail=json.dumps(counts))
