"""Batch secret audit over the stored archive.

Streams stored documents, runs the deterministic secret scanner on each, and
returns a report of which messages contain which secret *types* — counts and
identifying metadata only, never the secret values. This is the "find the SSNs /
recovery codes / credentials in my mail" pass; it depends on no model.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import leaks, pii, store

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

    Covers the pii recognizers/entities/gates, the leaks rules, and the recovery
    hint. It changes only when detection logic changes, so nothing needs manual
    bumping.
    """
    parts = (
        sorted(type(r).__name__ for r in pii._RECOGNIZERS),
        sorted(pii._ENTITY_NAMES.items()),
        sorted(pii._CONTEXT_REQUIRED),
        pii._MIN_SCORE,
        [(name, pattern.pattern) for name, pattern in leaks._RULES],
        sorted(leaks._RULE_MAP.items()),
        _RECOVERY_HINT.pattern,
    )
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:12]


#: Fingerprint of the deterministic detectors, stored with each audit so it records
#: which candidate-generation logic produced it. Derived, so it updates itself when
#: any recognizer, rule, or gate changes.
SCAN_VERSION = _detector_fingerprint()


def detect(content: str | None) -> dict[str, int]:
    """Merge the identity (``pii``) and credential (``leaks``) detectors.

    Returns a single ``{secret_type: count}`` map for one document.
    """
    counts = dict(pii.scan(content).secret_counts)
    for name, count in leaks.scan(content).items():
        counts[name] = counts.get(name, 0) + count
    return counts


def audit_candidates(content: str | None) -> list[str]:
    """Return the secret types worth an LLM confirmation for this document.

    The deterministic hits plus ``recovery_code`` when recovery wording is present.
    Empty means the message is not worth auditing.
    """
    if not content:
        return []
    candidates = sorted(detect(content))
    if _RECOVERY_HINT.search(content) and "recovery_code" not in candidates:
        candidates.append("recovery_code")
    return candidates


def scan_archive(
    source: str | None = None, account: str | None = None, limit: int = 0
) -> dict[str, Any]:
    """Scan stored documents for secrets.

    Returns ``{scanned, with_secrets, totals, hits}`` where ``totals`` is per-type
    counts and each hit is ``{id, secret_types, from_addr, subject, sent_at}`` — no
    values. ``limit`` of 0 scans everything.
    """
    totals: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    scanned = 0
    for doc_id, content, meta in store.iter_documents(source=source, account=account):
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
