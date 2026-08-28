"""Materialize the stored corpus into the vault — one file per document.

The bootstrap that turns the current DB into the canonical vault (the body is
byte-equivalent to what the fetcher produced, so this needs no re-fetch). From here
the vault is canonical and ingest writes it first; this command also re-runs safely
(idempotent) to refresh the vault after a backfill.
"""

from __future__ import annotations

import logging

from . import store, vault

log = logging.getLogger("corpus.export")


def export_archive(
    source: str | None = None, account: str | None = None, limit: int = 0, force: bool = False
) -> dict[str, int]:
    """Write every stored document to the vault. Returns ``{scanned, written,
    unchanged}``; ``limit`` of 0 does all."""
    scanned = written = unchanged = 0
    for doc_id, content, meta in store.iter_documents(source=source, account=account):
        if limit and scanned >= limit:
            break
        scanned += 1
        if vault.write(doc_id, content, meta, force=force):
            written += 1
        else:
            unchanged += 1
    log.info("wrote %d, unchanged %d of %d scanned", written, unchanged, scanned)
    return {"scanned": scanned, "written": written, "unchanged": unchanged}
