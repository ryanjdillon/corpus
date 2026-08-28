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

import msgspec

from . import scan
from .config import settings
from .enricher import Enricher
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
) -> dict[str, int]:
    """Enrich stored documents; audit only those with secret candidates. Resumable:
    already-enriched docs are skipped unless ``force``. ``limit`` of 0 does all.
    ``store`` is an open EnrichStore whose lifecycle the caller owns."""
    own = enricher is None
    enricher = enricher or Enricher()
    scanned = enriched = audited = 0
    try:
        seen = set() if force else store.enriched_ids()
        for doc_id, content, meta in documents(source=source, account=account):
            if limit and scanned >= limit:
                break
            scanned += 1
            if doc_id in seen:
                continue
            text = _model_text(meta, content)
            enrichment = enricher.enrich(text)
            store.save_enrichment(
                doc_id, msgspec.to_builtins(enrichment), enricher.model, SCHEMA_VERSION
            )
            enriched += 1
            candidates = scan.audit_candidates(content)
            if candidates:
                result = audit(text, candidates, model=enricher.model)
                store.save_audit(
                    doc_id, candidates, msgspec.to_builtins(result), enricher.model, scan.SCAN_VERSION
                )
                audited += 1
    finally:
        if own:
            enricher.close()
    log.info("enriched %d, audited %d of %d scanned", enriched, audited, scanned)
    return {"scanned": scanned, "enriched": enriched, "audited": audited}


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
