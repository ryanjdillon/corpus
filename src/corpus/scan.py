"""Batch secret audit over the stored archive.

Streams stored documents, runs the deterministic detectors (``corpus_detect``) on
each, and returns a report of which messages contain which secret *types* —
counts and identifying metadata only, never the secret values. This is the "find the SSNs /
recovery codes / credentials in my mail" pass; it depends on no model.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import closing
from typing import Any

from corpus_detect import Span, detect, detect_spans, fingerprint_parts
from corpus_detect.leaks import Scanner

from .store import DocumentStore

# Tight literal-phrase gate for recovery/backup codes: these have no deterministic
# value signature (that's why the noisy token regex was removed), but the wording
# reliably marks a message worth an LLM look. Selection only — the LLM confirms.
_RECOVERY_HINT = re.compile(
    r"(?:recovery|backup)\s+code|one[-\s]?time\s+(?:pass|code)|"
    r"two[-\s]?factor|\b2fa\b|verification\s+code",
    re.IGNORECASE,
)


def _detector_fingerprint() -> str:
    """Return a stable hash of what the deterministic detectors actually match.

    Covers the detectors' own parts (``corpus_detect.fingerprint_parts``) and the
    recovery hint. It changes only when detection logic changes, so nothing needs
    manual bumping.
    """
    parts = (*fingerprint_parts(), _RECOVERY_HINT.pattern)
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:12]


#: Fingerprint of the deterministic detectors, stored with each audit so it records
#: which candidate-generation logic produced it. Derived, so it updates itself when
#: any recognizer, rule, or gate changes.
SCAN_VERSION = _detector_fingerprint()


def candidate_spans(content: str | None) -> list[Span]:
    """Return where each audit candidate sits in ``content``: detector hits plus
    recovery wording.

    The positional counterpart of :func:`audit_candidates`, used to show a long
    document's audit only the text around its candidates. Values are never included.
    """
    if not content:
        return []
    spans = detect_spans(content)
    spans += [
        Span(m.start(), m.end(), "recovery_code", "hint")
        for m in _RECOVERY_HINT.finditer(content)
    ]
    return sorted(spans, key=lambda s: s.start)


def audit_candidates(
    content: str | None, *, on_scanner: Callable[[Scanner], None] | None = None
) -> list[str]:
    """Return the secret types worth an LLM confirmation for this document.

    The deterministic hits plus ``recovery_code`` when recovery wording is present.
    Empty means the message is not worth auditing. ``on_scanner`` is told which
    credential scanner ran (``corpus_detect.leaks.scan``).
    """
    if not content:
        return []
    candidates = sorted(detect(content, on_scanner=on_scanner))
    if _RECOVERY_HINT.search(content) and "recovery_code" not in candidates:
        candidates.append("recovery_code")
    return candidates


def scan_archive(
    documents: DocumentStore,
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
) -> dict[str, Any]:
    """Scan the documents in *documents* (an open store the caller owns) for secrets.

    Returns ``{scanned, with_secrets, totals, hits}`` where ``totals`` is per-type
    counts and each hit is ``{id, secret_types, from_addr, subject, sent_at}`` — no
    values. ``limit`` of 0 scans everything.
    """
    totals: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    scanned = 0
    with closing(documents.iter(source=source, account=account)) as docs:
        for doc_id, content, meta in docs:
            if limit and scanned >= limit:
                break
            scanned += 1
            counts = detect(content)
            if not counts:
                continue
            for secret_type, count in counts.items():
                totals[secret_type] = totals.get(secret_type, 0) + count
            meta = meta or {}
            hits.append(
                {
                    "id": doc_id,
                    "secret_types": sorted(counts),
                    "from_addr": meta.get("from_addr"),
                    "subject": meta.get("subject"),
                    "sent_at": meta.get("sent_at"),
                }
            )
    return {"scanned": scanned, "with_secrets": len(hits), "totals": totals, "hits": hits}
