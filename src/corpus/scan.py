"""Batch secret audit over the stored archive.

Streams stored documents, runs the deterministic secret scanner on each, and
returns a report of which messages contain which secret *types* — counts and
identifying metadata only, never the secret values. This is the "find the SSNs /
recovery codes / credentials in my mail" pass; it depends on no model.
"""

from __future__ import annotations

import re
from typing import Any

from . import leaks, pii, store

#: Bumped when the deterministic detectors change, so a stored audit records which
#: candidate-generation logic produced it.
SCAN_VERSION = 1

# Tight literal-phrase gate for recovery/backup codes: these have no deterministic
# value signature (that's why the noisy token regex was removed), but the wording
# reliably marks a message worth an LLM look. Selection only — the LLM confirms.
_RECOVERY_HINT = re.compile(
    r"(?:recovery|backup)\s+code|one[-\s]?time\s+(?:pass|code)|two[-\s]?factor|\b2fa\b|verification\s+code",
    re.IGNORECASE,
)


def detect(content: str | None) -> dict[str, int]:
    """Merge the identity (``pii``) and credential (``leaks``) detectors into a
    single {secret_type: count} map for one document."""
    counts = dict(pii.scan(content).secret_counts)
    for name, count in leaks.scan(content).items():
        counts[name] = counts.get(name, 0) + count
    return counts


def audit_candidates(content: str | None) -> list[str]:
    """Secret types worth an LLM confirmation for this document: the deterministic
    hits plus ``recovery_code`` when recovery wording is present. Empty means the
    message is not worth auditing."""
    if not content:
        return []
    candidates = sorted(detect(content))
    if _RECOVERY_HINT.search(content) and "recovery_code" not in candidates:
        candidates.append("recovery_code")
    return candidates


def scan_archive(
    source: str | None = None, account: str | None = None, limit: int = 0
) -> dict[str, Any]:
    """Scan stored documents for secrets. Returns
    ``{scanned, with_secrets, totals, hits}`` where ``totals`` is per-type counts
    and each hit is ``{id, secret_types, from_addr, subject, sent_at}`` — no values.
    ``limit`` of 0 scans everything.
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
