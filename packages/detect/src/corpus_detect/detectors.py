"""The identity (``pii``) and credential (``leaks``) detectors, merged.

Two views of one text: :func:`detect` counts each secret type, for an audit
that must never hold a value; :func:`detect_spans` locates each match, for
redaction. :func:`fingerprint_parts` names everything those matches depend on,
so a consumer can version what it stores by the detection logic that produced it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import leaks, pii
from .span import Span

#: Every fixed type name the detectors emit. Betterleaks, when configured, can
#: add ``leak_<ruleid>`` counts beyond these (see :mod:`.leaks`).
ENTITY_TYPES = pii.ENTITY_TYPES | leaks.ENTITY_TYPES


def detect(
    content: str | None,
    *,
    on_scanner: Callable[[leaks.Scanner], None] | None = None,
    leaks_config: leaks.LeaksSettings | None = None,
) -> dict[str, int]:
    """Merge the identity and credential detectors' counts.

    Returns a single ``{secret_type: count}`` map for one text. ``on_scanner`` and
    ``leaks_config`` are passed to :func:`leaks.scan`, which reports the credential
    scanner that ran and defaults its settings to the environment's.
    """
    counts = dict(pii.scan(content).secret_counts)
    for name, count in leaks.scan(
        content, config=leaks_config, on_scanner=on_scanner
    ).items():
        counts[name] = counts.get(name, 0) + count
    return counts


def detect_spans(content: str | None) -> list[Span]:
    """Merge the identity and credential detectors' match offsets for redaction.

    The span-level counterpart of :func:`detect`: the same two detectors, but each
    match's ``[start, end)`` extent and type rather than a count. Values are never
    included. Overlap resolution is the caller's concern (see ``redact``).
    """
    if not content:
        return []
    return pii.scan_spans(content) + leaks.scan_spans(content)


def fingerprint_parts() -> tuple[Any, ...]:
    """Return what the detectors match on, as stable, hashable-by-``repr`` parts.

    Covers the pii recognizers, entities, gates and Norwegian patterns, and the
    leaks rules and Betterleaks rule map. A consumer hashes these (with any
    selection logic of its own) into a version that changes only when detection
    does. The order and shape are part of the contract: changing either changes
    every consumer's version.
    """
    return (
        sorted(type(r).__name__ for r in pii._RECOGNIZERS),
        sorted(pii._ENTITY_NAMES.items()),
        sorted(pii._CONTEXT_REQUIRED),
        sorted(pii._EXTRA_CONTEXT.items()),
        pii._MIN_SCORE,
        pii._NO_NATIONAL_ID.pattern,
        pii._NO_BANK_ACCOUNT.pattern,
        pii._NO_BANK_ACCOUNT_CONTEXT,
        [(name, pattern.pattern) for name, pattern in leaks._RULES],
        sorted(leaks._RULE_MAP.items()),
    )
