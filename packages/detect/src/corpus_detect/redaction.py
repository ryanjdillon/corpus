"""Value-preserving redaction over free text, built on the detectors.

The count view (:func:`.detectors.detect`) answers *which* secret types a text
contains — it never needs the matched values or their positions. Redaction
does: to strip a secret from a body that is about to egress, we must know
exactly where each match sits so it can be replaced.

:func:`redact` composes the detectors' spans (:func:`.detectors.detect_spans`),
resolves overlaps, and rewrites the text with a neutral placeholder per match,
returning the redacted text plus a value-free :class:`RedactResult` summary
(types, counts, offsets).

Masking (a fixed ``[REDACTED:<type>]`` placeholder) is used rather than a
format-preserving anonymizer: it is dependency-free, deterministic, and
idempotent — the placeholder matches none of the detectors, so redacting an
already-redacted body is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .detectors import detect_spans
from .span import Span, resolve_overlaps


@dataclass(frozen=True)
class RedactResult:
    """The outcome of :func:`redact`: redacted text plus a value-free summary.

    ``findings`` are the applied spans (original-text offsets, no values);
    ``counts`` is the per-type tally.
    """

    text: str
    findings: tuple[Span, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        """Return True when at least one span was masked."""
        return bool(self.findings)


def _placeholder(span: Span) -> str:
    """Return the neutral masking token for a span (contains no matched value)."""
    return f"[REDACTED:{span.entity_type}]"


def _apply(text: str, spans: list[Span]) -> tuple[str, list[Span]]:
    """Replace each span in ``text`` with its placeholder, left to right."""
    out: list[str] = []
    cursor = 0
    applied: list[Span] = []
    for span in sorted(spans, key=lambda s: s.start):
        out.append(text[cursor : span.start])
        out.append(_placeholder(span))
        cursor = span.end
        applied.append(span)
    out.append(text[cursor:])
    return "".join(out), applied


def redact(text: str | None) -> RedactResult:
    """Redact every detected PII/secret span in ``text``.

    Returns the redacted text and a value-free summary. Deterministic,
    overlap-safe, and idempotent: the placeholders match none of the detectors,
    so ``redact(redact(t).text).text == redact(t).text``.
    """
    if not text:
        return RedactResult(text or "")
    resolved = resolve_overlaps(detect_spans(text))
    redacted, applied = _apply(text, resolved)
    counts: dict[str, int] = {}
    for span in applied:
        counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
    return RedactResult(redacted, tuple(applied), counts)
