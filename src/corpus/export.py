"""Materialize the stored corpus into the vault — one file per document.

The bootstrap that turns the current DB into the canonical vault (the body is
byte-equivalent to what the fetcher produced, so this needs no re-fetch). From here
the vault is canonical and ingest writes it first; this command also re-runs safely
(idempotent) to refresh the vault after a backfill.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from . import store, vault

log = logging.getLogger("corpus.export")


def export_archive(
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    force: bool = False,
    *,
    document_source: Callable[..., Iterable[tuple[str, str, dict]]] = store.iter_documents,
    writer: Callable[..., bool] = vault.write,
) -> dict[str, int]:
    """Write every stored document to the vault.

    Returns ``{scanned, written, unchanged}``; ``limit`` of 0 does all. The
    document source and vault writer are injectable for testing; the defaults
    stream from the store and write real vault files.
    """
    scanned = written = unchanged = 0
    for doc_id, content, meta in document_source(source=source, account=account):
        if limit and scanned >= limit:
            break
        scanned += 1
        if writer(doc_id, content, meta, force=force):
            written += 1
        else:
            unchanged += 1
    log.info("wrote %d, unchanged %d of %d scanned", written, unchanged, scanned)
    return {"scanned": scanned, "written": written, "unchanged": unchanged}
