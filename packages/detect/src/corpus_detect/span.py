"""Where a detector match sits, and how overlapping matches resolve.

A :class:`Span` is the one currency every detector speaks: half-open offsets,
a stable type name, and a category — never the matched value. It lives apart
from the detectors and from :mod:`.redaction` so both can depend on it without
depending on each other.
"""

from __future__ import annotations

import re
from bisect import bisect_right, insort
from collections.abc import Sequence
from dataclasses import dataclass

#: Category ranks for overlap resolution: a credential outranks an identity
#: number when two matches begin at the same offset.
_CATEGORY_RANK = {"secret": 0, "pii": 1}

#: Every category a detector emits; a span supplied from outside must carry one.
CATEGORIES = frozenset(_CATEGORY_RANK)

#: What a type name supplied from outside may look like: short, lower-case, and
#: free of anything a placeholder or a log line could misread.
_TYPE_NAME = re.compile(r"[a-z0-9:_-]{1,64}")


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


def validate_spans(spans: Sequence[Span], text_length: int, *, name: str = "spans") -> None:
    """Raise ``ValueError`` unless every span fits a text of ``text_length``.

    Each span needs integer offsets with ``0 <= start < end <= text_length``, a
    known category (:data:`CATEGORIES`) and a type matching
    ``^[a-z0-9:_-]{1,64}$``. The message names the span and the field, never a
    value from the span or the text, so it is safe to log.
    """
    # One exception type for every malformed span, wrong types included, so a
    # caller relaying spans from another process handles a single failure.
    for i, span in enumerate(spans):
        where = f"{name}[{i}]"
        if not isinstance(span, Span):
            raise ValueError(f"{where} must be a Span")  # noqa: TRY004
        for field in ("start", "end"):
            value = getattr(span, field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{where}.{field} must be an int")  # noqa: TRY004
        if not 0 <= span.start < span.end <= text_length:
            raise ValueError(f"{where}.start/end must satisfy 0 <= start < end <= len(text)")
        if span.category not in CATEGORIES:
            raise ValueError(f"{where}.category must be one of {sorted(CATEGORIES)}")
        if not isinstance(span.entity_type, str) or not _TYPE_NAME.fullmatch(span.entity_type):
            raise ValueError(f"{where}.entity_type must match ^[a-z0-9:_-]{{1,64}}$")


def resolve_strictest(spans: Sequence[Span]) -> list[Span]:
    """Return non-overlapping spans covering every character any input span covers.

    :func:`resolve_overlaps` drops a span that overlaps one already kept, so it
    can leave characters of the dropped span in the text, and prefers a secret
    only on an exact tie. Here nothing is dropped that is not already covered:
    spans are placed secrets first, then by start, longest first, then by type
    name, and each keeps only the parts no earlier span covers. So every secret
    character is masked under a secret's type, the masks cover exactly the union
    of the inputs whatever their order, and identical spans take the
    lexicographically smallest type. A span cut around an earlier one can yield
    several pieces, each under its own type. With no overlapping inputs the
    result equals :func:`resolve_overlaps`.
    """
    return [piece for piece, _ in resolve_strictest_with_origins(spans)]


def resolve_strictest_with_origins(spans: Sequence[Span]) -> list[tuple[Span, int]]:
    """:func:`resolve_strictest`, pairing each piece with its input span's index.

    A caller counting findings uses the index to count an input span once, even
    when it was cut into several pieces.
    """
    order = sorted(
        range(len(spans)),
        key=lambda i: (
            _CATEGORY_RANK.get(spans[i].category, len(_CATEGORY_RANK)),
            spans[i].start,
            -(spans[i].end - spans[i].start),
            spans[i].entity_type,
        ),
    )
    kept: list[Span] = []
    pieces: list[tuple[Span, int]] = []
    for i in order:
        for piece in _outside(spans[i], kept):
            insort(kept, piece, key=lambda s: s.start)
            pieces.append((piece, i))
    return sorted(pieces, key=lambda p: p[0].start)


def _outside(span: Span, cover: list[Span]) -> list[Span]:
    """The parts of ``span`` no span in ``cover`` (sorted, disjoint) overlaps."""
    parts: list[Span] = []
    start = span.start
    # Disjoint and sorted by start, so also sorted by end: skip what ends first.
    for c in cover[bisect_right(cover, span.start, key=lambda s: s.end) :]:
        if c.start >= span.end:
            break
        if c.start > start:
            parts.append(Span(start, c.start, span.entity_type, span.category))
        start = max(start, c.end)
    if start < span.end:
        parts.append(Span(start, span.end, span.entity_type, span.category))
    return parts
