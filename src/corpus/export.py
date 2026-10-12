"""Materialize the stored corpus into the vault — one file per document.

The bootstrap that turns the current DB into the canonical vault (the body is
byte-equivalent to what the fetcher produced, so this needs no re-fetch). From here
the vault is canonical and ingest writes it first; this command also re-runs safely
(idempotent) to refresh the vault after a backfill.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import vault
from .store import DocumentStore

log = logging.getLogger("corpus.export")


def export_archive(
    documents: DocumentStore,
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    force: bool = False,
    *,
    writer: Callable[..., bool] = vault.write,
) -> dict[str, int]:
    """Write every document in *documents* (an open store the caller owns) to the vault.

    Returns ``{scanned, written, unchanged}``; ``limit`` of 0 does all. The vault
    writer is injectable for testing; the default writes real vault files.
    """
    scanned = written = unchanged = 0
    for doc_id, content, meta in documents.iter(source=source, account=account):
        if limit and scanned >= limit:
            break
        scanned += 1
        if writer(doc_id, content, meta, force=force):
            written += 1
        else:
            unchanged += 1
    log.info("wrote %d, unchanged %d of %d scanned", written, unchanged, scanned)
    return {"scanned": scanned, "written": written, "unchanged": unchanged}
