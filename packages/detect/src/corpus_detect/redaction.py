"""Value-preserving redaction over free text, built on the detectors.

The count view (:func:`.detectors.detect`) answers *which* secret types a text
contains — it never needs the matched values or their positions. Redaction
does: to strip a secret from a body that is about to egress, we must know
exactly where each match sits so it can be replaced.

:func:`redact` composes the detectors' spans (:func:`.detectors.detect_spans`),
resolves overlaps, and rewrites the text with a neutral placeholder per match,
returning the redacted text plus a value-free :class:`RedactResult` summary
(types, counts, offsets). A caller with a detector of its own (a Betterleaks
sidecar, say) passes its matches as ``extra_spans``; they are resolved together
with the detected ones, a secret winning over any pii span it overlaps.

Masking (a fixed ``[REDACTED:<type>]`` placeholder) is used rather than a
format-preserving anonymizer: it is dependency-free, deterministic, and
idempotent — the placeholder matches none of the detectors, so redacting an
already-redacted body is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .detectors import detect_spans
from .span import Span, resolve_strictest_with_origins, validate_spans


@dataclass(frozen=True)
class RedactResult:
    """The outcome of :func:`redact`: redacted text plus a value-free summary.

    ``findings`` are the applied masks (original-text offsets, no values); a
    span cut around an overlapping one yields one finding per piece. ``counts``
    tallies, per type, the matched spans behind the masks: a span counts once
    however many pieces it was cut into, and a span wholly covered by others
    is not counted.
    """

    text: str
    findings: tuple[Span, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        """Return True when at least one span was masked."""
        return bool(self.findings)


def _placeholder(span: Span) -> str:
    """Return the neutral masking token for a span: ``[REDACTED:<type>]``.

    Built from the span alone, which holds no text, so no category and no type
    (known to the detectors or not) can put any of the matched value in the
    output.
    """
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


def redact(text: str | None, *, extra_spans: Sequence[Span] = ()) -> RedactResult:
    """Redact every detected PII/secret span in ``text``, and any ``extra_spans``.

    ``extra_spans`` are matches from a detector outside this package. Their
    offsets are code points into ``text`` (Python ``str`` indices); each needs
    ``0 <= start < end <= len(text)``, a category of ``"secret"`` or ``"pii"``
    and a type matching ``^[a-z0-9:_-]{1,64}$``, or ``ValueError`` is raised.
    They are resolved with the detected spans, a secret winning over any pii
    span it overlaps (see :func:`.span.resolve_strictest`). ``counts`` counts
    each matched span once, even when it is masked as several pieces.

    Returns the redacted text and a value-free summary. Deterministic,
    overlap-safe, and idempotent: the placeholders match none of the detectors,
    so ``redact(redact(t).text).text == redact(t).text``.
    """
    validate_spans(extra_spans, len(text or ""), name="extra_spans")
    if not text:
        return RedactResult(text or "")
    spans = [*detect_spans(text), *extra_spans]
    resolved = resolve_strictest_with_origins(spans)
    redacted, applied = _apply(text, [piece for piece, _ in resolved])
    counts: dict[str, int] = {}
    for origin in sorted({i for _, i in resolved}):
        entity_type = spans[origin].entity_type
        counts[entity_type] = counts.get(entity_type, 0) + 1
    return RedactResult(redacted, tuple(applied), counts)
