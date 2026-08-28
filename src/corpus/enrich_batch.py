"""Batch enrichment over the stored archive.

One pass per document: structured enrichment (LLM) always, plus an LLM secret
audit only where the deterministic detectors (or recovery wording) flagged
candidates -- so the local model does a single full pass, with the extra audit
falling on the small flagged subset rather than a second run over everything.

run_audit re-runs only the secret confirmation over the flagged documents,
without re-enriching -- for when the detectors or the model improve.

The caller owns the store (opens and closes it); the LLM, the document source, and
the audit call are injectable, so the orchestration can be exercised without I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice

import msgspec

from . import scan
from .config import settings
from .enricher import Enricher, EnrichError
from .enrichment import SCHEMA_VERSION
from .secret_audit import audit_secrets
from .store import iter_documents

log = logging.getLogger("corpus.enrich")


def _model_text(meta, content) -> str:
    """What the model sees: the subject prepended to the body. The subject is
    highly informative for classification (domain/category), and the deterministic
    detectors already run on the body separately."""
    subject = (meta or {}).get("subject") or ""
    return f"Subject: {subject}\n\n{content or ''}"


def run_enrich(
    store,
    *,
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    force: bool = False,
    enricher: Enricher | None = None,
    documents=iter_documents,
    audit=audit_secrets,
    concurrency: int | None = None,
) -> dict[str, int]:
    """Enrich stored documents; audit only those with secret candidates. Resumable:
    already-enriched docs are skipped unless ``force``. ``limit`` of 0 does all.
    ``store`` is an open EnrichStore whose lifecycle the caller owns.

    Enrichment/audit LLM calls run ``concurrency`` at a time (the local server
    batches them); the store writes stay single-threaded on the caller's one
    connection. A per-record ``EnrichError`` (a bad message) is skipped so it can't
    abort a long backfill; an ``EnrichUnavailableError`` still propagates."""
    concurrency = concurrency or settings.enrich_concurrency
    own = enricher is None
    enricher = enricher or Enricher()
    counts = {"scanned": 0, "enriched": 0, "audited": 0, "skipped": 0}

    def selected() -> Iterator[tuple]:
        seen = set() if force else store.enriched_ids()
        for doc_id, content, meta in documents(source=source, account=account):
            if limit and counts["scanned"] >= limit:
                return
            counts["scanned"] += 1
            if doc_id not in seen:
                yield doc_id, content, meta

    def work(item: tuple) -> tuple:
        doc_id, content, meta = item
        text = _model_text(meta, content)
        try:
            enrichment = enricher.enrich(text)
        except EnrichError as exc:
            log.warning("skipping %s: %s", doc_id, exc)
            return doc_id, None, None, None
        candidates = scan.audit_candidates(content)
        result = audit(text, candidates, model=enricher.model) if candidates else None
        return doc_id, enrichment, candidates, result

    def persist(res: tuple) -> None:
        doc_id, enrichment, candidates, result = res
        if enrichment is None:  # a per-record EnrichError was skipped
            counts["skipped"] += 1
            return
        store.save_enrichment(
            doc_id, msgspec.to_builtins(enrichment), enricher.model, SCHEMA_VERSION
        )
        counts["enriched"] += 1
        if candidates:
            store.save_audit(
                doc_id, candidates, msgspec.to_builtins(result), enricher.model, scan.SCAN_VERSION
            )
            counts["audited"] += 1

    items = selected()
    try:
        # Keep ``concurrency`` LLM calls in flight at all times: refill a slot the
        # moment one finishes and persist its result on this thread (the store stays
        # single-connection). Continuous streaming keeps the GPU saturated, unlike a
        # per-chunk barrier that stalls on the slowest record and the serial writes
        # between chunks.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            inflight = {pool.submit(work, item) for item in islice(items, concurrency)}
            while inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    persist(fut.result())
                inflight.update(pool.submit(work, item) for item in islice(items, len(done)))
    finally:
        if own:
            enricher.close()
    log.info(
        "enriched %d, audited %d, skipped %d of %d scanned",
        counts["enriched"], counts["audited"], counts["skipped"], counts["scanned"],
    )
    return counts


def run_audit(
    store,
    *,
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    documents=iter_documents,
    audit=audit_secrets,
    model: str | None = None,
) -> dict[str, int]:
    """Re-run only the LLM secret confirmation over documents with candidates. Does
    not enrich; upserts the audit idempotently."""
    model = model or settings.enrich_model
    if not model:
        raise ValueError("no model configured (set CORPUS_ENRICH_MODEL)")
    scanned = audited = 0
    for doc_id, content, meta in documents(source=source, account=account):
        if limit and scanned >= limit:
            break
        scanned += 1
        candidates = scan.audit_candidates(content)
        if not candidates:
            continue
        result = audit(_model_text(meta, content), candidates, model=model)
        store.save_audit(doc_id, candidates, msgspec.to_builtins(result), model, scan.SCAN_VERSION)
        audited += 1
    log.info("audited %d of %d scanned", audited, scanned)
    return {"scanned": scanned, "audited": audited}
