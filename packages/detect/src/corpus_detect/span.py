"""Where a detector match sits, and how overlapping matches resolve.

A :class:`Span` is the one currency every detector speaks: half-open offsets,
a stable type name, and a category — never the matched value. It lives apart
from the detectors and from :mod:`.redaction` so both can depend on it without
depending on each other.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Category ranks for overlap resolution: a credential outranks an identity
#: number when two matches begin at the same offset.
_CATEGORY_RANK = {"secret": 0, "pii": 1}


@dataclass(frozen=True)
class Span:
    """One detector match's extent and type — never the matched value.

    ``start``/``end`` are half-open character offsets into the scanned text;
    ``entity_type`` is the stable short name (e.g. ``"us_ssn"``,
    ``"private_key"``); ``category`` is ``"pii"`` or ``"secret"``.
    """

    start: int
    end: int
    entity_type: str
    category: str


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Return a non-overlapping subset of ``spans``, deterministically.

    Sorted by start, then longest-first, then category (secrets before pii); a
    span overlapping one already kept is dropped. Leftmost-longest wins, so the
    result is stable regardless of detector ordering.
    """
    ordered = sorted(
        spans,
        key=lambda s: (s.start, -(s.end - s.start), _CATEGORY_RANK.get(s.category, 9)),
    )
    kept: list[Span] = []
    last_end = -1
    for span in ordered:
        if span.start >= last_end:
            kept.append(span)
            last_end = span.end
    return kept
